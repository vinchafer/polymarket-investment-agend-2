"""
wallet_monitor.py — Smart Money Tracking for Polymarket

Fetches top 10 wallets by profit, keeps those with >5 active positions.
Computes conviction levels per market:
  HIGH   (3+ wallets same direction)
  MEDIUM (2 wallets same direction)
  LOW    (1 wallet)

Runs as background thread every 30 minutes.
Uses polymarket-apis (PolymarketDataClient) — no auth required.

Usage:
    monitor = WalletMonitor(db)
    monitor.start()
    markets = monitor.get_smart_money_markets()

Standalone test:
    python wallet_monitor.py --test
"""

import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 30 * 60  # 30 minutes
SIZE_THRESHOLD_USD = 1.0            # min position size (USDC) to track
TOP_N_FETCH = 10                    # how many wallets to fetch from leaderboard
MIN_POSITIONS_FOR_SIGNAL = 5        # filter: wallet must have >N active positions
HIGH_CONVICTION_THRESHOLD = 3       # 3+ wallets same direction = HIGH
MEDIUM_CONVICTION_THRESHOLD = 2     # 2 wallets = MEDIUM


class WalletMonitor:
    """
    Tracks top Polymarket wallets by profit and extracts their active positions.
    Stores signals in SQLite table top_wallet_signals.
    """

    def __init__(self, db):
        self._db = db
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_markets: list[dict] = []
        self._last_wallet_summary: list[dict] = []  # for 6h report

        self._ensure_table()

    def _ensure_table(self):
        """Create/migrate top_wallet_signals and groq_usage tables."""
        conn = sqlite3.connect(self._db.db_path)
        try:
            conn.execute("""
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
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS groq_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    requests_made INTEGER DEFAULT 0,
                    tokens_used INTEGER DEFAULT 0,
                    key2_requests_made INTEGER DEFAULT 0,
                    key2_tokens_used INTEGER DEFAULT 0,
                    UNIQUE(date)
                )
            """)
            conn.commit()

            # Migrations: add columns if they don't exist yet
            existing = {r[1] for r in conn.execute("PRAGMA table_info(top_wallet_signals)").fetchall()}
            if "conviction_level" not in existing:
                conn.execute("ALTER TABLE top_wallet_signals ADD COLUMN conviction_level TEXT DEFAULT 'LOW'")
                conn.commit()

            groq_cols = {r[1] for r in conn.execute("PRAGMA table_info(groq_usage)").fetchall()}
            for col in ("key2_requests_made", "key2_tokens_used"):
                if col not in groq_cols:
                    conn.execute(f"ALTER TABLE groq_usage ADD COLUMN {col} INTEGER DEFAULT 0")
            conn.commit()
        finally:
            conn.close()

    # =========================================================================
    # API Fetching (polymarket-apis, no auth)
    # =========================================================================

    def _get_client(self):
        """Lazy-init PolymarketDataClient (no auth required)."""
        from polymarket_apis.clients.data_client import PolymarketDataClient
        return PolymarketDataClient()

    def _fetch_top_wallets(self) -> list[dict]:
        """
        Fetch top TOP_N_FETCH wallets sorted by all-time profit.
        Returns list of dicts: {proxyWallet, profit, name}.
        """
        try:
            client = self._get_client()
            entries = client.get_leaderboard_top_users(
                metric="profit", window="all", limit=TOP_N_FETCH + 5
            )
            result = []
            for e in entries[:TOP_N_FETCH]:
                result.append({
                    "proxyWallet": e.proxy_wallet,
                    "profit": float(e.amount),
                    "name": e.name or e.proxy_wallet[:10],
                })
            return result
        except Exception as e:
            logger.warning(f"WalletMonitor: failed to fetch top wallets: {e}")
            return []

    def _fetch_positions(self, address: str) -> list[dict]:
        """
        Fetch active positions for a wallet address.
        Returns list of dicts with: condition_id, title, outcome, size,
        avgPrice, currentPrice, currentValue, unrealizedPnl.
        """
        try:
            client = self._get_client()
            positions = client.get_positions(
                user=address,
                size_threshold=SIZE_THRESHOLD_USD,
                limit=50,
                sort_by="CURRENT",
                sort_direction="DESC",
            )
            result = []
            for p in positions:
                if not p.condition_id:
                    continue
                result.append({
                    "condition_id": p.condition_id,
                    "title": p.title or "",
                    "outcome": (p.outcome or "YES").upper(),
                    "size": float(p.size or 0),
                    "avgPrice": float(p.avg_price or 0.5),
                    "currentPrice": float(p.current_price or p.avg_price or 0.5),
                    "currentValue": float(p.current_value or 0),
                    "unrealizedPnl": float(p.cash_pnl or 0),
                })
            return result
        except Exception as e:
            logger.warning(f"WalletMonitor: failed to fetch positions for {address[:12]}: {e}")
            return []

    # =========================================================================
    # Storage
    # =========================================================================

    def _store_signals(
        self,
        wallet_rank: int,
        wallet_address: str,
        positions: list[dict],
        conviction_map: dict,  # condition_id -> conviction_level
    ):
        """Upsert wallet signals into DB with conviction_level."""
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self._db.db_path)
        try:
            for pos in positions:
                condition_id = pos.get("condition_id") or ""
                if not condition_id:
                    continue

                question = pos.get("title") or ""
                outcome = (pos.get("outcome") or "YES").upper()
                direction = "YES" if "YES" in outcome else "NO"
                size = float(pos.get("currentValue") or pos.get("size") or 0)
                entry_price = float(pos.get("avgPrice") or 0.5)
                current_price = float(pos.get("currentPrice") or entry_price)
                unrealized_pnl = float(pos.get("unrealizedPnl") or 0)
                conviction = conviction_map.get(condition_id, "LOW")

                conn.execute("""
                    INSERT INTO top_wallet_signals
                        (wallet_rank, wallet_address, market_condition_id, market_question,
                         direction, size_usd, entry_price, current_price, unrealized_pnl,
                         conviction_level, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(wallet_address, market_condition_id) DO UPDATE SET
                        direction=excluded.direction,
                        size_usd=excluded.size_usd,
                        entry_price=excluded.entry_price,
                        current_price=excluded.current_price,
                        unrealized_pnl=excluded.unrealized_pnl,
                        conviction_level=excluded.conviction_level,
                        timestamp=excluded.timestamp
                """, (
                    wallet_rank, wallet_address, condition_id, question,
                    direction, size, entry_price, current_price, unrealized_pnl,
                    conviction, now,
                ))
            conn.commit()
        finally:
            conn.close()

    # =========================================================================
    # Aggregation
    # =========================================================================

    def _aggregate_markets(self, positions: list[dict]) -> list[dict]:
        """
        Aggregate positions by condition_id.
        Computes conviction_level per market:
          HIGH   = 3+ wallets same direction
          MEDIUM = 2 wallets same direction
          LOW    = 1 wallet
        Sort: wallet_count desc → total_size_usd desc
        """
        agg: dict[str, dict] = {}
        for pos in positions:
            cid = pos["condition_id"]
            if cid not in agg:
                agg[cid] = {
                    "condition_id": cid,
                    "question": pos["question"],
                    "wallet_count": 0,
                    "wallet_ranks": [],
                    "directions": [],
                    "total_size_usd": 0.0,
                    "total_pnl": 0.0,
                }
            entry = agg[cid]
            entry["wallet_count"] += 1
            entry["wallet_ranks"].append(pos["wallet_rank"])
            entry["directions"].append(pos["direction"])
            entry["total_size_usd"] += pos["size_usd"]
            entry["total_pnl"] += pos["unrealized_pnl"]

        for entry in agg.values():
            yes_count = entry["directions"].count("YES")
            no_count = entry["directions"].count("NO")
            majority = max(yes_count, no_count)
            entry["consensus_direction"] = "YES" if yes_count >= no_count else "NO"
            entry["direction_agreement"] = majority / len(entry["directions"])

            # Conviction level based on agreeing wallets
            if majority >= HIGH_CONVICTION_THRESHOLD:
                entry["conviction_level"] = "HIGH"
            elif majority >= MEDIUM_CONVICTION_THRESHOLD:
                entry["conviction_level"] = "MEDIUM"
            else:
                entry["conviction_level"] = "LOW"

        return sorted(
            agg.values(),
            key=lambda x: (x["wallet_count"], x["total_size_usd"]),
            reverse=True,
        )

    # =========================================================================
    # Main refresh
    # =========================================================================

    def refresh(self) -> list[dict]:
        """
        Fetch top wallets + their positions. Filter wallets with <=5 positions.
        Store in DB. Returns aggregated smart money market dicts.
        """
        logger.info("WalletMonitor: Refreshing top wallet signals...")
        wallets = self._fetch_top_wallets()

        if not wallets:
            logger.warning("WalletMonitor: No top wallets returned from API")
            return []

        # Step 1: fetch all positions for all wallets
        wallet_data: list[dict] = []
        for rank, wallet in enumerate(wallets, start=1):
            address = wallet.get("proxyWallet") or ""
            if not address:
                continue
            name = wallet.get("name") or address[:10]
            profit = wallet.get("profit") or 0
            positions = self._fetch_positions(address)

            # Filter: only wallets with >MIN_POSITIONS_FOR_SIGNAL active positions
            if len(positions) <= MIN_POSITIONS_FOR_SIGNAL:
                logger.info(
                    f"  Rank {rank}: {name} ({address[:12]}...) "
                    f"profit=${profit:,.0f} — {len(positions)} positions (below threshold, skip)"
                )
                continue

            logger.info(
                f"  Rank {rank}: {name} ({address[:12]}...) "
                f"profit=${profit:,.0f} — {len(positions)} active positions"
            )
            wallet_data.append({
                "rank": rank,
                "address": address,
                "name": name,
                "profit": profit,
                "positions": positions,
            })

        # Step 2: build all_positions list for aggregation
        all_positions: list[dict] = []
        for wd in wallet_data:
            for pos in wd["positions"]:
                cid = pos.get("condition_id") or ""
                if cid:
                    direction = "YES" if "YES" in (pos.get("outcome") or "YES").upper() else "NO"
                    all_positions.append({
                        "wallet_rank": wd["rank"],
                        "wallet_address": wd["address"],
                        "condition_id": cid,
                        "question": pos.get("title") or "",
                        "direction": direction,
                        "size_usd": float(pos.get("currentValue") or pos.get("size") or 0),
                        "unrealized_pnl": float(pos.get("unrealizedPnl") or 0),
                    })

        # Step 3: aggregate → compute conviction map
        markets = self._aggregate_markets(all_positions)
        conviction_map = {m["condition_id"]: m["conviction_level"] for m in markets}

        # Step 4: store signals with conviction level
        for wd in wallet_data:
            self._store_signals(wd["rank"], wd["address"], wd["positions"], conviction_map)

        high_count = sum(1 for m in markets if m["conviction_level"] == "HIGH")
        med_count = sum(1 for m in markets if m["conviction_level"] == "MEDIUM")
        logger.info(
            f"WalletMonitor: {len(markets)} smart money markets "
            f"(HIGH={high_count}, MEDIUM={med_count}, LOW={len(markets)-high_count-med_count}) "
            f"from {len(wallet_data)} active wallets"
        )

        with self._lock:
            self._last_markets = markets
            self._last_wallet_summary = [
                {"name": wd["name"], "address": wd["address"],
                 "profit": wd["profit"], "positions": len(wd["positions"])}
                for wd in wallet_data
            ]

        return markets

    # =========================================================================
    # Public API
    # =========================================================================

    def get_smart_money_markets(self) -> list[dict]:
        """Returns cached list of aggregated smart money market dicts. Thread-safe."""
        with self._lock:
            return list(self._last_markets)

    def get_smart_money_cids(self) -> set[str]:
        """Returns set of condition_ids being tracked by smart money."""
        with self._lock:
            return {m["condition_id"] for m in self._last_markets}

    def get_signal_for_market(self, condition_id: str) -> dict:
        """
        Returns smart money signal info for a specific market.
        Returns empty dict if no signal.
        Dict includes: conviction_level, consensus_direction, wallet_count,
                       direction_agreement, total_size_usd, total_pnl.
        """
        with self._lock:
            for m in self._last_markets:
                if m["condition_id"] == condition_id:
                    return m
        return {}

    def get_wallet_summary(self) -> list[dict]:
        """Returns list of active wallet summaries for 6h report."""
        with self._lock:
            return list(self._last_wallet_summary)

    def get_high_conviction_markets(self) -> list[dict]:
        """Returns only HIGH and MEDIUM conviction markets."""
        with self._lock:
            return [m for m in self._last_markets if m["conviction_level"] in ("HIGH", "MEDIUM")]

    # =========================================================================
    # Background thread
    # =========================================================================

    def start(self):
        """Start background refresh thread. Runs immediately on startup."""
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="WalletMonitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("WalletMonitor: background thread started")

    def stop(self):
        self._running = False

    def _run_loop(self):
        """Background refresh loop."""
        try:
            self.refresh()
        except Exception as e:
            logger.error(f"WalletMonitor initial refresh failed: {e}")

        while self._running:
            for _ in range(REFRESH_INTERVAL_SECONDS):
                if not self._running:
                    return
                time.sleep(1)
            try:
                self.refresh()
            except Exception as e:
                logger.error(f"WalletMonitor refresh error: {e}")


