#!/usr/bin/env python3
"""Agent 1 — Read-Only REST API. Port: 8767"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

DB_PATH = "/root/polymarket-agent/trading_log.db"
STARTING_CAPITAL = 50.0
PORT = 8767

app = FastAPI(title="Agent 1 Read-Only API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}

@app.get("/api/summary")
def api_summary() -> JSONResponse:
    conn = get_conn()
    try:
        c = conn.cursor()

        # Offene Positionen (unresolved trades)
        c.execute("""
            SELECT id, market_condition_id, market_question, action,
                   bet_usdc, entry_price, timestamp, market_category,
                   confidence_at_bet, days_to_resolution_at_bet
            FROM trades
            WHERE resolved = 0
            ORDER BY timestamp DESC
            LIMIT 50
        """)
        open_trades = [dict(r) for r in c.fetchall()]

        # Gesamtstatistik
        c.execute("""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN won = 1 THEN 1 ELSE 0 END), 0) AS wins,
                COALESCE(SUM(CASE WHEN pnl_usdc IS NOT NULL THEN pnl_usdc ELSE 0 END), 0) AS total_pnl,
                COALESCE(SUM(CASE WHEN resolved = 1 AND won IS NOT NULL THEN 1 ELSE 0 END), 0) AS resolved_count
            FROM trades
            WHERE dry_run = 1
        """)
        stats = dict(c.fetchone())

        # Tages-PnL
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        c.execute("""
            SELECT COALESCE(SUM(pnl_usdc), 0)
            FROM trades
            WHERE timestamp LIKE ? AND resolved = 1 AND dry_run = 1
        """, (f"{today}%",))
        daily_pnl = float(c.fetchone()[0] or 0.0)

        # Recent events
        c.execute("""
            SELECT id, timestamp, agent_source, event_type, market_id, market_question
            FROM agent_events
            ORDER BY timestamp DESC
            LIMIT 25
        """)
        raw_events = [dict(r) for r in c.fetchall()]

        # Top Wallet Signals (als tracked_wallets)
        c.execute("""
            SELECT DISTINCT wallet_address, wallet_rank
            FROM top_wallet_signals
            ORDER BY wallet_rank ASC
            LIMIT 10
        """)
        wallet_rows = [dict(r) for r in c.fetchall()]

    finally:
        conn.close()

    deployed = sum(float(t.get("bet_usdc") or 0) for t in open_trades)
    free_cash = max(STARTING_CAPITAL - deployed, 0.0)
    total_pnl = float(stats.get("total_pnl") or 0.0)
    nav = STARTING_CAPITAL + total_pnl
    wins = int(stats.get("wins") or 0)
    resolved = int(stats.get("resolved_count") or 0)
    win_rate = wins / resolved if resolved > 0 else 0.0

    positions: list[dict[str, Any]] = []
    for t in open_trades:
        positions.append({
            "id": t["id"],
            "market_id": t["market_condition_id"] or "",
            "market_question": t["market_question"] or "",
            "sector": (t.get("market_category") or "other").lower(),
            "side": "YES" if t.get("action") == "BET_YES" else "NO",
            "entry_price": float(t.get("entry_price") or 0),
            "stake_usdc": float(t.get("bet_usdc") or 0),
            "opened_at": t.get("timestamp") or "",
            "status": "OPEN",
            "current_price": float(t.get("entry_price") or 0),
            "pnl": 0.0,
        })

    recent_events: list[dict[str, Any]] = []
    for e in raw_events:
        recent_events.append({
            "timestamp": e.get("timestamp") or "",
            "event_type": e.get("event_type") or "",
            "agent_source": e.get("agent_source") or "agent1",
            "market_id": e.get("market_id") or "",
            "market_question": e.get("market_question") or "",
        })

    tracked_wallets: list[dict[str, Any]] = []
    for w in wallet_rows:
        tracked_wallets.append({
            "address": w.get("wallet_address") or "",
            "user_name": w.get("wallet_address", "")[:12] or "",
            "pnl": 0.0,
            "vol": 0.0,
            "rank": w.get("wallet_rank"),
        })

    return JSONResponse({
        "starting_capital_usdc": STARTING_CAPITAL,
        "free_cash_usdc": free_cash,
        "deployed_usdc": deployed,
        "nav_usdc": nav,
        "open_positions": len(open_trades),
        "daily_realized_pnl_usdc": daily_pnl,
        "total_realized_pnl_usdc": total_pnl,
        "total_trades": int(stats.get("total") or 0),
        "win_rate": win_rate,
        "wins": wins,
        "resolved_trades": resolved,
        "halt_until": "",
        "dry_run": True,
        "execution_mode": "paper",
        "positions": positions,
        "recent_events": recent_events,
        "tracked_wallets": tracked_wallets,
        "leaderboard": {
            "time_period": "MONTH",
            "category": "OVERALL",
            "order_by": "PNL",
        },
    })

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
