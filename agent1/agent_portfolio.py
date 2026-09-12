"""
agent_portfolio.py — Portfolio Manager Agent (Agent 6)

CEO-level portfolio overview every 6 hours.
Runs at 00:00, 06:00, 12:00, 18:00 UTC.
Sends structured Telegram report with full agent activity summary.

Usage:
    python agent_portfolio.py --test
"""

import json
import logging
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import config

logger = logging.getLogger(__name__)


def _secs_to_next_report() -> int:
    """Seconds until next 00/06/12/18 UTC slot."""
    now = datetime.now(timezone.utc)
    for h in [0, 6, 12, 18]:
        candidate = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if candidate > now:
            return max(1, int((candidate - now).total_seconds()))
    # All today's slots passed → next is 00:00 tomorrow
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


class PortfolioAgent:

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # -------------------------------------------------------------------------
    # Data queries
    # -------------------------------------------------------------------------

    def _recent_events(self, hours: int = 6) -> list:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("""
                SELECT agent_source, event_type, market_question, payload, timestamp
                FROM agent_events WHERE timestamp >= ?
                ORDER BY timestamp DESC
            """, (since,)).fetchall()
            result = []
            for r in rows:
                try:
                    result.append({
                        "source": r[0], "type": r[1],
                        "question": r[2] or "",
                        "payload": json.loads(r[3]) if r[3] else {},
                        "timestamp": r[4],
                    })
                except Exception:
                    pass
            return result
        finally:
            conn.close()

    def _open_positions(self) -> list:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("""
                SELECT market_question, action, bet_usdc, entry_price, dry_run
                FROM trades WHERE resolved=0 ORDER BY timestamp DESC
            """).fetchall()
            return [
                {"question": r[0], "action": r[1],
                 "bet_usdc": r[2], "entry_price": r[3], "dry_run": r[4]}
                for r in rows
            ]
        finally:
            conn.close()

    def _stats(self) -> dict:
        conn = sqlite3.connect(self.db_path)
        try:
            at_risk = conn.execute(
                "SELECT COALESCE(SUM(bet_usdc),0) FROM trades WHERE resolved=0"
            ).fetchone()[0]
            total_pnl = conn.execute(
                "SELECT COALESCE(SUM(pnl_usdc),0) FROM trades WHERE resolved=1"
            ).fetchone()[0]
            wins = conn.execute("SELECT COUNT(*) FROM trades WHERE won=1").fetchone()[0]
            losses = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE won=0 AND resolved=1"
            ).fetchone()[0]
            open_count = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE resolved=0"
            ).fetchone()[0]
            return {
                "open_count": open_count,
                "capital_at_risk": float(at_risk),
                "total_pnl": float(total_pnl),
                "wins": wins,
                "losses": losses,
                "win_rate": wins / (wins + losses) if (wins + losses) > 0 else 0.0,
            }
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Report generation
    # -------------------------------------------------------------------------

    def generate_report(self) -> str:
        now = datetime.now(timezone.utc)
        time_str = now.strftime("%H:%M UTC")
        dry = config.DRY_RUN

        events = self._recent_events(6)
        positions = self._open_positions()
        stats = self._stats()

        scout_evs   = [e for e in events if e["type"] == "NEW_POSITION"]
        analysis_evs = [e for e in events if e["type"] == "ANALYSIS_COMPLETE"]
        challenge_evs = [e for e in events if e["type"] == "CHALLENGE_COMPLETE"]
        approved_evs = [e for e in events if e["type"] == "RISK_APPROVED"]
        rejected_evs = [e for e in events if e["type"] == "RISK_REJECTED"]
        alert_evs   = [e for e in events if e["type"] == "POSITION_ALERT"]

        dry_label = "[DRY RUN] " if dry else ""
        lines = [
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"{dry_label}PORTFOLIO REPORT — {time_str}",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]

        # --- Scout ---
        lines.append(f"🔍 SCOUT: {len(scout_evs)} new wallet positions detected")
        for ev in scout_evs[:4]:
            p = ev["payload"]
            wallet = p.get("wallet_name", "?")
            direction = p.get("direction", "?")
            q = ev["question"][:45]
            h = p.get("hours_ago", 0)
            eff = p.get("efficiency", "?")
            lines.append(f"   {wallet} → {direction} {q} ({h:.1f}h ago | {eff})")

        # --- Analyst ---
        bets = sum(1 for e in analysis_evs
                   if e["payload"].get("action") in ("BET_YES", "BET_NO"))
        lines.append(f"\n📊 ANALYST: {len(analysis_evs)} analyzed, {bets} bet recommended")

        # --- Devil ---
        devil_skips = sum(1 for e in challenge_evs
                          if e["payload"].get("final_verdict") == "SKIP")
        lines.append(f"🔴 DEVIL: {devil_skips} overridden")

        # --- Risk ---
        first_rejection = ""
        if rejected_evs:
            first_rejection = rejected_evs[0]["payload"].get("rejection_reason", "")
        rej_note = f": {first_rejection}" if first_rejection else ""
        lines.append(
            f"⚖️  RISK: {len(approved_evs)} approved "
            f"({len(rejected_evs)} rejected{rej_note})"
        )

        # --- New bets ---
        if approved_evs:
            lines.append("\n💰 NEW BETS (last 6h)")
            for ev in approved_evs[:5]:
                p = ev["payload"]
                q = ev["question"][:50]
                action = p.get("action", "?")
                size = p.get("bet_size_usdc", 0)
                price = p.get("current_price", 0)
                reason = (p.get("reasoning") or "")[:60]
                lines.append(f"+ {q}")
                lines.append(f"  {action} — {size} USDC @ {price:.0%}")
                if reason:
                    lines.append(f"  Reason: {reason}")

        # --- Open positions ---
        max_pos = getattr(config, "MAX_OPEN_POSITIONS", 15)
        if positions:
            lines.append(f"\n📈 OPEN POSITIONS ({len(positions)}/{max_pos})")
            for p in positions[:8]:
                q = p["question"][:45]
                dry_flag = "~" if p["dry_run"] else " "
                lines.append(
                    f"{dry_flag}+ {q}: {p['action']} @ {p['entry_price']:.0%} "
                    f"({p['bet_usdc']} USDC)"
                )
        else:
            lines.append("\n📈 OPEN POSITIONS: none")

        # --- Portfolio summary ---
        portfolio_usdc = getattr(config, "PORTFOLIO_USDC", 50.0)
        current = portfolio_usdc + stats["total_pnl"]
        pnl_sign = "+" if stats["total_pnl"] >= 0 else ""

        lines.append("\n🏦 PORTFOLIO")
        lines.append(
            f"Start: {portfolio_usdc:.2f} | Current: {current:.2f} | "
            f"P&L: {pnl_sign}{stats['total_pnl']:.2f} USDC"
        )
        lines.append(
            f"Win/Loss: {stats['wins']}W/{stats['losses']}L | "
            f"Win Rate: {stats['win_rate']:.0%}"
        )
        lines.append(
            f"Capital at risk: {stats['capital_at_risk']:.2f} USDC "
            f"({stats['capital_at_risk']/portfolio_usdc:.0%})"
        )

        if alert_evs:
            lines.append(f"\n⚠️  ALERTS: {len(alert_evs)} position alerts in last 6h")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        return "\n".join(lines)

    def send_report(self):
        try:
            report = self.generate_report()
            logger.info("PortfolioManager: sending 6h report...")
            logger.info("\n" + report)

            try:
                from notifier import TelegramNotifier
                notifier = TelegramNotifier()
                if notifier.enabled:
                    notifier.send_message(report)
            except Exception as e:
                logger.warning(f"PortfolioManager: Telegram send failed: {e}")
        except Exception as e:
            logger.error(f"PortfolioManager: report failed: {e}", exc_info=True)

    # -------------------------------------------------------------------------
    # Threading
    # -------------------------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, name="PortfolioManager", daemon=True
        )
        self._thread.start()
        logger.info("PortfolioManager: background thread started")

    def stop(self):
        self._running = False

    def _run_loop(self):
        while self._running:
            wait = _secs_to_next_report()
            logger.info(f"PortfolioManager: next report in {wait // 60}min")
            for _ in range(wait):
                if not self._running:
                    return
                time.sleep(1)
            if self._running:
                self.send_report()


