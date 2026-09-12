"""
agent.py — Simple wallet-following decision engine.

Strategy: Top wallets took a position → quick 3-question validation:
  Q1: Is there public info that DIRECTLY contradicts the wallet's position?
      YES → SKIP. NO → continue.
  Q2: Does the current market price seem WRONG (opportunity)?
      WRONG → BET. FAIR → SKIP.
  Q3: How many wallets agree?
      1 wallet → 1 USDC | 2 wallets → 2 USDC | 3+ wallets → 3 USDC

Decision output:
  {"action": "BET_YES"|"BET_NO"|"SKIP", "reason": "one sentence",
   "contradicting_info": bool, "market_mispriced": bool}
"""

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from enum import Enum
from typing import Optional

import config

logger = logging.getLogger(__name__)


# =============================================================================
# Data classes (kept for backward compatibility with logger.py, risk_manager.py)
# =============================================================================

class TradeAction(Enum):
    BET_YES = "BET_YES"
    BET_NO = "BET_NO"
    SKIP = "SKIP"


@dataclass
class AgentDecision:
    """Agent decision for a single market."""
    market_question: str
    action: TradeAction
    agent_yes_probability: float
    market_yes_probability: float
    edge: float
    confidence: float
    recommended_bet_usdc: float
    reasoning: str
    key_factors: list
    risks: list
    model_used: str
    tokens_used: int
    skip_reason: str = ""
    source_quality_score: float = 0.5
    contradiction_detected: bool = False
    weighted_score: float = 0.0
    criteria_scores: dict = None
    cross_platform_spread: float = 0.0
    cross_platform_info: str = ""
    learning_adjustments: dict = None


# =============================================================================
# System prompt
# =============================================================================

# Ultra-short system prompt — keeps input tokens under 50
SYSTEM_PROMPT = (
    'Validate a prediction market bet. Rule: if news DIRECTLY contradicts the wallet bet → SKIP, '
    'else follow the wallet. Reply JSON only: {"action":"BET_YES/BET_NO/SKIP","reason":"one sentence"}'
)


# =============================================================================
# Trading Agent
# =============================================================================

