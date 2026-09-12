"""
health_monitor.py — background thread that scans provider_metrics and pages Telegram
when thresholds breached. Throttled to one alert per (kind) per hour.
"""

import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone

import config

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 600    # 10 min
ALERT_COOLDOWN_SECONDS = 21600  # 6h per (kind) — avoid hourly spam (FIX 1)
FAIL_WINDOW = "-3 hours"        # lookback; require failures to span >2h within it
MIN_ATTEMPTS_FOR_ALERT = 5      # ignore low-sample noise


class HealthMonitor:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._running = False
        self._thread = None
        self._last_alert: dict = {}  # kind -> ts
        # ensure tables exist (router does same; idempotent)
        try:
            from llm_router import _ensure_tables
            _ensure_tables(db_path)
        except Exception as e:
            logger.warning(f"HealthMonitor: table ensure failed: {e}")

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _alert(self, kind: str, text: str):
        now = time.time()
        if now - self._last_alert.get(kind, 0) < ALERT_COOLDOWN_SECONDS:
            return
        self._last_alert[kind] = now
        try:
            from notifier import TelegramNotifier
            n = TelegramNotifier()
            if n.enabled:
                n.send_message(f"🚨 HEALTH ALERT [{kind}]\n{text}")
        except Exception as e:
            logger.warning(f"HealthMonitor: telegram send failed: {e}")
        logger.warning(f"HealthMonitor ALERT [{kind}]: {text}")

    def check_once(self):
        conn = self._conn()
        try:
            # 1. Per-provider sustained failure over the last 2h.
            #    Only alert if a provider has been failing >80% across >=5 attempts
            #    spanning >2h — a single dead provider that fallbacks cover is NOT
            #    worth a Telegram page. (FIX 1)
            rows = conn.execute(f"""
                SELECT provider, status, COUNT(*) as n
                FROM provider_metrics
                WHERE ts >= datetime('now','{FAIL_WINDOW}')
                GROUP BY provider, status
            """).fetchall()
            stats = {}
            for prov, status, n in rows:
                stats.setdefault(prov, {})[status] = n
            for prov, s in stats.items():
                if prov == "pollinations":
                    continue
                total = sum(s.values())
                if total < MIN_ATTEMPTS_FOR_ALERT:
                    continue
                # Require the failures actually span >2h (sustained, not bursty)
                span = conn.execute(f"""
                    SELECT (julianday(MAX(ts)) - julianday(MIN(ts))) * 24.0
                    FROM provider_metrics
                    WHERE provider=? AND status IN ('429','error')
                      AND ts >= datetime('now','{FAIL_WINDOW}')
                """, (prov,)).fetchone()[0] or 0.0
                fail_rate = (s.get("429", 0) + s.get("error", 0)) / total
                if fail_rate >= 0.8 and span >= 2.0:
                    self._alert(
                        f"provider_{prov}",
                        f"{prov} fail-rate {fail_rate:.0%} sustained {span:.1f}h "
                        f"(n={total}). Status: {s}",
                    )

            # 2. All providers down in last 5min
            recent = conn.execute("""
                SELECT provider, MAX(ts) as last
                FROM provider_metrics
                WHERE status = 'success' AND ts >= datetime('now','-5 minutes')
                GROUP BY provider
            """).fetchall()
            if not recent:
                # check if any LLM calls were attempted
                attempts = conn.execute("""
                    SELECT COUNT(*) FROM provider_metrics WHERE ts >= datetime('now','-5 minutes')
                """).fetchone()[0]
                if attempts > 0:
                    self._alert("all_llm_down",
                                f"All LLM providers failed last 5min ({attempts} attempts, 0 success)")

            # 3. Today token budget warning
            budget = conn.execute("""
                SELECT provider, SUM(tokens_used) as total
                FROM provider_metrics
                WHERE date(ts) = date('now') AND status='success'
                GROUP BY provider
            """).fetchall()
            # Groq llama-3.1-8b free TPD = 500000; warn at 80%
            for prov, total in budget:
                if prov == "groq" and total and total > 400000:
                    self._alert("groq_budget",
                                f"Groq daily token usage {total:,} / 500k (80% threshold)")
                if prov == "gemini" and total and total > 1200:
                    # Gemini Free 2.0-flash = 1500 RPD ~roughly
                    self._alert("gemini_budget",
                                f"Gemini daily usage approaching free-tier ({total:,} tokens)")
        finally:
            conn.close()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="HealthMonitor", daemon=True)
        self._thread.start()
        logger.info("HealthMonitor: background thread started")

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                self.check_once()
            except Exception as e:
                logger.error(f"HealthMonitor: check error: {e}", exc_info=True)
            for _ in range(CHECK_INTERVAL_SECONDS):
                if not self._running:
                    return
                time.sleep(1)
