"""
config.py — Zentrale Konfiguration für den Polymarket AI Trading Agent
Alle Einstellungen werden aus der .env Datei geladen.
"""

import os
from dotenv import load_dotenv

load_dotenv()


# =============================================================================
# POLYMARKET / BLOCKCHAIN
# =============================================================================
POLYMARKET_HOST = "https://clob.polymarket.com"
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("POLYGON_PRIVATE_KEY")  # dein Wallet Private Key
POLYMARKET_FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS")  # deine Wallet-Adresse (0x...)
POLYGON_CHAIN_ID = 137                                               # Polygon Mainnet


# =============================================================================
# GROQ API (primäre Entscheidungs-Engine — kostenlos)
# =============================================================================
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
# Second Groq key for rotation when primary hits daily rate limit
GROQ_API_KEY_2 = os.getenv("GROQ_API_KEY_2", "")
# llama-3.3-70b-versatile: kostenlos, schnell, OpenAI-kompatibel
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


# =============================================================================
# FREE RESEARCH SOURCES — keyfree (DuckDuckGo, GDELT, Reddit) + NewsAPI optional
# Serper/Tavily/Anthropic removed (paid quotas exhausted, replaced with keyfree).
# =============================================================================
# NewsAPI — 100 free requests/day — get key at newsapi.org (optional)
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")


# =============================================================================
# RISIKO-MANAGEMENT (sehr wichtig!)
# =============================================================================
# Maximaler Einsatz pro einzelnem Bet (in USDC)
# Smart money strategy: 1-3 USDC per bet (based on wallet count)
MAX_BET_USDC = float(os.getenv("MAX_BET_USDC", "3.0"))

# Tägliches Verlust-Limit: Agent stoppt wenn dieser Betrag verloren wurde
MAX_DAILY_LOSS_USDC = float(os.getenv("MAX_DAILY_LOSS_USDC", "10.0"))

# Maximale gleichzeitig offene Positionen
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "15"))

# Maximaler Einsatz als Prozentsatz des Gesamtportfolios (6% max per bet)
MAX_BET_PCT_PORTFOLIO = float(os.getenv("MAX_BET_PCT_PORTFOLIO", "0.06"))

# Gesamtportfolio-Größe in USDC
PORTFOLIO_USDC = float(os.getenv("PORTFOLIO_USDC", "50.0"))

# Täglicher Drawdown-Schutz: Wenn x% des Portfolios verloren → 24h Stop
MAX_DRAWDOWN_PCT = float(os.getenv("MAX_DRAWDOWN_PCT", "0.10"))

# Minimale Anzahl Tier 1-2 Quellen für vergangene Events (sonst SKIP)
MIN_TIER12_SOURCES = int(os.getenv("MIN_TIER12_SOURCES", "3"))

# Minimale Konfidenz — not used as gate in smart money strategy (decision is YES/NO from agent)
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.0"))

# Minimaler Edge — not used as gate in smart money strategy
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.0"))

# Minimale Marktliquidität (Volumen in USDC) — ignoriere illiquide Märkte
# 10000 = nur Märkte mit >$10k Volumen (verhindert Manipulation)
MIN_MARKET_VOLUME = float(os.getenv("MIN_MARKET_VOLUME", "10000.0"))

# Maximale Tage bis zur Marktauflösung — keine Märkte die sehr lange laufen
MAX_DAYS_TO_RESOLUTION = int(os.getenv("MAX_DAYS_TO_RESOLUTION", "30"))

# =============================================================================
# MARKT-HORIZONT-FILTER (struktureller Deadlock-Fix, 2026-07-12)
# Harte Guardrail auf Scout-Ebene: nur Märkte handeln, die innerhalb dieses
# Fensters auflösen. Verhindert, dass Kapital in Langfrist-Märkten (Wahl 2028,
# BTC Dez, Playoff-Futures) versinkt und den Capital-at-Risk-Cap dauerhaft
# blockiert. Märkte ohne verlässliche end_date werden abgelehnt (no_end_date).
# Nur Env ändern zum Nachjustieren (14 → 21 → 30), kein Code.
# =============================================================================
MAX_MARKET_HORIZON_DAYS = int(os.getenv("MAX_MARKET_HORIZON_DAYS", "14"))

# Test-Startzeitpunkt (UTC ISO). Ab hier filtert jede A/B-Auswertung. Beim
# Portfolio-Vollreset gesetzt; leer = kein Filter (Alt-Betrieb).
TEST_EPOCH = os.getenv("TEST_EPOCH", "")