class TradingAgent:
    """
    Simple wallet-following decision engine using Groq (free) or Claude fallback.
    """

    def __init__(self):
        if not config.GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY required (Anthropic fallback removed)")
        from groq import Groq
        self._groq_clients = [Groq(api_key=config.GROQ_API_KEY)]
        if config.GROQ_API_KEY_2:
            self._groq_clients.append(Groq(api_key=config.GROQ_API_KEY_2))
        self._model = config.GROQ_MODEL
        self._use_groq = True
        logger.info(f"TradingAgent: Groq/{self._model} ({len(self._groq_clients)} key(s))")

    def analyze_market(
        self,
        market,
        research_text: str,
        wallet_count: int,
        wallet_direction: str,  # "YES" or "NO"
    ) -> AgentDecision:
        """
        3-question validation of a wallet position.
        Returns AgentDecision with BET_YES / BET_NO / SKIP.
        """
        # Keep total input under 300 tokens: short question + price + 400 chars research
        user_prompt = (
            f"Wallet bet: {wallet_count} top trader(s) → {wallet_direction} on '{market.question}' "
            f"(YES={market.yes_price:.0%})\n"
            f"News: {research_text[:400]}\n"
            f"Follow or skip? JSON:"
        )

        try:
            raw, tokens = self._call_model(user_prompt)
            parsed = self._parse_response(raw)

            action_str = parsed.get("action", "SKIP").upper().strip().strip('"')
            if action_str not in ("BET_YES", "BET_NO", "SKIP"):
                action_str = "SKIP"
            action = TradeAction[action_str]

            reason = str(parsed.get("reason", "No reason provided"))[:200]
            contradicting = (action == TradeAction.SKIP)
            mispriced = (action != TradeAction.SKIP)

            # Bet size: 1 USDC per agreeing wallet, max 3
            bet_usdc = float(min(wallet_count, 3))

            # Infer agent probability from action
            if action == TradeAction.BET_YES:
                agent_prob = min(0.95, market.yes_price + 0.12)
            elif action == TradeAction.BET_NO:
                agent_prob = max(0.05, market.yes_price - 0.12)
            else:
                agent_prob = market.yes_price

            edge = abs(agent_prob - market.yes_price)
            confidence = 0.75 if not contradicting else 0.25

            logger.debug(f"  Raw: {raw[:200]}")

            self._log_groq_usage(tokens)

            return AgentDecision(
                market_question=market.question,
                action=action,
                agent_yes_probability=agent_prob,
                market_yes_probability=market.yes_price,
                edge=edge,
                confidence=confidence,
                recommended_bet_usdc=bet_usdc,
                reasoning=reason,
                key_factors=[
                    f"{wallet_count} wallet(s) → {wallet_direction}",
                    f"Contradicting info: {contradicting}",
                    f"Market mispriced: {mispriced}",
                ],
                risks=[
                    "Wallets may be wrong" if not contradicting else "Contradicting evidence found",
                    "Research may be incomplete",
                ],
                model_used=self._model,
                tokens_used=tokens,
                skip_reason="" if action != TradeAction.SKIP else reason,
                contradiction_detected=contradicting,
                weighted_score=round(confidence * 10, 1),
                criteria_scores={},
                learning_adjustments={},
            )

        except Exception as e:
            logger.error(f"Agent error for '{market.question[:50]}': {e}")
            return self._error_decision(market, str(e))

    def _call_model(self, user_prompt: str) -> tuple[str, int]:
        """
        Call Groq (with instant key-2 fallback on 429) or Claude.
        Returns (raw_text, tokens_used).
        """
        if self._use_groq:
            last_err = None
            for idx, client in enumerate(self._groq_clients):
                try:
                    resp = client.chat.completions.create(
                        model=self._model,
                        messages=[
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": user_prompt},
                        ],
                        max_tokens=80,
                        temperature=0.1,
                    )
                    raw = resp.choices[0].message.content.strip()
                    tokens = resp.usage.total_tokens if resp.usage else 0
                    if idx > 0:
                        logger.info(f"  Used Groq key {idx + 1} (key 1 rate-limited)")
                    return raw, tokens
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "rate_limit" in err_str.lower() or "rate limit" in err_str.lower():
                        logger.warning(f"  Groq key {idx + 1} rate-limited (429) — trying next key")
                        last_err = e
                        continue
                    raise  # Non-429 error → propagate immediately
            raise Exception(f"All {len(self._groq_clients)} Groq key(s) rate-limited: {last_err}")

    def _parse_response(self, raw: str) -> dict:
        """Extract and parse JSON from model response."""
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            raw = match.group(0)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"JSON parse failed: {raw[:200]}")
            return {
                "action": "SKIP",
                "reason": "Could not parse agent response",
                "contradicting_info": False,
                "market_mispriced": False,
            }

    def _error_decision(self, market, error_msg: str) -> AgentDecision:
        return AgentDecision(
            market_question=market.question,
            action=TradeAction.SKIP,
            agent_yes_probability=market.yes_price,
            market_yes_probability=market.yes_price,
            edge=0.0,
            confidence=0.0,
            recommended_bet_usdc=0.0,
            reasoning=f"Agent error: {error_msg}",
            key_factors=[],
            risks=[],
            model_used=self._model,
            tokens_used=0,
            skip_reason=f"Agent error: {error_msg}",
            criteria_scores={},
            learning_adjustments={},
        )

    def _log_groq_usage(self, tokens: int):
        """Track daily Groq token usage in SQLite (best-effort)."""
        if not self._use_groq or tokens == 0:
            return
        try:
            with sqlite3.connect(config.DB_PATH) as conn:
                today = str(date.today())
                conn.execute("""
                    INSERT INTO groq_usage (date, requests_made, tokens_used)
                    VALUES (?, 1, ?)
                    ON CONFLICT(date) DO UPDATE SET
                        requests_made = requests_made + 1,
                        tokens_used = tokens_used + excluded.tokens_used
                """, (today, tokens))
        except Exception:
            pass  # Table may not exist yet — wallet_monitor creates it

    def get_groq_usage_today(self) -> dict:
        """Return today's Groq API usage stats for the 6h report."""
        try:
            with sqlite3.connect(config.DB_PATH) as conn:
                today = str(date.today())
                row = conn.execute(
                    "SELECT requests_made, tokens_used FROM groq_usage WHERE date = ?",
                    (today,)
                ).fetchone()
                if row:
                    return {"requests": row[0], "tokens": row[1]}
        except Exception:
            pass
        return {"requests": 0, "tokens": 0}

    def reset_run_state(self):
        """No-op — kept for backward compatibility."""
        pass

    def estimate_cost_per_decision(self) -> dict:
        """Rough cost estimate for logging."""
        return {
            "cost_per_decision_usd": 0.00005 if self._use_groq else 0.001,
            "cost_per_100_decisions_usd": 0.005 if self._use_groq else 0.1,
        }
