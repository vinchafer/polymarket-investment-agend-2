from __future__ import annotations

from typing import Any

import httpx

from .db import Database


def fetch_leaderboard_entries(
    base_url: str,
    *,
    limit: int = 8,
    time_period: str = "MONTH",
    category: str = "OVERALL",
    order_by: str = "PNL",
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """Polymarket Data API: GET /v1/leaderboard."""
    base = base_url.rstrip("/")
    url = f"{base}/v1/leaderboard"
    with httpx.Client(timeout=timeout) as http:
        resp = http.get(
            url,
            params={
                "limit": min(max(limit, 1), 50),
                "timePeriod": time_period,
                "category": category,
                "orderBy": order_by,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, list) else []


def refresh_tracked_wallets(
    db: Database,
    *,
    base_url: str,
    limit: int = 8,
    time_period: str,
    category: str,
    order_by: str,
    timeout: float,
) -> list[str]:
    """Fetch leaderboard and persist. Returns active wallet addresses (0x…)."""
    entries = fetch_leaderboard_entries(
        base_url,
        limit=limit,
        time_period=time_period,
        category=category,
        order_by=order_by,
        timeout=timeout,
    )
    rows: list[dict[str, Any]] = []
    addresses: list[str] = []
    for e in entries[:limit]:
        addr = str(e.get("proxyWallet", "")).strip()
        if not addr.startswith("0x") or len(addr) != 42:
            continue
        rows.append(
            {
                "address": addr,
                "user_name": str(e.get("userName", "")),
                "rank": str(e.get("rank", "")),
                "pnl": float(e.get("pnl", 0.0) or 0.0),
                "vol": float(e.get("vol", 0.0) or 0.0),
            }
        )
        addresses.append(addr)
    db.replace_tracked_wallets(rows)
    db.write_audit(
        "INFO",
        "wallet_curator",
        "roster_updated",
        {"count": len(addresses), "time_period": time_period, "category": category},
    )
    return addresses
