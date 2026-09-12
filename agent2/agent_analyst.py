"""
agent_analyst.py — Analyst Agent (Agent 2)

Polls agent_events for unprocessed NEW_POSITION events (efficiency=PROCEED).
Runs multi-source research in parallel (ESPN, Serper, NewsAPI, Tavily).
Uses Gemini Flash as primary LLM; falls back to Groq.
Writes ANALYSIS_COMPLETE events.

Usage:
    python agent_analyst.py --test
"""

import json
import logging
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

ANALYST_POLL_SECONDS = getattr(config, "ANALYST_POLL_SECONDS", 60)

ESPN_SPORT_MAP = {
    "nba": "basketball/nba",
    "basketball": "basketball/nba",
    "nfl": "americanfootball/nfl",
    "nhl": "icehockey/nhl",
    "mlb": "baseball/mlb",
    "epl": "soccer/eng.1",
    "premier league": "soccer/eng.1",
    "mls": "soccer/usa.1",
    "soccer": "soccer/eng.1",
    "ncaa": "basketball/mens-college-basketball",
}


# =============================================================================
# Research helpers
# =============================================================================

def _detect_espn_sport(question: str) -> Optional[str]:
    q = question.lower()
    for kw, path in ESPN_SPORT_MAP.items():
        if kw in q:
            return path
    return None


def _espn_fetch(question: str) -> str:
    path = _detect_espn_sport(question)
    if not path:
        return ""
    try:
        resp = requests.get(
            f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard",
            timeout=8,
        )
        if resp.status_code != 200:
            return ""
        events = resp.json().get("events", [])[:3]
        lines = []
        for ev in events:
            name = ev.get("name", "")
            status = ev.get("status", {}).get("type", {}).get("description", "")
            comps = ev.get("competitions", [{}])[0]
            scores = []
            for c in comps.get("competitors", []):
                team = c.get("team", {}).get("displayName", "")
                score = c.get("score", "")
                scores.append(f"{team} {score}")
            lines.append(f"{name} ({status}): {' vs '.join(scores)}")
        return " | ".join(lines)[:400]
    except Exception as e:
        logger.debug(f"Analyst/ESPN: {e}")
        return ""


def _newsapi_fetch(question: str) -> str:
    key = getattr(config, "NEWSAPI_KEY", "") or ""
    if not key:
        return ""
    try:
        words = [w for w in question.split() if len(w) > 3][:5]
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={"q": " ".join(words), "pageSize": 3, "apiKey": key, "sortBy": "publishedAt"},
            timeout=8,
        )
        if resp.status_code != 200:
            return ""
        articles = resp.json().get("articles", [])[:3]
        lines = [f"{a.get('title', '')}: {(a.get('description') or '')[:100]}" for a in articles]
        return " | ".join(lines)[:500]
    except Exception as e:
        logger.debug(f"Analyst/NewsAPI: {e}")
        return ""


# =============================================================================
# KEYFREE RESEARCH SOURCES — DuckDuckGo, GDELT, Reddit
# Replace Serper/Tavily. No API keys, no quota limits beyond mild rate limits.
# =============================================================================

_UA_HEADER = {"User-Agent": "polymarket-research-agent/1.0 (+https://github.com)"}


def _duckduckgo_fetch(question: str) -> str:
    """DuckDuckGo HTML endpoint scrape. No key, no quota. Returns top snippets."""
    import re
    import html as _html
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": question[:200]},
            headers={**_UA_HEADER, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=8,
        )
        if resp.status_code != 200:
            return ""
        snippets = re.findall(
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            resp.text,
            flags=re.DOTALL,
        )
        cleaned = []
        for s in snippets[:5]:
            text = re.sub(r"<[^>]+>", "", s)
            text = _html.unescape(re.sub(r"\s+", " ", text)).strip()
            if text:
                cleaned.append(text)
        return " | ".join(cleaned)[:500]
    except Exception as e:
        logger.debug(f"Analyst/DuckDuckGo: {e}")
        return ""


