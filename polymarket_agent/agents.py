from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .clients import ExternalIntelClient, LLMClient, PolymarketDataClient, TelegramClient
from .db import Database, utc_now
from .models import AnalystDecision, PositionSignal
from .settings import Settings
from .wallet_curator import refresh_tracked_wallets


class AgentSystem:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.pm_data = PolymarketDataClient(settings.polymarket_data_api, timeout=settings.api_timeout_seconds)
        self.intel = ExternalIntelClient(
            the_odds_api_key=settings.the_odds_api_key,
            news_api_key=settings.news_api_key,
            serper_api_key=settings.serper_api_key,
            tavily_api_key=settings.tavily_api_key,
            espn_api_base=settings.espn_api_base,
            timeout=settings.api_timeout_seconds,
        )
        self.llm = LLMClient(
            gemini_api_key=settings.gemini_api_key,
            groq_api_key=settings.groq_api_key,
            gemini_model=settings.gemini_model,
            groq_model=settings.groq_model,
            timeout=settings.llm_timeout_seconds,
            db=self.db,
            max_retries=settings.llm_max_retries,
        )
        self.telegram = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)

    def run_wallet_curator(self) -> None:
        """Refresh top-N proxy wallets from Polymarket leaderboard (Data API)."""
        refresh_tracked_wallets(
            self.db,
            base_url=self.settings.polymarket_data_api,
            limit=self.settings.scout_max_wallets,
            time_period=self.settings.leaderboard_time_period,
            category=self.settings.leaderboard_category,
            order_by=self.settings.leaderboard_order_by,
            timeout=self.settings.api_timeout_seconds,
        )

    def _active_scout_wallets(self) -> list[str]:
        rows = self.db.list_tracked_wallets()
        out = [str(r["address"]) for r in rows if r.get("address")]
        if not out:
            self.run_wallet_curator()
            rows = self.db.list_tracked_wallets()
            out = [str(r["address"]) for r in rows if r.get("address")]
        return out[: self.settings.scout_max_wallets]

    # Agent 1 + Agent 0
    def run_scout(self) -> None:
        for wallet in self._active_scout_wallets():
            for pos in self.pm_data.fetch_user_positions(wallet):
                first_seen = self.db.claim_scouting_signal(wallet, pos["market_id"])
                if first_seen is None:
                    continue
                pos["opened_at"] = first_seen
                if not self._passes_data_quality(pos):
                    self.db.publish_event(
                        "agent1_scout",
                        "DATA_QUALITY_REJECTED",
                        pos.get("market_id", "unknown"),
                        pos.get("market_question", "Unknown market"),
                        {"position": pos, "reason": "Data quality checks failed"},
                    )
                    continue
                eff = self.intel.efficiency_check(pos)
                if eff["recommendation"] == "SKIP_EFFICIENT":
                    self.db.publish_event(
                        "agent0_efficiency",
                        "EFFICIENCY_RESULT",
                        pos["market_id"],
                        pos["market_question"],
                        {"decision": "SKIP_EFFICIENT", "efficiency": eff, "position": pos},
                    )
                    continue
                self.db.publish_event(
                    "agent1_scout",
                    "NEW_POSITION",
                    pos["market_id"],
                    pos["market_question"],
                    {"position": pos, "efficiency": eff},
                )

    # Agent 2
    def run_analyst(self) -> None:
        for event in self.db.consume_events("NEW_POSITION"):
            position = event.payload["position"]
            research = self.intel.run_research(event.market_question)
            raw_decision = self.llm.analyst_decision(position, research)
            decision = AnalystDecision.model_validate(raw_decision).model_dump()
            self.db.publish_event(
                "agent2_analyst",
                "ANALYSIS_COMPLETE",
                event.market_id,
                event.market_question,
                {"position": position, "research": research, "analyst": decision},
            )

    # Agent 3
    def run_devils_advocate(self) -> None:
        for event in self.db.consume_events("ANALYSIS_COMPLETE"):
            analyst = event.payload["analyst"]
            position = event.payload["position"]
            if analyst["contradicting_info"] or not analyst["has_value"]:
                self.db.publish_event(
                    "agent3_devil",
                    "CHALLENGE_COMPLETE",
                    event.market_id,
                    event.market_question,
                    {"decision": "SKIP", "reason": "Analyst gating conditions not met.", "position": position},
                )
                continue
            challenge = self.llm.devil_advocate(position, analyst)
            fatal = bool(challenge.get("fatal_issue", False))
            self.db.publish_event(
                "agent3_devil",
                "CHALLENGE_COMPLETE",
                event.market_id,
                event.market_question,
                {
                    "decision": "SKIP" if fatal else "PROCEED",
                    "challenge": challenge,
                    "position": position,
                    "analyst": analyst,
                },
            )

    # Agent 4 + Execution
    def run_risk_officer(self) -> None:
        day_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._risk_halted(day_prefix):
            return
        for event in self.db.consume_events("CHALLENGE_COMPLETE"):
            payload = event.payload
            position = payload["position"]
            if payload["decision"] == "SKIP":
                self.db.publish_event(
                    "agent4_risk",
                    "RISK_REJECTED",
                    event.market_id,
                    event.market_question,
                    {"reason": "Devil's advocate rejected trade."},
                )
                continue

            if self.db.open_positions_count() >= self.settings.max_open_positions:
                self._reject(event, "Max open positions reached")
                continue
            if self.db.open_positions_in_sector(position["sector"]) >= self.settings.max_sector_positions:
                self._reject(event, "Sector concentration limit reached")
                continue
            if self.db.capital_at_risk() >= self.settings.max_capital_at_risk:
                self._reject(event, "Capital-at-risk limit reached")
                continue
            if self.db.daily_realized_pnl(day_prefix) <= -self.settings.daily_loss_limit:
                self._reject(event, "Daily loss limit reached (24h trading stop)")
                self.db.set_risk_state("halt_until", (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat())
                self.db.write_audit(
                    "CRITICAL",
                    "risk_officer",
                    "daily_loss_halt",
                    {"halt_for_hours": 24, "day_prefix": day_prefix},
                )
                continue
            if abs(position["current_price"] - position["entry_price"]) > self.settings.max_price_drift_from_entry:
                self._reject(event, "Price drift from source wallet entry exceeded threshold")
                continue

            conviction = payload.get("analyst", {}).get("confidence", "medium")
            stake = self._compute_stake(conviction=conviction, wallet_count=1)
            stake = min(stake, self.settings.max_single_bet)

            trade = {
                "market_id": event.market_id,
                "market_question": event.market_question,
                "entry_price": position["current_price"],
                "stake_usdc": stake,
                "side": position["side"],
                "sector": position["sector"],
            }
            position_id: int | None = None
            if self.settings.track_paper_cash:
                position_id = self.db.open_position_and_debit_cash(trade, stake)
                if position_id is None:
                    self._reject(event, "Insufficient paper cash for stake")
                    continue
            else:
                self.db.add_position(trade)
            fill_price = self._simulate_fill(trade["entry_price"])
            exec_status = "SIMULATED_FILLED" if self.settings.execution_mode in ("paper", "dry_run") else "PENDING_LIVE"
            ex_id = self.db.record_execution(
                market_id=event.market_id,
                side=trade["side"],
                stake_usdc=stake,
                expected_price=trade["entry_price"],
                simulated_fill_price=fill_price,
                mode=self.settings.execution_mode,
                status=exec_status,
                notes={
                    "dry_run": self.settings.dry_run,
                    "source_wallet": position.get("wallet", ""),
                },
            )
            slip = abs(fill_price - float(trade["entry_price"]))
            eta = self._resolution_eta_hours(str(position.get("resolution_end", "") or ""))
            self.db.insert_attribution_signal(
                execution_id=ex_id,
                position_id=position_id,
                source_wallet=str(position.get("wallet", "")),
                market_id=trade["market_id"],
                market_question=trade["market_question"],
                entry_signal_price=float(position.get("entry_price", 0.0)),
                fill_price=float(fill_price),
                slippage=float(slip),
                stake_usdc=float(stake),
                opened_at=str(position.get("opened_at") or utc_now()),
                resolved_at=None,
                resolution_eta_hours=eta,
                pnl_usdc=None,
                status="OPEN",
                notes={"execution_mode": self.settings.execution_mode},
            )
            self.db.publish_event("agent4_risk", "RISK_APPROVED", event.market_id, event.market_question, {"trade": trade})
            self.db.publish_event("execution", "BET_PLACED", event.market_id, event.market_question, trade)
            self.db.write_audit("INFO", "execution", "bet_placed", trade)
            if self.settings.execution_mode == "live" and not self.settings.dry_run:
                # Live: prefer post_only / limit near mid with max slippage (implemented in CLOB layer).
                pass

    def _reject(self, event: Any, reason: str) -> None:
        self.db.publish_event("agent4_risk", "RISK_REJECTED", event.market_id, event.market_question, {"reason": reason})
        self.db.write_audit("WARN", "risk_officer", "risk_rejected", {"market_id": event.market_id, "reason": reason})

    @staticmethod
    def _compute_stake(conviction: str, wallet_count: int) -> float:
        if wallet_count >= 2 and conviction == "high":
            return 5.0
        if wallet_count >= 2:
            return 3.5
        if conviction == "high":
            return 2.5
        return 1.5

    # Agent 5
    def run_position_monitor(self) -> None:
        for event in self.db.consume_events("BET_PLACED"):
            self.db.publish_event(
                "agent5_monitor",
                "POSITION_ALERT",
                event.market_id,
                event.market_question,
                {"message": "New position opened and now monitored every 2h."},
            )

    # Agent 6
    def run_portfolio_manager(self) -> None:
        day_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        halt_until = self.db.get_risk_state("halt_until", "")
        cash = self.db.get_portfolio_cash()
        deployed = self.db.capital_at_risk()
        nav = cash + deployed
        text = (
            "Portfolio Report (6h)\n"
            f"- Open positions: {self.db.open_positions_count()}\n"
            f"- Free cash: {cash:.2f} USDC\n"
            f"- Deployed (at risk): {deployed:.2f} USDC\n"
            f"- NAV (paper): {nav:.2f} USDC\n"
            f"- Daily realized PnL: {self.db.daily_realized_pnl(day_prefix):.2f} USDC\n"
            f"- Risk halt until: {halt_until or 'none'}"
        )
        self.db.publish_event("agent6_portfolio", "PORTFOLIO_REPORT", "portfolio", "Portfolio Health", {"report": text})
        self.telegram.send(text)

    def _passes_data_quality(self, pos: dict[str, Any]) -> bool:
        try:
            PositionSignal.model_validate(pos)
        except Exception as exc:
            self.db.write_audit(
                "WARN",
                "data_quality",
                "position_schema_invalid",
                {"error": str(exc), "position": pos},
            )
            return False
        return True

    def _risk_halted(self, day_prefix: str) -> bool:
        halt_until = self.db.get_risk_state("halt_until", "")
        if not halt_until:
            return False
        try:
            halt_dt = datetime.fromisoformat(halt_until.replace("Z", "+00:00"))
        except Exception:
            self.db.set_risk_state("halt_until", "")
            return False
        if datetime.now(timezone.utc) >= halt_dt:
            self.db.set_risk_state("halt_until", "")
            return False
        self.db.write_audit(
            "WARN",
            "risk_officer",
            "risk_halt_active",
            {"halt_until": halt_until, "daily_realized_pnl": self.db.daily_realized_pnl(day_prefix)},
        )
        return True

    @staticmethod
    def _simulate_fill(expected_price: float) -> float:
        slippage = 0.005
        return min(max(expected_price + slippage, 0.0), 1.0)

    @staticmethod
    def _resolution_eta_hours(end_iso: str) -> float | None:
        if not end_iso:
            return None
        try:
            dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            delta = (dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
            return max(0.0, float(delta))
        except Exception:
            return None

