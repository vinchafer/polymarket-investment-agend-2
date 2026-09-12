"""
polymarket.py — Polymarket CLOB API Wrapper
Kapselt alle Interaktionen mit der Polymarket API:
- Authentifizierung (L1 + L2)
- Märkte abrufen & filtern
- Order-Buch lesen
- Orders platzieren & stornieren
- Portfolio-Status abrufen
"""

import json
import logging
import requests
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass

GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    OrderArgs,
    OrderType,
    MarketOrderArgs,
    TradeParams,
)
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.constants import POLYGON

import config

logger = logging.getLogger(__name__)


# =============================================================================
# Datenklassen
# =============================================================================

@dataclass
class MarketInfo:
    """Zusammenfassung eines Polymarket-Markts."""
    condition_id: str
    question: str
    description: str
    category: str
    end_date: str
    days_to_resolution: int
    # YES Token
    yes_token_id: str
    yes_price: float       # aktueller Preis = implizierte Wahrscheinlichkeit (0-1)
    yes_best_bid: float
    yes_best_ask: float
    # NO Token
    no_token_id: str
    no_price: float
    # Markt-Metriken
    volume_24h: float
    liquidity: float
    active: bool


@dataclass
class Position:
    """Offene Position im Portfolio."""
    condition_id: str
    question: str
    token_id: str
    side: str              # "YES" oder "NO"
    size: float            # Anzahl Shares
    avg_price: float       # Durchschnittlicher Kaufpreis
    current_price: float   # Aktueller Preis
    pnl_usdc: float        # Unrealisierter P&L


@dataclass
class OrderResult:
    """Ergebnis einer platzierten Order."""
    success: bool
    order_id: Optional[str]
    error_message: Optional[str]
    filled_price: Optional[float]
    filled_size: Optional[float]


# =============================================================================
# Haupt-Client
# =============================================================================