def _gdelt_fetch(question: str) -> str:
    """GDELT global news index. Free, no key, last-24h coverage worldwide."""
    try:
        words = [w for w in question.split() if len(w) > 3][:6]
        if not words:
            return ""
        query = " ".join(words)
        resp = requests.get(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={
                "query": query,
                "mode": "artlist",
                "format": "json",
                "maxrecords": 5,
                "sort": "datedesc",
                "timespan": "3d",
            },
            headers=_UA_HEADER,
            timeout=8,
        )
        if resp.status_code != 200:
            return ""
        try:
            articles = resp.json().get("articles", [])[:5]
        except ValueError:
            return ""
        lines = []
        for a in articles:
            title = (a.get("title") or "").strip()
            source = (a.get("domain") or "").strip()
            if title:
                lines.append(f"{title} ({source})" if source else title)
        return " | ".join(lines)[:500]
    except Exception as e:
        logger.debug(f"Analyst/GDELT: {e}")
        return ""


def _reddit_fetch(question: str) -> str:
    """Reddit public JSON search. Captures community sentiment / discussion."""
    try:
        resp = requests.get(
            "https://www.reddit.com/search.json",
            params={"q": question[:200], "limit": 5, "sort": "new", "t": "week"},
            headers=_UA_HEADER,
            timeout=8,
        )
        if resp.status_code != 200:
            return ""
        children = resp.json().get("data", {}).get("children", [])[:5]
        lines = []
        for c in children:
            d = c.get("data", {})
            title = (d.get("title") or "").strip()
            sub = (d.get("subreddit") or "").strip()
            score = d.get("score", 0)
            if title:
                lines.append(f"[r/{sub} +{score}] {title}")
        return " | ".join(lines)[:500]
    except Exception as e:
        logger.debug(f"Analyst/Reddit: {e}")
        return ""


# =============================================================================
# LLM decision
# =============================================================================

_DECISION_PROMPT = """Smart-money signal. Wallet {wallet} (${profit:,.0f} profit) opened {direction} on:
"{question}"
Entry {entry:.0%} | Now {now:.0%} | Drift {drift:+.0%} | {hours:.0f}h ago
Research: {research}

JSON only: {{"contradicting_info":bool,"has_value":bool,"confidence":"low|medium|high","action":"BET_YES|BET_NO|SKIP","reasoning":"<50w","key_risk":"<15w"}}
Rules: research contradicts → SKIP+contradicting_info=true; drift >15% → SKIP; else follow wallet."""


def _build_prompt(payload: dict, research: str) -> str:
    entry = payload.get("entry_price", 0.5)
    current = payload.get("current_price", 0.5)
    drift = current - entry
    return _DECISION_PROMPT.format(
        direction=payload.get("direction", "YES"),
        question=(payload.get("market_question", "") or "")[:140],
        wallet=payload.get("wallet_name", "?"),
        profit=payload.get("wallet_profit", 0),
        entry=entry,
        now=current,
        drift=drift,
        hours=payload.get("hours_ago", 0),
        research=(research or "none")[:300],
    )


def _parse_llm_json(text: str) -> dict:
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        text = parts[1].strip()
        if text.startswith("json"):
            text = text[4:].strip()
    result = json.loads(text)
    result.setdefault("contradicting_info", False)
    result.setdefault("has_value", True)
    result.setdefault("confidence", "medium")
    result.setdefault("action", "SKIP")
    result.setdefault("reasoning", "")
    result.setdefault("key_risk", "")
    return result


_router_singleton = None


def _get_router():
    global _router_singleton
    if _router_singleton is None:
        from llm_router import LLMRouter
        _router_singleton = LLMRouter(db_path=config.DB_PATH, analyst_mode=True)
    return _router_singleton