# =============================================================================
# Standalone test: python agent_portfolio.py --test
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if "--test" not in sys.argv:
        print("Usage: python agent_portfolio.py --test")
        sys.exit(0)

    import tempfile, os
    tmp_db = tempfile.mktemp(suffix=".db")

    conn = sqlite3.connect(tmp_db)
    conn.executescript("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            market_condition_id TEXT, market_question TEXT,
            action TEXT, token_id TEXT, bet_usdc REAL, entry_price REAL,
            order_id TEXT, resolved INTEGER DEFAULT 0, dry_run INTEGER DEFAULT 1,
            market_category TEXT, confidence_at_bet REAL, pnl_usdc REAL, won INTEGER,
            resolution_timestamp TEXT, resolution_outcome TEXT
        );
        CREATE TABLE agent_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            agent_source TEXT, event_type TEXT,
            market_id TEXT, market_question TEXT,
            payload TEXT, processed INTEGER DEFAULT 0
        );
        INSERT INTO trades (market_question, action, bet_usdc, entry_price, dry_run, resolved, won, pnl_usdc)
        VALUES
            ('OKC Thunder NBA Finals?', 'BET_YES', 2.5, 0.38, 1, 1, 1, 4.08),
            ('Lakers vs Celtics?', 'BET_NO', 1.5, 0.62, 1, 0, NULL, NULL);
        INSERT INTO agent_events (agent_source, event_type, market_question, payload)
        VALUES
            ('scout', 'NEW_POSITION', 'OKC Thunder?', '{"wallet_name":"RN1","direction":"YES","hours_ago":0.8,"efficiency":"PROCEED"}'),
            ('analyst', 'ANALYSIS_COMPLETE', 'OKC Thunder?', '{"action":"BET_YES","confidence":"high"}'),
            ('risk', 'RISK_APPROVED', 'OKC Thunder?', '{"bet_size_usdc":2.5,"current_price":0.41,"action":"BET_YES","reasoning":"Strong smart money signal"}');
    """)
    conn.commit()
    conn.close()

    print("=== PortfolioAgent standalone test ===\n")
    agent = PortfolioAgent(tmp_db)
    report = agent.generate_report()
    # Safe print for Windows consoles that don't support emoji
    sys.stdout.buffer.write((report + "\n").encode("utf-8", errors="replace"))

    try:
        os.unlink(tmp_db)
    except Exception:
        pass
    print("\nTest PASSED")
