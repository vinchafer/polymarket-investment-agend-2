"""
agent_scout.py — Scout Agent (Agent 1)

Monitors top 8 active wallets every 3600s (dynamically selected from 1M leaderboard top 20).
Detects NEW positions (opened in last 24 hours).
Calls Agent 0 (efficiency check) for each position.
Writes NEW_POSITION events to agent_events table.

Usage:
    python agent_scout.py --test
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
from agent_efficiency import EfficiencyChecker

logger = logging.getLogger(__name__)

SCOUT_INTERVAL = getattr(config, "SCOUT_INTERVAL_SECONDS", 3600)
NEW_POSITION_WINDOW_HOURS = getattr(config, "NEW_POSITION_WINDOW_HOURS", 24)
# Markt-Horizont-Guardrail: nur Märkte handeln, die innerhalb dieses Fensters
# auflösen (struktureller Deadlock-Fix). Greift VOR Analyst/Devil (LLM/Copy).
MAX_MARKET_HORIZON_DAYS = getattr(config, "MAX_MARKET_HORIZON_DAYS", 14)
TOP_WALLETS = 8
WALLET_FETCH_POOL = 20       # fetch more, filter down to TOP_WALLETS active ones
MIN_ACTIVE_POSITIONS = getattr(config, "MIN_POSITIONS_FOR_SIGNAL", 3)
DATA_API_BASE = "https://data-api.polymarket.com"


def _ensure_agent_events_table(db_path: str):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                agent_source TEXT,
                event_type TEXT,
                market_id TEXT,
                market_question TEXT,
                payload TEXT,
                processed INTEGER DEFAULT 0
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _write_event(db_path: str, event_type: str, market_id: str,
                 market_question: str, payload: dict):
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            INSERT INTO agent_events
                (timestamp, agent_source, event_type, market_id, market_question, payload, processed)
            VALUES (?, 'scout', ?, ?, ?, ?, 0)
        """, (now, event_type, market_id, market_question, json.dumps(payload)))
        conn.commit()
    finally:
        conn.close()


def _fetch_start_dates(wallet_address: str) -> dict:
    """
    Fetch startDate for each position from Polymarket Data API.
    Returns: {condition_id: start_date_iso}
    """
    try:
        resp = requests.get(
            f"{DATA_API_BASE}/positions",
            params={"user": wallet_address, "limit": 50},
            timeout=10,
        )
        if resp.status_code != 200:
            return {}
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("data", [])
        result = {}
        for pos in data:
            cid = pos.get("conditionId") or pos.get("condition_id") or ""
            start = pos.get("startDate") or pos.get("start_date") or ""
            if cid and start:
                result[cid] = start
        return result
    except Exception as e:
        logger.debug(f"Scout: start-date fetch failed for {wallet_address[:12]}: {e}")
        return {}


def _is_new_position(start_date_iso: str) -> tuple:
    """Returns (is_new: bool, hours_ago: float)."""
    if not start_date_iso:
        return False, 999.0
    try:
        ts = datetime.fromisoformat(start_date_iso.replace("Z", "+00:00"))
        hours_ago = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
        return hours_ago <= NEW_POSITION_WINDOW_HOURS, round(hours_ago, 2)
    except Exception:
        return False, 999.0


def _horizon_days(end_date_iso: str):
    """Days from now until market end_date. Returns float (may be negative if the
    end_date is already past) or None if end_date is missing/unparseable."""
    if not end_date_iso:
        return None
    try:
        ts = datetime.fromisoformat(str(end_date_iso).replace("Z", "+00:00"))
        return (ts - datetime.now(timezone.utc)).total_seconds() / 86400.0
    except Exception:
        return None


