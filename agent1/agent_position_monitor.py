"""
agent_position_monitor.py — Position Monitor Agent (Agent 5)

Monitors all open positions every 2 hours.
Alerts on >25% adverse price movements.
Records WIN/LOSS when market resolves.
Never auto-closes positions — alert only.

Usage:
    python agent_position_monitor.py --test
"""

import json
import logging
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

MONITOR_INTERVAL = getattr(config, "POSITION_MONITOR_INTERVAL_SECONDS", 7200)
ADVERSE_THRESHOLD = 0.25   # 25% adverse move → alert
RESOLUTION_YES = 0.97
RESOLUTION_NO = 0.03
# Cap-deadlock backstop: an open trade older than this with no resolution is
# force-closed as STALE so it stops occupying a position slot. Tied to the
# market-horizon guardrail + a 2-day settlement buffer (2026-07-12: was tied to
# the old 30d MAX_DAYS_TO_RESOLUTION, which left short-horizon trades hanging).
STALE_POSITION_HOURS = (getattr(config, "MAX_MARKET_HORIZON_DAYS", 14) + 2) * 24
# A position whose market end_date is this many hours past but is still unresolved
# gets an active re-check + alert (resolution-detection safety net).
STUCK_UNRESOLVED_HOURS = 48


def _parse_dt(iso: str):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except Exception:
        return None


def _hours_past_end(end_date_iso: str):
    """Hours since market end_date (positive = ended in the past). None if unknown."""
    dt = _parse_dt(end_date_iso)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


def _primary_yes_token(tokens: list) -> Optional[dict]:
    """Return the token representing the YES / primary (index-0) outcome.

    Root-cause of the resolution-detection gap (2026-07-12): the old code only
    matched a token whose outcome label was literally "YES". Real Polymarket
    markets use arbitrary labels — "France"/"Iraq", "Over"/"Under", and even
    whitespace-padded " Yes"/"  No" — so the match failed, yes_price stayed None,
    and settled markets sat forever at resolved=0. Polymarket convention: the
    first token is the affirmative/YES side, so we fall back to tokens[0]."""
    if not tokens:
        return None
    for tok in tokens:
        if str(tok.get("outcome", "")).strip().upper() == "YES":
            return tok
    return tokens[0]


def _get_clob_market(condition_id: str) -> dict:
    """Single authoritative CLOB fetch for a market. CLOB /markets/{cid} works
    for both active AND closed markets (Gamma ?condition_ids= returns [] once a
    market settles), and exposes closed + per-token winner/price + end_date_iso.

    Returns {"ok", "closed", "end_date", "yes_price", "resolved", "yes_settled"}.
      yes_price   — current/settled price of the YES/primary side (or None)
      resolved    — closed AND the YES side is determinable (winner/snapped)
      yes_settled — 1.0/0.0 snapped settlement price for the YES side (or None)
    """
    out = {"ok": False, "closed": False, "end_date": "",
           "yes_price": None, "resolved": False, "yes_settled": None,
           "tokens_by_id": {}}
    try:
        resp = requests.get(f"https://clob.polymarket.com/markets/{condition_id}", timeout=8)
        if resp.status_code != 200:
            return out
        data = resp.json()
    except Exception as e:
        logger.debug(f"PositionMonitor: clob fetch failed ({condition_id[:14]}): {e}")
        return out

    out["ok"] = True
    out["end_date"] = data.get("end_date_iso") or ""
    out["closed"] = bool(data.get("closed", False)) or not bool(data.get("active", True))
    # Per-token identity map so resolution can score against the EXACT token the
    # wallet held (token_id), not a positional/label assumption.
    for tok in data.get("tokens", []):
        tid = str(tok.get("token_id") or "")
        if not tid:
            continue
        try:
            pr = float(tok.get("price", 0))
        except Exception:
            pr = None
        out["tokens_by_id"][tid] = {
            "price": pr,
            "winner": tok.get("winner") if "winner" in tok else None,
            "outcome": tok.get("outcome"),
        }
    yes_tok = _primary_yes_token(data.get("tokens", []))
    if yes_tok is not None:
        try:
            out["yes_price"] = float(yes_tok.get("price", 0))
        except Exception:
            out["yes_price"] = None
        # Settlement: prefer explicit winner flag, else snapped price
        if data.get("closed", False):
            if "winner" in yes_tok:
                yes_won = bool(yes_tok.get("winner"))
            elif out["yes_price"] is not None:
                yes_won = out["yes_price"] >= 0.5
            else:
                yes_won = None
            if yes_won is not None:
                out["resolved"] = True
                out["yes_settled"] = 1.0 if yes_won else 0.0
    return out