# Copy-Arm-Modus (A2): deterministischer Wallet-Copy statt LLM. Gated damit
# gemeinsamer Code (z.B. Router-Health-Probe) im Copy-Arm inaktiv bleibt.
COPY_MODE = os.getenv("COPY_MODE", "false").lower() == "true"

# Router-Health-Probe (nur A1/LLM-Arm): Intervall in Sekunden (Default 6h).
PROVIDER_PROBE_INTERVAL_SECONDS = int(os.getenv("PROVIDER_PROBE_INTERVAL_SECONDS", "21600"))

# Minimale Marktwahrscheinlichkeit und Maximum — meide extreme Märkte (>95% oder <5%)
MIN_MARKET_PROBABILITY = float(os.getenv("MIN_MARKET_PROBABILITY", "0.05"))
MAX_MARKET_PROBABILITY = float(os.getenv("MAX_MARKET_PROBABILITY", "0.95"))


# =============================================================================
# AGENT-VERHALTEN
# =============================================================================
# Wie oft läuft der Agent (in Minuten)
RUN_INTERVAL_MINUTES = int(os.getenv("RUN_INTERVAL_MINUTES", "60"))   # Standard: alle 1h

# Wie viele Märkte pro Durchlauf analysiert werden (API-Kosten beachten!)
MAX_MARKETS_TO_ANALYZE = int(os.getenv("MAX_MARKETS_TO_ANALYZE", "20"))

# DRY_RUN = True: Agent entscheidet, aber platziert KEINE echten Orders
# Immer erst mit DRY_RUN=true testen!
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# Kategorien die bevorzugt werden sollen (leer = alle)
# Beispiel: ["politics", "sports", "crypto", "economics"]
PREFERRED_CATEGORIES = os.getenv("PREFERRED_CATEGORIES", "").split(",") if os.getenv("PREFERRED_CATEGORIES") else []


# =============================================================================
# TELEGRAM BENACHRICHTIGUNGEN
# =============================================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# Welche Events sollen gemeldet werden?
NOTIFY_ON_BET = os.getenv("NOTIFY_ON_BET", "true").lower() == "true"
NOTIFY_ON_WIN = os.getenv("NOTIFY_ON_WIN", "true").lower() == "true"
NOTIFY_ON_LOSS = os.getenv("NOTIFY_ON_LOSS", "true").lower() == "true"
NOTIFY_ON_ERROR = os.getenv("NOTIFY_ON_ERROR", "true").lower() == "true"
NOTIFY_DAILY_SUMMARY = os.getenv("NOTIFY_DAILY_SUMMARY", "true").lower() == "true"
NOTIFY_ON_POSITION_WARNING = os.getenv("NOTIFY_ON_POSITION_WARNING", "false").lower() == "true"


# =============================================================================
# GEMINI API (Analyst primary LLM)
# =============================================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "") or os.getenv("GEMINI2_API_KEY", "")
# Legacy alias for backward-compat with older code paths
GEMINI2_API_KEY = GEMINI_API_KEY

# =============================================================================
# THE ODDS API (efficiency check — Pinnacle odds)
# =============================================================================
THE_ODDS_API_KEY = os.getenv("THE_ODDS_API_KEY", "")

# =============================================================================
# 6-AGENT ARCHITECTURE SETTINGS
# =============================================================================
MAX_SECTOR_POSITIONS = int(os.getenv("MAX_SECTOR_POSITIONS", "5"))  # unused — sector limits removed
MAX_CAPITAL_AT_RISK_PCT = float(os.getenv("MAX_CAPITAL_AT_RISK_PCT", "0.60"))
NEW_POSITION_WINDOW_HOURS = int(os.getenv("NEW_POSITION_WINDOW_HOURS", "24"))
SCOUT_INTERVAL_SECONDS = int(os.getenv("SCOUT_INTERVAL_SECONDS", "3600"))
ANALYST_POLL_SECONDS = int(os.getenv("ANALYST_POLL_SECONDS", "60"))
POSITION_MONITOR_INTERVAL_SECONDS = int(os.getenv("POSITION_MONITOR_INTERVAL_SECONDS", "7200"))

# =============================================================================
# PHASE 1 OPTIMIERUNGEN (GOLDMAN SACHS STANDARDS)
# =============================================================================

