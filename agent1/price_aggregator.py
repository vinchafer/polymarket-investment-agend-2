"""
price_aggregator.py — Cross-Platform Preisvergleich
Vergleicht Polymarket-Preise mit Kalshi und Manifold Markets.

Matching-Logik: Fuzzy-String-Matching (>80% Aehnlichkeit) auf Markt-Titel.
Bei Spread >8%: Arbitrage-Chance, Konfidenz-Bonus +0.15
"""

import logging
import requests
from difflib import SequenceMatcher
from typing import Optional

logger = logging.getLogger(__name__)

KALSHI_MARKETS_URL = "https://trading-api.kalshi.com/trade-api/v2/markets"
MANIFOLD_MARKETS_URL = "https://api.manifold.markets/v0/markets"

FUZZY_MATCH_THRESHOLD = 0.80   # Mindest-Aehnlichkeit fuer Match
ARBITRAGE_SPREAD_THRESHOLD = 0.08  # 8% Spread = Arbitrage
CONFIDENCE_BONUS_ARBITRAGE = 0.15
CONFIDENCE_BONUS_SPREAD = 0.05


class PriceAggregator:
    """
    Vergleicht Polymarket-Preise mit anderen Prediction-Market-Plattformen.
    Erkennt Arbitrage-Chancen und liefert Konfidenz-Boosts.
    """

    def __init__(self):
        self._kalshi_cache: Optional[list] = None
        self._manifold_cache: Optional[list] = None
        self._request_timeout = 8

    def compare_market(self, polymarket_question: str, polymarket_yes_price: float) -> dict:
        """
        Versucht denselben Markt auf Kalshi und Manifold zu finden.

        Returns:
            {
              "match_found": bool,
              "kalshi_price": float or None,
              "manifold_price": float or None,
              "kalshi_match": str or None,
              "manifold_match": str or None,
              "max_spread": float,
              "arbitrage_opportunity": bool,
              "confidence_bonus": float,
              "summary": str
            }
        """
        result = {
            "match_found": False,
            "kalshi_price": None,
            "manifold_price": None,
            "kalshi_match": None,
            "manifold_match": None,
            "max_spread": 0.0,
            "arbitrage_opportunity": False,
            "confidence_bonus": 0.0,
            "summary": "No cross-platform data",
        }

        # Kalshi suchen
        kalshi_result = self._find_on_kalshi(polymarket_question)
        manifold_result = self._find_on_manifold(polymarket_question)

        spreads = []

        if kalshi_result:
            result["match_found"] = True
            result["kalshi_price"] = kalshi_result["price"]
            result["kalshi_match"] = kalshi_result["title"][:80]
            spread = abs(kalshi_result["price"] - polymarket_yes_price)
            spreads.append(spread)

        if manifold_result:
            result["match_found"] = True
            result["manifold_price"] = manifold_result["price"]
            result["manifold_match"] = manifold_result["title"][:80]
            spread = abs(manifold_result["price"] - polymarket_yes_price)
            spreads.append(spread)

        if spreads:
            result["max_spread"] = max(spreads)

            if result["max_spread"] >= ARBITRAGE_SPREAD_THRESHOLD:
                result["arbitrage_opportunity"] = True
                result["confidence_bonus"] = CONFIDENCE_BONUS_ARBITRAGE
            elif result["max_spread"] >= 0.05:
                result["confidence_bonus"] = CONFIDENCE_BONUS_SPREAD

            # Summary aufbauen
            parts = []
            if result["kalshi_price"] is not None:
                parts.append(f"Kalshi: {result['kalshi_price']:.1%}")
            if result["manifold_price"] is not None:
                parts.append(f"Manifold: {result['manifold_price']:.1%}")
            parts.append(f"Polymarket: {polymarket_yes_price:.1%}")

            result["summary"] = " | ".join(parts)
            if result["arbitrage_opportunity"]:
                result["summary"] += f" | ARBITRAGE: {result['max_spread']:.1%} Spread"

        return result

    def _find_on_kalshi(self, question: str) -> Optional[dict]:
        """Sucht einen aehnlichen Markt auf Kalshi."""
        try:
            markets = self._get_kalshi_markets()
            best_match = self._find_best_match(question, markets, title_key="title", price_key="yes_price")
            return best_match
        except Exception as e:
            logger.debug(f"Kalshi-Suche fehlgeschlagen: {e}")
            return None

    def _find_on_manifold(self, question: str) -> Optional[dict]:
        """Sucht einen aehnlichen Markt auf Manifold Markets."""
        try:
            markets = self._get_manifold_markets()
            best_match = self._find_best_match(question, markets, title_key="question", price_key="probability")
            return best_match
        except Exception as e:
            logger.debug(f"Manifold-Suche fehlgeschlagen: {e}")
            return None

    def _get_kalshi_markets(self) -> list:
        """Laedt aktive Kalshi-Maerkte (gecacht)."""
        if self._kalshi_cache is not None:
            return self._kalshi_cache

        try:
            resp = requests.get(
                KALSHI_MARKETS_URL,
                params={"limit": 200, "status": "open"},
                timeout=self._request_timeout,
                headers={"Accept": "application/json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                # Kalshi gibt {"markets": [...]} zurueck
                markets = data.get("markets", data if isinstance(data, list) else [])
                # Normalisiere Preise: Kalshi nutzt yes_bid/yes_ask
                normalized = []
                for m in markets:
                    try:
                        yes_bid = float(m.get("yes_bid", 0) or 0)
                        yes_ask = float(m.get("yes_ask", 1) or 1)
                        yes_price = (yes_bid + yes_ask) / 2
                        normalized.append({
                            "title": m.get("title", m.get("subtitle", "")),
                            "yes_price": yes_price,
                            "ticker": m.get("ticker", ""),
                        })
                    except Exception:
                        continue
                self._kalshi_cache = normalized
                logger.debug(f"Kalshi: {len(normalized)} Maerkte geladen")
                return normalized
            else:
                logger.debug(f"Kalshi API: {resp.status_code}")
        except requests.Timeout:
            logger.debug("Kalshi API Timeout")
        except Exception as e:
            logger.debug(f"Kalshi API Fehler: {e}")

        self._kalshi_cache = []
        return []

    def _get_manifold_markets(self) -> list:
        """Laedt aktive Manifold-Maerkte (gecacht)."""
        if self._manifold_cache is not None:
            return self._manifold_cache

        try:
            resp = requests.get(
                MANIFOLD_MARKETS_URL,
                params={"limit": 200},
                timeout=self._request_timeout,
                headers={"Accept": "application/json"},
            )
            if resp.status_code == 200:
                markets = resp.json()
                # Manifold gibt direkt eine Liste zurueck
                normalized = []
                for m in (markets if isinstance(markets, list) else []):
                    try:
                        prob = float(m.get("probability", 0.5) or 0.5)
                        question = m.get("question", "")
                        if question and m.get("isResolved") is False:
                            normalized.append({
                                "question": question,
                                "yes_price": prob,
                                "id": m.get("id", ""),
                            })
                    except Exception:
                        continue
                self._manifold_cache = normalized
                logger.debug(f"Manifold: {len(normalized)} Maerkte geladen")
                return normalized
            else:
                logger.debug(f"Manifold API: {resp.status_code}")
        except requests.Timeout:
            logger.debug("Manifold API Timeout")
        except Exception as e:
            logger.debug(f"Manifold API Fehler: {e}")

        self._manifold_cache = []
        return []

    def _find_best_match(
        self,
        query: str,
        markets: list,
        title_key: str,
        price_key: str,
    ) -> Optional[dict]:
        """
        Findet den besten Treffer in einer Markt-Liste via Fuzzy-Matching.
        Gibt None zurueck wenn Aehnlichkeit unter FUZZY_MATCH_THRESHOLD.
        """
        if not markets:
            return None

        query_lower = query.lower()
        best_score = 0.0
        best_market = None

        for market in markets:
            title = market.get(title_key, "")
            if not title:
                continue

            score = SequenceMatcher(None, query_lower, title.lower()).ratio()
            if score > best_score:
                best_score = score
                best_market = market

        if best_score >= FUZZY_MATCH_THRESHOLD and best_market:
            return {
                "title": best_market.get(title_key, ""),
                "price": best_market.get(price_key, 0.5),
                "similarity": best_score,
            }

        return None

    def clear_cache(self):
        """Leert den Cache (nach jedem Analyse-Durchlauf aufrufen)."""
        self._kalshi_cache = None
        self._manifold_cache = None
