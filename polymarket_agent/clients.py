from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

_WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


class ApiBudget:
    """Simple in-memory budget guard per process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def hit(self, key: str, max_hits: int) -> bool:
        with self._lock:
            cur = self._counts.get(key, 0)
            if cur >= max_hits:
                return False
            self._counts[key] = cur + 1
            return True


class PolymarketDataClient:
    def __init__(self, base_url: str, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(timeout=timeout)

    def fetch_user_positions(self, wallet: str) -> list[dict[str, Any]]:
        """GET /positions?user=0x… — Polymarket Data API current positions."""
        if not _WALLET_RE.match(wallet or ""):
            return []
        try:
            resp = self.http.get(
                f"{self.base_url}/positions",
                params={"user": wallet, "limit": 100, "sortBy": "TOKENS", "sortDirection": "DESC"},
            )
            resp.raise_for_status()
            raw = resp.json()
        except Exception:
            return []

        out: list[dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            cid = str(item.get("conditionId", "") or "")
            if not cid:
                continue
            title = str(item.get("title", "") or "Unknown market")
            avg = float(item.get("avgPrice", 0.0) or 0.0)
            cur = float(item.get("curPrice", avg) or avg)
            opened_at = datetime.now(timezone.utc).isoformat()
            end_raw = item.get("endDate")
            out.append(
                {
                    "market_id": cid,
                    "market_question": title,
                    "entry_price": max(0.0, min(1.0, avg)),
                    "current_price": max(0.0, min(1.0, cur)),
                    "side": self._normalize_side(item.get("outcome", "YES")),
                    "sector": self._infer_sector(title, str(item.get("slug", "") or "")),
                    "wallet": wallet,
                    "opened_at": opened_at,
                    "resolution_end": str(end_raw) if end_raw else "",
                }
            )
        return out

    @staticmethod
    def _infer_sector(title: str, slug: str) -> str:
        blob = f"{title} {slug}".lower()
        if any(k in blob for k in ("nba", "nfl", "nhl", "mlb", "ufc", "soccer", "premier league", "f1", "tennis")):
            return "sports"
        if any(k in blob for k in ("election", "president", "senate", "trump", "biden", "parliament")):
            return "politics"
        if any(k in blob for k in ("btc", "eth", "crypto", "bitcoin")):
            return "crypto"
        return "other"

    @staticmethod
    def _normalize_side(value: str) -> str:
        upper = str(value).strip().upper()
        if upper in ("NO", "N", "FALSE"):
            return "NO"
        return "YES"


class ExternalIntelClient:
    def __init__(
        self,
        the_odds_api_key: str,
        news_api_key: str,
        serper_api_key: str,
        tavily_api_key: str,
        espn_api_base: str,
        timeout: float = 20.0,
    ) -> None:
        self.the_odds_api_key = the_odds_api_key
        self.news_api_key = news_api_key
        self.serper_api_key = serper_api_key
        self.tavily_api_key = tavily_api_key
        self.espn_api_base = espn_api_base.rstrip("/")
        self.http = httpx.Client(timeout=timeout)
        self.budget = ApiBudget()

    def efficiency_check(self, position: dict[str, Any]) -> dict[str, Any]:
        current = position["current_price"]
        external_price = self._fetch_external_price_hint(position)
        if external_price is None:
            # Conservative fallback when no external feed is available.
            external_price = current
        diff = abs(current - external_price)
        return {
            "external_price": external_price,
            "polymarket_price": current,
            "price_diff": diff,
            "is_inefficient": diff >= 0.05,
            "recommendation": "PROCEED" if diff >= 0.05 else "SKIP_EFFICIENT",
        }

    def run_research(self, market_question: str) -> dict[str, Any]:
        espn = self._espn_lookup(market_question)
        serper = self._serper_lookup(market_question)
        news = self._news_lookup(market_question)
        tavily = "not_required"
        if self._low_confidence_signals(espn, serper, news):
            tavily = self._tavily_lookup(market_question)
        return {
            "espn": espn,
            "serper": serper,
            "news": news,
            "tavily": tavily,
        }

    def _fetch_external_price_hint(self, position: dict[str, Any]) -> float | None:
        # The Odds API can provide price hints for sports markets.
        if not self.the_odds_api_key:
            return None
        if not self.budget.hit("the_odds_month", 500):
            return None
        try:
            sport = "soccer_epl" if "soccer" in position.get("sector", "").lower() else "basketball_nba"
            resp = self.http.get(
                f"https://api.the-odds-api.com/v4/sports/{sport}/odds",
                params={"apiKey": self.the_odds_api_key, "regions": "us", "markets": "h2h", "bookmakers": "pinnacle"},
            )
            resp.raise_for_status()
            rows = resp.json()
            if not isinstance(rows, list) or not rows:
                return None
            # Light heuristic: convert first decimal odd to implied probability.
            bookmakers = rows[0].get("bookmakers", [])
            if not bookmakers:
                return None
            outcomes = bookmakers[0].get("markets", [{}])[0].get("outcomes", [])
            if not outcomes:
                return None
            price = float(outcomes[0].get("price", 2.0))
            implied = 1.0 / price if price > 1 else 0.5
            return float(min(max(implied, 0.01), 0.99))
        except Exception:
            return None

    def _espn_lookup(self, question: str) -> str:
        if not self.budget.hit("espn_daily", 1000):
            return "budget_exceeded"
        try:
            resp = self.http.get(f"{self.espn_api_base}/sports/basketball/nba/scoreboard")
            resp.raise_for_status()
            data = resp.json()
            event_count = len(data.get("events", [])) if isinstance(data, dict) else 0
            return f"ESPN scoreboard check ok, events={event_count}, query={question[:120]}"
        except Exception as exc:
            return f"espn_unavailable: {exc}"

    def _serper_lookup(self, question: str) -> str:
        if not self.serper_api_key:
            return "serper_key_missing"
        if not self.budget.hit("serper_month", 2500):
            return "serper_budget_exceeded"
        try:
            resp = self.http.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": self.serper_api_key},
                json={"q": question, "num": 5},
            )
            resp.raise_for_status()
            data = resp.json()
            snippets = [x.get("snippet", "") for x in data.get("organic", [])[:3]]
            return " | ".join(s for s in snippets if s)[:600] or "serper_no_snippets"
        except Exception as exc:
            return f"serper_unavailable: {exc}"

    def _news_lookup(self, question: str) -> str:
        if not self.news_api_key:
            return "news_key_missing"
        if not self.budget.hit("news_daily", 100):
            return "news_budget_exceeded"
        try:
            resp = self.http.get(
                "https://newsapi.org/v2/everything",
                params={"q": question, "language": "en", "sortBy": "publishedAt", "pageSize": 5, "apiKey": self.news_api_key},
            )
            resp.raise_for_status()
            data = resp.json()
            titles = [a.get("title", "") for a in data.get("articles", [])[:3]]
            return " | ".join(t for t in titles if t)[:600] or "news_no_titles"
        except Exception as exc:
            return f"news_unavailable: {exc}"

    def _tavily_lookup(self, question: str) -> str:
        if not self.tavily_api_key:
            return "tavily_key_missing"
        if not self.budget.hit("tavily_month", 1000):
            return "tavily_budget_exceeded"
        try:
            resp = self.http.post(
                "https://api.tavily.com/search",
                json={"api_key": self.tavily_api_key, "query": question, "max_results": 3},
            )
            resp.raise_for_status()
            data = resp.json()
            chunks = [r.get("content", "") for r in data.get("results", [])[:2]]
            return " | ".join(c for c in chunks if c)[:800] or "tavily_no_results"
        except Exception as exc:
            return f"tavily_unavailable: {exc}"

    @staticmethod
    def _low_confidence_signals(espn: str, serper: str, news: str) -> bool:
        text = f"{espn} {serper} {news}".lower()
        weak_markers = ("unavailable", "no_", "missing", "budget_exceeded")
        return any(m in text for m in weak_markers)


class LLMClient:
    def __init__(
        self,
        gemini_api_key: str,
        groq_api_key: str,
        gemini_model: str,
        groq_model: str,
        timeout: float = 25.0,
        db: Any | None = None,
        max_retries: int = 3,
    ) -> None:
        self.gemini_api_key = gemini_api_key
        self.groq_api_key = groq_api_key
        self.gemini_model = gemini_model
        self.groq_model = groq_model
        self.http = httpx.Client(timeout=timeout)
        self.db = db
        self.max_retries = max(1, max_retries)

    def _audit(self, level: str, action: str, details: dict[str, Any]) -> None:
        if self.db is not None:
            self.db.write_audit(level, "llm", action, details)

    def analyst_decision(self, position: dict[str, Any], research: dict[str, Any]) -> dict[str, Any]:
        from .models import AnalystDecision

        prompt = (
            "You are an institutional prediction-market analyst.\n"
            "Return strict JSON only with keys: contradicting_info (bool), has_value (bool), "
            "confidence (low|medium|high), action (PROCEED|SKIP), reasoning (string), key_risk (string).\n"
            f"Position: {json.dumps(position)}\n"
            f"Research: {json.dumps(research)}\n"
        )
        if self.gemini_api_key:
            for attempt in range(self.max_retries):
                raw = self._gemini_raw_text(prompt)
                if raw is None:
                    self._audit("WARN", "gemini_empty_response", {"attempt": attempt + 1})
                    continue
                try:
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict):
                        raise ValueError("not_a_dict")
                    return AnalystDecision.model_validate(parsed).model_dump()
                except Exception as exc:
                    self._audit(
                        "WARN",
                        "gemini_schema_fail",
                        {
                            "attempt": attempt + 1,
                            "error": str(exc),
                            "raw_prefix": raw[:2000],
                        },
                    )
        return self._fallback_analyst(position)

    def devil_advocate(self, position: dict[str, Any], analyst: dict[str, Any]) -> dict[str, Any]:
        from .models import DevilAdvocateDecision

        prompt = (
            "You are a devil's advocate for prediction market trades.\n"
            "Return strict JSON only with keys: fatal_issue (bool), price_move (number), reason (string), action (PROCEED|SKIP).\n"
            "Find reasons to reject this trade.\n"
            f"Position: {json.dumps(position)}\n"
            f"Analyst: {json.dumps(analyst)}\n"
        )
        if self.groq_api_key:
            for attempt in range(self.max_retries):
                raw = self._groq_raw_text(prompt)
                if raw is None:
                    self._audit("WARN", "groq_empty_response", {"attempt": attempt + 1})
                    continue
                try:
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict):
                        raise ValueError("not_a_dict")
                    return DevilAdvocateDecision.model_validate(parsed).model_dump()
                except Exception as exc:
                    self._audit(
                        "WARN",
                        "groq_schema_fail",
                        {
                            "attempt": attempt + 1,
                            "error": str(exc),
                            "raw_prefix": raw[:2000],
                        },
                    )
        price_move = abs(position["current_price"] - position["entry_price"])
        fatal = price_move > 0.15
        return {
            "fatal_issue": fatal,
            "price_move": price_move,
            "reason": "Entry moved more than 15% from source wallet." if fatal else "No fatal flaws found.",
            "action": "SKIP" if fatal else "PROCEED",
        }

    def _gemini_raw_text(self, prompt: str) -> str | None:
        try:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self.gemini_model}:generateContent?key={self.gemini_api_key}"
            )
            resp = self.http.post(
                url,
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return str(text)
        except Exception as exc:
            self._audit("WARN", "gemini_request_fail", {"error": str(exc)})
            return None

    def _groq_raw_text(self, prompt: str) -> str | None:
        try:
            resp = self.http.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.groq_api_key}"},
                json={
                    "model": self.groq_model,
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return str(content)
        except Exception as exc:
            self._audit("WARN", "groq_request_fail", {"error": str(exc)})
            return None

    @staticmethod
    def _fallback_analyst(position: dict[str, Any]) -> dict[str, Any]:
        confidence = "high" if position["entry_price"] < position["current_price"] else "medium"
        return {
            "contradicting_info": False,
            "has_value": True,
            "confidence": confidence,
            "action": "PROCEED",
            "reasoning": "Fallback analyst used due to LLM/API unavailability.",
            "key_risk": "Model output fallback; reduced confidence in signal quality.",
        }


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.http = httpx.Client(timeout=15.0)

    def send(self, text: str) -> None:
        if not self.bot_token or not self.chat_id:
            return
        self.http.post(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            json={"chat_id": self.chat_id, "text": text},
        )