# ✅ KELLY KRITERIUM
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.5"))  # Half Kelly = 0.5 (empfohlen)
MIN_BET_USDC = float(os.getenv("MIN_BET_USDC", "0.5"))
USE_KELLY_SIZING = os.getenv("USE_KELLY_SIZING", "true").lower() == "true"

# ✅ SEKTOR KONZENTRATION LIMITS
MAX_SECTOR_CAPITAL_PCT = float(os.getenv("MAX_SECTOR_CAPITAL_PCT", "0.18"))  # Max 18% pro Sektor
MAX_SECTOR_POSITIONS = int(os.getenv("MAX_SECTOR_POSITIONS", "3"))  # Max 3 Positionen pro Sektor
ENABLE_SECTOR_LIMITS = os.getenv("ENABLE_SECTOR_LIMITS", "true").lower() == "true"

# ✅ STOP LOSS & GEWINNMITNAHME
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.40"))  # Verkauf bei -40%
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.65"))  # Verkauf bei +65%
ENABLE_AUTO_EXIT = os.getenv("ENABLE_AUTO_EXIT", "true").lower() == "true"

# ✅ SLIPPAGE & GEBÜHREN
DEFAULT_SLIPPAGE_PCT = float(os.getenv("DEFAULT_SLIPPAGE_PCT", "0.03"))  # 3% durchschnittlicher Slippage
POLYMARKET_FEES_PCT = float(os.getenv("POLYMARKET_FEES_PCT", "0.02"))  # 2% Polymarket Gebühren
ENABLE_SLIPPAGE_DEDUCTION = os.getenv("ENABLE_SLIPPAGE_DEDUCTION", "true").lower() == "true"

# =============================================================================
# PHASE 2 OPTIMIERUNGEN (GOLDMAN SACHS STANDARDS)
# =============================================================================

# ✅ KORRELATIONSSCHUTZ
ENABLE_CORRELATION_DETECTION = os.getenv("ENABLE_CORRELATION_DETECTION", "true").lower() == "true"
MAX_CORRELATED_POSITIONS = int(os.getenv("MAX_CORRELATED_POSITIONS", 1))

# ✅ LIQUIDITÄTSFILTER
MAX_ORDER_VOLUME_RATIO = float(os.getenv("MAX_ORDER_VOLUME_RATIO", "0.33"))  # Max 1/3 des bekannten Volumens
MIN_MARKET_DAILY_VOLUME = float(os.getenv("MIN_MARKET_DAILY_VOLUME", "100.0"))
ENABLE_LIQUIDITY_CHECK = os.getenv("ENABLE_LIQUIDITY_CHECK", "true").lower() == "true"

# ✅ TIME DECAY
TIME_DECAY_START_DAYS = int(os.getenv("TIME_DECAY_START_DAYS", 3))  # Starte Decay 3 Tage vor Ende
TIME_DECAY_FINAL_MULTIPLIER = float(os.getenv("TIME_DECAY_FINAL_MULTIPLIER", "0.5"))
ENABLE_TIME_DECAY = os.getenv("ENABLE_TIME_DECAY", "true").lower() == "true"

# ✅ CIRCUIT BREAKER
CIRCUIT_BREAKER_ERROR_THRESHOLD = int(os.getenv("CIRCUIT_BREAKER_ERROR_THRESHOLD", 3))
CIRCUIT_BREAKER_SLEEP_MINUTES = int(os.getenv("CIRCUIT_BREAKER_SLEEP_MINUTES", 10))
MAX_TRADES_PER_HOUR = int(os.getenv("MAX_TRADES_PER_HOUR", 12))
ENABLE_CIRCUIT_BREAKER = os.getenv("ENABLE_CIRCUIT_BREAKER", "true").lower() == "true"

# =============================================================================
# PHASE 3 OPTIMIERUNGEN (GOLDMAN SACHS STANDARDS)
# =============================================================================

# ✅ ONLINE LEARNING ENGINE
LEARNING_RATE = float(os.getenv("LEARNING_RATE", "0.03"))  # 3% Anpassung pro Trade
MIN_TRADE_HISTORY = int(os.getenv("MIN_TRADE_HISTORY", "10"))
MAX_AGENT_WEIGHT = float(os.getenv("MAX_AGENT_WEIGHT", "2.0"))
MIN_AGENT_WEIGHT = float(os.getenv("MIN_AGENT_WEIGHT", "0.3"))
ENABLE_ONLINE_LEARNING = os.getenv("ENABLE_ONLINE_LEARNING", "true").lower() == "true"

