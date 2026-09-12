"""
agent_efficiency.py — Market Efficiency Checker (Agent 0)

Compares Polymarket price against Pinnacle odds (The Odds API) and Kalshi.
If |polymarket_price - external_price| < 0.05 → EFFICIENT → skip
If >= 0.05 → INEFFICIENT → proceed

Usage:
    checker = EfficiencyChecker()
    result = checker.check(market_question, polymarket_price)

    python agent_efficiency.py --test
"""

import logging
import os
import requests
from typing import Optional

import config

logger = logging.getLogger(__name__)

EFFICIENCY_THRESHOLD = 0.05  # 5% gap = inefficient

SPORT_MAP = {
    "nba": "basketball_nba",
    "nfl": "americanfootball_nfl",
    "nhl": "icehockey_nhl",
    "mlb": "baseball_mlb",
    "epl": "soccer_epl",
    "premier league": "soccer_epl",
    "mls": "soccer_usa_mls",
    "champions league": "soccer_uefa_champs_league",
}

SKIP_KEYWORDS = {"tennis", "crypto", "bitcoin", "ethereum", "politics", "election", "president"}


class EfficiencyChecker:

    def __init__(self):
        self.odds_api_key = getattr(config, "THE_ODDS_API_KEY", "") or ""

    def _detect_sport(self, question: str) -> Optional[str]:
        q = question.lower()
        for kw in SKIP_KEYWORDS:
            if kw in q:
                return None
        for kw, sport in SPORT_MAP.items():
            if kw in q:
                return sport
        return None

    def _get_pinnacle_price(self, sport: str, question: str) -> Optional[float]:
        """Fetch Pinnacle odds via The Odds API; fuzzy-match market to question."""
        if not self.odds_api_key:
            return None
        try:
            resp = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{sport}/odds",
                params={
                    "apiKey": self.odds_api_key,
                    "regions": "eu",
                    "markets": "h2h",
                    "bookmakers": "pinnacle",
                },
                timeout=8,
            )
            if resp.status_code != 200:
                return None

            query_words = set(question.lower().split())
            best_match = None
            best_score = 0

            for game in resp.json():
                home = (game.get("home_team") or "").lower()
                away = (game.get("away_team") or "").lower()
                score = len(query_words & set((home + " " + away).split()))
                if score > best_score:
                    best_score = score
                    best_match = game

            if not best_match or best_score < 1:
                return None

            for bm in best_match.get("bookmakers", []):
                if bm.get("key") == "pinnacle":
                    for mkt in bm.get("markets", []):
                        if mkt.get("key") == "h2h":
                            outcomes = mkt.get("outcomes", [])
                            if outcomes:
                                # Convert favourite's decimal odds to probability
                                best_odds = min(o.get("price", 2.0) for o in outcomes)
                                return round(1.0 / best_odds, 4)
        except Exception as e:
            logger.debug(f"EfficiencyChecker: Pinnacle fetch failed: {e}")
        return None

    def _get_kalshi_price(self, question: str) -> Optional[float]:
        """Fuzzy-match a Kalshi market and return its yes price."""
        try:
            words = question.split()[:5]
            resp = requests.get(
                "https://trading-api.kalshi.com/trade-api/v2/markets",
                params={"limit": 10, "status": "open", "search": " ".join(words)},
                timeout=8,
            )
            if resp.status_code != 200:
                return None
            markets = resp.json().get("markets", [])
            if not markets:
                return None
            first = markets[0]
            yes_price = first.get("yes_bid") or first.get("yes_ask")
            if yes_price is not None:
                return float(yes_price) / 100.0  # Kalshi uses cents
        except Exception as e:
            logger.debug(f"EfficiencyChecker: Kalshi fetch failed: {e}")
        return None

    def check(self, market_question: str, polymarket_price: float) -> dict:
        """
        Returns efficiency_result dict:
        {
          "is_efficient": bool,
          "polymarket_price": float,
          "external_price": float | None,
          "price_gap": float,
          "source": "pinnacle/kalshi/unknown",
          "recommendation": "SKIP_EFFICIENT/PROCEED"
        }
        """
        sport = self._detect_sport(market_question)
        external_price = None
        source = "unknown"

        if sport:
            p = self._get_pinnacle_price(sport, market_question)
            if p is not None:
                external_price = p
                source = "pinnacle"

        if external_price is None:
            k = self._get_kalshi_price(market_question)
            if k is not None:
                external_price = k
                source = "kalshi"

        if external_price is None:
            return {
                "is_efficient": False,
                "polymarket_price": polymarket_price,
                "external_price": None,
                "price_gap": 0.0,
                "source": "unknown",
                "recommendation": "PROCEED",
            }

        gap = abs(polymarket_price - external_price)
        is_efficient = gap < EFFICIENCY_THRESHOLD

        return {
            "is_efficient": is_efficient,
            "polymarket_price": polymarket_price,
            "external_price": external_price,
            "price_gap": round(gap, 4),
            "source": source,
            "recommendation": "SKIP_EFFICIENT" if is_efficient else "PROCEED",
        }


# =============================================================================
# Standalone test: python agent_efficiency.py --test
# =============================================================================

if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if "--test" not in sys.argv:
        print("Usage: python agent_efficiency.py --test")
        sys.exit(0)

    print("=== EfficiencyChecker standalone test ===\n")
    checker = EfficiencyChecker()

    test_cases = [
        ("Will the Oklahoma City Thunder win the NBA Finals?", 0.38),
        ("Will the LA Lakers win tonight?", 0.55),
        ("Will Bitcoin reach $100k by end of 2025?", 0.42),
        ("Will the Kansas City Chiefs win Super Bowl?", 0.30),
    ]

    for question, price in test_cases:
        result = checker.check(question, price)
        print(f"Q: {question[:60]}")
        print(f"   Polymarket: {price:.0%} | External: {result['external_price']} ({result['source']})")
        print(f"   Gap: {result['price_gap']:.0%} | -> {result['recommendation']}")
        print()