class ScoutAgent:

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.efficiency = EfficiencyChecker()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._seen: set = set()  # (wallet_address, condition_id) already emitted
        _ensure_agent_events_table(db_path)

    # -------------------------------------------------------------------------
    # Polymarket API
    # -------------------------------------------------------------------------

    def _fetch_top_wallets(self) -> list:
        """Fetch pool of recent top traders, filter to active ones, return best TOP_WALLETS."""
        try:
            from polymarket_apis.clients.data_client import PolymarketDataClient
            client = PolymarketDataClient()
            # Use 30d window: active profitable traders this month, not all-time legends
            entries = client.get_leaderboard_top_users(
                metric="profit", window="30d", limit=WALLET_FETCH_POOL + 5
            )
            candidates = [
                {
                    "rank": i,
                    "address": e.proxy_wallet,
                    "name": e.name or e.proxy_wallet[:10],
                    "profit": float(e.amount),
                }
                for i, e in enumerate(entries[:WALLET_FETCH_POOL], 1)
                if e.proxy_wallet
            ]
        except Exception as e:
            logger.error(f"Scout: leaderboard fetch failed: {e}")
            return []

        # Filter: keep only wallets with active positions (= currently trading)
        active = []
        for w in candidates:
            positions = self._fetch_positions(w["address"])
            if len(positions) >= MIN_ACTIVE_POSITIONS:
                w["_positions_cached"] = positions
                active.append(w)
            if len(active) >= TOP_WALLETS:
                break

        logger.info(
            f"Scout: wallet pool={len(candidates)}, active={len(active)} "
            f"[window=1M, min_positions={MIN_ACTIVE_POSITIONS}]"
        )
        # Re-rank by monthly profit among active set
        for i, w in enumerate(active, 1):
            w["rank"] = i
        return active

    def _fetch_positions(self, address: str) -> list:
        try:
            from polymarket_apis.clients.data_client import PolymarketDataClient
            client = PolymarketDataClient()
            positions = client.get_positions(
                user=address, size_threshold=1.0, limit=50,
                sort_by="CURRENT", sort_direction="DESC",
            )
            result = []
            for p in positions:
                if not p.condition_id:
                    continue
                # Canonical outcome index (0 = primary/YES side, 1 = secondary/NO)
                # and the exact token_id the wallet holds — the reliable side
                # identity. Never derive the side from a "YES" substring match on
                # the outcome label (labels are "France"/"Over 3.5"/" Yes", so that
                # mislabels every non-binary market). (2026-07-13)
                oidx = getattr(p, "outcome_index", None)
                try:
                    oidx = int(oidx) if oidx is not None else None
                except Exception:
                    oidx = None
                result.append({
                    "condition_id": p.condition_id,
                    "title": p.title or "",
                    "outcome": (p.outcome or "YES").upper(),
                    "outcome_label": p.outcome or "",
                    "outcome_index": oidx,
                    "token_id": str(getattr(p, "token_id", "") or ""),
                    "avgPrice": float(p.avg_price or 0.5),
                    "currentPrice": float(p.current_price or p.avg_price or 0.5),
                    "currentValue": float(p.current_value or 0),
                    "size": float(p.size or 0),
                })
            return result
        except Exception as e:
            logger.warning(f"Scout: positions fetch failed for {address[:12]}: {e}")
            return []

    def _fetch_market_meta(self, condition_id: str) -> dict:
        """Volume (Gamma) + resolution end_date (CLOB, authoritative).
        Returns {"volume": float, "end_date": str}. Both empty/0.0 on failure.

        end_date is taken from CLOB /markets/{cid}.end_date_iso, which is present
        for active AND settled markets and never silently drops a market — Gamma
        ?condition_ids= returns [] for some markets, which would otherwise cause a
        false 'no_end_date' horizon reject. Gamma is kept only for 24h volume
        (CLOB does not expose it); its endDate is a fallback."""
        meta = {"volume": 0.0, "end_date": ""}
        # --- Volume + fallback end_date from Gamma ---
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"condition_ids": condition_id},
                timeout=6,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    m = data[0]
                    for k in ("volume24hr", "volumeNum", "volume"):
                        v = m.get(k)
                        if v:
                            try:
                                meta["volume"] = float(v)
                                break
                            except Exception:
                                pass
                    meta["end_date"] = m.get("endDateIso") or m.get("endDate") or ""
        except Exception as e:
            logger.debug(f"Scout: gamma volume fetch failed for {condition_id[:10]}: {e}")
        # --- Authoritative end_date from CLOB (overrides Gamma when available) ---
        try:
            resp = requests.get(
                f"https://clob.polymarket.com/markets/{condition_id}", timeout=6,
            )
            if resp.status_code == 200:
                ed = resp.json().get("end_date_iso") or ""
                if ed:
                    meta["end_date"] = ed
        except Exception as e:
            logger.debug(f"Scout: clob end_date fetch failed for {condition_id[:10]}: {e}")
        return meta

    def _fetch_market_volume(self, condition_id: str) -> float:
        """Backward-compat shim: volume only (see _fetch_market_meta)."""
        return self._fetch_market_meta(condition_id).get("volume", 0.0)

    # -------------------------------------------------------------------------
    # Duplicate guard
    # -------------------------------------------------------------------------

    def _has_open_position(self, condition_id: str, question: str) -> bool:
        """Returns True if an open (unresolved) trade exists for this market."""
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                exact = conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE market_condition_id=? AND resolved=0",
                    (condition_id,),
                ).fetchone()[0]
                if exact > 0:
                    return True
                prefix = question[:35].replace("%", "%%") + "%"
                fuzzy = conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE market_question LIKE ? AND resolved=0",
                    (prefix,),
                ).fetchone()[0]
                return fuzzy > 0
            finally:
                conn.close()
        except Exception:
            # trades table may not exist yet — safe to proceed
            return False

    # -------------------------------------------------------------------------
    # Core scan
    # -------------------------------------------------------------------------

    def scan_once(self) -> int:
        """Full scan: fetch wallets → check timestamps → write events. Returns count."""
        logger.info("Scout: starting scan...")
        wallets = self._fetch_top_wallets()
        if not wallets:
            logger.warning("Scout: no wallets returned")
            return 0

        events_written = 0

        for wallet in wallets:
            address = wallet["address"]
            name = wallet["name"]
            profit = wallet["profit"]
            rank = wallet["rank"]

            # Reuse positions already fetched during activity filter (avoids double API call)
            positions = wallet.pop("_positions_cached", None) or self._fetch_positions(address)
            if not positions:
                continue

            logger.info(f"  Rank {rank}: {name} — {len(positions)} positions | profit={profit:.0f} USDC")

            for pos in positions:
                cid = pos["condition_id"]
                key = (address, cid)
                if key in self._seen:
                    continue

                self._seen.add(key)

                # --- Duplicate guard: skip if open position already exists ---
                if self._has_open_position(cid, pos["title"]):
                    logger.info(f"    SKIP: open position exists for {pos['title'][:45]}")
                    continue

                # Side = the wallet's real held outcome, keyed off the canonical
                # outcome_index (0 = primary/YES, 1 = secondary/NO), NOT a "YES"
                # substring on the label. token_id is the exact side identity that
                # resolution scores against. Legacy fallback: string heuristic only
                # when outcome_index is unavailable.
                oidx = pos.get("outcome_index")
                if oidx is None:
                    direction = "YES" if "YES" in pos["outcome"] else "NO"
                else:
                    direction = "YES" if oidx == 0 else "NO"
                outcome_label = pos.get("outcome_label", "")
                token_id = pos.get("token_id", "")
                entry_price = pos["avgPrice"]
                current_price = pos["currentPrice"]
                size_usd = pos["currentValue"] or pos["size"]
                question = pos["title"]
                hours_ago = 0.0
                start_date = ""

                # --- Market meta (volume + resolution horizon) in one Gamma call ---
                meta = self._fetch_market_meta(cid)
                market_volume = meta["volume"]
                end_date = meta["end_date"]
                horizon = _horizon_days(end_date)

                # --- MARKT-HORIZONT-GUARDRAIL (vor Analyst/Devil) ---
                # Lehnt Langfrist-Märkte und Märkte ohne verlässliche end_date ab.
                # Reject-Grund wird als eigenes Event geloggt (auswertbar), es wird
                # KEIN NEW_POSITION erzeugt → die Fill erreicht LLM/Copy nie.
                horizon_reject = None
                if horizon is None:
                    horizon_reject = "no_end_date"
                elif horizon < 0:
                    horizon_reject = "market_ended"
                elif horizon > MAX_MARKET_HORIZON_DAYS:
                    horizon_reject = "horizon_exceeded"

                if horizon_reject:
                    days_str = "n/a" if horizon is None else f"{horizon:.1f}d"
                    logger.info(
                        f"    HORIZON-REJECT ({horizon_reject}, {days_str}, "
                        f"max={MAX_MARKET_HORIZON_DAYS}d): {question[:45]}"
                    )
                    _write_event(
                        self.db_path,
                        "HORIZON_REJECTED",
                        cid,
                        question,
                        {
                            "wallet_address": address,
                            "wallet_name": name,
                            "wallet_rank": rank,
                            "direction": direction,
                            "size_usd": round(size_usd, 2),
                            "entry_price": round(entry_price, 4),
                            "current_price": round(current_price, 4),
                            "reason": horizon_reject,
                            "end_date": end_date,
                            "days_to_resolution": None if horizon is None else round(horizon, 2),
                            "max_horizon_days": MAX_MARKET_HORIZON_DAYS,
                            "market_volume": round(market_volume, 2),
                        },
                    )
                    continue

                eff = self.efficiency.check(question, current_price)

                logger.info(
                    f"    NEW: {question[:50]} | {direction}({outcome_label}) @ {entry_price:.0%} "
                    f"| ~{horizon:.1f}d to resolve | {eff['recommendation']}"
                )

                _write_event(
                    self.db_path,
                    "NEW_POSITION",
                    cid,
                    question,
                    {
                        "wallet_address": address,
                        "wallet_name": name,
                        "wallet_profit": profit,
                        "wallet_rank": rank,
                        "direction": direction,
                        "outcome_label": outcome_label,
                        "outcome_index": oidx,
                        "token_id": token_id,
                        "size_usd": round(size_usd, 2),
                        "entry_price": round(entry_price, 4),
                        "current_price": round(current_price, 4),
                        "start_date": start_date,
                        "hours_ago": hours_ago,
                        "end_date": end_date,
                        "days_to_resolution": round(horizon, 2),
                        "efficiency": eff["recommendation"],
                        "efficiency_details": eff,
                        "market_volume": round(market_volume, 2),
                    },
                )
                events_written += 1

        logger.info(f"Scout: scan complete — {events_written} new position events written")
        return events_written

    # -------------------------------------------------------------------------
    # Threading
    # -------------------------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="ScoutAgent", daemon=True)
        self._thread.start()
        logger.info("Scout: background thread started")

    def stop(self):
        self._running = False

    def _run_loop(self):
        try:
            self.scan_once()
        except Exception as e:
            logger.error(f"Scout: initial scan failed: {e}", exc_info=True)
        while self._running:
            for _ in range(SCOUT_INTERVAL):
                if not self._running:
                    return
                time.sleep(1)
            try:
                self.scan_once()
            except Exception as e:
                logger.error(f"Scout: scan error: {e}", exc_info=True)


