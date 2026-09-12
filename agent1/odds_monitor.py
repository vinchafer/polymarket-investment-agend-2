"""
odds_monitor.py — Echtzeit-Kursbewegungserkennnung

Laueft als eigener leichtgewichtiger Prozess/Thread.
Alle 15 Minuten: Pruefe alle aktiven Maerkte auf Kursbewegungen.

Signale:
  >5%  Bewegung  -> Sofortige Analyse ausloesen (kein Warten auf 2h-Zyklus)
  >15% Bewegung  -> Sofortiger Telegram-Alert "SHARP MOVEMENT"
  3x   >5% gleiche Richtung -> Signal-Boost +0.10 auf naechste Analyse

Standalone-Aufruf: python odds_monitor.py
"""

import logging
import sys
import time
from datetime import datetime, timezone

import config
from polymarket import PolymarketClient

logger = logging.getLogger(__name__)

MOVEMENT_THRESHOLD_ALERT = 0.05    # 5%: Analyse ausloesen
MOVEMENT_THRESHOLD_TELEGRAM = 0.20 # 20%: Sofort-Alert
SIGNAL_BOOST_BONUS = 0.10          # Konfidenz-Bonus bei 3x gleiche Richtung
SIGNAL_BOOST_THRESHOLD = 3         # Anzahl Bewegungen gleiche Richtung fuer Boost
CHECK_INTERVAL_SECONDS = 900       # 15 Minuten