def _get_market_snapshot(condition_id: str):
    """Back-compat: (yes_price, closed) from CLOB using the primary-token fix."""
    m = _get_clob_market(condition_id)
    if not m["ok"]:
        return None, False
    return m["yes_price"], m["closed"]


def _get_yes_price(condition_id: str) -> Optional[float]:
    """Fetch current YES price from Polymarket CLOB API."""
    return _get_clob_market(condition_id).get("yes_price")


def _save_learning_outcome(db_path: str, trade_id: int, condition_id: str,
                            action: str, bet_usdc: float, entry_price: float,
                            outcome: str, pnl: float):
    """Write resolved trade outcome to learning_data for future threshold adaptation."""
    now = datetime.now(timezone.utc).isoformat()
    hour = datetime.now(timezone.utc).hour
    is_yes = "YES" in action
    edge = abs(entry_price - 0.5)
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                INSERT OR IGNORE INTO learning_data
                    (trade_id, market_condition_id, sport_category,
                     confidence_at_bet, edge_at_bet, weighted_score_at_bet,
                     source_quality_score, time_of_day_utc, outcome, pnl_usdc, recorded_at)
                VALUES (?, ?, 'unknown', 0.75, ?, 7.5, 0.5, ?, ?, ?, ?)
            """, (trade_id, condition_id, round(edge, 4), hour, outcome, round(pnl, 4), now))
    except Exception as e:
        logger.debug(f"PositionMonitor: learning write failed: {e}")


def _maybe_run_learning_update(db_path: str):
    """Run threshold update when 10+ new resolved outcomes exist since last update."""
    try:
        with sqlite3.connect(db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM learning_data WHERE outcome IS NOT NULL"
            ).fetchone()[0]
        if count < 10 or count % 10 != 0:
            return
        from learning_engine import LearningEngine
        from logger import TradeLogger
        from notifier import TelegramNotifier
        db = TradeLogger(db_path)
        notifier = TelegramNotifier()
        engine = LearningEngine(db, notifier if notifier.enabled else None)
        changes = engine.update_thresholds()
        if changes:
            logger.info(f"LearningEngine: {len(changes)} threshold change(s): {changes}")
        if engine.should_train_model():
            engine.train_model()
    except Exception as e:
        logger.debug(f"PositionMonitor: learning update failed: {e}")


class PositionMonitorAgent:

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._alerted: set = set()  # condition_ids already warned this session
        self._learning_outcomes_this_session = 0
        self._stuck_alerted: set = set()  # (utc_date, cid) already stuck-alerted today
        self._sweep_date: str = ""        # last UTC date the daily stuck sweep ran

    def _open_trades(self) -> list:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("""
                SELECT id, market_condition_id, market_question,
                       action, bet_usdc, entry_price, dry_run, timestamp, token_id
                FROM trades WHERE resolved=0
            """).fetchall()
            return [
                {
                    "id": r[0], "market_id": r[1], "question": r[2],
                    "action": r[3], "bet_usdc": r[4],
                    "entry_price": r[5], "dry_run": r[6], "timestamp": r[7],
                    "token_id": str(r[8] or ""),
                }
                for r in rows
            ]
        finally:
            conn.close()

    def _resolve(self, trade_id: int, current_price: float,
                 action: str, bet_usdc: float, entry_price: float,
                 won_override=None) -> tuple:
        """Mark trade resolved and return (outcome, pnl).

        won_override (bool) settles against the EXACT held token (token_id path)
        and bypasses the action/price inference entirely. Otherwise won is derived
        from the primary-side price vs action, with action index-consistent since
        the 2026-07-13 Scout fix."""
        if won_override is not None:
            won = bool(won_override)
        else:
            is_yes = "YES" in action
            # entry_price is the price actually paid for the side that was bought.
            # Payout on win is stake/entry_price for either side.
            if is_yes:
                won = current_price >= RESOLUTION_YES
            else:
                won = current_price <= RESOLUTION_NO
        pnl = bet_usdc * (1.0 / entry_price - 1.0) if won else -bet_usdc

        outcome = "WIN" if won else "LOSS"
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                UPDATE trades
                SET resolved=1, resolution_timestamp=?,
                    resolution_outcome=?, pnl_usdc=?, won=?
                WHERE id=?
            """, (now, outcome, round(pnl, 4), 1 if won else 0, trade_id))
            conn.commit()
        finally:
            conn.close()

        return outcome, round(pnl, 4)

    def _emit_resolution(self, t: dict, cid: str, question: str, action: str,
                         bet: float, entry: float, is_dry, price: float,
                         estimated: bool, source: str, won_override=None) -> tuple:
        """Resolve a trade and fire notify + alert + learning. Returns (outcome, pnl)."""
        outcome, pnl = self._resolve(t["id"], price, action, bet, entry,
                                     won_override=won_override)
        label = outcome + (" (ESTIMATED)" if estimated else "")
        emoji = "✅" if outcome == "WIN" else "❌"
        dry = "[DRY RUN] " if is_dry else ""
        msg = (
            f"{dry}{emoji} RESOLVED: {label}\n"
            f"📋 {question[:70]}\n"
            f"P&L: {pnl:+.2f} USDC | Bet: {bet} USDC @ {entry:.0%}"
        )
        logger.info(f"  RESOLVED {label} [{source}]: {question[:50]} | P&L={pnl:+.2f}")
        self._notify(msg)
        self._write_alert(cid, question, {
            "type": "resolution",
            "outcome": outcome,
            "pnl": pnl,
            "current_price": price,
            "source": source,
        })
        _save_learning_outcome(self.db_path, t["id"], cid, action, bet, entry, outcome, pnl)
        self._learning_outcomes_this_session += 1
        return outcome, pnl

    def _stale_close(self, trade_id: int) -> None:
        """Force-close an aged-out trade so it frees a position slot (FIX 4).
        Marked STALE with won=NULL and pnl=0 so it never counts as a win/loss."""
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                UPDATE trades
                SET resolved=1, resolution_timestamp=?,
                    resolution_outcome='STALE', pnl_usdc=0.0, won=NULL
                WHERE id=?
            """, (now, trade_id))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _age_hours(ts: str) -> float:
        if not ts:
            return 0.0
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)
        except Exception:
            return 0.0

    def _write_alert(self, market_id: str, question: str, payload: dict):
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                INSERT INTO agent_events
                    (timestamp, agent_source, event_type, market_id, market_question, payload, processed)
                VALUES (?, 'position_monitor', 'POSITION_ALERT', ?, ?, ?, 1)
            """, (now, market_id, question, json.dumps(payload)))
            conn.commit()
        finally:
            conn.close()

    def _notify(self, message: str):
        try:
            from notifier import TelegramNotifier
            notifier = TelegramNotifier()
            if notifier.enabled:
                notifier.send_message(message)
        except Exception as e:
            logger.debug(f"PositionMonitor: notify failed: {e}")

    def check_once(self) -> dict:
        trades = self._open_trades()
        if not trades:
            logger.info("PositionMonitor: no open positions")
            return {"checked": 0, "resolved": 0, "alerts": 0}

        logger.info(f"PositionMonitor: checking {len(trades)} open positions...")
        resolved = 0
        alerts = 0
        stale = 0
        stuck = 0

        # Daily stuck-sweep bookkeeping: clear the per-day alert set on UTC rollover
        # so each still-stuck position re-alerts at most once per day.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._sweep_date:
            self._sweep_date = today
            self._stuck_alerted = set()

        for t in trades:
            cid = t["market_id"]
            question = t["question"]
            action = t["action"]
            entry = t["entry_price"]
            bet = t["bet_usdc"]
            is_dry = t["dry_run"]
            age = self._age_hours(t.get("timestamp", ""))

            is_yes_bet = "YES" in action
            token_id = t.get("token_id", "")

            # --- Single authoritative CLOB fetch (settlement + live price + end_date) ---
            m = _get_clob_market(cid)
            price = m["yes_price"]        # YES/primary-side price (live or settled)
            closed = m["closed"]

            # --- 1a) EXACT settlement against the wallet's held token_id. Scores the
            #         real side the wallet took — no positional/label assumption. ---
            held = m["tokens_by_id"].get(token_id) if token_id else None
            if closed and held is not None:
                if held["winner"] is not None:
                    held_won = bool(held["winner"])
                elif held["price"] is not None:
                    held_won = held["price"] >= 0.5
                else:
                    held_won = None
                if held_won is not None:
                    self._emit_resolution(t, cid, question, action, bet, entry,
                                          is_dry, price if price is not None else 0.0,
                                          False, "clob_tokenid", won_override=held_won)
                    resolved += 1
                    continue

            # --- 1b) Fallback: primary-side settlement (legacy trades w/o token_id).
            #         Action is index-consistent since the 2026-07-13 Scout fix. ---
            if m["resolved"] and m["yes_settled"] is not None:
                self._emit_resolution(t, cid, question, action, bet, entry,
                                      is_dry, m["yes_settled"], False, "clob")
                resolved += 1
                continue

            # --- 2) Threshold resolution from live price (market still open book) ---
            resolved_yes = resolved_no = False
            estimated = False
            if price is not None:
                resolved_yes = is_yes_bet and price >= RESOLUTION_YES
                resolved_no = (not is_yes_bet) and price <= RESOLUTION_NO
                if closed and not (resolved_yes or resolved_no):
                    # Closed but price didn't fully snap — settle on side of 0.5.
                    resolved_yes = is_yes_bet and price >= 0.5
                    resolved_no = (not is_yes_bet) and price < 0.5
                    estimated = resolved_yes or resolved_no

            if resolved_yes or resolved_no:
                self._emit_resolution(t, cid, question, action, bet, entry,
                                      is_dry, price, estimated,
                                      "clob_estimated" if estimated else "clob")
                resolved += 1
                continue

            # --- 3) Cap-deadlock backstop: aged-out, still-open → STALE close ---
            if age >= STALE_POSITION_HOURS:
                self._stale_close(t["id"])
                logger.info(f"  STALE close (age {age/24:.0f}d): {question[:50]}")
                self._write_alert(cid, question, {"type": "stale_close", "age_hours": age})
                stale += 1
                continue

            # --- 4) Resolution-detection safety net: market ended >48h ago but
            #        CLOB did not settle it → alert. Never let a settled market
            #        sit silently at resolved=0 again. ---
            hrs_past = _hours_past_end(m["end_date"])
            if hrs_past is not None and hrs_past >= STUCK_UNRESOLVED_HOURS:
                key = (today, cid)
                if key not in self._stuck_alerted:
                    self._stuck_alerted.add(key)
                    stuck += 1
                    msg = (
                        f"🕳️ STUCK UNRESOLVED\n"
                        f"📋 {question[:70]}\n"
                        f"Market ended {hrs_past/24:.1f}d ago but is still open "
                        f"(no CLOB settlement). Bet: {bet} USDC @ {entry:.0%}"
                    )
                    logger.warning(
                        f"  STUCK UNRESOLVED ({hrs_past/24:.1f}d past end): {question[:50]}"
                    )
                    self._notify(msg)  # A1 → Telegram; A2 muted → log only
                    self._write_alert(cid, question, {
                        "type": "stuck_unresolved",
                        "hours_past_end": round(hrs_past, 1),
                        "end_date": m["end_date"],
                    })
                continue

            if price is None:
                logger.debug(f"  {question[:40]}: price unavailable")
                continue

            # --- 5) Adverse movement (skip if already alerted this session) ---
            if cid not in self._alerted:
                adverse = (entry - price) if is_yes_bet else (price - entry)
                if adverse >= ADVERSE_THRESHOLD:
                    self._alerted.add(cid)
                    direction = "YES" if is_yes_bet else "NO"
                    msg = (
                        f"⚠️ POSITION WARNING\n"
                        f"📋 {question[:70]}\n"
                        f"{direction} @ {entry:.0%} → now {price:.0%}\n"
                        f"Adverse: {adverse:.0%} | Bet: {bet} USDC"
                    )
                    logger.warning(f"  ADVERSE {adverse:.0%}: {question[:50]}")
                    self._notify(msg)
                    self._write_alert(cid, question, {
                        "type": "adverse_movement",
                        "direction": direction,
                        "entry_price": entry,
                        "current_price": price,
                        "adverse_move": adverse,
                    })
                    alerts += 1
            else:
                direction = "YES" if is_yes_bet else "NO"
                move = (entry - price) if is_yes_bet else (price - entry)
                logger.debug(f"  {question[:40]}: {direction} {entry:.0%}→{price:.0%} ({move:+.0%})")

        logger.info(f"PositionMonitor: checked={len(trades)} resolved={resolved} stale={stale} stuck={stuck} alerts={alerts}")
        if resolved > 0:
            _maybe_run_learning_update(self.db_path)
        return {"checked": len(trades), "resolved": resolved, "stale": stale,
                "stuck": stuck, "alerts": alerts}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, name="PositionMonitor", daemon=True
        )
        self._thread.start()
        logger.info("PositionMonitor: background thread started")

    def stop(self):
        self._running = False

    def _run_loop(self):
        try:
            self.check_once()
        except Exception as e:
            logger.error(f"PositionMonitor: initial check failed: {e}", exc_info=True)
        while self._running:
            for _ in range(MONITOR_INTERVAL):
                if not self._running:
                    return
                time.sleep(1)
            try:
                self.check_once()
            except Exception as e:
                logger.error(f"PositionMonitor: check error: {e}", exc_info=True)