class PolymarketClient:
    """
    Vollständiger Wrapper für die Polymarket CLOB API.
    Authentifiziert sich automatisch und verwaltet Credentials.
    """

    def __init__(self):
        self.host = config.POLYMARKET_HOST
        self.private_key = config.POLYMARKET_PRIVATE_KEY
        self.funder = config.POLYMARKET_FUNDER_ADDRESS
        self.chain_id = config.POLYGON_CHAIN_ID
        self._client: Optional[ClobClient] = None
        self._initialized = False

    def initialize(self) -> bool:
        """
        Initialisiert den Client und authentifiziert sich.
        Wird einmal beim Start aufgerufen.
        Returns True wenn erfolgreich.
        """
        try:
            logger.info("Initialisiere Polymarket CLOB Client...")

            # L1-Authentifizierung (mit Private Key)
            self._client = ClobClient(
                host=self.host,
                key=self.private_key,
                chain_id=self.chain_id,
                signature_type=1,   # 1 = EOA (Standard Ethereum Wallet)
                funder=self.funder,
            )

            # L2-Credentials ableiten oder erstellen (für Order-Platzierung benötigt)
            creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(creds)
            logger.info(f"  API Key: {creds.api_key[:8]}...")

            # Verbindung testen
            ok = self._client.get_ok()
            if not ok:
                logger.error("Polymarket API Verbindungstest fehlgeschlagen")
                return False

            self._initialized = True
            logger.info("✅ Polymarket Client erfolgreich initialisiert")
            return True

        except Exception as e:
            logger.error(f"❌ Polymarket Client Initialisierung fehlgeschlagen: {e}")
            return False

    def _ensure_initialized(self):
        """Stellt sicher dass der Client initialisiert ist."""
        if not self._initialized:
            raise RuntimeError("PolymarketClient nicht initialisiert. Rufe zuerst initialize() auf.")

    # -------------------------------------------------------------------------
    # Märkte abrufen
    # -------------------------------------------------------------------------

    def get_filtered_markets(
        self,
        max_results: int = 50,
        min_volume: float = None,
        max_days: int = None,
        min_prob: float = None,
        max_prob: float = None,
        categories: list = None,
        blacklisted_categories: set = None,
        category_win_rates: dict = None,
        smart_money_cids: set = None,
        conviction_map: dict = None,
    ) -> list[MarketInfo]:
        """
        Holt aktive Märkte via Gamma API, filtert und sortiert nach Pre-Filter-Score.
        Gibt die Top max_results Märkte nach Score zurück.

        Args:
            max_results: Maximale Anzahl Märkte (nach Scoring: beste N)
            min_volume: Minimales 24h-Handelsvolumen (USDC)
            max_days: Maximale Tage bis zur Auflösung
            min_prob: Minimale Marktwahrscheinlichkeit (YES-Preis)
            max_prob: Maximale Marktwahrscheinlichkeit (YES-Preis)
            categories: Liste erlaubter Kategorien (None = alle)
            blacklisted_categories: Kategorien die ausgeschlossen werden (schlechte Win-Rate)
            category_win_rates: {category: win_rate} für Scoring-Bonus
            smart_money_cids: Set von condition_ids die von Top-Wallets gehalten werden
            conviction_map: {condition_id: conviction_level} für Scoring-Bonus
        """
        self._ensure_initialized()

        # Defaults aus config
        min_volume = min_volume if min_volume is not None else config.MIN_MARKET_VOLUME
        max_days = max_days or config.MAX_DAYS_TO_RESOLUTION
        min_prob = min_prob if min_prob is not None else config.MIN_MARKET_PROBABILITY
        max_prob = max_prob if max_prob is not None else config.MAX_MARKET_PROBABILITY
        categories = categories or config.PREFERRED_CATEGORIES
        blacklisted_categories = blacklisted_categories or set()
        smart_money_cids = smart_money_cids or set()
        conviction_map = conviction_map or {}

        logger.info(f"Lade Märkte von Polymarket (Gamma API)...")

        try:
            response = requests.get(
                GAMMA_API_URL,
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": min(max_results * 10, 500),  # Genug Kandidaten zum Scoring
                    "order": "volume24hr",
                    "ascending": "false",
                },
                timeout=15,
            )
            response.raise_for_status()
            raw_markets = response.json()
            logger.info(f"  {len(raw_markets)} Rohmarkt-Einträge geladen")
        except Exception as e:
            logger.error(f"Fehler beim Laden der Märkte von Gamma API: {e}")
            return []

        # Märkte parsen und Basis-Filter anwenden (alle Kandidaten sammeln)
        candidates = []
        now = datetime.now(timezone.utc)

        for raw in raw_markets:
            try:
                market = self._parse_gamma_market(raw, now)
                if market is None:
                    continue

                # Basis-Filter
                if min_volume and market.volume_24h < min_volume:
                    continue
                # Bypass max_days for wallet-tracked markets (smart money may hold long-term)
                is_wallet_tracked = market.condition_id in smart_money_cids
                if not is_wallet_tracked and market.days_to_resolution > max_days:
                    continue
                if market.yes_price < min_prob or market.yes_price > max_prob:
                    continue
                if categories and market.category.lower() not in [c.lower() for c in categories]:
                    continue

                candidates.append(market)

            except Exception as e:
                logger.debug(f"Fehler beim Parsen eines Markts: {e}")
                continue

        # Pre-Filter-Scoring: alle Kandidaten bewerten und sortieren
        scored = []
        for market in candidates:
            score = self._score_market_prefilter(
                market,
                blacklisted_categories=blacklisted_categories,
                category_win_rates=category_win_rates,
                smart_money_cids=smart_money_cids,
                conviction_map=conviction_map,
            )
            if score >= 0:  # -1 = blacklisted, ausschliessen
                scored.append((score, market))

        scored.sort(key=lambda x: x[0], reverse=True)
        markets = [m for _, m in scored[:max_results]]

        if scored:
            top_score = scored[0][0]
            logger.info(
                f"  {len(candidates)} Kandidaten -> {len(scored)} nach Blacklist-Filter "
                f"-> Top {len(markets)} nach Pre-Filter-Score (beste: {top_score}/100)"
            )
        else:
            logger.info(f"  {len(candidates)} Kandidaten, keine nach Scoring übrig")

        return markets

    @staticmethod
    def _detect_category_from_title(title: str) -> str:
        """Maps market title keywords to category names."""
        t = title.lower()
        if any(kw in t for kw in ["nba", "lakers", "celtics", "thunder", "76ers", "pistons", "spread"]):
            return "NBA"
        if any(kw in t for kw in ["nhl", "stanley cup", "maple leafs", "hurricanes"]):
            return "NHL"
        if any(kw in t for kw in ["nfl", "super bowl"]):
            return "NFL"
        if any(kw in t for kw in ["soccer", " fc ", "real madrid", "arsenal"]):
            return "Soccer"
        if any(kw in t for kw in ["bitcoin", "btc", "eth", "crypto"]):
            return "Crypto"
        if any(kw in t for kw in ["election", "president", "senate"]):
            return "Politics"
        if any(kw in t for kw in ["tennis", " open "]):
            return "Tennis"
        # Default: first capitalized word from the title
        words = [w.strip("?.,!") for w in title.split() if len(w) > 2 and w[0].isupper()]
        return words[0] if words else "other"

    def _score_market_prefilter(
        self,
        market: "MarketInfo",
        blacklisted_categories: set = None,
        category_win_rates: dict = None,
        smart_money_cids: set = None,
        conviction_map: dict = None,
    ) -> int:
        """
        Bewertet einen Markt 0-100 für die Pre-Filter-Auswahl.
        Höher = besserer Kandidat für die teure API-Analyse.
        Gibt -1 zurück wenn der Markt ausgeschlossen werden soll (Blacklist).

        Kriterien:
          Volume    (0-30): Höheres Volumen = liquider Markt
          Liquidity (0-20): Mehr Liquidität = geringerer Spread
          Timing    (0-25): Kurz vor/nach Auflösung = mehr Edge
          Price zone(0-15): Nicht bei Extremen; Edge-Zonen bevorzugt
          Category  (0-10): Historische Win-Rate Bonus
          Smart money bonuses (additional):
            +20: Market matches active top wallet position
            +15: HIGH conviction (3+ wallets agree)
            +10: NHL/NBA championship market keyword
        """
        blacklisted_categories = blacklisted_categories or set()
        category_win_rates = category_win_rates or {}
        smart_money_cids = smart_money_cids or set()
        conviction_map = conviction_map or {}

        # Blacklisted: sofort ausschliessen
        if market.category.lower() in {c.lower() for c in blacklisted_categories}:
            return -1

        score = 0

        # Volume score (0-30)
        vol = market.volume_24h
        if vol >= 200_000:
            score += 30
        elif vol >= 50_000:
            score += 25
        elif vol >= 10_000:
            score += 15
        elif vol >= 1_000:
            score += 8
        else:
            score += 2

        # Liquidity score (0-20)
        liq = market.liquidity
        if liq >= 20_000:
            score += 20
        elif liq >= 5_000:
            score += 12
        elif liq >= 1_000:
            score += 6
        else:
            score += 1

        # Timing score (0-25)
        d = market.days_to_resolution
        if d < 0:         # past event (already resolved, high certainty)
            score += 25
        elif d == 0:
            score += 22
        elif d <= 3:
            score += 18
        elif d <= 7:
            score += 12
        elif d <= 14:
            score += 7
        elif d <= 30:
            score += 4
        else:
            score += 1

        # Price zone score (0-15): interesting zones, avoid near-certainty
        p = market.yes_price
        if 0.05 <= p <= 0.95:
            distance_from_50 = abs(p - 0.5)
            if distance_from_50 >= 0.25:   # 75%+ or 25%- price zones
                score += 15
            elif distance_from_50 >= 0.10:
                score += 10
            else:
                score += 5
        # else: near certainty (< 5% or > 95%), add nothing

        # Category history score (0-10)
        cat_lower = market.category.lower()
        win_rate = next(
            (v for k, v in category_win_rates.items() if k.lower() == cat_lower),
            None,
        )
        if win_rate is not None:
            if win_rate >= 0.70:
                score += 10
            elif win_rate >= 0.55:
                score += 6
            elif win_rate >= 0.45:
                score += 3
            # else: near blacklist threshold, add nothing
        else:
            score += 5  # unbekannte Kategorie: neutral

        # Smart money bonus
        cid = market.condition_id
        if cid in smart_money_cids:
            score += 20  # Active top wallet position
            conviction = conviction_map.get(cid, "LOW")
            if conviction == "HIGH":
                score += 15  # 3+ wallets agree

        # NHL/NBA championship keyword bonus
        q_lower = market.question.lower()
        if any(kw in q_lower for kw in ("nhl", "nba", "stanley cup", "nba champion", "nba finals")):
            score += 10

        return score

    def _parse_gamma_market(self, raw: dict, now: datetime) -> Optional[MarketInfo]:
        """Parst einen Gamma-API-Market-Response in ein MarketInfo-Objekt."""
        try:
            # Nur Märkte die Orders akzeptieren
            if not raw.get("acceptingOrders") or raw.get("closed") or not raw.get("active"):
                return None

            # Token IDs (JSON-String oder bereits Liste)
            clob_token_ids = raw.get("clobTokenIds", "[]")
            if isinstance(clob_token_ids, str):
                clob_token_ids = json.loads(clob_token_ids)
            if len(clob_token_ids) < 2:
                return None

            # Preise (JSON-String oder bereits Liste)
            outcome_prices = raw.get("outcomePrices", '["0.5","0.5"]')
            if isinstance(outcome_prices, str):
                outcome_prices = json.loads(outcome_prices)

            yes_token_id = clob_token_ids[0]
            no_token_id = clob_token_ids[1]
            yes_price = float(outcome_prices[0]) if outcome_prices else 0.5
            no_price = float(outcome_prices[1]) if len(outcome_prices) > 1 else 1.0 - yes_price

            # Auflösungsdatum berechnen
            end_date_str = raw.get("endDateIso", "") or raw.get("endDate", "")
            days_to_resolution = 999
            if end_date_str:
                try:
                    end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    # Falls nur Datum ohne Zeit: auf UTC Mitternacht normalisieren
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    days_to_resolution = max(0, (end_dt - now).days)
                except Exception:
                    pass

            # Kategorie aus events ableiten (falls vorhanden)
            events = raw.get("events", [])
            category = "unknown"
            if events and isinstance(events, list):
                tag = events[0].get("tag") or ""
                category = tag if tag else "unknown"

            # Fallback: keyword-based category from market title
            if category == "unknown":
                category = PolymarketClient._detect_category_from_title(
                    raw.get("question", "")
                )

            best_bid = float(raw.get("bestBid") or yes_price - 0.01)
            best_ask = float(raw.get("bestAsk") or yes_price + 0.01)

            return MarketInfo(
                condition_id=raw.get("conditionId", ""),
                question=raw.get("question", ""),
                description=raw.get("description", ""),
                category=category,
                end_date=end_date_str,
                days_to_resolution=days_to_resolution,
                yes_token_id=yes_token_id,
                yes_price=yes_price,
                yes_best_bid=best_bid,
                yes_best_ask=best_ask,
                no_token_id=no_token_id,
                no_price=no_price,
                volume_24h=float(raw.get("volume24hr") or 0),
                liquidity=float(raw.get("liquidityClob") or 0),
                active=True,
            )

        except Exception as e:
            logger.debug(f"Parse-Fehler (Gamma): {e}")
            return None

    # -------------------------------------------------------------------------
    # Order-Buch
    # -------------------------------------------------------------------------

    def get_orderbook_spread(self, token_id: str) -> dict:
        """
        Gibt aktuellen Spread und Liquidität für ein Token zurück.
        Nützlich für die finale Entscheidung ob ein Trade sinnvoll ist.
        """
        self._ensure_initialized()
        try:
            book = self._client.get_order_book(token_id)
            if not book:
                return {}

            raw_bids = book.bids or []
            raw_asks = book.asks or []
            bids = sorted([(float(b.price), float(b.size)) for b in raw_bids], reverse=True)
            asks = sorted([(float(a.price), float(a.size)) for a in raw_asks], reverse=True)

            best_bid = bids[0][0] if bids else 0
            best_ask = asks[0][0] if asks else 1
            spread = best_ask - best_bid
            mid = (best_bid + best_ask) / 2

            return {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": spread,
                "mid_price": mid,
                "bid_liquidity": sum(b[1] for b in bids[:5]),
                "ask_liquidity": sum(a[1] for a in asks[:5]),
            }

        except Exception as e:
            logger.error(f"Fehler beim Laden des Order-Buchs für {token_id}: {e}")
            return {}

    # -------------------------------------------------------------------------
    # Orders platzieren
    # -------------------------------------------------------------------------

    def place_market_order(
        self,
        token_id: str,
        side: str,            # "YES" oder "NO"
        amount_usdc: float,   # Betrag in USDC
        dry_run: bool = None,
    ) -> OrderResult:
        """
        Platziert eine Market Order (sofortige Ausführung zum besten verfügbaren Preis).

        Args:
            token_id: Token ID (YES oder NO token)
            side: "BUY" (Einstieg) oder "SELL" (Ausstieg)
            amount_usdc: Betrag in USDC
            dry_run: Überschreibt config.DRY_RUN wenn angegeben
        """
        self._ensure_initialized()

        if dry_run is None:
            dry_run = config.DRY_RUN

        if dry_run:
            logger.info(f"[DRY RUN] Würde Order platzieren: {side} {amount_usdc} USDC für Token {token_id[:16]}...")
            return OrderResult(
                success=True,
                order_id=f"dry_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                error_message=None,
                filled_price=None,
                filled_size=None,
            )

        try:
            logger.info(f"Platziere Market Order: {side} {amount_usdc} USDC...")

            mo = MarketOrderArgs(
                token_id=token_id,
                amount=amount_usdc,
                side=BUY if side == "BUY" else SELL,
            )

            signed_order = self._client.create_market_order(mo)
            resp = self._client.post_order(signed_order, OrderType.FOK)

            if resp and resp.get("success"):
                order_id = resp.get("orderID", "unknown")
                logger.info(f"  ✅ Order erfolgreich: {order_id}")
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    error_message=None,
                    filled_price=resp.get("price"),
                    filled_size=resp.get("size"),
                )
            else:
                msg = resp.get("errorMsg", "Unbekannter Fehler") if resp else "Keine Antwort"
                logger.warning(f"  ❌ Order fehlgeschlagen: {msg}")
                return OrderResult(success=False, order_id=None, error_message=msg,
                                   filled_price=None, filled_size=None)

        except Exception as e:
            logger.error(f"Fehler beim Platzieren der Order: {e}")
            return OrderResult(success=False, order_id=None, error_message=str(e),
                               filled_price=None, filled_size=None)

    def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        dry_run: bool = None,
    ) -> OrderResult:
        """
        Platziert eine Limit Order (bestimmter Preis, nicht sofort ausgeführt).
        Besser für bessere Ausführungspreise, kann aber nicht gefüllt werden.

        Args:
            token_id: Token ID
            side: "BUY" oder "SELL"
            price: Gewünschter Preis (0-1, z.B. 0.65 = 65 Cents)
            size: Menge in USDC
        """
        self._ensure_initialized()

        if dry_run is None:
            dry_run = config.DRY_RUN

        if dry_run:
            logger.info(f"[DRY RUN] Würde Limit Order platzieren: {side} {size} USDC @ {price:.3f}")
            return OrderResult(
                success=True,
                order_id=f"dry_run_limit_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                error_message=None,
                filled_price=price,
                filled_size=size,
            )

        try:
            logger.info(f"Platziere Limit Order: {side} {size} USDC @ {price:.3f}...")

            order_args = OrderArgs(
                price=price,
                size=size,
                side=BUY if side == "BUY" else SELL,
                token_id=token_id,
            )

            signed_order = self._client.create_order(order_args)
            resp = self._client.post_order(signed_order, OrderType.GTC)  # Good-Till-Cancelled

            if resp and resp.get("success"):
                order_id = resp.get("orderID", "unknown")
                logger.info(f"  ✅ Limit Order platziert: {order_id}")
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    error_message=None,
                    filled_price=price,
                    filled_size=size,
                )
            else:
                msg = resp.get("errorMsg", "Unbekannter Fehler") if resp else "Keine Antwort"
                return OrderResult(success=False, order_id=None, error_message=msg,
                                   filled_price=None, filled_size=None)

        except Exception as e:
            logger.error(f"Fehler beim Platzieren der Limit Order: {e}")
            return OrderResult(success=False, order_id=None, error_message=str(e),
                               filled_price=None, filled_size=None)

    # -------------------------------------------------------------------------
    # Portfolio & Positionen
    # -------------------------------------------------------------------------

    def get_portfolio_balance(self) -> dict:
        """Gibt aktuellen USDC-Balance zurück."""
        self._ensure_initialized()
        try:
            # Trades und Balance abfragen
            trades = self._client.get_trades(TradeParams(maker_address=self.funder))
            balance_info = {
                "available_usdc": 0.0,
                "total_trades": len(trades) if trades else 0,
            }
            return balance_info
        except Exception as e:
            logger.error(f"Fehler beim Laden des Portfolio-Balance: {e}")
            return {"available_usdc": 0.0, "total_trades": 0}

    def get_open_orders(self) -> list:
        """Gibt alle offenen Orders zurück."""
        self._ensure_initialized()
        try:
            return self._client.get_orders() or []
        except Exception as e:
            logger.error(f"Fehler beim Laden offener Orders: {e}")
            return []

    def cancel_all_orders(self) -> bool:
        """Storniert alle offenen Orders (Notfall-Funktion)."""
        self._ensure_initialized()
        try:
            resp = self._client.cancel_all()
            logger.info(f"Alle Orders storniert: {resp}")
            return True
        except Exception as e:
            logger.error(f"Fehler beim Stornieren aller Orders: {e}")
            return False

    def get_market_by_question(self, question_contains: str) -> Optional[MarketInfo]:
        """Sucht einen Markt anhand eines Fragebestandteils (für Tests nützlich)."""
        markets = self.get_filtered_markets(max_results=100)
        for m in markets:
            if question_contains.lower() in m.question.lower():
                return m
        return None