# ✅ SHADOW MODE
SHADOW_MODE_ENABLED = os.getenv("SHADOW_MODE_ENABLED", "false").lower() == "true"
SHADOW_MODEL_NAME = os.getenv("SHADOW_MODEL_NAME", "experimental")
ENABLE_SHADOW_TRACKING = os.getenv("ENABLE_SHADOW_TRACKING", "true").lower() == "true"

# ✅ PERFORMANCE TRACKING
TRACK_AGENT_PERFORMANCE = os.getenv("TRACK_AGENT_PERFORMANCE", "true").lower() == "true"
CONFIDENCE_CALIBRATION_ENABLED = os.getenv("CONFIDENCE_CALIBRATION_ENABLED", "true").lower() == "true"

# =============================================================================
# PHASE 4 OPTIMIERUNGEN (GOLDMAN SACHS STANDARDS)
# =============================================================================

# ✅ MONITORING & OBSERVABILITY
METRICS_ENABLED = os.getenv("METRICS_ENABLED", "true").lower() == "true"
METRICS_PORT = int(os.getenv("METRICS_PORT", "9090"))
HEALTH_CHECK_ENABLED = os.getenv("HEALTH_CHECK_ENABLED", "true").lower() == "true"
PROMETHEUS_EXPORT = os.getenv("PROMETHEUS_EXPORT", "true").lower() == "true"

# ✅ BACKUP
AUTO_BACKUP_ENABLED = os.getenv("AUTO_BACKUP_ENABLED", "true").lower() == "true"
BACKUP_INTERVAL_HOURS = int(os.getenv("BACKUP_INTERVAL_HOURS", "24"))
MAX_BACKUP_COUNT = int(os.getenv("MAX_BACKUP_COUNT", "7"))

# ✅ ALARMING
ALARM_ON_DRAWDOWN_PCT = float(os.getenv("ALARM_ON_DRAWDOWN_PCT", "0.15"))
ALARM_ON_WINSTREAK = int(os.getenv("ALARM_ON_WINSTREAK", "6"))
ALARM_ON_LOSSSTREAK = int(os.getenv("ALARM_ON_LOSSSTREAK", "4"))
ALARM_ON_ERROR_COUNT = int(os.getenv("ALARM_ON_ERROR_COUNT", "10"))

# ✅ OPERATIONALE STABILITÄT
GRACEFUL_SHUTDOWN_TIMEOUT = int(os.getenv("GRACEFUL_SHUTDOWN_TIMEOUT", "30"))
WATCHDOG_ENABLED = os.getenv("WATCHDOG_ENABLED", "true").lower() == "true"

# =============================================================================
# DATENBANK & LOGGING
# =============================================================================
DB_PATH = os.getenv("DB_PATH", "trading_log.db")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")  # DEBUG, INFO, WARNING, ERROR
LOG_FILE = os.getenv("LOG_FILE", "agent.log")


# =============================================================================
# VALIDIERUNG beim Start
# =============================================================================
def validate_config():
    """Prüft ob alle kritischen Konfigurationswerte gesetzt sind."""
    errors = []
    warnings = []

    if not POLYMARKET_PRIVATE_KEY:
        errors.append("POLYMARKET_PRIVATE_KEY ist nicht gesetzt")
    if not POLYMARKET_FUNDER_ADDRESS:
        errors.append("POLYMARKET_FUNDER_ADDRESS ist nicht gesetzt")
    if not GROQ_API_KEY:
        errors.append("GROQ_API_KEY ist nicht gesetzt — primäres LLM erforderlich")
    if not GEMINI_API_KEY:
        warnings.append("GEMINI_API_KEY nicht gesetzt (OK — Groq übernimmt LLM-Entscheidungen)")

    if not TELEGRAM_BOT_TOKEN:
        warnings.append("TELEGRAM_BOT_TOKEN nicht gesetzt — keine Benachrichtigungen")
    if DRY_RUN:
        warnings.append("DRY_RUN=true — es werden KEINE echten Orders platziert")

    if errors:
        raise ValueError(f"Konfigurationsfehler:\n" + "\n".join(f"  ❌ {e}" for e in errors))

    for w in warnings:
        print(f"  ⚠️  {w}")

    print(f"  ✅ Konfiguration geladen (DRY_RUN={DRY_RUN}, MAX_BET={MAX_BET_USDC} USDC)")