# =============================================================================
# Standalone test: python agent_scout.py --test
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if "--test" not in sys.argv:
        print("Usage: python agent_scout.py --test")
        sys.exit(0)

    import tempfile, os
    tmp_db = tempfile.mktemp(suffix=".db")

    class _StubConfig:
        DB_PATH = tmp_db

    print("=== ScoutAgent standalone test ===\n")
    scout = ScoutAgent(tmp_db)

    print("--- Fetching top wallets ---")
    wallets = scout._fetch_top_wallets()
    if not wallets:
        print("ERROR: no wallets returned")
        sys.exit(1)

    for w in wallets[:5]:
        print(f"  Rank {w['rank']}: {w['name']} profit=${w['profit']:,.0f}")

    print(f"\n--- Full scan (NEW positions only = opened in last {NEW_POSITION_WINDOW_HOURS}h) ---")
    n = scout.scan_once()
    print(f"\nNew position events written: {n}")

    conn = sqlite3.connect(tmp_db)
    rows = conn.execute("SELECT * FROM agent_events").fetchall()
    conn.close()
    for r in rows[:5]:
        p = json.loads(r[6])
        print(f"  [{r[3]}] {r[5][:50]} | eff={p.get('efficiency')}")

    try:
        os.unlink(tmp_db)
    except Exception:
        pass
    print("\nTest passed" if n >= 0 else "Test FAILED")
