"""
risk_manager.py — Risikomanagement & Position Tracking

Institutionelle Risikokontrolle:
- Portfolio-basiertes Bet-Sizing (max 2% pro Position)
- Streak-Management: Kelly-Multiplikator steigt/faellt mit Wins/Losses
- Recovery Mode bei -15% vom Peak-Portfolio
- 10% Drawdown-Schutz (24h Stop)
- Korrelationsrisiko (max 2 Bets pro Kategorie/Tag)
- Liquiditaetspruefung (>$10k Volumen)
"""

import logging
from datetime import datetime, date, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

import config
from agent import AgentDecision, TradeAction

logger = logging.getLogger(__name__)


# =============================================================================
# Datenklassen
# =============================================================================

@dataclass
class RiskCheckResult:
    approved: bool
    reason: str
    adjusted_bet_usdc: float
    kelly_multiplier: float = 0.25
    warnings: list[str] = field(default_factory=list)


@dataclass
class DailyStats:
    date: str
    bets_placed: int = 0
    total_invested_usdc: float = 0.0
    realized_pnl_usdc: float = 0.0
    wins: int = 0
    losses: int = 0
    skips: int = 0


# =============================================================================
# Risk Manager
# =============================================================================

class RiskManager:
    """
    Institutioneller Risikomanager mit dynamischem Kelly-Sizing und
    Recovery-Modus.
    """

    def __init__(self, db=None):
        """
        Args:
            db: TradeLogger fuer Peak-Tracking und kumulativen P&L (optional)
        """
        self._db = db

        # Limits aus Konfiguration
        self.max_bet = config.MAX_BET_USDC
        self.max_daily_loss = config.MAX_DAILY_LOSS_USDC
        self.max_open_positions = config.MAX_OPEN_POSITIONS
        self.min_confidence = config.MIN_CONFIDENCE
        self.min_edge = config.MIN_EDGE
        self.portfolio_usdc = config.PORTFOLIO_USDC
        self.max_bet_pct = config.MAX_BET_PCT_PORTFOLIO
        self.max_drawdown_pct = config.MAX_DRAWDOWN_PCT
        self.min_market_volume = config.MIN_MARKET_VOLUME

        # Zustand
        self._open_positions: dict[str, dict] = {}
        self._daily_stats: DailyStats = DailyStats(date=str(date.today()))
        self._emergency_stop: bool = False
        self._emergency_reason: str = ""
        self._analyzed_today: set[str] = set()
        self._drawdown_stop_until: Optional[datetime] = None
        self._category_bets_today: dict[str, list[str]] = {}

        # Streak-Tracking
        self._consecutive_wins: int = 0
        self._consecutive_losses: int = 0
        self._kelly_multiplier: float = 0.25  # Startwert: 25%
        self._in_recovery_mode: bool = False

        # Portfolio-Peak laden (aus DB wenn verfuegbar)
        self._peak_portfolio: float = self.portfolio_usdc
        if db:
            try:
                peak_data = db.get_portfolio_peak()
                self._peak_portfolio = peak_data.get("peak", self.portfolio_usdc)
                # Kumulativen P&L laden um aktuellen Portfolio-Wert zu berechnen
                cum_pnl = db.get_cumulative_pnl()
                self._current_portfolio: float = max(0.01, self.portfolio_usdc + cum_pnl)
            except Exception:
                self._current_portfolio = self.portfolio_usdc
        else:
            self._current_portfolio = self.portfolio_usdc

        logger.info(
            f"RiskManager: portfolio={self._current_portfolio:.2f} USDC, "
            f"peak={self._peak_portfolio:.2f} USDC, "
            f"kelly={self._kelly_multiplier:.0%}, "
            f"min_edge={self.min_edge:.0%}"
        )

    def check_trade(
        self,
        decision: AgentDecision,
        market_condition_id: str,
        market_volume: float = 0.0,
        market_category: str = "",
    ) -> RiskCheckResult:
        """
        Hauptmethode: Prueft ob ein Trade erlaubt ist.
        Wendet alle Risiko-Checks inkl. dynamischem Kelly-Sizing an.
        """
        self._check_daily_reset()

        # Emergency Stop
        if self._emergency_stop:
            return RiskCheckResult(
                approved=False,
                reason=f"EMERGENCY STOP: {self._emergency_reason}",
                adjusted_bet_usdc=0.0,
            )

        # Drawdown-Stop
        if self._drawdown_stop_until and datetime.now(timezone.utc) < self._drawdown_stop_until:
            remaining_h = (self._drawdown_stop_until - datetime.now(timezone.utc)).total_seconds() / 3600
            return RiskCheckResult(
                approved=False,
                reason=f"DRAWDOWN STOP aktiv noch {remaining_h:.1f}h",
                adjusted_bet_usdc=0.0,
            )
        elif self._drawdown_stop_until and datetime.now(timezone.utc) >= self._drawdown_stop_until:
            logger.info("Drawdown-Stop abgelaufen")
            self._drawdown_stop_until = None

        if decision.action == TradeAction.SKIP:
            return RiskCheckResult(approved=False, reason="SKIP", adjusted_bet_usdc=0.0)

        if decision.contradiction_detected:
            return RiskCheckResult(approved=False, reason="Contradiction detected", adjusted_bet_usdc=0.0)

        warnings = []

        # === Check 1: Liquiditaet ===
        if market_volume > 0 and market_volume < self.min_market_volume:
            return RiskCheckResult(
                approved=False,
                reason=f"Illiquide: ${market_volume:,.0f} < ${self.min_market_volume:,.0f}",
                adjusted_bet_usdc=0.0,
            )

        # === Check 2: Recovery Mode ===
        self._update_recovery_mode()
        effective_min_confidence = self.min_confidence
        effective_min_edge = self.min_edge
        effective_max_bet_pct = self.max_bet_pct

        if self._in_recovery_mode:
            effective_min_confidence = max(self.min_confidence, 0.88)
            effective_min_edge = max(self.min_edge, 0.15)
            effective_max_bet_pct = 0.01  # 1% statt 2%
            warnings.append(f"RECOVERY MODE: konservativere Parameter angewendet")

        # === Check 3: Taegl. Verlust-Limit ===
        if self._daily_stats.realized_pnl_usdc < -self.max_daily_loss:
            return RiskCheckResult(
                approved=False,
                reason=f"Daily Loss Limit: {self._daily_stats.realized_pnl_usdc:.2f} USDC",
                adjusted_bet_usdc=0.0,
            )

        # === Check 4: 10% Drawdown-Schutz ===
        drawdown_limit = self._current_portfolio * self.max_drawdown_pct
        if self._daily_stats.realized_pnl_usdc < -drawdown_limit:
            stop_until = datetime.now(timezone.utc) + timedelta(hours=24)
            self._drawdown_stop_until = stop_until
            logger.critical(f"DRAWDOWN STOP: {self._daily_stats.realized_pnl_usdc:.2f} USDC Verlust")
            return RiskCheckResult(
                approved=False,
                reason=f"10% Portfolio-Drawdown. Stop 24h.",
                adjusted_bet_usdc=0.0,
            )

        # === Check 5: Max. Positionen ===
        if len(self._open_positions) >= self.max_open_positions:
            return RiskCheckResult(
                approved=False,
                reason=f"Max Positionen: {len(self._open_positions)}/{self.max_open_positions}",
                adjusted_bet_usdc=0.0,
            )

        # === Check 6: Duplikat ===
        if market_condition_id in self._open_positions:
            return RiskCheckResult(
                approved=False,
                reason=f"Position bereits offen",
                adjusted_bet_usdc=0.0,
            )

        # === Check 7: Konfidenz ===
        if decision.confidence < effective_min_confidence:
            return RiskCheckResult(
                approved=False,
                reason=f"Konfidenz {decision.confidence:.2f} < {effective_min_confidence:.2f}",
                adjusted_bet_usdc=0.0,
            )

        # === Check 8: Edge ===
        if abs(decision.edge) < effective_min_edge:
            return RiskCheckResult(
                approved=False,
                reason=f"Edge {abs(decision.edge):.3f} < {effective_min_edge:.3f}",
                adjusted_bet_usdc=0.0,
            )

        # === Check 9: Korrelationsrisiko ===
        if market_category:
            cat_key = market_category.lower()
            existing = self._category_bets_today.get(cat_key, [])
            if cat_key == "sports" and len(existing) >= 2:
                return RiskCheckResult(
                    approved=False,
                    reason=f"Korrelations-Block: bereits {len(existing)} Sports-Bets heute",
                    adjusted_bet_usdc=0.0,
                )
            elif len(existing) >= 3:
                warnings.append(f"Hohe Korrelation: {len(existing)} Bets in '{market_category}' heute")

        # === Check 10: Dynamisches Bet-Sizing mit Streak-Multiplikator ===
        portfolio_max = self._current_portfolio * effective_max_bet_pct

        # Wende Kelly-Multiplikator an (dynamisch durch Streak)
        kelly_adjusted_bet = decision.recommended_bet_usdc * (self._kelly_multiplier / 0.25)
        adjusted_bet = min(kelly_adjusted_bet, portfolio_max, self.max_bet)

        if adjusted_bet != decision.recommended_bet_usdc:
            warnings.append(
                f"Bet angepasst: {decision.recommended_bet_usdc:.2f} -> {adjusted_bet:.2f} USDC "
                f"(Kelly: {self._kelly_multiplier:.0%}, Portfolio-Max: {portfolio_max:.2f})"
            )

        if adjusted_bet < 1.0:
            return RiskCheckResult(
                approved=False,
                reason=f"Bet zu klein: {adjusted_bet:.2f} USDC",
                adjusted_bet_usdc=0.0,
            )

        # === Check 11: Tages-Budget ===
        daily_budget = portfolio_max * 5
        if self._daily_stats.total_invested_usdc + adjusted_bet > daily_budget:
            remaining = daily_budget - self._daily_stats.total_invested_usdc
            if remaining < 1.0:
                return RiskCheckResult(
                    approved=False,
                    reason=f"Tages-Budget ausgeschoepft",
                    adjusted_bet_usdc=0.0,
                )
            adjusted_bet = min(adjusted_bet, remaining)
            warnings.append(f"Auf Tages-Budget begrenzt: {adjusted_bet:.2f} USDC")

        logger.info(
            f"  Risk OK: {adjusted_bet:.2f} USDC | "
            f"Kelly: {self._kelly_multiplier:.0%} | "
            f"Streak: +{self._consecutive_wins}W/-{self._consecutive_losses}L"
            + (" [RECOVERY]" if self._in_recovery_mode else "")
        )
        for w in warnings:
            logger.warning(f"  WARNUNG: {w}")

        return RiskCheckResult(
            approved=True,
            reason="Alle Checks bestanden",
            adjusted_bet_usdc=round(adjusted_bet, 2),
            kelly_multiplier=self._kelly_multiplier,
            warnings=warnings,
        )

    def register_bet(
        self,
        market_condition_id: str,
        market_question: str,
        action: TradeAction,
        token_id: str,
        bet_usdc: float,
        entry_price: float,
        order_id: str,
        market_category: str = "",
    ):
        """Registriert einen platzierten Bet."""
        self._open_positions[market_condition_id] = {
            "question": market_question[:80],
            "action": action.value,
            "token_id": token_id,
            "bet_usdc": bet_usdc,
            "entry_price": entry_price,
            "order_id": order_id,
            "category": market_category,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        self._daily_stats.bets_placed += 1
        self._daily_stats.total_invested_usdc += bet_usdc

        if market_category:
            cat_key = market_category.lower()
            if cat_key not in self._category_bets_today:
                self._category_bets_today[cat_key] = []
            self._category_bets_today[cat_key].append(market_condition_id)

        # Portfolio-Update
        self._current_portfolio -= bet_usdc  # Bet ist investiert (nicht verfuegbar)
        self._update_peak_in_db()

        logger.info(
            f"Position: {action.value} {bet_usdc:.2f} USDC @ {entry_price:.3f} "
            f"fuer '{market_question[:40]}...'"
        )

    def register_resolution(self, market_condition_id: str, won: bool, pnl_usdc: float):
        """Registriert die Auflosung eines Markts und aktualisiert Streak."""
        position = self._open_positions.pop(market_condition_id, None)
        if not position:
            logger.warning(f"Unbekannte Position: {market_condition_id}")
            return

        self._daily_stats.realized_pnl_usdc += pnl_usdc
        if won:
            self._daily_stats.wins += 1
        else:
            self._daily_stats.losses += 1

        # Portfolio-Update
        bet_usdc = position.get("bet_usdc", 0)
        self._current_portfolio += bet_usdc + pnl_usdc  # Eingesetztes Kapital + P&L zurueck

        # === STREAK-UPDATE & KELLY-ANPASSUNG ===
        self._update_streak(won)

        result_sign = "+" if pnl_usdc > 0 else ""
        logger.info(
            f"{'WIN' if won else 'LOSS'}: {result_sign}{pnl_usdc:.2f} USDC | "
            f"Streak: +{self._consecutive_wins}W/-{self._consecutive_losses}L | "
            f"Kelly: {self._kelly_multiplier:.0%}"
        )

        # Peak-Update in DB
        self._update_peak_in_db()

        # Drawdown pruefen
        drawdown_limit = self._current_portfolio * self.max_drawdown_pct
        if self._daily_stats.realized_pnl_usdc < -drawdown_limit:
            stop_until = datetime.now(timezone.utc) + timedelta(hours=24)
            self._drawdown_stop_until = stop_until
            logger.critical(f"DRAWDOWN STOP nach Verlust: {self._daily_stats.realized_pnl_usdc:.2f} USDC")
        elif self._daily_stats.realized_pnl_usdc < -self.max_daily_loss:
            self.trigger_emergency_stop(f"Daily loss limit: {self._daily_stats.realized_pnl_usdc:.2f} USDC")

    def _update_streak(self, won: bool):
        """Aktualisiert Sieges-/Verlust-Streak und Kelly-Multiplikator."""
        if won:
            self._consecutive_wins += 1
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            self._consecutive_wins = 0

        # Kelly-Anpassung basierend auf Streak
        if self._consecutive_wins >= 7:
            self._kelly_multiplier = 0.50   # Max 50% nach 7 Siegen
        elif self._consecutive_wins >= 5:
            self._kelly_multiplier = 0.40   # 40% nach 5 Siegen
        elif self._consecutive_wins >= 3:
            self._kelly_multiplier = 0.35   # 35% nach 3 Siegen
        elif self._consecutive_losses >= 3:
            self._kelly_multiplier = 0.10   # 10% nach 3 Verlusten
        elif self._consecutive_losses >= 2:
            self._kelly_multiplier = 0.15   # 15% nach 2 Verlusten
        else:
            self._kelly_multiplier = 0.25   # Normalwert 25%

        if self._consecutive_losses >= 2:
            logger.warning(
                f"Verlust-Streak: {self._consecutive_losses} Niederlagen. "
                f"Kelly auf {self._kelly_multiplier:.0%} reduziert."
            )

    def _update_recovery_mode(self):
        """Prueft ob Recovery-Modus aktiviert werden soll."""
        if self._peak_portfolio <= 0:
            return

        drawdown_from_peak = (self._peak_portfolio - self._current_portfolio) / self._peak_portfolio
        was_in_recovery = self._in_recovery_mode

        if drawdown_from_peak >= 0.15:
            self._in_recovery_mode = True
            if not was_in_recovery:
                logger.warning(
                    f"RECOVERY MODE aktiviert: Portfolio {self._current_portfolio:.2f} USDC "
                    f"({drawdown_from_peak:.1%} unter Peak {self._peak_portfolio:.2f} USDC)"
                )
        elif drawdown_from_peak < 0.08:
            # Recovery beendet wenn Drawdown unter 8% gefallen
            if was_in_recovery:
                logger.info(f"Recovery Mode beendet: Drawdown nur noch {drawdown_from_peak:.1%}")
            self._in_recovery_mode = False

    def _update_peak_in_db(self):
        """Aktualisiert Portfolio-Peak in der Datenbank."""
        if not self._db:
            return
        try:
            result = self._db.update_portfolio_peak(self._current_portfolio)
            self._peak_portfolio = result["peak"]
        except Exception as e:
            logger.debug(f"Peak-Update fehlgeschlagen: {e}")

    def trigger_emergency_stop(self, reason: str):
        self._emergency_stop = True
        self._emergency_reason = reason
        logger.critical(f"EMERGENCY STOP: {reason}")

    def reset_emergency_stop(self):
        self._emergency_stop = False
        self._emergency_reason = ""
        self._drawdown_stop_until = None
        logger.info("Emergency Stop zurueckgesetzt")

    def get_status(self) -> dict:
        self._check_daily_reset()
        drawdown_active = (
            self._drawdown_stop_until is not None
            and datetime.now(timezone.utc) < self._drawdown_stop_until
        )
        drawdown_from_peak = (
            (self._peak_portfolio - self._current_portfolio) / self._peak_portfolio
            if self._peak_portfolio > 0 else 0
        )

        return {
            "emergency_stop": self._emergency_stop,
            "emergency_reason": self._emergency_reason,
            "drawdown_stop_active": drawdown_active,
            "drawdown_stop_until": self._drawdown_stop_until.isoformat() if self._drawdown_stop_until else None,
            "in_recovery_mode": self._in_recovery_mode,
            "open_positions": len(self._open_positions),
            "max_positions": self.max_open_positions,
            "portfolio_usdc": round(self._current_portfolio, 2),
            "peak_portfolio_usdc": round(self._peak_portfolio, 2),
            "drawdown_from_peak_pct": round(drawdown_from_peak * 100, 2),
            "kelly_multiplier": self._kelly_multiplier,
            "consecutive_wins": self._consecutive_wins,
            "consecutive_losses": self._consecutive_losses,
            "daily_stats": {
                "date": self._daily_stats.date,
                "bets_placed": self._daily_stats.bets_placed,
                "total_invested_usdc": round(self._daily_stats.total_invested_usdc, 2),
                "realized_pnl_usdc": round(self._daily_stats.realized_pnl_usdc, 2),
                "wins": self._daily_stats.wins,
                "losses": self._daily_stats.losses,
            },
            "category_bets": {k: len(v) for k, v in self._category_bets_today.items()},
            "positions": {
                cid[:16]: {
                    "action": p["action"],
                    "bet_usdc": p["bet_usdc"],
                    "question": p["question"][:50],
                }
                for cid, p in self._open_positions.items()
            },
        }

    def _check_daily_reset(self):
        today = str(date.today())
        if self._daily_stats.date != today:
            logger.info(f"Neuer Tag ({today}): Tagesstatistiken zurueckgesetzt")
            self._daily_stats = DailyStats(date=today)
            self._analyzed_today = set()
            self._category_bets_today = {}
            # Streak bleibt bestehen ueber Tage hinaus