class OddsMonitor:
    """
    Ueberwacht Kursbewegungen auf Polymarket.
    Schreibt alle Erkenntnisse in die gemeinsame SQLite-Datenbank.
    """

    def __init__(self, db, notifier=None, analysis_callback=None):
        """
        Args:
            db: TradeLogger Instanz
            notifier: TelegramNotifier (optional)
            analysis_callback: Callable(market_condition_id) fuer sofortige Analyse
        """
        self.db = db
        self.notifier = notifier
        self.analysis_callback = analysis_callback
        self._polymarket = PolymarketClient()
        self._running = False
        # In-Memory: Letzte bekannte Preise fuer schnellen Vergleich
        self._last_prices: dict[str, float] = {}
        # Bewegungsrichtungs-Tracking fuer Signal-Boost
        self._direction_counts: dict[str, dict] = {}  # {cid: {"up": N, "down": N}}

    def check_movements(self) -> dict:
        """
        Fuehrt einen einzelnen Check aller aktiven Maerkte durch.

        Returns:
            {
              "markets_checked": int,
              "movements_detected": int,
              "alerts_sent": int,
              "analyses_triggered": int,
              "signal_boosts": int
            }
        """
        stats = {
            "markets_checked": 0,
            "movements_detected": 0,
            "alerts_sent": 0,
            "analyses_triggered": 0,
            "signal_boosts": 0,
        }

        try:
            # Aktive Maerkte laden
            markets = self._polymarket.get_filtered_markets(
                max_results=200,
                min_volume=config.MIN_MARKET_VOLUME,
            )
            stats["markets_checked"] = len(markets)

            for market in markets:
                cid = market.condition_id
                current_price = market.yes_price
                question = market.question[:100]

                # Letzten bekannten Preis aus DB oder In-Memory holen
                last_price = self._last_prices.get(cid)
                if last_price is None:
                    # Erste Sichtung: aus DB laden
                    last_price = self.db.get_last_known_price(cid)

                if last_price is None:
                    # Unbekannter Markt: nur speichern, kein Vergleich
                    self._last_prices[cid] = current_price
                    continue

                movement = current_price - last_price
                movement_pct = abs(movement)

                if movement_pct < MOVEMENT_THRESHOLD_ALERT:
                    # Keine signifikante Bewegung
                    self._last_prices[cid] = current_price
                    continue

                # Signifikante Bewegung erkannt
                direction = "up" if movement > 0 else "down"
                stats["movements_detected"] += 1

                # Richtungs-Tracking
                if cid not in self._direction_counts:
                    self._direction_counts[cid] = {"up": 0, "down": 0}
                self._direction_counts[cid][direction] += 1
                consecutive = self._direction_counts[cid][direction]

                # Signal-Boost pruefen
                if consecutive >= SIGNAL_BOOST_THRESHOLD:
                    stats["signal_boosts"] += 1
                    logger.info(
                        f"SIGNAL BOOST: '{question[:50]}' bewegt sich {direction} "
                        f"{consecutive}x in Folge. Boost: +{SIGNAL_BOOST_BONUS:.0%}"
                    )

                # In Datenbank speichern
                self.db.log_odds_movement(
                    market_condition_id=cid,
                    market_question=question,
                    old_price=last_price,
                    new_price=current_price,
                    consecutive_count=consecutive,
                    triggered_analysis=(movement_pct >= MOVEMENT_THRESHOLD_ALERT),
                )

                logger.info(
                    f"BEWEGUNG: '{question[:50]}' "
                    f"{last_price:.1%} -> {current_price:.1%} "
                    f"({movement:+.1%}) [{direction.upper()}]"
                )

                # Analyse ausloesen (>5%)
                if movement_pct >= MOVEMENT_THRESHOLD_ALERT:
                    stats["analyses_triggered"] += 1
                    if self.analysis_callback:
                        try:
                            self.analysis_callback(cid, market)
                        except Exception as e:
                            logger.error(f"Analyse-Callback fehlgeschlagen: {e}")

                # Preis aktualisieren
                self._last_prices[cid] = current_price
                # Gegenrichtungs-Counter zuruecksetzen
                opposite = "down" if direction == "up" else "up"
                self._direction_counts[cid][opposite] = 0

        except Exception as e:
            logger.error(f"Fehler bei Odds-Check: {e}", exc_info=True)

        return stats

    def _send_sharp_movement_alert(
        self,
        question: str,
        old_price: float,
        new_price: float,
        movement_pct: float,
        consecutive: int,
    ):
        """Sendet einen sofortigen Telegram-Alert fuer starke Kursbewegungen."""
        direction_text = "gestiegen" if new_price > old_price else "gefallen"
        direction_arrow = "UP" if new_price > old_price else "DOWN"

        text = (
            f"<b>SHARP MOVEMENT: {direction_arrow} {movement_pct:.1%}</b>\n\n"
            f"{question}\n\n"
            f"Alt: {old_price:.1%} -> Neu: {new_price:.1%}\n"
            f"Bewegung: {new_price - old_price:+.1%} ({direction_text})\n"
        )

        if consecutive >= SIGNAL_BOOST_THRESHOLD:
            text += f"\n{consecutive}x in Folge diese Richtung - starkes Signal!\n"
            text += f"Naechste Analyse erhaelt Konfidenz-Bonus +{SIGNAL_BOOST_BONUS:.0%}"

        text += f"\nZeit: {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')}"

        if self.notifier:
            self.notifier.send_message(text)
        logger.warning(f"Alert gesendet: {text[:100]}")

    def get_signal_boost(self, market_condition_id: str) -> float:
        """
        Gibt den Signal-Boost fuer einen Markt zurueck.
        Wird von der Analyse-Engine abgefragt.

        Returns:
            0.0 (kein Boost) oder SIGNAL_BOOST_BONUS
        """
        counts = self._direction_counts.get(market_condition_id, {})
        max_count = max(counts.get("up", 0), counts.get("down", 0))
        return SIGNAL_BOOST_BONUS if max_count >= SIGNAL_BOOST_THRESHOLD else 0.0

    def run_once(self) -> dict:
        """Fuehrt einen einzelnen Check-Zyklus aus."""
        logger.info(f"Odds-Check: {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
        stats = self.check_movements()
        logger.info(
            f"Odds-Check: {stats['markets_checked']} Maerkte | "
            f"{stats['movements_detected']} Bewegungen | "
            f"{stats['alerts_sent']} Alerts | "
            f"{stats['analyses_triggered']} Analysen"
        )
        return stats

    def run_continuous(self, interval_seconds: int = CHECK_INTERVAL_SECONDS):
        """
        Laeuft kontinuierlich alle interval_seconds Sekunden.
        Kann als separater Thread gestartet werden.
        """
        self._running = True
        logger.info(f"Odds-Monitor gestartet (Intervall: {interval_seconds}s)")

        while self._running:
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"Odds-Monitor Fehler: {e}")

            # Warten
            for _ in range(interval_seconds):
                if not self._running:
                    break
                time.sleep(1)

        logger.info("Odds-Monitor gestoppt")

    def stop(self):
        """Stoppt die kontinuierliche Schleife."""
        self._running = False


# =============================================================================
# Standalone-Betrieb
# =============================================================================

if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(__file__))

    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    from logger import TradeLogger
    from notifier import TelegramNotifier

    db = TradeLogger()
    notifier = TelegramNotifier()

    client = PolymarketClient()
    if not client.initialize():
        logger.error("Polymarket-Initialisierung fehlgeschlagen")
        sys.exit(1)

    monitor = OddsMonitor(db=db, notifier=notifier)
    monitor._polymarket = client

    logger.info("Odds Monitor laeuft standalone...")
    monitor.run_continuous()
