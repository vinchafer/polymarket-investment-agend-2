"""
main.py — 6-Agent Polymarket Investment System

Orchestrates 6 specialized agents communicating via SQLite event bus.

Event flow:
  Scout → agent_events → Analyst → Devil → Risk → Execution
  Position Monitor (background, 2h)
  Portfolio Manager (background, 6h)

Start modes:
  python main.py          — DRY RUN, continuous
  python main.py --live   — Live trading (real money!)
  python main.py --once   — Single pipeline run then exit
  python main.py --status — Show current portfolio status and exit
  python main.py --report — Send Telegram portfolio report now and exit
"""

import argparse
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone

import config
from config import validate_config


# =============================================================================
# Logging
# =============================================================================

def setup_logging():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(ch)

    fh = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(fh)

    for lib in ("urllib3", "httpx", "google", "groq"):
        logging.getLogger(lib).setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


# =============================================================================
# DB bootstrap
# =============================================================================

def ensure_db(db_path: str):
    """Ensure all required tables exist (agent_events + legacy tables)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                agent_source TEXT,
                event_type TEXT,
                market_id TEXT,
                market_question TEXT,
                payload TEXT,
                processed INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER,
                timestamp TEXT NOT NULL,
                market_condition_id TEXT NOT NULL,
                market_question TEXT NOT NULL,
                action TEXT NOT NULL,
                token_id TEXT NOT NULL DEFAULT '',
                bet_usdc REAL NOT NULL,
                entry_price REAL,
                order_id TEXT,
                resolved INTEGER DEFAULT 0,
                resolution_timestamp TEXT,
                resolution_outcome TEXT,
                pnl_usdc REAL,
                won INTEGER,
                dry_run INTEGER DEFAULT 1,
                market_category TEXT,
                confidence_at_bet REAL,
                edge_at_bet REAL,
                source_quality_at_bet REAL,
                weighted_score_at_bet REAL
            );

            CREATE TABLE IF NOT EXISTS top_wallet_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_rank INTEGER,
                wallet_address TEXT,
                market_condition_id TEXT,
                market_question TEXT,
                direction TEXT,
                size_usd REAL,
                entry_price REAL,
                current_price REAL,
                unrealized_pnl REAL,
                conviction_level TEXT DEFAULT 'LOW',
                timestamp TEXT,
                UNIQUE(wallet_address, market_condition_id)
            );

            CREATE TABLE IF NOT EXISTS learning_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER,
                market_condition_id TEXT,
                sport_category TEXT,
                confidence_at_bet REAL,
                edge_at_bet REAL,
                weighted_score_at_bet REAL,
                source_quality_score REAL,
                tier1_2_count INTEGER DEFAULT 0,
                days_to_resolution_at_bet INTEGER DEFAULT 0,
                market_volume_usd REAL DEFAULT 0,
                time_of_day_utc INTEGER DEFAULT 0,
                outcome TEXT,
                pnl_usdc REAL DEFAULT 0,
                model_predicted_prob REAL,
                recorded_at TEXT
            );

            CREATE TABLE IF NOT EXISTS groq_usage (
                date TEXT PRIMARY KEY,
                requests_made INTEGER DEFAULT 0,
                tokens_used INTEGER DEFAULT 0
            );
        """)
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Status display
# =============================================================================

def show_status(db_path: str):
    """Print current portfolio status to stdout."""
    conn = sqlite3.connect(db_path)
    try:
        open_trades = conn.execute(
            "SELECT market_question, action, bet_usdc, entry_price, dry_run FROM trades WHERE resolved=0"
        ).fetchall()
        total_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl_usdc),0) FROM trades WHERE resolved=1"
        ).fetchone()[0]
        wins = conn.execute("SELECT COUNT(*) FROM trades WHERE won=1").fetchone()[0]
        losses = conn.execute("SELECT COUNT(*) FROM trades WHERE won=0 AND resolved=1").fetchone()[0]
        event_counts = conn.execute(
            "SELECT event_type, COUNT(*) FROM agent_events GROUP BY event_type"
        ).fetchall()
    finally:
        conn.close()

    print("\n=== POLYMARKET AGENT STATUS ===")
    print(f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"DRY_RUN: {config.DRY_RUN}")
    print(f"\nOpen Positions ({len(open_trades)}):")
    for t in open_trades:
        dry = " [DRY]" if t[4] else ""
        print(f"  {t[0][:55]}: {t[1]} @ {t[3]:.0%} | {t[2]} USDC{dry}")
    print(f"\nResolved: {wins}W / {losses}L | Total P&L: {total_pnl:+.2f} USDC")
    print(f"\nAgent Events:")
    for etype, cnt in sorted(event_counts):
        print(f"  {etype:<25} {cnt}")
    print()


