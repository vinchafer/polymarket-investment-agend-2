"""
position_monitor.py — Offene Positionen Ueberwachung

Laueft als eigener leichtgewichtiger Prozess/Thread.
Alle 30 Minuten: Pruefe alle offenen Positionen.

Aktionen:
  >20% adverse Bewegung  -> Telegram-Warnung "POSITION WARNING"
  yes_price >= 0.98      -> Market resolved YES (automatische P&L-Buchung)
  yes_price <= 0.02      -> Market resolved NO (automatische P&L-Buchung)
  Unrealisierter P&L     -> In Echtzeit berechnen

Standalone-Aufruf: python position_monitor.py
"""

import logging
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import config
from polymarket import PolymarketClient

logger = logging.getLogger(__name__)

ADVERSE_MOVEMENT_THRESHOLD = 0.20    # 20% Gegenbewegung = Warnung
RESOLUTION_THRESHOLD_YES = 0.98      # >= 98%: als YES aufgeloest behandeln
RESOLUTION_THRESHOLD_NO = 0.02       # <= 2%: als NO aufgeloest behandeln
CHECK_INTERVAL_SECONDS = 1800        # 30 Minuten


class PositionMonitor:
    """
    Ueberwacht alle offenen Positionen in Echtzeit.
    Erkennt adverse Kursbewegungen und automatische Marktaufloesungen.
    """

    def __init__(self, db, risk_manager=None, notifier=None):
        """
        Args:
            db: TradeLogger Instanz
            risk_manager: RiskManager fuer P&L-Updates (optional)
            notifier: TelegramNotifier fuer Warnungen (optional)
        """
        self.db = db
        self.risk_manager = risk_manager
        self.notifier = notifier
        self._polymarket = PolymarketClient()
        self._running = False
        self._warned_positions: set[str] = set()  # Bereits gewarnter Markt

    def check_positions(self) -> dict:
        """
        Prueft alle offenen Positionen auf Kursbewegungen und Aufloesungen.

        Returns:
            {
              "positions_checked": int,
              "warnings_sent": int,
              "resolved_count": int,
              "total_unrealized_pnl": float
            }
        """
        stats = {
            "positions_checked": 0,
            "warnings_sent": 0,
            "resolved_count": 0,
            "total_unrealized_pnl": 0.0,
        }

        # Offene Trades aus DB laden
        open_trades = self.db.get_open_trades()
        if not open_trades:
            return stats

        stats["positions_checked"] = len(open_trades)
        logger.info(f"Positionscheck: {len(open_trades)} offene Positionen")

        for trade in open_trades:
            condition_id = trade.get("market_condition_id", "")
            question = trade.get("market_question", "")[:80]
            action = trade.get("action", "")
            entry_price = float(trade.get("entry_price", 0.5) or 0.5)
            bet_usdc = float(trade.get("bet_usdc", 0) or 0)

            if not condition_id:
                continue

            # Aktuellen Preis abrufen
            current_price = self._get_current_price(condition_id)
            if current_price is None:
                logger.debug(f"Kein aktueller Preis fuer {condition_id[:16]}")
                continue

            is_bet_yes = (action == "BET_YES")

            # === AUFLOESUNGS-ERKENNUNG ===
            resolved = False
            won = False
            pnl_usdc = 0.0

            if current_price >= RESOLUTION_THRESHOLD_YES:
                resolved = True
                won = is_bet_yes  # BET_YES gewinnt wenn YES aufgeloest
                pnl_usdc = self._calculate_pnl(bet_usdc, entry_price, won, current_price)
                outcome = "YES"
                logger.info(f"Auto-Resolution YES: '{question}'")

            elif current_price <= RESOLUTION_THRESHOLD_NO:
                resolved = True
                won = not is_bet_yes  # BET_NO gewinnt wenn NO aufgeloest
                pnl_usdc = self._calculate_pnl(bet_usdc, entry_price, won, current_price)
                outcome = "NO"
                logger.info(f"Auto-Resolution NO: '{question}'")

            if resolved:
                stats["resolved_count"] += 1
                # P&L in DB buchen
                self.db.log_resolution(condition_id, outcome, pnl_usdc, won)
                # Risk Manager informieren
                if self.risk_manager:
                    self.risk_manager.register_resolution(condition_id, won, pnl_usdc)
                # Telegram-Benachrichtigung
                if self.notifier:
                    self.notifier.notify_resolution(
                        market_question=question,
                        action=action,
                        bet_usdc=bet_usdc,
                        pnl_usdc=pnl_usdc,
                        won=won,
                    )
                continue

            # === ADVERSE MOVEMENT CHECK ===
            # Berechne wie stark sich der Preis gegen uns bewegt hat
            if is_bet_yes:
                adverse_movement = entry_price - current_price  # YES bet: fallender Preis ist schlecht
            else:
                adverse_movement = current_price - entry_price  # NO bet: steigender Preis ist schlecht

            # Only warn for real-money positions (dry_run=0)
            is_dry_run = bool(trade.get("dry_run", 1))

            if adverse_movement >= ADVERSE_MOVEMENT_THRESHOLD and not is_dry_run:
                # Noch nicht gewarnt fuer diese Position
                if condition_id not in self._warned_positions:
                    self._warned_positions.add(condition_id)
                    stats["warnings_sent"] += 1
                    self._send_position_warning(
                        question=question,
                        action=action,
                        entry_price=entry_price,
                        current_price=current_price,
                        adverse_movement=adverse_movement,
                        bet_usdc=bet_usdc,
                    )

            # === UNREALISIERTER P&L ===
            unrealized = self._calculate_unrealized_pnl(
                bet_usdc=bet_usdc,
                entry_price=entry_price,
                current_price=current_price,
                is_bet_yes=is_bet_yes,
            )
            stats["total_unrealized_pnl"] += unrealized

        if stats["positions_checked"] > 0:
            logger.info(
                f"Positionscheck: {stats['resolved_count']} aufgeloest | "
                f"{stats['warnings_sent']} Warnungen | "
                f"Unrealisiert: {stats['total_unrealized_pnl']:+.2f} USDC"
            )

        return stats

    def _get_current_price(self, condition_id: str) -> Optional[float]:
        """Holt den aktuellen YES-Preis fuer einen Markt."""
        try:
            # Zuerst aus der DB (Odds-Monitor aktualisiert diese)
            last_price = self.db.get_last_known_price(condition_id)
            if last_price is not None:
                return last_price

            # Direkt von Polymarket holen (teurer aber aktuell)
            # (Vereinfacht: via gefilterte Maerkte)
            markets = self._polymarket.get_filtered_markets(max_results=1000)
            for m in markets:
                if m.condition_id == condition_id:
                    return m.yes_price

        except Exception as e:
            logger.debug(f"Preis-Abruf fehlgeschlagen fuer {condition_id[:16]}: {e}")
        return None

    def _calculate_pnl(
        self,
        bet_usdc: float,
        entry_price: float,
        won: bool,
        resolution_price: float,
    ) -> float:
        """Berechnet den P&L bei Marktauflosung."""
        if entry_price <= 0:
            return 0.0

        shares = bet_usdc / entry_price
        if won:
            # Jeder Share wird zu $1 aufgeloest
            return round(shares - bet_usdc, 2)  # Gewinn = (1 - entry_price) * shares
        else:
            return round(-bet_usdc, 2)  # Vollstaendiger Verlust

    def _calculate_unrealized_pnl(
        self,
        bet_usdc: float,
        entry_price: float,
        current_price: float,
        is_bet_yes: bool,
    ) -> float:
        """Berechnet den unrealisierten P&L basierend auf aktuellem Preis."""
        if entry_price <= 0:
            return 0.0

        shares = bet_usdc / entry_price
        current_value = shares * current_price if is_bet_yes else shares * (1 - current_price)
        return round(current_value - bet_usdc, 2)

    def _send_position_warning(
        self,
        question: str,
        action: str,
        entry_price: float,
        current_price: float,
        adverse_movement: float,
        bet_usdc: float,
    ):
        """Sendet eine Positions-Warnung via Telegram."""
        direction = "gefallen" if action == "BET_YES" else "gestiegen"
        text = (
            f"<b>POSITION WARNING: {adverse_movement:.1%} adverse Bewegung</b>\n\n"
            f"{question}\n\n"
            f"Position: {action}\n"
            f"Einstieg: {entry_price:.1%} | Aktuell: {current_price:.1%}\n"
            f"Bewegung gegen uns: -{adverse_movement:.1%}\n"
            f"Einsatz: {bet_usdc:.2f} USDC\n\n"
            f"Preis ist {direction}. Manuelle Ueberpruefung empfohlen.\n"
            f"Zeit: {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')}"
        )

        if self.notifier:
            self.notifier.send_message(text)
        logger.warning(f"Positions-Warnung: '{question[:50]}' bewegt sich {adverse_movement:.1%} gegen uns")

    def get_unrealized_pnl_summary(self) -> dict:
        """Berechnet den Gesamt-unrealisierten P&L aller offenen Positionen."""
        open_trades = self.db.get_open_trades()
        total_unrealized = 0.0
        positions = []

        for trade in open_trades:
            condition_id = trade.get("market_condition_id", "")
            current_price = self._get_current_price(condition_id)
            if current_price is None:
                continue

            entry_price = float(trade.get("entry_price", 0.5) or 0.5)
            bet_usdc = float(trade.get("bet_usdc", 0) or 0)
            is_bet_yes = (trade.get("action") == "BET_YES")

            unrealized = self._calculate_unrealized_pnl(
                bet_usdc=bet_usdc,
                entry_price=entry_price,
                current_price=current_price,
                is_bet_yes=is_bet_yes,
            )
            total_unrealized += unrealized

            positions.append({
                "question": trade.get("market_question", "")[:60],
                "action": trade.get("action"),
                "entry_price": entry_price,
                "current_price": current_price,
                "bet_usdc": bet_usdc,
                "unrealized_pnl": unrealized,
            })

        return {
            "total_unrealized_pnl": round(total_unrealized, 2),
            "open_count": len(positions),
            "positions": positions,
        }

    def run_once(self) -> dict:
        """Fuehrt einen einzelnen Check-Zyklus aus."""
        logger.info(f"Positions-Check: {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
        return self.check_positions()

    def run_continuous(self, interval_seconds: int = CHECK_INTERVAL_SECONDS):
        """
        Laeuft kontinuierlich alle interval_seconds Sekunden.
        Kann als separater Thread gestartet werden.
        """
        self._running = True
        logger.info(f"Position Monitor gestartet (Intervall: {interval_seconds}s)")

        while self._running:
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"Position Monitor Fehler: {e}")

            for _ in range(interval_seconds):
                if not self._running:
                    break
                time.sleep(1)

        logger.info("Position Monitor gestoppt")

    def stop(self):
        self._running = False


# =============================================================================
# Standalone-Betrieb
# =============================================================================

if __name__ == "__main__":
    import os
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

    monitor = PositionMonitor(db=db, notifier=notifier)
    monitor._polymarket = client

    logger.info("Position Monitor laeuft standalone...")
    monitor.run_continuous()