# =============================================================================
# Standalone test: python wallet_monitor.py --test
# =============================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if "--test" not in sys.argv:
        print("Usage: python wallet_monitor.py --test")
        sys.exit(0)

    print("=== WalletMonitor standalone test ===\n")

    import tempfile, os
    tmp = tempfile.mktemp(suffix=".db")

    class _StubDB:
        db_path = tmp

    monitor = WalletMonitor(_StubDB())

    print("--- Fetching top wallets ---")
    wallets = monitor._fetch_top_wallets()
    if not wallets:
        print("ERROR: No wallets returned")
        sys.exit(1)
    for i, w in enumerate(wallets, 1):
        print(f"  Rank {i}: {w['name']} ({w['proxyWallet'][:14]}...) profit=${w['profit']:,.0f}")

    print(f"\n--- Fetching positions (filter: >{MIN_POSITIONS_FOR_SIGNAL} positions) ---")
    for i, w in enumerate(wallets[:5], 1):
        positions = monitor._fetch_positions(w["proxyWallet"])
        status = "INCLUDED" if len(positions) > MIN_POSITIONS_FOR_SIGNAL else "SKIP (too few)"
        print(f"  Rank {i} ({w['name']}): {len(positions)} positions — {status}")
        if len(positions) > MIN_POSITIONS_FOR_SIGNAL:
            for p in positions[:2]:
                print(f"    {p['title'][:55]:<55} | {p['outcome']:<3} | val=${p['currentValue']:,.0f}")

    print(f"\n--- Full refresh ---")
    markets = monitor.refresh()
    print(f"Smart money markets: {len(markets)}")
    high = [m for m in markets if m["conviction_level"] == "HIGH"]
    med  = [m for m in markets if m["conviction_level"] == "MEDIUM"]
    print(f"  HIGH conviction: {len(high)}, MEDIUM: {len(med)}, LOW: {len(markets)-len(high)-len(med)}")
    for m in markets[:8]:
        print(
            f"  [{m['conviction_level']:<6}] [{m['consensus_direction']}] "
            f"{m['question'][:55]:<55} "
            f"wallets={m['wallet_count']} size=${m['total_size_usd']:,.0f}"
        )

    import sqlite3 as _sq
    conn = _sq.connect(tmp)
    rows = conn.execute("SELECT COUNT(*) FROM top_wallet_signals").fetchone()[0]
    conv_rows = conn.execute(
        "SELECT conviction_level, COUNT(*) FROM top_wallet_signals GROUP BY conviction_level"
    ).fetchall()
    conn.close()
    try:
        os.unlink(tmp)
    except Exception:
        pass
    print(f"\nDB rows: {rows} | Conviction breakdown: {conv_rows}")
    print("Test passed" if rows > 0 else "Test FAILED: 0 rows stored")
