"""
agent_devil.py — Devil's Advocate Agent (Agent 3)

Challenges every BET decision from Analyst.
Uses Groq (different model = different perspective).
Actively tries to find reasons NOT to bet.
Writes CHALLENGE_COMPLETE events.

Usage:
    python agent_devil.py --test
"""

import json
import logging
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import config

logger = logging.getLogger(__name__)

POLL_SECONDS = getattr(config, "ANALYST_POLL_SECONDS", 60)

_CHALLENGE_PROMPT = """
An analyst recommends {action} on: {question}

Analyst reasoning: {reasoning}
Analyst confidence: {confidence}
Key risk: {key_risk}

Position details:
- Opened {hours_ago:.1f} hours ago
- Wallet entry price: {entry_price:.0%} | Current price: {current_price:.0%}
- Price drift since entry: {price_drift:.0%}

Your job: Find reasons this bet could FAIL. Be skeptical. Be contrarian. Challenge everything.

Respond JSON only:
{{
  "fatal_flaw_found": true/false,
  "flaws": ["flaw1", "flaw2"],
  "override_to_skip": true/false,
  "confidence_adjustment": <float -0.2 to 0.1>,
  "final_verdict": "PROCEED/SKIP",
  "override_reason": "<reason if skipping, else empty>"
}}

MUST override to SKIP if any of these:
- hours_ago > 1.8 (position not fresh enough, missed entry window)
- price drift > 15% since wallet entry (missed optimal entry)
- analyst confidence was "low"
- contradicting_info was true in the analysis
"""


_devil_router = None


def _get_devil_router():
    global _devil_router
    if _devil_router is None:
        from llm_router import LLMRouter
        _devil_router = LLMRouter(db_path=config.DB_PATH, analyst_mode=False)
    return _devil_router


def _groq_challenge(payload: dict) -> dict:
    """
    === A2 COPY-ARM OVERRIDE (deterministic, NO LLM) ===
    Naiver Copy-Arm: kein Devil's-Advocate-LLM. Jede vom Analyst kopierte
    Wallet-Fill wird durchgewunken (final_verdict=PROCEED, kein Override).
    Alle Guardrails bleiben erhalten — sie sitzen downstream in agent_risk
    (Caps, Dedup, Liquiditaet, Time-Decay), byte-identisch von A1 geerbt.
    Router wird NICHT aufgerufen -> 0 Quota-Last.
    """
    return {
        "fatal_flaw_found": False,
        "flaws": [],
        "override_to_skip": False,
        "confidence_adjustment": 0.0,
        "final_verdict": "PROCEED",
        "override_reason": "",
    }


class DevilAgent:

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def _get_pending(self) -> list:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("""
                SELECT id, market_id, market_question, payload
                FROM agent_events
                WHERE event_type = 'ANALYSIS_COMPLETE' AND processed = 0
            """).fetchall()

            results = []
            for row in rows:
                try:
                    payload = json.loads(row[3])
                    action = payload.get("action", "SKIP")
                    if action in ("BET_YES", "BET_NO"):
                        results.append({
                            "event_id": row[0],
                            "market_id": row[1],
                            "market_question": row[2],
                            "payload": payload,
                        })
                    else:
                        # SKIP decisions pass straight through
                        conn.execute("UPDATE agent_events SET processed=1 WHERE id=?", (row[0],))
                except Exception:
                    pass
            conn.commit()
            return results
        finally:
            conn.close()

    def _mark_processed(self, event_id: int):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE agent_events SET processed=1 WHERE id=?", (event_id,))
            conn.commit()
        finally:
            conn.close()

    def _write_result(self, event: dict, challenge: dict):
        now = datetime.now(timezone.utc).isoformat()
        market_id = event["market_id"]
        market_question = event["market_question"]
        event_payload = {
            **event["payload"],
            "devil_flaws": challenge.get("flaws", []),
            "devil_fatal_flaw": challenge.get("fatal_flaw_found", False),
            "devil_override": challenge.get("override_to_skip", False),
            "devil_confidence_adj": challenge.get("confidence_adjustment", 0.0),
            "final_verdict": challenge.get("final_verdict", "PROCEED"),
            "override_reason": challenge.get("override_reason", ""),
        }
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                INSERT INTO agent_events
                    (timestamp, agent_source, event_type, market_id, market_question, payload, processed)
                VALUES (?, 'devil', 'CHALLENGE_COMPLETE', ?, ?, ?, 0)
            """, (now, market_id, market_question, json.dumps(event_payload)))
            conn.commit()
        finally:
            conn.close()

    def challenge_one(self, event: dict):
        payload = event["payload"]
        question = event["market_question"]
        action = payload.get("action", "BET_YES")

        logger.info(f"Devil: challenging {action} on '{question[:55]}'...")
        challenge = _groq_challenge(payload)
        verdict = challenge.get("final_verdict", "PROCEED")
        flaws = challenge.get("flaws", [])

        logger.info(
            f"  → verdict={verdict} | fatal={challenge.get('fatal_flaw_found')} "
            f"| adj={challenge.get('confidence_adjustment', 0):+.2f}"
        )
        if flaws:
            for f in flaws[:2]:
                logger.info(f"    flaw: {f}")

        self._mark_processed(event["event_id"])
        self._write_result(event, challenge)

    def poll_once(self):
        events = self._get_pending()
        if not events:
            return
        logger.info(f"Devil: {len(events)} bets to challenge")
        for ev in events:
            try:
                self.challenge_one(ev)
            except Exception as e:
                logger.error(f"Devil: failed for event {ev['event_id']}: {e}", exc_info=True)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="DevilAgent", daemon=True)
        self._thread.start()
        logger.info("Devil: background thread started")

    def stop(self):
        self._running = False

    def _run_loop(self):
        while self._running:
            try:
                self.poll_once()
            except Exception as e:
                logger.error(f"Devil: poll error: {e}", exc_info=True)
            time.sleep(POLL_SECONDS)


# =============================================================================
# Standalone test: python agent_devil.py --test
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if "--test" not in sys.argv:
        print("Usage: python agent_devil.py --test")
        sys.exit(0)

    print("=== DevilAgent standalone test ===\n")

    test_payload = {
        "action": "BET_YES",
        "confidence": "medium",
        "contradicting_info": False,
        "reasoning": "OKC Thunder are the top seed, wallet conviction is high.",
        "key_risk": "Injury to Shai Gilgeous-Alexander",
        "market_question": "Will the Oklahoma City Thunder win the NBA Championship 2025?",
        "direction": "YES",
        "entry_price": 0.38,
        "current_price": 0.41,
        "hours_ago": 1.2,
        "wallet_name": "TopTrader",
    }

    print(f"Challenging: BET_YES on '{test_payload['market_question'][:55]}'")
    result = _groq_challenge(test_payload)
    print(f"\nVerdict:     {result['final_verdict']}")
    print(f"Fatal flaw:  {result['fatal_flaw_found']}")
    print(f"Conf adj:    {result['confidence_adjustment']:+.2f}")
    print(f"Flaws:       {result['flaws']}")
    if result.get("override_reason"):
        print(f"Override:    {result['override_reason']}")

    print("\nTest PASSED" if "final_verdict" in result else "Test FAILED")
