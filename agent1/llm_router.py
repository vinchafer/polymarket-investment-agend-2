"""
llm_router.py — Multi-Provider LLM Router with health-aware failover.

Provider tiers (in order):
  1. Groq (existing key)             — primary, llama-3.1-8b-instant (500k TPD)
  2. Gemini (existing key)           — secondary, gemini-2.0-flash (1500 RPD)
  3. GitHub Models (PAT)             — tertiary, gpt-4o-mini (free, ~150/day)
  4. Pollinations.ai                 — quaternary, zero-signup public endpoint

State persisted in `provider_state` table (SQLite). Per-provider:
  - cooldown_until: timestamp ISO — provider skipped until this point
  - tokens_used_today: int — daily token meter (reset at UTC midnight)
  - last_status: TEXT — success | 429 | timeout | error

Usage:
    router = LLMRouter(db_path=config.DB_PATH)
    result = router.decide_json(prompt, schema_hint="action, confidence, reasoning")
    # result is dict or {"action":"SKIP","reason":"all_providers_down"}
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_429 = 300       # 5min after 429
DEFAULT_COOLDOWN_5XX = 30        # 30s after server error
DEFAULT_TIMEOUT_SEC = 20


# =============================================================================
# State table
# =============================================================================

def _ensure_tables(db_path: str):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS provider_state (
                provider TEXT PRIMARY KEY,
                cooldown_until TEXT,
                tokens_used_today INTEGER DEFAULT 0,
                day_bucket TEXT,
                last_status TEXT,
                last_seen TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS provider_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                provider TEXT,
                status TEXT,
                tokens_used INTEGER DEFAULT 0,
                latency_ms INTEGER DEFAULT 0,
                error_snippet TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_provider_metrics_ts ON provider_metrics(ts)")
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Provider base
# =============================================================================

class _Provider:
    name = "base"

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()

    # ----- state helpers -----
    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> dict:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT cooldown_until, tokens_used_today, day_bucket, last_status "
                "FROM provider_state WHERE provider=?", (self.name,)
            ).fetchone()
            if not row:
                return {"cooldown_until": "", "tokens_used_today": 0, "day_bucket": self._today(), "last_status": ""}
            cooldown, tokens, day, status = row
            if day != self._today():
                # day rollover → reset counters
                tokens = 0
                day = self._today()
            return {"cooldown_until": cooldown or "", "tokens_used_today": int(tokens),
                    "day_bucket": day, "last_status": status or ""}
        finally:
            conn.close()

    def _save(self, cooldown: str, tokens_used: int, status: str):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                INSERT OR REPLACE INTO provider_state
                  (provider, cooldown_until, tokens_used_today, day_bucket, last_status, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (self.name, cooldown, tokens_used, self._today(), status, self._now()))
            conn.commit()
        finally:
            conn.close()

    def _emit_metric(self, status: str, tokens: int, latency_ms: int, err: str = ""):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                INSERT INTO provider_metrics (ts, provider, status, tokens_used, latency_ms, error_snippet)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (self._now(), self.name, status, tokens, latency_ms, (err or "")[:200]))
            conn.commit()
        finally:
            conn.close()

    def healthy(self) -> bool:
        st = self._load()
        if not st["cooldown_until"]:
            return True
        try:
            until = datetime.fromisoformat(st["cooldown_until"])
        except Exception:
            return True
        return datetime.now(timezone.utc) >= until

    def _cooldown(self, seconds: int, reason: str):
        from datetime import timedelta
        until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
        st = self._load()
        with self._lock:
            self._save(until, st["tokens_used_today"], reason)

    def _record_success(self, tokens: int):
        st = self._load()
        with self._lock:
            self._save("", st["tokens_used_today"] + tokens, "success")

    # ----- override -----
    def call(self, prompt: str) -> str:
        raise NotImplementedError


# =============================================================================
# Concrete providers
# =============================================================================

class _GroqProvider(_Provider):
    name = "groq"

    def __init__(self, db_path: str, model: str = "llama-3.1-8b-instant"):
        super().__init__(db_path)
        self.model = model
        # Primary + optional backup key for silent rotation on 429 (FIX 4)
        keys = [
            getattr(config, "GROQ_API_KEY", "") or "",
            getattr(config, "GROQ_API_KEY_2", "") or "",
        ]
        self.api_keys = [k for k in keys if k]
        self.api_key = self.api_keys[0] if self.api_keys else ""

    def healthy(self) -> bool:
        return bool(self.api_key) and super().healthy()

    def call(self, prompt: str) -> str:
        keys = self.api_keys or ([self.api_key] if self.api_key else [])
        for idx, key in enumerate(keys):
            start = time.time()
            is_last = (idx + 1 >= len(keys))
            try:
                resp = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": self.model,
                        "temperature": 0.1,
                        "max_tokens": 300,
                        "response_format": {"type": "json_object"},
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=DEFAULT_TIMEOUT_SEC,
                )
            except Exception as e:
                self._emit_metric("error", 0, int((time.time()-start)*1000), str(e)[:200])
                raise
            latency_ms = int((time.time() - start) * 1000)
            if resp.status_code == 429:
                self._emit_metric("429", 0, latency_ms, resp.text[:200])
                if not is_last:
                    # Silent rotation to backup key — no Telegram, no cooldown yet
                    logger.info(f"Router: groq key #{idx+1} hit 429 → rotating to backup key")
                    continue
                self._cooldown(DEFAULT_COOLDOWN_429, "429")
                raise RuntimeError("groq_429")
            try:
                resp.raise_for_status()
            except requests.HTTPError as e:
                self._cooldown(DEFAULT_COOLDOWN_5XX, f"http_{resp.status_code}")
                self._emit_metric("error", 0, latency_ms, str(e)[:200])
                raise
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            tokens = data.get("usage", {}).get("total_tokens", 0)
            self._record_success(tokens)
            self._emit_metric("success", tokens, latency_ms)
            return text
        raise RuntimeError("groq_no_key")


class _GeminiProvider(_Provider):
    name = "gemini"

    def __init__(self, db_path: str, model: str = "gemini-2.0-flash"):
        super().__init__(db_path)
        self.model = model
        self.api_key = getattr(config, "GEMINI_API_KEY", "") or getattr(config, "GEMINI2_API_KEY", "") or ""

    def healthy(self) -> bool:
        return bool(self.api_key) and super().healthy()

    def call(self, prompt: str) -> str:
        start = time.time()
        try:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self.model}:generateContent?key={self.api_key}"
            )
            resp = requests.post(
                url,
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "temperature": 0.1,
                        "maxOutputTokens": 300,
                    },
                },
                timeout=DEFAULT_TIMEOUT_SEC,
            )
            latency_ms = int((time.time() - start) * 1000)
            if resp.status_code == 429 or "RESOURCE_EXHAUSTED" in resp.text:
                self._cooldown(DEFAULT_COOLDOWN_429, "429")
                self._emit_metric("429", 0, latency_ms, resp.text[:200])
                raise RuntimeError("gemini_429")
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            tokens = data.get("usageMetadata", {}).get("totalTokenCount", 0)
            self._record_success(tokens)
            self._emit_metric("success", tokens, latency_ms)
            return text
        except requests.HTTPError as e:
            self._cooldown(DEFAULT_COOLDOWN_5XX, f"http_{e.response.status_code}")
            self._emit_metric("error", 0, int((time.time()-start)*1000), str(e)[:200])
            raise
        except Exception as e:
            self._emit_metric("error", 0, int((time.time()-start)*1000), str(e)[:200])
            raise


class _GitHubModelsProvider(_Provider):
    name = "github_models"

    def __init__(self, db_path: str, model: str = "gpt-4o-mini"):
        super().__init__(db_path)
        self.model = model
        self.token = os.getenv("GITHUB_PERSONAL_TOKEN", "") or os.getenv("GITHUB_TOKEN", "")

    def healthy(self) -> bool:
        return bool(self.token) and super().healthy()

    def call(self, prompt: str) -> str:
        start = time.time()
        try:
            resp = requests.post(
                "https://models.inference.ai.azure.com/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "temperature": 0.1,
                    "max_tokens": 300,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=DEFAULT_TIMEOUT_SEC,
            )
            latency_ms = int((time.time() - start) * 1000)
            if resp.status_code == 429:
                self._cooldown(DEFAULT_COOLDOWN_429, "429")
                self._emit_metric("429", 0, latency_ms, resp.text[:200])
                raise RuntimeError("github_429")
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            tokens = data.get("usage", {}).get("total_tokens", 0)
            self._record_success(tokens)
            self._emit_metric("success", tokens, latency_ms)
            return text
        except requests.HTTPError as e:
            self._cooldown(DEFAULT_COOLDOWN_5XX, f"http_{e.response.status_code}")
            self._emit_metric("error", 0, int((time.time()-start)*1000), str(e)[:200])
            raise
        except Exception as e:
            self._emit_metric("error", 0, int((time.time()-start)*1000), str(e)[:200])
            raise


class _PollinationsProvider(_Provider):
    """Zero-auth public LLM endpoint. Best-effort, no key needed."""
    name = "pollinations"

    def call(self, prompt: str) -> str:
        start = time.time()
        try:
            resp = requests.post(
                "https://text.pollinations.ai/openai",
                json={
                    "model": "openai",
                    "temperature": 0.1,
                    "max_tokens": 300,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=DEFAULT_TIMEOUT_SEC,
            )
            latency_ms = int((time.time() - start) * 1000)
            if resp.status_code == 429:
                self._cooldown(DEFAULT_COOLDOWN_429, "429")
                self._emit_metric("429", 0, latency_ms, resp.text[:200])
                raise RuntimeError("pollinations_429")
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            self._record_success(0)
            self._emit_metric("success", 0, latency_ms)
            return text
        except requests.HTTPError as e:
            self._cooldown(DEFAULT_COOLDOWN_5XX, f"http_{e.response.status_code}")
            self._emit_metric("error", 0, int((time.time()-start)*1000), str(e)[:200])
            raise
        except Exception as e:
            self._cooldown(DEFAULT_COOLDOWN_5XX, "error")
            self._emit_metric("error", 0, int((time.time()-start)*1000), str(e)[:200])
            raise


# =============================================================================
# Router
# =============================================================================

class LLMRouter:
    def __init__(self, db_path: str, analyst_mode: bool = True):
        _ensure_tables(db_path)
        self.db_path = db_path
        if analyst_mode:
            # High-volume: 8b primary
            self.providers = [
                _GroqProvider(db_path, model="llama-3.1-8b-instant"),
                _GeminiProvider(db_path),
                _GitHubModelsProvider(db_path),
                _PollinationsProvider(db_path),
            ]
        else:
            # Quality-critical (Devil's Advocate): 70b primary
            self.providers = [
                _GroqProvider(db_path, model="llama-3.3-70b-versatile"),
                _GeminiProvider(db_path),
                _GitHubModelsProvider(db_path),
                _PollinationsProvider(db_path),
            ]

    def probe_fallbacks(self) -> dict:
        """Health-probe the non-primary providers (gemini, github_models,
        pollinations) with ONE minimal call each, to keep provider_state fresh
        so A1 knows where to fail over when Groq collapses (SPOF mitigation).

        Groq (primary) is skipped — it is exercised on every real decision.
        A provider still in cooldown is not re-hit (respects backoff). Each
        provider.call() self-updates provider_state.last_seen/last_status and
        provider_metrics, so this method just drives them minimally.
        1 call/provider, no retry — free-tier quota friendly.
        """
        results = {}
        probe_prompt = 'Reply with only this JSON and nothing else: {"ok": true}'
        for p in self.providers:
            if p.name == "groq":
                continue
            if not p.healthy():
                results[p.name] = "cooldown"
                continue
            try:
                raw = p.call(probe_prompt)
                results[p.name] = "ok" if _parse_json(raw) else "bad_json"
            except Exception as e:
                results[p.name] = f"error:{str(e)[:40]}"
        logger.info(f"Router probe: {results}")
        return results

    def decide_json(self, prompt: str) -> tuple[dict, str]:
        """Returns (parsed_dict, provider_name_used). Empty dict + 'none' if all dead."""
        for p in self.providers:
            if not p.healthy():
                logger.debug(f"Router: skip {p.name} (cooldown)")
                continue
            try:
                raw = p.call(prompt)
                parsed = _parse_json(raw)
                if parsed:
                    logger.info(f"Router: {p.name} → ok")
                    return parsed, p.name
                logger.warning(f"Router: {p.name} returned unparseable JSON")
            except Exception as e:
                logger.warning(f"Router: {p.name} failed: {str(e)[:120]}")
                continue
        return {}, "none"


def _parse_json(text: str) -> Optional[dict]:
    if not text:
        return None
    t = text.strip()
    if "```" in t:
        parts = t.split("```")
        if len(parts) >= 2:
            t = parts[1]
            if t.startswith("json"):
                t = t[4:]
            t = t.strip()
    # Extract first {...} blob if surrounded by extra text
    m = re.search(r"\{[\s\S]*\}", t)
    if m:
        t = m.group(0)
    try:
        return json.loads(t)
    except Exception:
        return None
