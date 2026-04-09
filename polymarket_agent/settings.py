from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _load_env_files() -> None:
    """Load .env / env.txt from repo and common Cursor project locations (no secrets logged)."""
    here = Path(__file__).resolve().parent.parent
    candidates: list[Path] = [
        here / ".env",
        here / "env.txt",
        Path.cwd() / ".env",
        Path.cwd() / "env.txt",
    ]
    home = Path.home()
    cursor_guess = home / ".cursor" / "projects" / "c-Users-vinch-polymarket-investment-agend-2" / "env.txt"
    if cursor_guess.is_file():
        candidates.append(cursor_guess)
    extra = os.getenv("EXTRA_ENV_FILE", "").strip()
    if extra:
        candidates.append(Path(extra))
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except Exception:
            continue
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        load_dotenv(path, override=False)


_load_env_files()


@dataclass(frozen=True)
class Settings:
    database_path: str = os.getenv("DATABASE_PATH", "./agent.db")
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"
    execution_mode: str = os.getenv("EXECUTION_MODE", "paper")  # dry_run | paper | live

    starting_capital_usdc: float = float(os.getenv("STARTING_CAPITAL_USDC", "50"))
    track_paper_cash: bool = os.getenv("TRACK_PAPER_CASH", "true").lower() == "true"

    polymarket_data_api: str = os.getenv("POLYMARKET_DATA_API", "https://data-api.polymarket.com")
    the_odds_api_key: str = os.getenv("THE_ODDS_API_KEY", "")
    news_api_key: str = os.getenv("NEWS_API_KEY", "")
    serper_api_key: str = os.getenv("SERPER_API_KEY", "")
    tavily_api_key: str = os.getenv("TAVILY_API_KEY", "")
    espn_api_base: str = os.getenv("ESPN_API_BASE", "https://site.api.espn.com/apis/site/v2")

    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    groq_model: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "25"))
    api_timeout_seconds: float = float(os.getenv("API_TIMEOUT_SECONDS", "20"))
    scout_max_wallets: int = int(os.getenv("SCOUT_MAX_WALLETS", "8"))

    dashboard_host: str = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    dashboard_port: int = int(os.getenv("DASHBOARD_PORT", "8765"))
    dashboard_cors_origins: str = os.getenv("DASHBOARD_CORS_ORIGINS", "*")
    dashboard_read_token: str = os.getenv("DASHBOARD_READ_TOKEN", "")

    leaderboard_time_period: str = os.getenv("LEADERBOARD_TIME_PERIOD", "MONTH")
    leaderboard_category: str = os.getenv("LEADERBOARD_CATEGORY", "OVERALL")
    leaderboard_order_by: str = os.getenv("LEADERBOARD_ORDER_BY", "PNL")
    wallet_curator_interval_hours: int = int(os.getenv("WALLET_CURATOR_INTERVAL_HOURS", "6"))

    llm_max_retries: int = int(os.getenv("LLM_MAX_RETRIES", "3"))

    max_open_positions: int = 15
    max_sector_positions: int = 5
    max_capital_at_risk: float = 35.0
    max_single_bet: float = 5.0
    daily_loss_limit: float = 10.0
    max_entry_lag_minutes: int = 120
    max_price_drift_from_entry: float = 0.15
    max_drawdown_guard: float = 15.0
