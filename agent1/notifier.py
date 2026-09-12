"""
notifier.py — Telegram Benachrichtigungen + Command Handler

Sendet detaillierte Push-Nachrichten und empfaengt Bot-Befehle.

Bot-Befehle (in Telegram):
  /status      — Offene Positionen, unrealisierter P&L, Tagesstatistiken
  /performance — Win-Rate der letzten 30 Tage nach Kategorie
  /stop        — Trading pausieren (DRY RUN temporaer aktivieren)
  /resume      — Trading wiederaufnehmen
  /portfolio   — Aktueller Portfolio-Stand, Peak, Drawdown

Der Command-Handler laueft als Background-Thread (start_command_listener()).
"""

import logging
import requests
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import config
from agent import AgentDecision, TradeAction

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """
    Sendet Telegram-Nachrichten via Bot API und empfaengt Befehle.
    Alle Methoden sind fail-safe: Fehler stoppen den Agent nicht.
    """

    def __init__(self):
        self.bot_token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.enabled = bool(self.bot_token and self.chat_id)
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"

        # Command Handler State
        self._command_thread: Optional[threading.Thread] = None
        self._stop_command_listener = threading.Event()
        self._trading_paused = threading.Event()  # Wenn gesetzt: Trading pausiert
        self._update_offset: int = 0

        # Referenzen fuer Command Handler
        self._db = None
        self._risk_manager = None
        self._position_monitor = None
        self._adaptive_config = None

        if self.enabled:
            logger.info("Telegram Notifier aktiv")
        else:
            logger.info("Telegram Notifier deaktiviert (kein Token/Chat-ID)")

    # =========================================================================
    # Basis-Messaging
    # =========================================================================

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Sendet eine Nachricht. Returns True wenn erfolgreich."""
        if not self.enabled:
            return False

        try:
            response = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if response.status_code == 200:
                return True
            logger.warning(f"Telegram {response.status_code}: {response.text[:200]}")
            return False
        except Exception as e:
            logger.warning(f"Telegram send fehlgeschlagen: {e}")
            return False

    # =========================================================================
    # Standard-Benachrichtigungen
    # =========================================================================

    def notify_agent_start(self, dry_run: bool):
        # Suppressed — replaced by 6h summary reports
        pass

    def notify_bet_placed(
        self,
        decision: AgentDecision,
        market_condition_id: str,
        actual_bet_usdc: float,
        order_id: str,
        dry_run: bool,
        sources_count: int = 0,
        tier12_count: int = 0,
        source_quality: float = 0.0,
    ):
        if not config.NOTIFY_ON_BET:
            return

        action_symbol = "YES" if decision.action == TradeAction.BET_YES else "NO"
        dry_text = " [DRY RUN]" if dry_run else ""

        source_rating = (
            "Exzellent" if tier12_count >= 5 else
            "Gut" if tier12_count >= 3 else
            "Ausreichend" if tier12_count >= 1 else
            "Gering"
        )

        factors_text = "\n".join(f"  + {f}" for f in decision.key_factors[:3]) or "  (keine)"
        criteria_text = ""
        if hasattr(decision, 'criteria_scores') and decision.criteria_scores:
            scores = decision.criteria_scores
            criteria_lines = []
            for k, v in scores.items():
                if isinstance(v, dict):
                    criteria_lines.append(f"  {k[:20]}: {v.get('score', '?')}/10")
            criteria_text = "\n<b>Kriterien-Scores:</b>\n" + "\n".join(criteria_lines[:4])

        cp_text = ""
        if hasattr(decision, 'cross_platform_info') and decision.cross_platform_info:
            cp_text = f"\n<b>Cross-Platform:</b>\n  {decision.cross_platform_info}"

        text = (
            f"<b>Neuer Bet{dry_text}</b>\n\n"
            f"<b>Markt:</b>\n{decision.market_question[:180]}\n\n"
            f"<b>Position:</b> {action_symbol} | <b>Einsatz:</b> {actual_bet_usdc:.2f} USDC\n\n"
            f"<b>Analyse:</b>\n"
            f"  Score: {getattr(decision, 'weighted_score', 0):.1f}/10\n"
            f"  Marktpreis: {decision.market_yes_probability:.1%} YES\n"
            f"  Agent-Schaetzung: {decision.agent_yes_probability:.1%} YES\n"
            f"  Edge: {decision.edge:+.1%} | Konfidenz: {decision.confidence:.0%}\n\n"
            f"<b>Quellen:</b> {sources_count} gesamt | Tier 1-2: {tier12_count} ({source_rating})\n"
            f"Gewichteter Score: {source_quality:.0%}"
            f"{cp_text}"
            f"{criteria_text}\n\n"
            f"<b>Hauptfaktoren:</b>\n{factors_text}\n\n"
            f"<b>Begruendung:</b>\n{decision.reasoning[:400]}\n\n"
            f"<b>Order ID:</b> <code>{order_id[:20]}</code>"
        )
        self.send_message(text)

    def notify_skip_details(self, market_question: str, skip_reason: str,
                             has_contradiction: bool, tier12_count: int):
        # Suppressed — skip noise, 6h report covers summary
        pass

    def notify_resolution(self, market_question: str, action: str,
                           bet_usdc: float, pnl_usdc: float, won: bool):
        # Suppressed — covered by 6h report
        pass

    def notify_daily_summary(self, date: str, bets_placed: int, wins: int,
                              losses: int, total_invested: float,
                              realized_pnl: float, api_cost: float):
        # Suppressed — replaced by 6h reports
        pass

    def notify_error(self, component: str, error: str, critical: bool = False):
        if not config.NOTIFY_ON_ERROR:
            return
        severity = "KRITISCHER FEHLER" if critical else "Fehler"
        text = (
            f"<b>{severity}</b>\n\n"
            f"Komponente: <code>{component}</code>\n"
            f"Zeit: {self._now()}\n\n"
            f"<code>{error[:500]}</code>"
        )
        self.send_message(text)

    def notify_emergency_stop(self, reason: str):
        text = (
            f"<b>EMERGENCY STOP</b>\n\n"
            f"Grund: {reason}\n"
            f"Zeit: {self._now()}\n\n"
            f"Manueller Reset erforderlich."
        )
        self.send_message(text)

    def notify_drawdown_stop(self, pnl_usdc: float, stop_until: str):
        text = (
            f"<b>DRAWDOWN STOP (10% Portfolio)</b>\n\n"
            f"Tagesverlust: {pnl_usdc:.2f} USDC\n"
            f"Pausiert bis: {stop_until}\n\n"
            f"Automatisch wieder aktiv nach 24h."
        )
        self.send_message(text)

    def notify_run_complete(self, markets_analyzed: int, bets_placed: int,
                             skipped: int, contradictions_found: int = 0,
                             insufficient_sources: int = 0, api_cost: float = 0.0):
        # Suppressed — replaced by 6h summary reports
        pass

    def send_6h_report(
        self,
        report_time: str,
        stats_6h: dict,
        new_bets: list,
        open_positions: list,
        portfolio: dict,
        dry_run: bool = False,
        wallet_summary: list = None,
        loss_analysis: dict = None,
        groq_usage: dict = None,
    ):
        """
        Sends the 6-hour summary report (at 00, 06, 12, 18 UTC).

        stats_6h:       {runs, markets_analyzed, bets_placed, skips, api_cost_usd}
        new_bets:       [{question, action, bet_usdc, confidence, entry_price}]
        open_positions: [{question, action, bet_usdc, entry_price, current_price, pnl_pct}]
        portfolio:      {initial, current, pnl_today, open_count, wins, losses}
        wallet_summary: [{address, rank, position_count, top_position, conviction}]
        loss_analysis:  {total_losses, total_pnl, high_conf_losses, worst_categories, summary}
        groq_usage:     {requests_made, tokens_used, key2_requests_made, key2_tokens_used}
        """
        if not self.enabled:
            return

        prefix = "[DRY RUN] " if dry_run else ""
        model_name = "Groq (kostenlos)" if config.GROQ_API_KEY else "Claude"
        div = "━━━━━━━━━━━━━━━━━━━━━━"

        text = (
            f"{div}\n"
            f"<b>{prefix}AGENT REPORT — {report_time}</b>\n"
            f"{div}\n\n"
            f"<b>LETZTE 6 STUNDEN</b>\n"
            f"- Runs: {stats_6h.get('runs', 0)} | "
            f"Märkte: {stats_6h.get('markets_analyzed', 0)}\n"
            f"- Bets: {stats_6h.get('bets_placed', 0)} | "
            f"Skips: {stats_6h.get('skips', 0)}\n"
            f"- API Kosten: ${stats_6h.get('api_cost_usd', 0.0):.4f} ({model_name})\n\n"
        )

        # New positions this period
        if new_bets:
            text += "<b>NEUE POSITIONEN</b>\n"
            for bet in new_bets[:6]:
                action_label = "YES" if "YES" in str(bet.get("action", "")).upper() else "NO"
                text += (
                    f"+ {bet['question'][:45]} "
                    f"— {bet['bet_usdc']:.2f} USDC @ {bet['confidence']:.0%} "
                    f"({action_label})\n"
                )
        else:
            text += "<b>NEUE POSITIONEN</b>\nKeine neuen Positionen\n"

        text += "\n"

        # All open positions
        if open_positions:
            text += f"<b>OFFENE POSITIONEN ({len(open_positions)})</b>\n"
            for pos in open_positions[:8]:
                action_label = "YES" if "YES" in str(pos.get("action", "")).upper() else "NO"
                entry = pos.get("entry_price") or 0.0
                current = pos.get("current_price")
                q = pos.get("question", "")[:42]
                if current is not None:
                    diff = current - entry
                    warn = " (!)" if diff < -0.05 else ""
                    text += (
                        f"- {q} ({action_label}): "
                        f"{entry:.0%} -> {current:.0%} ({diff:+.0%}){warn}\n"
                    )
                else:
                    text += f"- {q} ({action_label}): Entry {entry:.0%}\n"
        else:
            text += "<b>OFFENE POSITIONEN</b>\nKeine\n"

        text += "\n"

        # Portfolio summary
        initial = portfolio.get("initial", 0.0)
        current_val = portfolio.get("current", initial)
        pnl_today = portfolio.get("pnl_today", 0.0)
        open_count = portfolio.get("open_count", 0)
        wins = portfolio.get("wins", 0)
        losses = portfolio.get("losses", 0)
        total_change = current_val - initial
        total_change_pct = (total_change / max(initial, 0.01)) * 100

        text += (
            f"<b>PORTFOLIO</b>\n"
            f"- Start: {initial:.2f} USDC\n"
            f"- Aktuell: {current_val:.2f} USDC ({total_change_pct:+.1f}%)\n"
            f"- Heute P&L: {pnl_today:+.2f} USDC\n"
            f"- Offene Positionen: {open_count}\n"
            f"- Gewonnen: {wins} | Verloren: {losses} | Offen: {open_count}\n"
        )

        # Smart Money wallet activity
        if wallet_summary:
            text += f"\n<b>SMART MONEY ({len(wallet_summary)} Wallets aktiv)</b>\n"
            for i, w in enumerate(wallet_summary[:5], 1):
                addr = w.get("address", "")[:10]
                name = w.get("name", "")
                pos_count = w.get("positions", 0)
                profit = w.get("profit", 0.0)
                display = name if name else f"{addr}..."
                text += f"- #{i} {display}: {pos_count} Positionen | ${profit:,.0f} Profit\n"
        else:
            text += "\n<b>SMART MONEY</b>\nKeine Wallet-Daten verfuegbar\n"

        # Loss analysis
        if loss_analysis and loss_analysis.get("total_losses", 0) > 0:
            total_l = loss_analysis.get("total_losses", 0)
            total_pnl = loss_analysis.get("total_pnl", 0.0)
            high_conf = loss_analysis.get("high_conf_losses", 0)
            worst_cats = loss_analysis.get("worst_categories", [])
            text += f"\n<b>VERLUST-ANALYSE ({total_l} Verluste)</b>\n"
            text += f"- Gesamt P&L: {total_pnl:+.2f} USDC\n"
            if high_conf > 0:
                text += f"- Hochkonfidenz-Verluste: {high_conf} (!)  \n"
            if worst_cats:
                cats_str = ", ".join(
                    f"{c['category']} ({c['losses']}x)" for c in worst_cats[:3]
                )
                text += f"- Schwaeche-Kategorien: {cats_str}\n"

        # Groq usage
        if groq_usage:
            req1 = groq_usage.get("requests", 0)
            tok1 = groq_usage.get("tokens", 0)
            req2 = groq_usage.get("key2_requests", 0)
            tok2 = groq_usage.get("key2_tokens", 0)
            text += f"\n<b>GROQ USAGE (heute)</b>\n"
            text += f"- Key 1: {req1} Requests | {tok1:,} Tokens\n"
            if req2 > 0 or tok2 > 0:
                text += f"- Key 2: {req2} Requests | {tok2:,} Tokens\n"

        text += div

        self.send_message(text)

    def notify_shutdown(self, open_positions: int, last_run: str):
        """Graceful-Shutdown Benachrichtigung."""
        text = (
            f"<b>Agent wird heruntergefahren</b>\n\n"
            f"Zeit: {self._now()}\n"
            f"Offene Positionen: {open_positions}\n"
            f"Letzter Run: {last_run or 'unbekannt'}"
        )
        self.send_message(text)

    def notify_learning_update(self, message: str):
        """Learning Engine Update Benachrichtigung."""
        text = f"<b>Learning Update</b>\n\n{message}\n{self._now()}"
        self.send_message(text)

    # =========================================================================
    # Telegram Command Handler (Background Thread)
    # =========================================================================

    def start_command_listener(self, db, risk_manager, position_monitor=None,
                               adaptive_config=None):
        """
        Startet den Command-Handler als Background-Thread.

        Args:
            db: TradeLogger fuer /performance
            risk_manager: RiskManager fuer /status, /portfolio
            position_monitor: PositionMonitor fuer unrealisierten P&L
            adaptive_config: AdaptiveConfig fuer /config
        """
        if not self.enabled:
            logger.info("Command Listener nicht gestartet (Telegram nicht konfiguriert)")
            return

        self._db = db
        self._risk_manager = risk_manager
        self._position_monitor = position_monitor
        self._adaptive_config = adaptive_config

        self._stop_command_listener.clear()
        self._command_thread = threading.Thread(
            target=self._command_listener_loop,
            name="TelegramCommandListener",
            daemon=True,
        )
        self._command_thread.start()
        logger.info("Telegram Command Listener gestartet")

    def stop_command_listener(self):
        """Stoppt den Command-Handler Thread."""
        self._stop_command_listener.set()
        if self._command_thread:
            self._command_thread.join(timeout=5)
            logger.info("Telegram Command Listener gestoppt")

    def is_trading_paused(self) -> bool:
        """Prueft ob Trading via /stop-Befehl pausiert ist."""
        return self._trading_paused.is_set()

    def _command_listener_loop(self):
        """Haupt-Loop: Pollt Telegram auf neue Updates."""
        logger.debug("Command Listener Loop gestartet")
        while not self._stop_command_listener.is_set():
            try:
                updates = self._get_updates()
                for update in updates:
                    self._handle_update(update)
            except Exception as e:
                logger.debug(f"Command Listener Fehler: {e}")

            # 3 Sekunden warten zwischen Polls
            self._stop_command_listener.wait(timeout=3)

    def _get_updates(self) -> list:
        """Holt neue Telegram-Updates."""
        try:
            resp = requests.get(
                f"{self.base_url}/getUpdates",
                params={
                    "offset": self._update_offset,
                    "timeout": 2,
                    "allowed_updates": ["message"],
                },
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                updates = data.get("result", [])
                if updates:
                    self._update_offset = updates[-1]["update_id"] + 1
                return updates
        except Exception:
            pass
        return []

    def _handle_update(self, update: dict):
        """Verarbeitet eine eingehende Telegram-Nachricht."""
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "").strip()

        # Nur vom konfigurierten Chat akzeptieren
        if chat_id != str(self.chat_id):
            return

        if not text.startswith("/"):
            return

        command = text.split()[0].lower()
        logger.info(f"Telegram-Befehl: {command}")

        try:
            if command == "/status":
                self._cmd_status()
            elif command == "/performance":
                self._cmd_performance()
            elif command == "/stop":
                self._cmd_stop()
            elif command == "/resume":
                self._cmd_resume()
            elif command == "/portfolio":
                self._cmd_portfolio()
            elif command == "/config":
                self._cmd_config()
            elif command == "/health":
                self._cmd_health()
            elif command == "/help":
                self._cmd_help()
        except Exception as e:
            logger.error(f"Command-Fehler bei {command}: {e}")
            self.send_message(f"Fehler bei {command}: {str(e)[:200]}")

    def _cmd_status(self):
        """Antwort auf /status."""
        if not self._risk_manager:
            self.send_message("Risk Manager nicht verfuegbar")
            return

        status = self._risk_manager.get_status()
        daily = status.get("daily_stats", {})

        # Unrealisierter P&L
        unrealized_text = ""
        if self._position_monitor:
            try:
                pnl_data = self._position_monitor.get_unrealized_pnl_summary()
                unrealized = pnl_data.get("total_unrealized_pnl", 0)
                open_count = pnl_data.get("open_count", 0)
                unrealized_text = f"Unrealisiert: {unrealized:+.2f} USDC ({open_count} Positionen)\n"
            except Exception:
                pass

        # Offene Positionen
        positions_text = ""
        positions = status.get("positions", {})
        if positions:
            positions_text = "\n<b>Offene Positionen:</b>\n"
            for cid, pos in list(positions.items())[:5]:
                positions_text += f"  {pos['action']}: {pos['bet_usdc']:.1f} USDC - {pos['question'][:40]}\n"

        paused_text = " [PAUSIERT]" if self.is_trading_paused() else ""
        recovery_text = " [RECOVERY]" if status.get("in_recovery_mode") else ""

        text = (
            f"<b>Agent Status{paused_text}{recovery_text}</b>\n"
            f"{self._now()}\n\n"
            f"<b>Heute ({daily.get('date', '?')}):</b>\n"
            f"  Bets: {daily.get('bets_placed', 0)} | "
            f"Win: {daily.get('wins', 0)} / Loss: {daily.get('losses', 0)}\n"
            f"  Investiert: {daily.get('total_invested_usdc', 0):.2f} USDC\n"
            f"  P&L: {daily.get('realized_pnl_usdc', 0):+.2f} USDC\n"
            f"{unrealized_text}\n"
            f"<b>Risiko:</b>\n"
            f"  Kelly: {status.get('kelly_multiplier', 0.25):.0%} | "
            f"Streak: +{status.get('consecutive_wins', 0)}W/-{status.get('consecutive_losses', 0)}L\n"
            f"  Emergency Stop: {'JA' if status.get('emergency_stop') else 'Nein'}\n"
            f"  Drawdown Stop: {'JA' if status.get('drawdown_stop_active') else 'Nein'}"
            f"{positions_text}"
        )
        self.send_message(text)

    def _cmd_performance(self):
        """Antwort auf /performance."""
        if not self._db:
            self.send_message("Datenbank nicht verfuegbar")
            return

        perf = self._db.get_performance_summary(days=30)
        by_cat = self._db.get_performance_by_category(days=30)

        t = perf.get("trades", {})
        f = perf.get("financials", {})

        text = (
            f"<b>Performance letzte 30 Tage</b>\n\n"
            f"Trades: {t.get('wins', 0)}W / {t.get('losses', 0)}L "
            f"({t.get('win_rate', '0%')} Win-Rate)\n"
            f"P&L: {f.get('total_pnl_usdc', 0):+.2f} USDC "
            f"(ROI: {f.get('roi_pct', '0%')})\n\n"
            f"<b>Nach Kategorie:</b>\n"
        )

        if by_cat:
            for c in by_cat[:8]:
                total = c.get("total_bets", 0)
                wins = c.get("wins", 0)
                wr = wins / max(total, 1)
                pnl = c.get("total_pnl", 0) or 0
                bar = "OK" if wr >= 0.5 else "SCHLECHT"
                text += (
                    f"  {c.get('market_category', '?')}: "
                    f"{wr:.0%} ({total} Bets, {pnl:+.1f} USDC) [{bar}]\n"
                )
        else:
            text += "  Noch keine aufgeloesten Trades"

        # Blacklisted
        blacklisted = self._db.get_blacklisted_categories()
        if blacklisted:
            text += f"\n<b>Blackgelistet:</b> {', '.join(blacklisted)}"

        self.send_message(text)

    def _cmd_stop(self):
        """Antwort auf /stop — Trading pausieren."""
        self._trading_paused.set()
        text = (
            f"<b>Trading PAUSIERT</b>\n"
            f"Zeit: {self._now()}\n\n"
            f"Der Agent analysiert weiterhin, platziert aber keine Bets.\n"
            f"Zum Fortsetzen: /resume"
        )
        self.send_message(text)
        logger.warning("Trading via Telegram-Befehl pausiert")

    def _cmd_resume(self):
        """Antwort auf /resume — Trading wiederaufnehmen."""
        self._trading_paused.clear()
        text = (
            f"<b>Trading FORTGESETZT</b>\n"
            f"Zeit: {self._now()}\n\n"
            f"Der Agent platziert ab jetzt wieder Bets."
        )
        self.send_message(text)
        logger.info("Trading via Telegram-Befehl fortgesetzt")

    def _cmd_portfolio(self):
        """Antwort auf /portfolio."""
        if not self._risk_manager:
            self.send_message("Risk Manager nicht verfuegbar")
            return

        status = self._risk_manager.get_status()
        current = status.get("portfolio_usdc", 0)
        peak = status.get("peak_portfolio_usdc", config.PORTFOLIO_USDC)
        drawdown_pct = status.get("drawdown_from_peak_pct", 0)
        initial = config.PORTFOLIO_USDC
        total_change = current - initial

        text = (
            f"<b>Portfolio</b>\n"
            f"{self._now()}\n\n"
            f"Aktuell: {current:.2f} USDC\n"
            f"Peak: {peak:.2f} USDC\n"
            f"Initial: {initial:.2f} USDC\n\n"
            f"Gesamtveraenderung: {total_change:+.2f} USDC "
            f"({(total_change / max(initial, 0.01)) * 100:+.1f}%)\n"
            f"Drawdown vom Peak: -{drawdown_pct:.1f}%\n\n"
            f"Recovery Mode: {'JA' if status.get('in_recovery_mode') else 'Nein'}\n"
            f"Kelly-Multiplikator: {status.get('kelly_multiplier', 0.25):.0%}"
        )
        self.send_message(text)

    def _cmd_config(self):
        """Antwort auf /config — Zeigt aktuelle adaptive Konfiguration."""
        if not self._adaptive_config:
            self.send_message("AdaptiveConfig nicht verfuegbar")
            return

        entries = self._adaptive_config.get_display()
        lines = []
        for e in entries:
            changed_mark = " *" if e["changed"] else ""
            lines.append(
                f"  <b>{e['key']}</b>{changed_mark}: {e['current']:.4f} "
                f"(Basis: {e['base']:.4f})"
            )
            if e["changed"] and e["reason"]:
                lines.append(f"    -> {e['reason'][:80]} [{e['last_updated']}]")

        text = (
            f"<b>Adaptive Konfiguration</b>\n"
            f"{self._now()}\n\n"
            + "\n".join(lines)
            + "\n\n* = vom Basis-Wert abweichend (durch Learning)"
        )
        self.send_message(text)

    def _cmd_health(self):
        """Antwort auf /health — Systemzustand."""
        lines = [f"<b>System Health</b>\n{self._now()}\n"]

        # DB Check
        if self._db:
            try:
                count = self._db.get_learning_data_count()
                last_run = self._db.get_last_successful_run()
                lines.append(f"DB: OK | Learning-Daten: {count}")
                lines.append(f"Letzter Run: {last_run or 'noch keiner'}")
            except Exception as e:
                lines.append(f"DB: Fehler ({e})")
        else:
            lines.append("DB: nicht verbunden")

        # Risk Manager
        if self._risk_manager:
            try:
                status = self._risk_manager.get_status()
                lines.append(
                    f"Risk Manager: OK | Kelly: {status.get('kelly_multiplier', 0.25):.0%} | "
                    f"Emergency Stop: {'JA' if status.get('emergency_stop') else 'Nein'}"
                )
            except Exception:
                lines.append("Risk Manager: Fehler")
        else:
            lines.append("Risk Manager: nicht verbunden")

        # Position Monitor
        if self._position_monitor:
            lines.append("Position Monitor: aktiv")
        else:
            lines.append("Position Monitor: nicht verbunden")

        # Adaptive Config
        if self._adaptive_config:
            lines.append("Adaptive Config: aktiv")

        # Trading Status
        lines.append(f"Trading: {'PAUSIERT' if self.is_trading_paused() else 'aktiv'}")

        self.send_message("\n".join(lines))

    def _cmd_help(self):
        """Antwort auf /help."""
        text = (
            f"<b>Verfuegbare Befehle:</b>\n\n"
            f"/status — Aktuelle Positionen und Tagesstatistiken\n"
            f"/performance — Win-Rate der letzten 30 Tage nach Kategorie\n"
            f"/portfolio — Portfolio-Stand, Peak, Drawdown\n"
            f"/config — Adaptive Konfiguration (Lern-Anpassungen)\n"
            f"/health — Systemzustand und letzte Aktivitaet\n"
            f"/stop — Trading pausieren\n"
            f"/resume — Trading fortsetzen\n"
            f"/help — Diese Hilfe"
        )
        self.send_message(text)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    def test_connection(self) -> bool:
        if not self.enabled:
            logger.info("Telegram nicht konfiguriert")
            return True
        result = self.send_message("Polymarket Agent: Verbindungstest OK")
        if result:
            logger.info("Telegram OK")
        else:
            logger.warning("Telegram fehlgeschlagen")
        return result


class NullNotifier:
    """Dummy-Notifier wenn Telegram nicht konfiguriert."""
    def __getattr__(self, name):
        return lambda *args, **kwargs: None
    def is_trading_paused(self) -> bool:
        return False