def _decide(payload: dict, research: str) -> tuple:
    """
    === A2 COPY-ARM OVERRIDE (deterministic, NO LLM) ===
    A/B-Test naiver Copy-Arm: KEIN LLM-Call. Kopiert deterministisch die
    Richtung der getrackten Wallet. Jede qualifizierende Fill wird zum BET.
    - action     = BET_<wallet-side>   (kopiert Wallet-Richtung 1:1)
    - confidence = "medium" (FIX)  -> Kelly-Sizing-Input konstant (0.61)
                   Formel in agent_risk._bet_size bleibt byte-identisch zu A1;
                   nur der Confidence-Input ist konstant statt LLM-variabel.
    Downstream (Devil, Risk, PositionMonitor) unveraendert von A1 geerbt.
    Router/Research werden bewusst NICHT aufgerufen -> 0 Quota-Last, kein 400-Bug.
    """
    direction = str(payload.get("direction", "YES")).upper()
    action = "BET_YES" if "YES" in direction else "BET_NO"
    return {
        "contradicting_info": False,
        "has_value": True,
        "confidence": "medium",
        "action": action,
        "reasoning": "deterministic copy of tracked wallet (no LLM)",
        "key_risk": "",
    }, "copy-deterministic"


# =============================================================================
# Agent class
# =============================================================================

_THROTTLE_MAX_PER_MIN = 12
_DEDUP_WINDOW_HOURS = 6


