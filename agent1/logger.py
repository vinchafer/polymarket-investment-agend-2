"""
logger.py — SQLite Trade-Logging & Analyse
Speichert alle Entscheidungen, Trades, Ergebnisse und Lern-Insights persistent.

Tabellen:
  decisions          — Alle Agent-Entscheidungen (inkl. SKIPs)
  trades             — Platzierte Trades mit P&L
  daily_summaries    — Tagesstatistiken
  errors             — Fehler-Log
  agent_learnings    — Wochentliche Selbst-Analyse Erkenntnisse
  research_cache     — Gecachte Tavily-Rechercheergebnisse
  odds_movements     — Erkannte Kursveraenderungen
  portfolio_peaks    — Portfolio-Hochpunkte fuer Drawdown-Tracking
"""

import sqlite3
import logging
import json
from datetime import datetime, timezone
from typing import Optional

import config
from agent import AgentDecision, TradeAction

logger = logging.getLogger(__name__)


class TradeLogger:
    """
    Persistente Protokollierung aller Agent-Aktivitaeten in SQLite.
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or config.DB_PATH
        self._initialize_db()

    def _initialize_db(self):
        """Erstellt alle benoetigten Tabellen wenn sie noch nicht existieren."""
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                -- Alle Entscheidungen des Agenten (inkl. SKIPs)
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    market_condition_id TEXT NOT NULL,
                    market_question TEXT NOT NULL,
                    market_category TEXT,
                    days_to_resolution INTEGER,
                    market_yes_price REAL,
                    market_no_price REAL,
                    action TEXT NOT NULL,
                    agent_yes_probability REAL,
                    edge REAL,
                    confidence REAL,
                    recommended_bet_usdc REAL,
                    reasoning TEXT,
                    key_factors TEXT,
                    risks TEXT,
                    research_sentiment TEXT,
                    research_confidence REAL,
                    research_sources TEXT,
                    model_used TEXT,
                    tokens_used INTEGER,
                    dry_run INTEGER DEFAULT 1,
                    weighted_score REAL,
                    source_quality_score REAL
                );

                -- Platzierte Trades
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id INTEGER REFERENCES decisions(id),
                    timestamp TEXT NOT NULL,
                    market_condition_id TEXT NOT NULL,
                    market_question TEXT NOT NULL,
                    action TEXT NOT NULL,
                    token_id TEXT NOT NULL,
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

                -- Taeglche Zusammenfassungen
                CREATE TABLE IF NOT EXISTS daily_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT UNIQUE NOT NULL,
                    bets_placed INTEGER DEFAULT 0,
                    bets_skipped INTEGER DEFAULT 0,
                    total_invested_usdc REAL DEFAULT 0.0,
                    realized_pnl_usdc REAL DEFAULT 0.0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    win_rate REAL,
                    api_cost_usd REAL DEFAULT 0.0,
                    created_at TEXT
                );

                -- Fehler-Log
                CREATE TABLE IF NOT EXISTS errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    component TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    details TEXT
                );

                -- Wochentliche Selbst-Analyse Erkenntnisse
                CREATE TABLE IF NOT EXISTS agent_learnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    analysis_date TEXT NOT NULL,
                    insight_type TEXT NOT NULL,
                    key TEXT NOT NULL,
                    win_rate REAL,
                    total_bets INTEGER,
                    recommendation TEXT,
                    reasoning TEXT,
                    action_taken TEXT,
                    active INTEGER DEFAULT 1
                );

                -- Gecachte Rechercheergebnisse
                CREATE TABLE IF NOT EXISTS research_cache (
                    cache_key TEXT PRIMARY KEY,
                    market_id TEXT NOT NULL,
                    cached_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    is_past_event INTEGER DEFAULT 0,
                    result_json TEXT NOT NULL
                );

                -- Erkannte Kursveraenderungen
                CREATE TABLE IF NOT EXISTS odds_movements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    market_condition_id TEXT NOT NULL,
                    market_question TEXT,
                    old_yes_price REAL,
                    new_yes_price REAL,
                    movement_pct REAL,
                    direction TEXT,
                    consecutive_same_direction INTEGER DEFAULT 1,
                    triggered_analysis INTEGER DEFAULT 0
                );

                -- Portfolio-Hochpunkte
                CREATE TABLE IF NOT EXISTS portfolio_peaks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    current_portfolio_usdc REAL,
                    peak_portfolio_usdc REAL,
                    drawdown_pct REAL
                );

                -- Self-Learning Daten (Phase 3)
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

                -- Vollstaendiger Audit-Trail pro Entscheidung (Phase 3)
                CREATE TABLE IF NOT EXISTS decision_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id INTEGER,
                    criterion_scores_json TEXT,
                    learning_adjustments_json TEXT,
                    cross_platform_data_json TEXT,
                    odds_movement_data_json TEXT,
                    final_reasoning TEXT,
                    override_reason TEXT,
                    created_at TEXT
                );

                -- Dynamische Konfiguration (lernbasierte Ueberschreibungen, Phase 3)
                CREATE TABLE IF NOT EXISTS adaptive_config (
                    key TEXT PRIMARY KEY,
                    value REAL NOT NULL,
                    base_value REAL,
                    last_updated TEXT,
                    update_reason TEXT,
                    update_count INTEGER DEFAULT 0
                );

                -- Groq API usage tracking
                CREATE TABLE IF NOT EXISTS groq_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    requests_made INTEGER DEFAULT 0,
                    tokens_used INTEGER DEFAULT 0,
                    UNIQUE(date)
                );

                -- Smart money wallet signals
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
                    timestamp TEXT,
                    UNIQUE(wallet_address, market_condition_id)
                );

                -- Indizes
                CREATE INDEX IF NOT EXISTS idx_decisions_market ON decisions(market_condition_id);
                CREATE INDEX IF NOT EXISTS idx_decisions_timestamp ON decisions(timestamp);
                CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_condition_id);
                CREATE INDEX IF NOT EXISTS idx_trades_resolved ON trades(resolved);
                CREATE INDEX IF NOT EXISTS idx_learnings_type ON agent_learnings(insight_type, key);
                CREATE INDEX IF NOT EXISTS idx_odds_market ON odds_movements(market_condition_id);
                CREATE INDEX IF NOT EXISTS idx_cache_expires ON research_cache(expires_at);
                CREATE INDEX IF NOT EXISTS idx_learning_trade ON learning_data(trade_id);
                CREATE INDEX IF NOT EXISTS idx_learning_outcome ON learning_data(outcome);
                CREATE INDEX IF NOT EXISTS idx_learning_category ON learning_data(sport_category);
                CREATE INDEX IF NOT EXISTS idx_audit_decision ON decision_audit(decision_id);
            """)

            # Migrate existing tables: add columns that may be missing
            existing_decisions = {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
            existing_trades = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}

            decisions_migrations = [
                ("weighted_score", "REAL"),
                ("source_quality_score", "REAL"),
            ]
            trades_migrations = [
                ("market_category", "TEXT"),
                ("confidence_at_bet", "REAL"),
                ("edge_at_bet", "REAL"),
                ("source_quality_at_bet", "REAL"),
                ("weighted_score_at_bet", "REAL"),
            ]

            for col, col_type in decisions_migrations:
                if col not in existing_decisions:
                    conn.execute(f"ALTER TABLE decisions ADD COLUMN {col} {col_type}")
                    logger.info(f"Migration: decisions.{col} hinzugefuegt")

            for col, col_type in trades_migrations:
                if col not in existing_trades:
                    conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_type}")
                    logger.info(f"Migration: trades.{col} hinzugefuegt")

            # Add index for trades.market_category after ensuring the column exists
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trades_category ON trades(market_category)"
            )

            # Phase 3 migrations: additional trades columns
            existing_trades = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
            trades_phase3 = [
                ("market_volume_usd", "REAL"),
                ("tier1_2_count_at_bet", "INTEGER"),
                ("days_to_resolution_at_bet", "INTEGER"),
            ]
            for col, col_type in trades_phase3:
                if col not in existing_trades:
                    conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_type}")
                    logger.info(f"Migration: trades.{col} hinzugefuegt")

        logger.info(f"Datenbank initialisiert: {self.db_path}")

    # =========================================================================
    # Decisions & Trades
    # =========================================================================

    def log_decision(
        self,
        decision: AgentDecision,
        market_condition_id: str,
        market_category: str = "",
        days_to_resolution: int = 0,
        market_yes_price: float = 0.0,
        market_no_price: float = 0.0,
        research_sentiment: str = "",
        research_confidence: float = 0.0,
        research_sources: list = None,
        dry_run: bool = True,
        weighted_score: float = 0.0,
    ) -> int:
        """Loggt eine Agent-Entscheidung (inkl. SKIPs)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                INSERT INTO decisions (
                    timestamp, market_condition_id, market_question, market_category,
                    days_to_resolution, market_yes_price, market_no_price,
                    action, agent_yes_probability, edge, confidence,
                    recommended_bet_usdc, reasoning, key_factors, risks,
                    research_sentiment, research_confidence, research_sources,
                    model_used, tokens_used, dry_run, weighted_score, source_quality_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                market_condition_id,
                decision.market_question[:500],
                market_category,
                days_to_resolution,
                market_yes_price,
                market_no_price,
                decision.action.value,
                decision.agent_yes_probability,
                decision.edge,
                decision.confidence,
                decision.recommended_bet_usdc,
                decision.reasoning[:2000],
                json.dumps(decision.key_factors),
                json.dumps(decision.risks),
                research_sentiment,
                research_confidence,
                json.dumps(research_sources or []),
                decision.model_used,
                decision.tokens_used,
                1 if dry_run else 0,
                weighted_score,
                getattr(decision, 'source_quality_score', 0.0),
            ))
            return cursor.lastrowid

    def log_trade(
        self,
        decision_id: int,
        market_condition_id: str,
        market_question: str,
        action: TradeAction,
        token_id: str,
        bet_usdc: float,
        entry_price: float,
        order_id: str,
        dry_run: bool = True,
        market_category: str = "",
        confidence: float = 0.0,
        edge: float = 0.0,
        source_quality: float = 0.0,
        weighted_score: float = 0.0,
        market_volume_usd: float = 0.0,
        tier1_2_count: int = 0,
        days_to_resolution: int = 0,
    ) -> int:
        """Loggt einen platzierten Trade."""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.execute("""
                INSERT INTO trades (
                    decision_id, timestamp, market_condition_id, market_question,
                    action, token_id, bet_usdc, entry_price, order_id, dry_run,
                    market_category, confidence_at_bet, edge_at_bet,
                    source_quality_at_bet, weighted_score_at_bet,
                    market_volume_usd, tier1_2_count_at_bet, days_to_resolution_at_bet
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                decision_id,
                datetime.now(timezone.utc).isoformat(),
                market_condition_id,
                market_question[:500],
                action.value,
                token_id,
                bet_usdc,
                entry_price,
                order_id,
                1 if dry_run else 0,
                market_category,
                confidence,
                edge,
                source_quality,
                weighted_score,
                market_volume_usd,
                tier1_2_count,
                days_to_resolution,
            ))
            return cursor.lastrowid

    def log_resolution(self, market_condition_id: str, outcome: str, pnl_usdc: float, won: bool):
        """Markiert einen Trade als aufgeloest."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE trades
                SET resolved = 1,
                    resolution_timestamp = ?,
                    resolution_outcome = ?,
                    pnl_usdc = ?,
                    won = ?
                WHERE market_condition_id = ? AND resolved = 0
            """, (
                datetime.now(timezone.utc).isoformat(),
                outcome,
                pnl_usdc,
                1 if won else 0,
                market_condition_id,
            ))

    def log_error(self, component: str, error_message: str, details: str = ""):
        """Loggt einen Fehler."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO errors (timestamp, component, error_message, details)
                VALUES (?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                component,
                error_message[:1000],
                details[:2000],
            ))

    def save_daily_summary(
        self,
        date: str,
        bets_placed: int,
        bets_skipped: int,
        total_invested: float,
        realized_pnl: float,
        wins: int,
        losses: int,
        api_cost: float,
    ):
        """Speichert taeglche Zusammenfassung (UPSERT)."""
        win_rate = wins / max(wins + losses, 1)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO daily_summaries
                    (date, bets_placed, bets_skipped, total_invested_usdc,
                     realized_pnl_usdc, wins, losses, win_rate, api_cost_usd, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    bets_placed = excluded.bets_placed,
                    bets_skipped = excluded.bets_skipped,
                    total_invested_usdc = excluded.total_invested_usdc,
                    realized_pnl_usdc = excluded.realized_pnl_usdc,
                    wins = excluded.wins,
                    losses = excluded.losses,
                    win_rate = excluded.win_rate,
                    api_cost_usd = excluded.api_cost_usd
            """, (
                date, bets_placed, bets_skipped, total_invested,
                realized_pnl, wins, losses, win_rate, api_cost,
                datetime.now(timezone.utc).isoformat(),
            ))

    # =========================================================================
    # Agent Learnings
    # =========================================================================

    def save_learning(
        self,
        analysis_date: str,
        insight_type: str,
        key: str,
        win_rate: float,
        total_bets: int,
        recommendation: str,
        reasoning: str,
        action_taken: str = "",
    ):
        """Speichert ein Lern-Insight aus der woechentlichen Analyse."""
        with sqlite3.connect(self.db_path) as conn:
            # Deaktiviere alte Insights desselben Typs/Keys
            conn.execute("""
                UPDATE agent_learnings SET active = 0
                WHERE insight_type = ? AND key = ? AND active = 1
            """, (insight_type, key))
            # Fuege neues Insight ein
            conn.execute("""
                INSERT INTO agent_learnings
                    (created_at, analysis_date, insight_type, key, win_rate,
                     total_bets, recommendation, reasoning, action_taken, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (
                datetime.now(timezone.utc).isoformat(),
                analysis_date,
                insight_type,
                key,
                win_rate,
                total_bets,
                recommendation,
                reasoning[:1000],
                action_taken,
            ))

    def get_active_learnings(self, insight_type: str = None) -> list[dict]:
        """Gibt aktive Lern-Insights zurueck."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if insight_type:
                rows = conn.execute("""
                    SELECT * FROM agent_learnings
                    WHERE active = 1 AND insight_type = ?
                    ORDER BY created_at DESC
                """, (insight_type,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM agent_learnings
                    WHERE active = 1
                    ORDER BY insight_type, key
                """).fetchall()
            return [dict(r) for r in rows]

    def get_blacklisted_categories(self) -> set[str]:
        """Gibt blackgelistete Kategorien zurueck."""
        learnings = self.get_active_learnings("category_performance")
        return {
            l["key"]
            for l in learnings
            if l["recommendation"] == "blacklist"
        }

    # =========================================================================
    # Research Cache
    # =========================================================================

    def get_cached_research(self, cache_key: str) -> Optional[str]:
        """Gibt gecachte Recherche zurueck (als JSON-String) oder None."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT result_json FROM research_cache
                WHERE cache_key = ?
                  AND expires_at > datetime('now')
            """, (cache_key,)).fetchone()
            return row[0] if row else None

    def save_cached_research(
        self,
        cache_key: str,
        market_id: str,
        result_json: str,
        ttl_minutes: int,
        is_past_event: bool = False,
    ):
        """Speichert Recherche-Ergebnis in Cache."""
        from datetime import timedelta
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        ).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO research_cache
                    (cache_key, market_id, cached_at, expires_at, is_past_event, result_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    cached_at = excluded.cached_at,
                    expires_at = excluded.expires_at,
                    result_json = excluded.result_json
            """, (
                cache_key,
                market_id,
                datetime.now(timezone.utc).isoformat(),
                expires_at,
                1 if is_past_event else 0,
                result_json,
            ))

    def clean_expired_cache(self):
        """Loescht abgelaufene Cache-Eintraege."""
        with sqlite3.connect(self.db_path) as conn:
            deleted = conn.execute("""
                DELETE FROM research_cache WHERE expires_at <= datetime('now')
            """).rowcount
            if deleted > 0:
                logger.debug(f"Cache bereinigt: {deleted} abgelaufene Eintraege geloescht")

    # =========================================================================
    # Odds Movements
    # =========================================================================

    def log_odds_movement(
        self,
        market_condition_id: str,
        market_question: str,
        old_price: float,
        new_price: float,
        consecutive_count: int = 1,
        triggered_analysis: bool = False,
    ):
        """Loggt eine erkannte Kursbewegung."""
        movement_pct = new_price - old_price  # positiv = up, negativ = down
        direction = "up" if movement_pct > 0 else "down"

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO odds_movements
                    (timestamp, market_condition_id, market_question,
                     old_yes_price, new_yes_price, movement_pct, direction,
                     consecutive_same_direction, triggered_analysis)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                market_condition_id,
                market_question[:300],
                old_price,
                new_price,
                abs(movement_pct),
                direction,
                consecutive_count,
                1 if triggered_analysis else 0,
            ))

    def get_recent_movements(self, market_condition_id: str, hours: int = 2) -> list[dict]:
        """Gibt letzte Kursbewegungen fuer einen Markt zurueck."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM odds_movements
                WHERE market_condition_id = ?
                  AND timestamp >= datetime('now', ?)
                ORDER BY timestamp DESC
            """, (market_condition_id, f'-{hours} hours')).fetchall()
            return [dict(r) for r in rows]

    def get_last_known_price(self, market_condition_id: str) -> Optional[float]:
        """Gibt den zuletzt gesehenen YES-Preis fuer einen Markt zurueck."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT new_yes_price FROM odds_movements
                WHERE market_condition_id = ?
                ORDER BY timestamp DESC LIMIT 1
            """, (market_condition_id,)).fetchone()
            return row[0] if row else None

    # =========================================================================
    # Portfolio Peaks
    # =========================================================================

    def update_portfolio_peak(self, current_usdc: float) -> dict:
        """
        Aktualisiert den Portfolio-Hochpunkt.
        Gibt {current, peak, drawdown_pct} zurueck.
        """
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT peak_portfolio_usdc FROM portfolio_peaks
                ORDER BY timestamp DESC LIMIT 1
            """).fetchone()

            peak = row[0] if row else current_usdc
            new_peak = max(peak, current_usdc)
            drawdown_pct = max(0, (new_peak - current_usdc) / new_peak) if new_peak > 0 else 0

            conn.execute("""
                INSERT INTO portfolio_peaks (timestamp, current_portfolio_usdc, peak_portfolio_usdc, drawdown_pct)
                VALUES (?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                current_usdc,
                new_peak,
                drawdown_pct,
            ))

            return {
                "current": current_usdc,
                "peak": new_peak,
                "drawdown_pct": drawdown_pct,
            }

    def get_portfolio_peak(self) -> dict:
        """Gibt den aktuellen Portfolio-Stand und Peak zurueck."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT current_portfolio_usdc, peak_portfolio_usdc, drawdown_pct
                FROM portfolio_peaks
                ORDER BY timestamp DESC LIMIT 1
            """).fetchone()
            if row:
                return {"current": row[0], "peak": row[1], "drawdown_pct": row[2]}
            return {"current": config.PORTFOLIO_USDC, "peak": config.PORTFOLIO_USDC, "drawdown_pct": 0.0}

    # =========================================================================
    # Performance-Analyse
    # =========================================================================

    def get_performance_summary(self, days: int = 30) -> dict:
        """Gibt eine Performance-Zusammenfassung der letzten N Tage zurueck."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            totals = conn.execute("""
                SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN won = 1 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN won = 0 AND resolved = 1 THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN resolved = 0 THEN 1 ELSE 0 END) as open,
                    SUM(bet_usdc) as total_invested,
                    SUM(COALESCE(pnl_usdc, 0)) as total_pnl,
                    AVG(CASE WHEN resolved = 1 THEN COALESCE(pnl_usdc, 0) END) as avg_pnl
                FROM trades
                WHERE timestamp >= datetime('now', ?)
            """, (f'-{days} days',)).fetchone()

            decision_stats = conn.execute("""
                SELECT
                    COUNT(*) as total_analyzed,
                    SUM(CASE WHEN action = 'SKIP' THEN 1 ELSE 0 END) as skipped,
                    SUM(CASE WHEN action != 'SKIP' THEN 1 ELSE 0 END) as acted,
                    AVG(confidence) as avg_confidence,
                    AVG(ABS(edge)) as avg_edge
                FROM decisions
                WHERE timestamp >= datetime('now', ?)
            """, (f'-{days} days',)).fetchone()

            if not totals:
                return {"error": "Keine Daten verfuegbar"}

            total_invested = totals["total_invested"] or 0
            total_pnl = totals["total_pnl"] or 0
            wins = totals["wins"] or 0
            losses = totals["losses"] or 0

            return {
                "period_days": days,
                "trades": {
                    "total": totals["total_trades"],
                    "open": totals["open"],
                    "resolved": wins + losses,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": f"{wins / max(wins + losses, 1):.1%}",
                },
                "financials": {
                    "total_invested_usdc": round(total_invested, 2),
                    "total_pnl_usdc": round(total_pnl, 2),
                    "roi_pct": f"{(total_pnl / max(total_invested, 0.01)) * 100:.1f}%",
                    "avg_pnl_per_trade": round((totals["avg_pnl"] or 0), 2),
                },
                "decisions": {
                    "total_analyzed": decision_stats["total_analyzed"],
                    "skipped": decision_stats["skipped"],
                    "acted": decision_stats["acted"],
                    "skip_rate": f"{(decision_stats['skipped'] or 0) / max(decision_stats['total_analyzed'] or 1, 1):.1%}",
                    "avg_confidence": f"{(decision_stats['avg_confidence'] or 0):.1%}",
                    "avg_edge": f"{(decision_stats['avg_edge'] or 0):.1%}",
                },
            }

    def get_performance_by_category(self, days: int = 30) -> list[dict]:
        """Performance aufgeschluesselt nach Kategorie."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT
                    market_category,
                    COUNT(*) as total_bets,
                    SUM(CASE WHEN won = 1 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN won = 0 AND resolved = 1 THEN 1 ELSE 0 END) as losses,
                    AVG(CASE WHEN resolved = 1 THEN COALESCE(pnl_usdc, 0) END) as avg_pnl,
                    SUM(COALESCE(pnl_usdc, 0)) as total_pnl,
                    AVG(confidence_at_bet) as avg_confidence,
                    AVG(edge_at_bet) as avg_edge
                FROM trades
                WHERE timestamp >= datetime('now', ?)
                  AND resolved = 1
                GROUP BY market_category
                ORDER BY total_bets DESC
            """, (f'-{days} days',)).fetchall()
            return [dict(r) for r in rows]

    def get_performance_by_confidence_bucket(self, days: int = 30) -> list[dict]:
        """Performance aufgeschluesselt nach Konfidenz-Bucket."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT
                    CASE
                        WHEN confidence_at_bet >= 0.95 THEN '0.95+'
                        WHEN confidence_at_bet >= 0.90 THEN '0.90-0.95'
                        WHEN confidence_at_bet >= 0.85 THEN '0.85-0.90'
                        ELSE '0.80-0.85'
                    END as confidence_bucket,
                    COUNT(*) as total_bets,
                    SUM(CASE WHEN won = 1 THEN 1 ELSE 0 END) as wins,
                    ROUND(AVG(CASE WHEN resolved = 1 THEN COALESCE(pnl_usdc, 0) END), 3) as avg_pnl
                FROM trades
                WHERE timestamp >= datetime('now', ?)
                  AND resolved = 1
                GROUP BY confidence_bucket
                ORDER BY confidence_bucket DESC
            """, (f'-{days} days',)).fetchall()
            return [dict(r) for r in rows]

    def get_performance_by_edge_bucket(self, days: int = 30) -> list[dict]:
        """Performance aufgeschluesselt nach Edge-Groesse."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT
                    CASE
                        WHEN ABS(edge_at_bet) >= 0.30 THEN '30%+'
                        WHEN ABS(edge_at_bet) >= 0.20 THEN '20-30%'
                        WHEN ABS(edge_at_bet) >= 0.15 THEN '15-20%'
                        ELSE '12-15%'
                    END as edge_bucket,
                    COUNT(*) as total_bets,
                    SUM(CASE WHEN won = 1 THEN 1 ELSE 0 END) as wins,
                    ROUND(AVG(CASE WHEN resolved = 1 THEN COALESCE(pnl_usdc, 0) END), 3) as avg_pnl
                FROM trades
                WHERE timestamp >= datetime('now', ?)
                  AND resolved = 1
                GROUP BY edge_bucket
                ORDER BY edge_bucket DESC
            """, (f'-{days} days',)).fetchall()
            return [dict(r) for r in rows]

    def get_resolved_trades_for_analysis(self, days: int = 7) -> list[dict]:
        """Gibt aufgeloeste Trades fuer die wochentliche Analyse zurueck."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT
                    t.*,
                    d.research_sentiment,
                    d.research_confidence,
                    d.weighted_score,
                    d.source_quality_score
                FROM trades t
                LEFT JOIN decisions d ON t.decision_id = d.id
                WHERE t.timestamp >= datetime('now', ?)
                  AND t.resolved = 1
                ORDER BY t.timestamp DESC
            """, (f'-{days} days',)).fetchall()
            return [dict(r) for r in rows]

    def get_recent_trades(self, limit: int = 10) -> list[dict]:
        """Gibt die letzten N Trades zurueck."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(row) for row in rows]

    def get_open_trades(self) -> list[dict]:
        """Gibt alle offenen (unaufgeloesten) Trades zurueck."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM trades WHERE resolved = 0 ORDER BY timestamp DESC
            """).fetchall()
            return [dict(row) for row in rows]

    def get_cumulative_pnl(self) -> float:
        """Gibt den kumulierten P&L aller Zeit zurueck."""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            row = conn.execute("""
                SELECT COALESCE(SUM(pnl_usdc), 0) FROM trades WHERE resolved = 1
            """).fetchone()
            return row[0] if row else 0.0

    # =========================================================================
    # Phase 3: Learning Data
    # =========================================================================

    def save_learning_datapoint(
        self,
        trade_id: Optional[int],
        market_condition_id: str,
        sport_category: str,
        confidence_at_bet: float,
        edge_at_bet: float,
        weighted_score_at_bet: float,
        source_quality_score: float,
        tier1_2_count: int,
        days_to_resolution_at_bet: int,
        market_volume_usd: float,
        time_of_day_utc: int,
        outcome: str,
        pnl_usdc: float,
        model_predicted_prob: float = None,
    ):
        """Speichert einen Learning-Datenpunkt nach Trade-Aufloesung."""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            # Nur speichern wenn noch nicht vorhanden
            if trade_id:
                existing = conn.execute(
                    "SELECT id FROM learning_data WHERE trade_id = ?", (trade_id,)
                ).fetchone()
                if existing:
                    return
            conn.execute("""
                INSERT INTO learning_data (
                    trade_id, market_condition_id, sport_category,
                    confidence_at_bet, edge_at_bet, weighted_score_at_bet,
                    source_quality_score, tier1_2_count, days_to_resolution_at_bet,
                    market_volume_usd, time_of_day_utc, outcome, pnl_usdc,
                    model_predicted_prob, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id, market_condition_id, sport_category,
                confidence_at_bet, edge_at_bet, weighted_score_at_bet,
                source_quality_score, tier1_2_count, days_to_resolution_at_bet,
                market_volume_usd, time_of_day_utc, outcome, pnl_usdc,
                model_predicted_prob, datetime.now(timezone.utc).isoformat(),
            ))

    def get_unprocessed_resolved_trades(self) -> list[dict]:
        """Gibt aufgeloeste Trades zurueck, die noch nicht in learning_data sind."""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT t.* FROM trades t
                LEFT JOIN learning_data ld ON t.id = ld.trade_id
                WHERE t.resolved = 1
                  AND t.won IS NOT NULL
                  AND ld.id IS NULL
                ORDER BY t.timestamp DESC
                LIMIT 100
            """).fetchall()
            return [dict(r) for r in rows]

    def get_learning_data(self, min_count: int = 0) -> list[dict]:
        """Gibt alle Learning-Datenpunkte zurueck."""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM learning_data
                WHERE outcome IN ('WIN', 'LOSS')
                ORDER BY recorded_at DESC
            """).fetchall()
            data = [dict(r) for r in rows]
            return data if len(data) >= min_count else []

    def get_learning_data_count(self) -> int:
        """Gibt Anzahl der Learning-Datenpunkte zurueck."""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM learning_data WHERE outcome IN ('WIN', 'LOSS')"
            ).fetchone()
            return row[0] if row else 0

    # =========================================================================
    # Phase 3: Decision Audit
    # =========================================================================

    def save_decision_audit(
        self,
        decision_id: int,
        criterion_scores: dict,
        learning_adjustments: dict,
        cross_platform_data: dict,
        odds_movement_data: dict,
        final_reasoning: str,
        override_reason: str = "",
    ):
        """Speichert vollstaendigen Audit-Trail fuer eine Entscheidung."""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.execute("""
                INSERT INTO decision_audit (
                    decision_id, criterion_scores_json, learning_adjustments_json,
                    cross_platform_data_json, odds_movement_data_json,
                    final_reasoning, override_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                decision_id,
                json.dumps(criterion_scores or {}),
                json.dumps(learning_adjustments or {}),
                json.dumps(cross_platform_data or {}),
                json.dumps(odds_movement_data or {}),
                (final_reasoning or "")[:2000],
                override_reason[:500],
                datetime.now(timezone.utc).isoformat(),
            ))

    # =========================================================================
    # Phase 3: Adaptive Config
    # =========================================================================

    def get_adaptive_config(self, key: str) -> Optional[float]:
        """Gibt einen dynamisch gelernten Konfigurationswert zurueck (oder None)."""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            row = conn.execute(
                "SELECT value FROM adaptive_config WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else None

    def set_adaptive_config(self, key: str, value: float, reason: str = "", base_value: float = None):
        """Setzt einen dynamischen Konfigurationswert (UPSERT)."""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            existing = conn.execute(
                "SELECT update_count FROM adaptive_config WHERE key = ?", (key,)
            ).fetchone()
            count = (existing[0] + 1) if existing else 1
            conn.execute("""
                INSERT INTO adaptive_config
                    (key, value, base_value, last_updated, update_reason, update_count)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    last_updated = excluded.last_updated,
                    update_reason = excluded.update_reason,
                    update_count = excluded.update_count
            """, (
                key, value, base_value,
                datetime.now(timezone.utc).isoformat(),
                reason[:500], count,
            ))

    def get_all_adaptive_configs(self) -> list[dict]:
        """Gibt alle adaptiven Konfigurationswerte zurueck."""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM adaptive_config ORDER BY key"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_last_successful_run(self) -> Optional[str]:
        """Gibt den Timestamp des letzten erfolgreichen Durchlaufs zurueck."""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            row = conn.execute(
                "SELECT value FROM adaptive_config WHERE key = 'last_successful_run_ts'"
            ).fetchone()
            # value is stored as float (unix ts), return as iso string from errors table fallback
            row2 = conn.execute("""
                SELECT timestamp FROM decisions ORDER BY timestamp DESC LIMIT 1
            """).fetchone()
            return row2[0] if row2 else None