# =============================================================================
# Orchestrator
# =============================================================================

class _ProviderProbe:
    """Periodic router health-probe (Aufgabe 4, SPOF mitigation). Keeps the
    fallback providers' state fresh so A1 knows where to fail over if Groq
    collapses. Inert in the copy-arm (COPY_MODE) — A2 never calls an LLM."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._running = False
        self._thread = None

    def start(self):
        if config.COPY_MODE:
            logger.info("ProviderProbe: disabled (COPY_MODE — copy-deterministic arm)")
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="ProviderProbe", daemon=True)
        self._thread.start()
        logger.info("ProviderProbe: background thread started")

    def stop(self):
        self._running = False

    def _loop(self):
        from llm_router import LLMRouter
        router = LLMRouter(self.db_path, analyst_mode=True)
        interval = getattr(config, "PROVIDER_PROBE_INTERVAL_SECONDS", 21600)
        try:
            router.probe_fallbacks()  # immediate freshness at startup
        except Exception as e:
            logger.debug(f"ProviderProbe: initial probe failed: {e}")
        while self._running:
            for _ in range(interval):
                if not self._running:
                    return
                time.sleep(1)
            try:
                router.probe_fallbacks()
            except Exception as e:
                logger.debug(f"ProviderProbe: probe failed: {e}")


class Orchestrator:

    def __init__(self):
        self.db_path = config.DB_PATH
        ensure_db(self.db_path)

        # Import agents
        from agent_scout import ScoutAgent
        from agent_analyst import AnalystAgent
        from agent_devil import DevilAgent
        from agent_risk import RiskAgent
        from agent_position_monitor import PositionMonitorAgent
        from agent_portfolio import PortfolioAgent
        from health_monitor import HealthMonitor

        self.scout = ScoutAgent(self.db_path)
        self.analyst = AnalystAgent(self.db_path)
        self.devil = DevilAgent(self.db_path)
        self.risk = RiskAgent(self.db_path)
        self.position_monitor = PositionMonitorAgent(self.db_path)
        self.portfolio = PortfolioAgent(self.db_path)
        self.health = HealthMonitor(self.db_path)
        self.probe = _ProviderProbe(self.db_path)

    def start_all(self):
        """Start all 6 agents + health monitor + router probe as daemon threads."""
        self.scout.start()
        self.analyst.start()
        self.devil.start()
        self.risk.start()
        self.position_monitor.start()
        self.portfolio.start()
        self.health.start()
        self.probe.start()
        logger.info("All 6 agents + health monitor started")

    def stop_all(self):
        self.scout.stop()
        self.analyst.stop()
        self.devil.stop()
        self.risk.stop()
        self.position_monitor.stop()
        self.portfolio.stop()
        self.health.stop()
        self.probe.stop()
        logger.info("All agents stopped")

    def run_once(self, timeout_seconds: int = 300):
        """
        Run a single full pipeline cycle:
        Scout scan → wait for Analyst → wait for Devil → wait for Risk.
        Returns when pipeline drains or timeout reached.
        """
        logger.info("=== SINGLE PIPELINE RUN (--once mode) ===")

        # Step 1: Scout scan
        logger.info("Step 1/4: Scout scanning wallets...")
        n_new = self.scout.scan_once()
        logger.info(f"Scout: {n_new} new positions found")

        if n_new == 0:
            logger.info("No new positions — pipeline complete (nothing to process)")
            return

        # Step 2: Wait for Analyst to process all NEW_POSITION events
        logger.info("Step 2/4: Analyst processing...")
        self._drain_event_type("NEW_POSITION", "ANALYSIS_COMPLETE",
                               self.analyst.poll_once, timeout_seconds)

        # Step 3: Wait for Devil to challenge all BET decisions
        logger.info("Step 3/4: Devil challenging bets...")
        self._drain_event_type("ANALYSIS_COMPLETE", "CHALLENGE_COMPLETE",
                               self.devil.poll_once, timeout_seconds)

        # Step 4: Risk Officer processes approved decisions
        logger.info("Step 4/4: Risk Officer processing...")
        self._drain_event_type("CHALLENGE_COMPLETE", "RISK_APPROVED",
                               self.risk.poll_once, timeout_seconds)

        # Router health-probe once per --once run (A1/LLM arm only) so fallback
        # provider_state stays fresh and the probe is exercised in verification.
        if not config.COPY_MODE:
            try:
                from llm_router import LLMRouter
                LLMRouter(self.db_path, analyst_mode=True).probe_fallbacks()
            except Exception as e:
                logger.debug(f"Router probe (--once) failed: {e}")

        logger.info("=== PIPELINE COMPLETE ===")
        show_status(self.db_path)

    def _drain_event_type(self, from_type: str, to_type: str,
                           poll_fn, timeout: int):
        """Poll until no unprocessed `from_type` events remain or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            conn = sqlite3.connect(self.db_path)
            pending = conn.execute(
                "SELECT COUNT(*) FROM agent_events WHERE event_type=? AND processed=0",
                (from_type,),
            ).fetchone()[0]
            conn.close()

            if pending == 0:
                break
            try:
                poll_fn()
            except Exception as e:
                logger.error(f"Pipeline poll error: {e}", exc_info=True)
            time.sleep(2)
        else:
            logger.warning(f"Timeout waiting for {from_type} → {to_type}")


