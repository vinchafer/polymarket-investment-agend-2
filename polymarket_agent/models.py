from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

_WALLET_ADDR = re.compile(r"^0x[a-fA-F0-9]{40}$")


ALLOWED_EVENT_TYPES = {
    "NEW_POSITION",
    "EFFICIENCY_RESULT",
    "ANALYSIS_COMPLETE",
    "CHALLENGE_COMPLETE",
    "RISK_APPROVED",
    "RISK_REJECTED",
    "BET_PLACED",
    "POSITION_ALERT",
    "PORTFOLIO_REPORT",
    "DATA_QUALITY_REJECTED",
}


class PositionSignal(BaseModel):
    market_id: str = Field(min_length=1)
    market_question: str = Field(min_length=3)
    entry_price: float = Field(ge=0.0, le=1.0)
    current_price: float = Field(ge=0.0, le=1.0)
    side: Literal["YES", "NO"]
    sector: str = Field(min_length=2, default="other")
    wallet: str = Field(min_length=42, max_length=42)

    @field_validator("wallet")
    @classmethod
    def validate_wallet(cls, value: str) -> str:
        if not _WALLET_ADDR.match(value):
            raise ValueError("wallet must be a 0x-prefixed 40-hex address")
        return value

    opened_at: str

    @field_validator("opened_at")
    @classmethod
    def validate_recent_timestamp(cls, value: str) -> str:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if dt > now:
            raise ValueError("opened_at cannot be in the future")
        return value


class AnalystDecision(BaseModel):
    contradicting_info: bool
    has_value: bool
    confidence: Literal["low", "medium", "high"]
    action: Literal["PROCEED", "SKIP"]
    reasoning: str = Field(min_length=5)
    key_risk: str = Field(min_length=3)


class DevilAdvocateDecision(BaseModel):
    fatal_issue: bool
    price_move: float = 0.0
    reason: str = Field(min_length=3)
    action: Literal["PROCEED", "SKIP"]


class EventEnvelope(BaseModel):
    agent_source: str = Field(min_length=3)
    event_type: str
    market_id: str = Field(min_length=1)
    market_question: str = Field(min_length=3)
    payload: dict

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: str) -> str:
        if value not in ALLOWED_EVENT_TYPES:
            raise ValueError(f"Unsupported event type: {value}")
        return value
