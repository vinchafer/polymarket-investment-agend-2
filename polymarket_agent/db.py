from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from .models import EventEnvelope


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AgentEvent:
    id: int
    timestamp: str
    agent_source: str
    event_type: str
    market_id: str
    market_question: str
    payload: dict[str, Any]
    processed: int


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    agent_source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    market_question TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_agent_events_unprocessed
                  ON agent_events(processed, event_type, timestamp);

                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT NOT NULL,
                    market_question TEXT NOT NULL,
                    sector TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stake_usdc REAL NOT NULL,
                    opened_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    current_price REAL NOT NULL,
                    pnl REAL NOT NULL DEFAULT 0.0
                );

                CREATE TABLE IF NOT EXISTS executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    stake_usdc REAL NOT NULL,
                    expected_price REAL NOT NULL,
                    simulated_fill_price REAL NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    notes TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    component TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS risk_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tracked_wallets (
                    address TEXT PRIMARY KEY,
                    user_name TEXT NOT NULL DEFAULT '',
                    rank TEXT NOT NULL DEFAULT '',
                    pnl REAL NOT NULL DEFAULT 0,
                    vol REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scout_position_sightings (
                    wallet TEXT NOT NULL,
                    condition_id TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    emitted INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (wallet, condition_id)
                );

                CREATE TABLE IF NOT EXISTS attribution_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_id INTEGER,
                    position_id INTEGER,
                    source_wallet TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    market_question TEXT NOT NULL,
                    entry_signal_price REAL NOT NULL,
                    fill_price REAL NOT NULL,
                    slippage REAL NOT NULL,
                    stake_usdc REAL NOT NULL,
                    opened_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolution_eta_hours REAL,
                    pnl_usdc REAL,
                    status TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '{}'
                );
                """
            )

    def publish_event(
        self,
        agent_source: str,
        event_type: str,
        market_id: str,
        market_question: str,
        payload: dict[str, Any],
    ) -> None:
        EventEnvelope(
            agent_source=agent_source,
            event_type=event_type,
            market_id=market_id,
            market_question=market_question,
            payload=payload,
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_events(timestamp, agent_source, event_type, market_id, market_question, payload, processed)
                VALUES(?, ?, ?, ?, ?, ?, 0)
                """,
                (utc_now(), agent_source, event_type, market_id, market_question, json.dumps(payload)),
            )

    def consume_events(self, event_type: str, limit: int = 50) -> list[AgentEvent]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_events
                WHERE processed = 0 AND event_type = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (event_type, limit),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                conn.executemany("UPDATE agent_events SET processed = 1 WHERE id = ?", [(i,) for i in ids])
        return [
            AgentEvent(
                id=r["id"],
                timestamp=r["timestamp"],
                agent_source=r["agent_source"],
                event_type=r["event_type"],
                market_id=r["market_id"],
                market_question=r["market_question"],
                payload=json.loads(r["payload"]),
                processed=r["processed"],
            )
            for r in rows
        ]

    def open_positions_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM positions WHERE status='OPEN'").fetchone()
            return int(row["c"])

    def open_positions_in_sector(self, sector: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM positions WHERE status='OPEN' AND sector=?",
                (sector,),
            ).fetchone()
            return int(row["c"])

    def capital_at_risk(self) -> float:
        with self.connect() as conn:
            row = conn.execute("SELECT COALESCE(SUM(stake_usdc),0) AS total FROM positions WHERE status='OPEN'").fetchone()
            return float(row["total"])

    def daily_realized_pnl(self, day_prefix: str) -> float:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(pnl),0) AS total
                FROM positions
                WHERE status IN ('WIN','LOSS') AND opened_at LIKE ?
                """,
                (f"{day_prefix}%",),
            ).fetchone()
            return float(row["total"])

    def add_position(self, payload: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO positions(market_id, market_question, sector, side, entry_price, stake_usdc, opened_at, current_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["market_id"],
                    payload["market_question"],
                    payload["sector"],
                    payload["side"],
                    payload["entry_price"],
                    payload["stake_usdc"],
                    utc_now(),
                    payload["entry_price"],
                ),
            )

    def ensure_portfolio_seed(self, starting_cash: float) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_config WHERE key = 'portfolio_cash'").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO app_config(key, value) VALUES ('portfolio_cash', ?)",
                    (str(starting_cash),),
                )
                conn.execute(
                    "INSERT INTO app_config(key, value) VALUES ('starting_capital', ?)",
                    (str(starting_cash),),
                )

    def get_config_value(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_config WHERE key = ?", (key,)).fetchone()
            if row is None:
                return default
            return str(row["value"])

    def get_portfolio_cash(self) -> float:
        raw = self.get_config_value("portfolio_cash", "")
        try:
            return float(raw) if raw else 0.0
        except ValueError:
            return 0.0

    def get_starting_capital(self) -> float:
        raw = self.get_config_value("starting_capital", "")
        try:
            return float(raw) if raw else 0.0
        except ValueError:
            return 0.0

    def open_position_and_debit_cash(self, payload: dict[str, Any], stake: float) -> int | None:
        """Atomically debit paper cash and insert OPEN position. Returns position id or None if insufficient cash."""
        if stake <= 0:
            return None
        opened = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM app_config WHERE key = 'portfolio_cash'").fetchone()
            if row is None:
                conn.rollback()
                return None
            cash = float(row["value"])
            if cash < stake:
                conn.rollback()
                return None
            new_cash = cash - stake
            conn.execute(
                "UPDATE app_config SET value = ? WHERE key = 'portfolio_cash'",
                (str(new_cash),),
            )
            cur = conn.execute(
                """
                INSERT INTO positions(market_id, market_question, sector, side, entry_price, stake_usdc, opened_at, current_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["market_id"],
                    payload["market_question"],
                    payload["sector"],
                    payload["side"],
                    payload["entry_price"],
                    stake,
                    opened,
                    payload["entry_price"],
                ),
            )
            pid = int(cur.lastrowid)
            conn.commit()
            return pid

    def list_open_positions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM positions WHERE status = 'OPEN' ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_recent_events(self, limit: int = 40) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, timestamp, agent_source, event_type, market_id, market_question, payload
                FROM agent_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"])
            except Exception:
                d["payload"] = {}
            out.append(d)
        return out

    def list_recent_executions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM executions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["notes"] = json.loads(d["notes"])
            except Exception:
                d["notes"] = {}
            out.append(d)
        return out

    def list_recent_audit(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["details"] = json.loads(d["details"])
            except Exception:
                d["details"] = {}
            out.append(d)
        return out

    def record_execution(
        self,
        market_id: str,
        side: str,
        stake_usdc: float,
        expected_price: float,
        simulated_fill_price: float,
        mode: str,
        status: str,
        notes: dict[str, Any],
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO executions(timestamp, market_id, side, stake_usdc, expected_price, simulated_fill_price, mode, status, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    market_id,
                    side,
                    stake_usdc,
                    expected_price,
                    simulated_fill_price,
                    mode,
                    status,
                    json.dumps(notes),
                ),
            )
            return int(cur.lastrowid)

    def replace_tracked_wallets(self, rows: list[dict[str, Any]]) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("DELETE FROM tracked_wallets")
            for r in rows:
                conn.execute(
                    """
                    INSERT INTO tracked_wallets(address, user_name, rank, pnl, vol, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r["address"],
                        r.get("user_name", ""),
                        str(r.get("rank", "")),
                        float(r.get("pnl", 0.0)),
                        float(r.get("vol", 0.0)),
                        now,
                    ),
                )

    def list_tracked_wallets(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tracked_wallets ORDER BY pnl DESC, address ASC"
            ).fetchall()
        return [dict(x) for x in rows]

    def claim_scouting_signal(self, wallet: str, condition_id: str) -> str | None:
        """First observation of wallet+market: insert row and return first_seen ISO; duplicates return None."""
        if not wallet.startswith("0x") or not condition_id:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT first_seen FROM scout_position_sightings WHERE wallet = ? AND condition_id = ?",
                (wallet, condition_id),
            ).fetchone()
            if row:
                return None
            fs = utc_now()
            conn.execute(
                """
                INSERT INTO scout_position_sightings(wallet, condition_id, first_seen, emitted)
                VALUES (?, ?, ?, ?)
                """,
                (wallet, condition_id, fs, 0),
            )
            return fs

    def insert_attribution_signal(
        self,
        *,
        execution_id: int | None,
        position_id: int | None,
        source_wallet: str,
        market_id: str,
        market_question: str,
        entry_signal_price: float,
        fill_price: float,
        slippage: float,
        stake_usdc: float,
        opened_at: str,
        resolved_at: str | None,
        resolution_eta_hours: float | None,
        pnl_usdc: float | None,
        status: str,
        notes: dict[str, Any],
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO attribution_signals(
                  execution_id, position_id, source_wallet, market_id, market_question,
                  entry_signal_price, fill_price, slippage, stake_usdc, opened_at,
                  resolved_at, resolution_eta_hours, pnl_usdc, status, notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    execution_id,
                    position_id,
                    source_wallet,
                    market_id,
                    market_question,
                    entry_signal_price,
                    fill_price,
                    slippage,
                    stake_usdc,
                    opened_at,
                    resolved_at,
                    resolution_eta_hours,
                    pnl_usdc,
                    status,
                    json.dumps(notes),
                ),
            )
            return int(cur.lastrowid)

    def list_attribution_signals(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM attribution_signals ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["notes"] = json.loads(d["notes"])
            except Exception:
                d["notes"] = {}
            out.append(d)
        return out

    def write_audit(self, level: str, component: str, action: str, details: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_log(timestamp, level, component, action, details)
                VALUES(?, ?, ?, ?, ?)
                """,
                (utc_now(), level, component, action, json.dumps(details)),
            )

    def set_risk_state(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO risk_state(key, value, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, utc_now()),
            )

    def get_risk_state(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM risk_state WHERE key = ?", (key,)).fetchone()
            if row is None:
                return default
            return str(row["value"])