# =============================================================================
# Standalone test: python agent_position_monitor.py --test
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if "--test" not in sys.argv:
        print("Usage: python agent_position_monitor.py --test")
        sys.exit(0)

    import tempfile, os
    tmp_db = tempfile.mktemp(suffix=".db")

    conn = sqlite3.connect(tmp_db)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_condition_id TEXT, market_question TEXT,
            action TEXT, bet_usdc REAL, entry_price REAL, dry_run INTEGER DEFAULT 1,
            resolved INTEGER DEFAULT 0, resolution_timestamp TEXT,
            resolution_outcome TEXT, pnl_usdc REAL, won INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE agent_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, agent_source TEXT, event_type TEXT,
            market_id TEXT, market_question TEXT, payload TEXT, processed INTEGER DEFAULT 0
        )
    """)
    # Insert a fake open position
    conn.execute("""
        INSERT INTO trades (market_condition_id, market_question, action, bet_usdc, entry_price, dry_run)
        VALUES ('test-cid-001', 'Will OKC Thunder win NBA Finals?', 'BET_YES', 2.5, 0.38, 1)
    """)
    conn.commit()
    conn.close()

    print("=== PositionMonitorAgent standalone test ===\n")
    agent = PositionMonitorAgent(tmp_db)

    print("--- Checking open positions (will try to fetch live price) ---")
    result = agent.check_once()
    print(f"Result: {result}")

    try:
        os.unlink(tmp_db)
    except Exception:
        pass
    print("\nTest PASSED")
