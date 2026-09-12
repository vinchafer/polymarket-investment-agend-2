"""
adaptive_config.py — Dynamische Konfiguration mit SQLite-Persistenz

Liest Basis-Schwellenwerte aus config.py und ueberschreibt diese mit
gelernten Anpassungen aus der SQLite Tabelle 'adaptive_config'.

Jede Aenderung wird mit Zeitstempel und Begruendung gespeichert.
Der /config Telegram-Befehl zeigt aktuelle vs. Basis-Werte.
"""

import logging
from typing import Optional

import config

logger = logging.getLogger(__name__)

# Konfigurierbare Schluesse mit Basis-Wert-Funktionen
BASE_CONFIG_KEYS = {
    "MIN_CONFIDENCE": lambda: config.MIN_CONFIDENCE,
    "MIN_EDGE": lambda: config.MIN_EDGE,
    "MIN_TIER12_SOURCES": lambda: float(config.MIN_TIER12_SOURCES),
    "MAX_BET_USDC": lambda: config.MAX_BET_USDC,
}


class AdaptiveConfig:
    """
    Dynamische Konfiguration: Basis-Werte aus config.py,
    lernbasierte Ueberschreibungen aus SQLite.

    Jedes set() wird in die DB geschrieben und im Log festgehalten.
    get() gibt immer den aktuellen effektiven Wert zurueck.
    """

    def __init__(self, db):
        self._db = db

    def get(self, key: str, default=None) -> float:
        """
        Gibt den aktuellen aktiven Wert zurueck.
        Prioritaet: DB-Wert > config.py-Wert > default.
        """
        db_val = self._db.get_adaptive_config(key)
        if db_val is not None:
            return db_val

        base_fn = BASE_CONFIG_KEYS.get(key)
        if base_fn:
            return base_fn()

        if default is not None:
            return default

        return getattr(config, key, None)

    def set(self, key: str, value: float, reason: str = ""):
        """Setzt einen Konfigurationswert und speichert ihn in der DB."""
        base_fn = BASE_CONFIG_KEYS.get(key)
        base_val = base_fn() if base_fn else None
        self._db.set_adaptive_config(key, value, reason, base_value=base_val)
        logger.info(f"AdaptiveConfig: {key} = {value:.4f} | Grund: {reason}")

    def get_display(self) -> list[dict]:
        """
        Gibt alle Konfigurationen fuer den /config Telegram-Befehl zurueck.
        Jeder Eintrag hat: key, current, base, changed, reason, last_updated.
        """
        all_overrides = self._db.get_all_adaptive_configs()
        override_map = {row["key"]: row for row in all_overrides}

        result = []
        for key, base_fn in BASE_CONFIG_KEYS.items():
            base_val = base_fn()
            override = override_map.get(key)
            current_val = override["value"] if override else base_val
            result.append({
                "key": key,
                "current": current_val,
                "base": base_val,
                "changed": override is not None,
                "reason": override.get("update_reason", "") if override else "",
                "last_updated": override.get("last_updated", "")[:10] if override else "",
                "update_count": override.get("update_count", 0) if override else 0,
            })

        return result

    def get_min_confidence(self) -> float:
        return self.get("MIN_CONFIDENCE", config.MIN_CONFIDENCE)

    def get_min_edge(self) -> float:
        return self.get("MIN_EDGE", config.MIN_EDGE)