class AnalystAgent:

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._recent_calls: list = []  # timestamps for throttle
        self._recent_markets: dict = {}  # market_id -> last_analyzed_ts

    def _get_pending(self) -> list:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("""
                SELECT id, market_id, market_question, payload
                FROM agent_events
                WHERE event_type = 'NEW_POSITION' AND processed = 0
            """).fetchall()
            results = []
            for row in rows:
                try:
                    payload = json.loads(row[3])
                    if payload.get("efficiency") == "SKIP_EFFICIENT":
                        conn.execute("UPDATE agent_events SET processed=1 WHERE id=?", (row[0],))
                    else:
                        results.append({
                            "event_id": row[0],
                            "market_id": row[1],
                            "market_question": row[2],
                            "payload": payload,
                        })
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

    def _write_result(self, original_id: int, market_id: str, market_question: str,
                      analysis: dict, payload: dict, sources: list, model: str):
        now = datetime.now(timezone.utc).isoformat()
        event_payload = {
            "action": analysis["action"],
            "confidence": analysis["confidence"],
            "contradicting_info": analysis["contradicting_info"],
            "has_value": analysis["has_value"],
            "reasoning": analysis["reasoning"],
            "key_risk": analysis["key_risk"],
            "research_sources": sources,
            "model_used": model,
            "original_event_id": original_id,
            # pass-through for downstream agents
            "market_question": market_question,
            "market_id": market_id,
            "direction": payload.get("direction", "YES"),
            "entry_price": payload.get("entry_price", 0.5),
            "current_price": payload.get("current_price", 0.5),
            "size_usd": payload.get("size_usd", 0),
            "wallet_address": payload.get("wallet_address", ""),
            "wallet_name": payload.get("wallet_name", ""),
            "wallet_rank": payload.get("wallet_rank", 0),
            "wallet_profit": payload.get("wallet_profit", 0),
            "hours_ago": payload.get("hours_ago", 0),
            "efficiency_details": payload.get("efficiency_details", {}),
            "market_volume": payload.get("market_volume", 0),
        }
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                INSERT INTO agent_events
                    (timestamp, agent_source, event_type, market_id, market_question, payload, processed)
                VALUES (?, 'analyst', 'ANALYSIS_COMPLETE', ?, ?, ?, 0)
            """, (now, market_id, market_question, json.dumps(event_payload)))
            conn.commit()
        finally:
            conn.close()

    def _throttle(self):
        """Block until <12 calls in last 60s."""
        now = time.time()
        self._recent_calls = [t for t in self._recent_calls if now - t < 60]
        while len(self._recent_calls) >= _THROTTLE_MAX_PER_MIN:
            time.sleep(2)
            now = time.time()
            self._recent_calls = [t for t in self._recent_calls if now - t < 60]
        self._recent_calls.append(now)

    def _is_dup_recent(self, market_id: str) -> bool:
        now = time.time()
        last = self._recent_markets.get(market_id, 0)
        if now - last < _DEDUP_WINDOW_HOURS * 3600:
            return True
        self._recent_markets[market_id] = now
        # GC old entries
        cutoff = now - _DEDUP_WINDOW_HOURS * 3600
        self._recent_markets = {k: v for k, v in self._recent_markets.items() if v > cutoff}
        return False

    def analyze_one(self, event: dict):
        event_id = event["event_id"]
        market_id = event["market_id"]
        market_question = event["market_question"]
        payload = event["payload"]
        payload["market_question"] = market_question
        payload["market_id"] = market_id

        if self._is_dup_recent(market_id):
            logger.info(f"Analyst: SKIP duplicate market within {_DEDUP_WINDOW_HOURS}h: {market_question[:50]}")
            self._mark_processed(event_id)
            return

        self._throttle()
        logger.info(f"Analyst: analyzing '{market_question[:60]}'...")

        # === A2 COPY-ARM: research skipped (only fed the removed LLM) ===
        sources_used = []
        research_summary = ""
        analysis, model_used = _decide(payload, research_summary)

        logger.info(
            f"  → {analysis['action']} | conf={analysis['confidence']} "
            f"| model={model_used} | sources={sources_used}"
        )

        self._mark_processed(event_id)
        self._write_result(event_id, market_id, market_question, analysis, payload, sources_used, model_used)

    def poll_once(self):
        events = self._get_pending()
        if not events:
            return
        logger.info(f"Analyst: {len(events)} events to analyze")
        for ev in events:
            try:
                self.analyze_one(ev)
            except Exception as e:
                logger.error(f"Analyst: failed for event {ev['event_id']}: {e}", exc_info=True)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="AnalystAgent", daemon=True)
        self._thread.start()
        logger.info("Analyst: background thread started")

    def stop(self):
        self._running = False

    def _run_loop(self):
        while self._running:
            try:
                self.poll_once()
            except Exception as e:
                logger.error(f"Analyst: poll error: {e}", exc_info=True)
            time.sleep(ANALYST_POLL_SECONDS)


# =============================================================================
# Standalone test: python agent_analyst.py --test
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if "--test" not in sys.argv:
        print("Usage: python agent_analyst.py --test")
        sys.exit(0)

    print("=== AnalystAgent standalone test ===\n")

    test_payload = {
        "market_question": "Will the Oklahoma City Thunder win the NBA Championship 2025?",
        "market_id": "test-cid-123",
        "direction": "YES",
        "entry_price": 0.38,
        "current_price": 0.41,
        "size_usd": 500,
        "hours_ago": 1.2,
        "wallet_name": "TopTrader",
        "wallet_profit": 250000,
        "wallet_rank": 2,
        "efficiency": "PROCEED",
        "efficiency_details": {"external_price": 0.35, "source": "pinnacle", "price_gap": 0.06},
    }

    print("--- Research phase ---")
    espn_r = _espn_fetch(test_payload["market_question"])
    ddg_r = _duckduckgo_fetch(test_payload["market_question"])
    gdelt_r = _gdelt_fetch(test_payload["market_question"])
    reddit_r = _reddit_fetch(test_payload["market_question"])
    newsapi_r = _newsapi_fetch(test_payload["market_question"])
    print(f"ESPN:       {espn_r[:80] or '(no data)'}")
    print(f"DuckDuckGo: {ddg_r[:80] or '(no data)'}")
    print(f"GDELT:      {gdelt_r[:80] or '(no data)'}")
    print(f"Reddit:     {reddit_r[:80] or '(no data)'}")
    print(f"NewsAPI:    {newsapi_r[:80] or '(no data)'}")

    research = "\n".join(filter(None, [espn_r, ddg_r, gdelt_r, reddit_r, newsapi_r]))

    print("\n--- LLM decision ---")
    try:
        decision, model = _decide(test_payload, research)
        print(f"Model:  {model}")
        print(f"Action: {decision['action']}")
        print(f"Conf:   {decision['confidence']}")
        print(f"Reason: {decision['reasoning']}")
        print(f"Risk:   {decision['key_risk']}")
        print("\nTest PASSED")
    except Exception as e:
        print(f"Test FAILED: {e}")
        sys.exit(1)
