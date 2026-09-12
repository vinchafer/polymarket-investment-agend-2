"""
agent_risk.py — Risk Officer Agent (Agent 4)

Portfolio-level risk management.
Reads CHALLENGE_COMPLETE events where final_verdict=PROCEED.
Runs portfolio checks, calculates bet size, places bet, records trade.
Writes RISK_APPROVED or RISK_REJECTED events.

Usage:
    python agent_risk.py --test
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
MAX_OPEN = getattr(config, "MAX_OPEN_POSITIONS", 15)
MAX_RISK_PCT = getattr(config, "MAX_CAPITAL_AT_RISK_PCT", 0.60)
MAX_BET_USDC = getattr(config, "MAX_BET_USDC", 3.0)
MAX_DAILY_LOSS = getattr(config, "MAX_DAILY_LOSS_USDC", 10.0)
PORTFOLIO_USDC = getattr(config, "PORTFOLIO_USDC", 50.0)

SECTOR_MAP = {
    "nba": "NBA", "thunder": "NBA", "lakers": "NBA", "celtics": "NBA",
    "basketball": "NBA",
    "nfl": "NFL", "chiefs": "NFL", "patriots": "NFL", "football": "NFL",
    "nhl": "NHL", "hockey": "NHL",
    "mlb": "MLB", "baseball": "MLB",
    "soccer": "SOCCER", "epl": "SOCCER", "premier league": "SOCCER",
    "mls": "SOCCER", "bundesliga": "SOCCER", "champions league": "SOCCER",
    "tennis": "TENNIS",
    "bitcoin": "CRYPTO", "crypto": "CRYPTO", "ethereum": "CRYPTO",
    "election": "POLITICS", "politics": "POLITICS", "president": "POLITICS",
}


def _detect_sector(question: str) -> str:
    q = question.lower()
    for kw, sector in SECTOR_MAP.items():
        if kw in q:
            return sector
    return "OTHER"


def _bet_size(payload: dict) -> float:
    """
    HALF KELLY KRITERIUM - Goldman Sachs Standard
    Formel: Kelly % = (p * b - (1-p)) / b
    p = Wahrscheinlichkeit des Gewinns
    b = Odds = (1/price) - 1
    
    Wir verwenden IMMER Half Kelly (0.5 * Kelly)
    """
    if not config.USE_KELLY_SIZING:
        # Fallback auf altes statisches System falls Kelly deaktiviert ist
        confidence = payload.get("confidence", "medium")
        wallet_rank = payload.get("wallet_rank", 10)
        multi_wallet = wallet_rank <= 2
        if multi_wallet:
            size = 3.0
        elif confidence == "high":
            size = 2.5
        else:
            size = 1.5
        return min(size, MAX_BET_USDC)

    # Extrahiere Werte aus Payload
    confidence = payload.get("confidence", "medium")
    conf_value = {"high": 0.72, "medium": 0.61, "low": 0.53}.get(confidence, 0.55)
    current_price = payload.get("current_price", 0.5)
    
    # Kelly Berechnung
    true_probability = conf_value
    odds = (1.0 / current_price) - 1.0
    
    kelly_pct = (true_probability * odds - (1 - true_probability)) / odds
    kelly_pct = max(0.0, kelly_pct)  # Kein negativer Einsatz
    
    # Half Kelly Anwendung
    kelly_pct = kelly_pct * config.KELLY_FRACTION
    
    # Berechne absoluten Einsatz
    size = config.PORTFOLIO_USDC * kelly_pct
    
    # Hard Limits
    size = max(size, config.MIN_BET_USDC)
    size = min(size, MAX_BET_USDC)
    size = min(size, config.PORTFOLIO_USDC * config.MAX_BET_PCT_PORTFOLIO)
    
    # Runde auf 2 Dezimalstellen (USDC hat 6, aber Polymarket akzeptiert 2)
    return round(size, 2)


class RiskAgent:

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # -------------------------------------------------------------------------
    # Portfolio state
    # -------------------------------------------------------------------------

    def _portfolio_state(self) -> dict:
        conn = sqlite3.connect(self.db_path)
        try:
            open_count = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE resolved=0"
            ).fetchone()[0]

            capital_at_risk = conn.execute(
                "SELECT COALESCE(SUM(bet_usdc),0) FROM trades WHERE resolved=0"
            ).fetchone()[0]

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            daily_pnl = conn.execute(
                "SELECT COALESCE(SUM(pnl_usdc),0) FROM trades WHERE date(timestamp)=? AND resolved=1",
                (today,),
            ).fetchone()[0]

            return {
                "open_positions": open_count,
                "capital_at_risk": float(capital_at_risk),
                "daily_pnl": float(daily_pnl),
            }
        finally:
            conn.close()

    def _check_duplicate(self, market_id: str, market_question: str) -> str:
        """Returns rejection reason string if duplicate open position exists, else ''."""
        conn = sqlite3.connect(self.db_path)
        try:
            exact = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE market_condition_id=? AND resolved=0",
                (market_id,),
            ).fetchone()[0]
            if exact > 0:
                return "duplicate position: open trade exists for this market_condition_id"

            prefix = market_question[:35].replace("%", "%%") + "%"
            fuzzy = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE market_question LIKE ? AND resolved=0",
                (prefix,),
            ).fetchone()[0]
            if fuzzy > 0:
                return "duplicate position: similar market already open (question prefix match)"

            return ""
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Risk checks
    # -------------------------------------------------------------------------

    def _check(self, payload: dict, portfolio: dict, sector: str, market_question: str) -> tuple:
        """Returns (approved: bool, reason: str)."""
        if portfolio["open_positions"] >= MAX_OPEN:
            return False, f"Max positions reached ({MAX_OPEN})"

        if portfolio["daily_pnl"] < -MAX_DAILY_LOSS:
            return False, f"Daily loss limit: {portfolio['daily_pnl']:.2f} USDC"

        max_capital = PORTFOLIO_USDC * MAX_RISK_PCT
        if portfolio["capital_at_risk"] >= max_capital:
            return False, (
                f"Capital at risk limit: {portfolio['capital_at_risk']:.2f}"
                f"/{max_capital:.2f} USDC"
            )
            
        # ✅ PHASE 1: SEKTOR KONZENTRATIONS LIMITS
        if config.ENABLE_SECTOR_LIMITS:
            conn = sqlite3.connect(self.db_path)
            try:
                sector_count = conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE market_category=? AND resolved=0",
                    (sector,)
                ).fetchone()[0]
                
                sector_capital = conn.execute(
                    "SELECT COALESCE(SUM(bet_usdc), 0) FROM trades WHERE market_category=? AND resolved=0",
                    (sector,)
                ).fetchone()[0]
                
                if sector_count >= config.MAX_SECTOR_POSITIONS:
                    return False, f"Sektor Limit erreicht: {sector_count}/{config.MAX_SECTOR_POSITIONS} Positionen in {sector}"
                
                max_sector_cap = PORTFOLIO_USDC * config.MAX_SECTOR_CAPITAL_PCT
                if sector_capital >= max_sector_cap:
                    return False, f"Sektor Kapital Limit erreicht: {sector_capital:.2f}/{max_sector_cap:.2f} USDC in {sector}"
                    
            finally:
                conn.close()

        # ✅ PHASE 2: KORRELATIONSERKENNUNG
        if config.ENABLE_CORRELATION_DETECTION:
            conn = sqlite3.connect(self.db_path)
            try:
                # Finde ähnliche Märkte mit gleichem Präfix (korrelierte Ereignisse)
                prefix = market_question[:30].replace("%", "%%") + "%"
                correlated = conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE market_question LIKE ? AND resolved=0",
                    (prefix,)
                ).fetchone()[0]
                
                if correlated >= config.MAX_CORRELATED_POSITIONS:
                    return False, f"Korrelationsschutz: Bereits {correlated} Position auf ähnliche Ereignisse offen"
                    
            finally:
                conn.close()

        # ✅ PHASE 2: LIQUIDITÄTSCHECK
        # Skip if market_volume unknown (== 0). Cap order at 1/3 of known liquidity.
        if config.ENABLE_LIQUIDITY_CHECK:
            market_volume = payload.get("market_volume", 0) or 0
            if market_volume > 0:
                if market_volume < config.MIN_MARKET_DAILY_VOLUME:
                    return False, f"Unzureichende Liquidität: Nur {market_volume:.0f} USDC Volumen/24h"

                proposed_size = _bet_size(payload)
                if proposed_size > market_volume * config.MAX_ORDER_VOLUME_RATIO:
                    max_allowed = market_volume * config.MAX_ORDER_VOLUME_RATIO
                    return False, f"Order zu gross: Max {max_allowed:.2f} USDC erlaubt bei diesem Volumen"

        # ✅ PHASE 2: TIME DECAY
        if config.ENABLE_TIME_DECAY:
            days_to_resolution = payload.get("days_to_resolution", 999)
            
            if days_to_resolution <= 0:
                return False, "Markt wird heute aufgelöst - kein Trade"
                
            if days_to_resolution <= config.TIME_DECAY_START_DAYS:
                progress = 1.0 - (days_to_resolution / config.TIME_DECAY_START_DAYS)
                decay_multiplier = 1.0 - (progress * (1.0 - config.TIME_DECAY_FINAL_MULTIPLIER))
                
                # Reduziere Konfidenz dynamisch
                if decay_multiplier < 0.7:
                    return False, f"Time Decay: Markt zu nah an Auflösung ({days_to_resolution} Tage)"

        return True, "Approved"

    # -------------------------------------------------------------------------
    # Bet placement
    # -------------------------------------------------------------------------

    def _place_bet(self, market_id: str, payload: dict, size: float) -> dict:
        if config.DRY_RUN:
            logger.info(f"  [DRY RUN] Would bet {size} USDC on {market_id[:20]}")
            return {"dry_run": True, "bet_usdc": size, "status": "DRY_RUN", "order_id": "dry"}

        try:
            from polymarket import PolymarketClient
            client = PolymarketClient()
            direction = payload.get("action", "BET_YES")
            outcome = "YES" if "YES" in direction else "NO"
            result = client.place_market_order(
                condition_id=market_id,
                outcome=outcome,
                amount_usdc=size,
            )
            return result or {"status": "placed", "bet_usdc": size}
        except Exception as e:
            logger.error(f"RiskAgent: bet placement failed: {e}")
            return {"error": str(e), "bet_usdc": size, "status": "FAILED"}

    def _record_trade(self, market_id: str, market_question: str, payload: dict,
                      size: float, order: dict, sector: str):
        now = datetime.now(timezone.utc).isoformat()
        action = payload.get("action", "BET_YES")
        entry_price = payload.get("current_price", 0.5)
        conf_val = {"high": 0.8, "medium": 0.5, "low": 0.3}.get(
            payload.get("confidence", "medium"), 0.5
        )
        dry = 1 if config.DRY_RUN else 0
        # Persist the exact held-token identity so resolution scores against the
        # wallet's real side. In DRY_RUN the order carries no token_id → fall back
        # to the token_id Scout captured. The analyst/devil rebuild the payload and
        # drop token_id, so recover it from the originating NEW_POSITION event.
        token_id = str(order.get("token_id") or payload.get("token_id", "") or "")
        if not token_id:
            try:
                c0 = sqlite3.connect(self.db_path)
                row = c0.execute(
                    "SELECT payload FROM agent_events WHERE market_id=? "
                    "AND event_type='NEW_POSITION' ORDER BY id DESC LIMIT 1",
                    (market_id,),
                ).fetchone()
                c0.close()
                if row:
                    token_id = str(json.loads(row[0]).get("token_id", "") or "")
            except Exception as e:
                logger.debug(f"RiskAgent: token_id recovery failed: {e}")

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                INSERT INTO trades
                    (timestamp, market_condition_id, market_question, action,
                     token_id, bet_usdc, entry_price, order_id,
                     resolved, dry_run, market_category, confidence_at_bet)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """, (
                now, market_id, market_question, action,
                token_id,
                size, entry_price,
                str(order.get("order_id", order.get("status", ""))),
                dry, sector, conf_val,
            ))
            conn.commit()
        finally:
            conn.close()

    def _notify_bet(self, market_question: str, payload: dict, size: float):
        try:
            from notifier import TelegramNotifier
            notifier = TelegramNotifier()
            if not notifier.enabled:
                return
            action = payload.get("action", "BET_YES")
            conf = payload.get("confidence", "medium")
            reasoning = payload.get("reasoning", "")[:100]
            wallet = payload.get("wallet_name", "unknown")
            entry = payload.get("entry_price", 0.5)
            current = payload.get("current_price", 0.5)
            dry = "[DRY RUN] " if config.DRY_RUN else ""
            emoji = "🟢" if "YES" in action else "🔴"
            msg = (
                f"{dry}{emoji} NEW BET PLACED\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📋 {market_question[:80]}\n"
                f"Direction: {action} @ {current:.0%}\n"
                f"Size: {size} USDC | Confidence: {conf.upper()}\n"
                f"Following: {wallet} (entry @ {entry:.0%})\n"
                f"Reason: {reasoning}"
            )
            notifier.send_message(msg)
        except Exception as e:
            logger.debug(f"RiskAgent: notify failed: {e}")

    # -------------------------------------------------------------------------
    # Event I/O
    # -------------------------------------------------------------------------

    def _get_pending(self) -> list:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("""
                SELECT id, market_id, market_question, payload
                FROM agent_events
                WHERE event_type = 'CHALLENGE_COMPLETE' AND processed = 0
            """).fetchall()

            results = []
            for row in rows:
                try:
                    payload = json.loads(row[3])
                    if payload.get("final_verdict") == "PROCEED":
                        results.append({
                            "event_id": row[0],
                            "market_id": row[1],
                            "market_question": row[2],
                            "payload": payload,
                        })
                    else:
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

    def _write_result(self, market_id: str, market_question: str, payload: dict,
                      approved: bool, reason: str, size: float, order: Optional[dict]):
        now = datetime.now(timezone.utc).isoformat()
        event_type = "RISK_APPROVED" if approved else "RISK_REJECTED"
        event_payload = {
            **payload,
            "approved": approved,
            "rejection_reason": "" if approved else reason,
            "bet_size_usdc": size,
            "order_result": order or {},
        }
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                INSERT INTO agent_events
                    (timestamp, agent_source, event_type, market_id, market_question, payload, processed)
                VALUES (?, 'risk', ?, ?, ?, ?, 1)
            """, (now, event_type, market_id, market_question, json.dumps(event_payload)))
            conn.commit()
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Core processing
    # -------------------------------------------------------------------------

    def process_one(self, event: dict):
        payload = event["payload"]
        market_id = event["market_id"]
        market_question = event["market_question"]
        sector = _detect_sector(market_question)

        logger.info(f"Risk: evaluating '{market_question[:55]}'... [sector={sector}]")

        # Duplicate guard — must run before portfolio state to prevent YES+NO on same market
        dup_reason = self._check_duplicate(market_id, market_question)
        if dup_reason:
            logger.info(f"  → REJECTED: {dup_reason}")
            self._mark_processed(event["event_id"])
            self._write_result(market_id, market_question, payload, False, dup_reason, 0.0, None)
            return

        portfolio = self._portfolio_state()
        approved, reason = self._check(payload, portfolio, sector, market_question)

        size = 0.0
        order = None

        if approved:
            size = _bet_size(payload)
            logger.info(
                f"  → APPROVED {size} USDC | open={portfolio['open_positions']} "
                f"| at_risk={portfolio['capital_at_risk']:.2f} USDC"
            )
            order = self._place_bet(market_id, payload, size)
            self._record_trade(market_id, market_question, payload, size, order, sector)
            self._notify_bet(market_question, payload, size)
        else:
            logger.info(f"  → REJECTED: {reason}")

        self._mark_processed(event["event_id"])
        self._write_result(market_id, market_question, payload, approved, reason, size, order)

    def poll_once(self):
        events = self._get_pending()
        if not events:
            return
        logger.info(f"Risk: {len(events)} decisions to process")
        for ev in events:
            try:
                self.process_one(ev)
            except Exception as e:
                logger.error(f"Risk: failed for event {ev['event_id']}: {e}", exc_info=True)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="RiskAgent", daemon=True)
        self._thread.start()
        logger.info("Risk: background thread started")

    def stop(self):
        self._running = False

    def _run_loop(self):
        while self._running:
            try:
                self.poll_once()
            except Exception as e:
                logger.error(f"Risk: poll error: {e}", exc_info=True)
            time.sleep(POLL_SECONDS)


# =============================================================================
# Standalone test: python agent_risk.py --test
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if "--test" not in sys.argv:
        print("Usage: python agent_risk.py --test")
        sys.exit(0)

    import tempfile, os
    tmp_db = tempfile.mktemp(suffix=".db")

    # Create minimal trades table
    conn = sqlite3.connect(tmp_db)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, market_condition_id TEXT, market_question TEXT,
            action TEXT, token_id TEXT, bet_usdc REAL, entry_price REAL,
            order_id TEXT, resolved INTEGER DEFAULT 0, dry_run INTEGER DEFAULT 1,
            market_category TEXT, confidence_at_bet REAL, pnl_usdc REAL, won INTEGER,
            resolution_timestamp TEXT, resolution_outcome TEXT
        )
    """)
    conn.commit()
    conn.close()

    print("=== RiskAgent standalone test ===\n")
    agent = RiskAgent(tmp_db)
    portfolio = agent._portfolio_state()
    print(f"Portfolio state: {portfolio}")

    test_payload = {
        "action": "BET_YES",
        "confidence": "high",
        "market_question": "Will the Oklahoma City Thunder win the NBA Championship?",
        "direction": "YES",
        "current_price": 0.41,
        "entry_price": 0.38,
        "wallet_rank": 1,
        "wallet_name": "TopTrader",
        "final_verdict": "PROCEED",
    }

    approved, reason = agent._check(test_payload, portfolio, _detect_sector(test_payload['market_question']), test_payload['market_question'])
    size = _bet_size(test_payload)
    print(f"Approved: {approved} | Reason: {reason}")
    print(f"Bet size: {size} USDC")
    print(f"Sector: {_detect_sector(test_payload['market_question'])}")

    try:
        os.unlink(tmp_db)
    except Exception:
        pass

    print("\nTest PASSED")