# =============================================================================
# Entry point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Polymarket 6-Agent Investment System")
    parser.add_argument("--live", action="store_true",
                        help="Enable live trading (overrides DRY_RUN=true)")
    parser.add_argument("--once", action="store_true",
                        help="Run one pipeline cycle then exit")
    parser.add_argument("--status", action="store_true",
                        help="Show portfolio status and exit")
    parser.add_argument("--report", action="store_true",
                        help="Send Telegram portfolio report now and exit")
    args = parser.parse_args()

    setup_logging()

    # Override DRY_RUN if --live flag
    if args.live:
        config.DRY_RUN = False
        os.environ["DRY_RUN"] = "false"

    logger.info("=" * 60)
    logger.info("  POLYMARKET 6-AGENT SYSTEM STARTING")
    logger.info(f"  DRY_RUN: {config.DRY_RUN}")
    logger.info(f"  DB: {config.DB_PATH}")
    logger.info("=" * 60)

    # Validate config (warn on missing optional keys, error on critical ones)
    try:
        validate_config()
    except ValueError as e:
        logger.error(f"Config validation failed: {e}")
        sys.exit(1)

    # Warn about new keys
    if not getattr(config, "GEMINI2_API_KEY", ""):
        logger.warning("GEMINI2_API_KEY not set — Analyst will fall back to Groq")
    if not getattr(config, "THE_ODDS_API_KEY", ""):
        logger.warning("THE_ODDS_API_KEY not set — efficiency checker will use Kalshi only")

    # Ensure DB
    ensure_db(config.DB_PATH)

    # --status mode
    if args.status:
        show_status(config.DB_PATH)
        return

    # --report mode
    if args.report:
        from agent_portfolio import PortfolioAgent
        agent = PortfolioAgent(config.DB_PATH)
        agent.send_report()
        return

    # Build orchestrator
    orchestrator = Orchestrator()

    # --once mode
    if args.once:
        orchestrator.run_once(timeout_seconds=300)
        return

    # Continuous mode
    def _shutdown(sig, frame):
        logger.info("Shutdown signal received — stopping agents...")
        orchestrator.stop_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    orchestrator.start_all()

    logger.info("All agents running. Press Ctrl+C to stop.")
    logger.info(f"Scout scans every {config.SCOUT_INTERVAL_SECONDS // 60}min | "
                f"Analyst polls every {config.ANALYST_POLL_SECONDS}s | "
                f"Position check every {config.POSITION_MONITOR_INTERVAL_SECONDS // 60}min")

    # Keep main thread alive
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
