"""
learning_engine.py — Self-Learning System fuer den Polymarket Trading Agent

Ablauf nach jedem Analyse-Durchlauf:
1. Erkennt aufgeloeste Maerkte (yes_price >= 0.97 oder <= 0.03)
2. Extrahiert Features aus jedem aufgeloesten Bet
3. Speichert Ergebnisse in SQLite Tabelle learning_data
4. Nach je 10 neuen Ergebnissen: Dynamic Threshold Adjustment
5. Nach 50+ Ergebnissen: Logistisches Regressionsmodell fuer Win-Vorhersage

scikit-learn ist optional: ohne es laeuft alles ausser dem ML-Modell.
"""

import json
import logging
import os
import pickle
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

MODEL_PATH = "models/win_predictor.pkl"
THRESHOLD_UPDATE_INTERVAL = 10   # Schwellenwert-Update alle N neuen Ergebnisse
MODEL_TRAIN_THRESHOLD = 50       # Modell-Training erst ab N Datenpunkten
MODEL_RETRAIN_INTERVAL = 50      # Nachtraining alle N neuen Ergebnisse

RESOLUTION_YES_THRESHOLD = 0.97  # Markt gilt als YES aufgeloest
RESOLUTION_NO_THRESHOLD = 0.03   # Markt gilt als NO aufgeloest

MIN_WIN_RATE_FOR_BLACKLIST = 0.45
MIN_WIN_RATE_FOR_BONUS = 0.70
MIN_CATEGORY_SAMPLES = 5
CATEGORY_CONFIDENCE_BONUS = 0.05
MODEL_WIN_PROB_THRESHOLD = 0.55  # Unter diesem Wert: Modell ueberstimmt


class LearningEngine:
    """
    Self-Learning System: Erkennt Outcomes, extrahiert Features,
    passt Schwellenwerte dynamisch an, optional ML Win-Vorhersage.
    """

    def __init__(self, db, notifier=None, adaptive_config=None):
        """
        Args:
            db: TradeLogger Instanz
            notifier: TelegramNotifier (optional)
            adaptive_config: AdaptiveConfig Instanz (optional)
        """
        self._db = db
        self._notifier = notifier
        self._adaptive_config = adaptive_config

        self._model = None          # sklearn LogisticRegression bundle
        self._model_accuracy = 0.0
        self._new_outcomes_since_threshold = 0  # Seit letztem Threshold-Update
        self._new_outcomes_since_train = 0      # Seit letztem Modell-Training
        self._category_win_rates: dict[str, float] = {}
        self._category_sample_counts: dict[str, int] = {}

        os.makedirs("models", exist_ok=True)
        self._load_model()

        # Lade bestehende Kategorie-Stats aus DB
        self._refresh_category_stats()

        logger.info(
            f"LearningEngine initialisiert | "
            f"Modell: {'geladen' if self._model else 'nicht vorhanden (OK)'} | "
            f"Kategorien bekannt: {len(self._category_win_rates)}"
        )

    # =========================================================================
    # Outcome-Erkennung
    # =========================================================================

    def detect_and_record_outcomes(self, polymarket_client) -> int:
        """
        Prueft alle offenen Positionen auf Aufloesung via Polymarket-Preis.
        Speichert neue Outcomes in learning_data.
        Returns: Anzahl neu erkannter Outcomes.
        """
        new_outcomes = 0
        open_trades = self._db.get_open_trades()

        if not open_trades:
            return 0

        for trade in open_trades:
            condition_id = trade.get("market_condition_id", "")
            if not condition_id:
                continue

            try:
                current_price = self._get_current_yes_price(condition_id, polymarket_client)
                if current_price is None:
                    continue

                is_bet_yes = (trade.get("action") == "BET_YES")
                bet_usdc = float(trade.get("bet_usdc") or 0)
                entry_price = float(trade.get("entry_price") or 0.5)

                resolved = False
                won = False
                pnl_usdc = 0.0
                resolution_outcome = ""

                if current_price >= RESOLUTION_YES_THRESHOLD:
                    resolved = True
                    won = is_bet_yes
                    pnl_usdc = self._calc_pnl(bet_usdc, entry_price, won)
                    resolution_outcome = "YES"

                elif current_price <= RESOLUTION_NO_THRESHOLD:
                    resolved = True
                    won = not is_bet_yes
                    pnl_usdc = self._calc_pnl(bet_usdc, entry_price, won)
                    resolution_outcome = "NO"

                if resolved:
                    # Aufloesung in Trades-Tabelle sichern (idempotent)
                    self._db.log_resolution(
                        condition_id, resolution_outcome, pnl_usdc, won
                    )
                    outcome_str = "WIN" if won else "LOSS"
                    self._save_learning_datapoint(trade, outcome_str, pnl_usdc)
                    new_outcomes += 1
                    self._new_outcomes_since_threshold += 1
                    self._new_outcomes_since_train += 1
                    logger.info(
                        f"  Neues Outcome [{outcome_str}]: "
                        f"{trade.get('market_question', '')[:50]}"
                    )

            except Exception as e:
                logger.debug(f"Outcome-Check fehlgeschlagen fuer {condition_id[:16]}: {e}")

        # Bereits aufgeloeste Trades nachholen die noch nicht in learning_data sind
        try:
            unprocessed = self._db.get_unprocessed_resolved_trades()
            for trade in unprocessed:
                try:
                    won = bool(trade.get("won"))
                    pnl_usdc = float(trade.get("pnl_usdc") or 0)
                    outcome_str = "WIN" if won else "LOSS"
                    self._save_learning_datapoint(trade, outcome_str, pnl_usdc)
                    new_outcomes += 1
                    self._new_outcomes_since_threshold += 1
                    self._new_outcomes_since_train += 1
                except Exception as e:
                    logger.debug(f"Unprocessed trade Fehler: {e}")
        except Exception as e:
            logger.debug(f"Unprocessed trades Abfrage fehlgeschlagen: {e}")

        return new_outcomes

    def _get_current_yes_price(
        self, condition_id: str, polymarket_client
    ) -> Optional[float]:
        """Holt aktuellen YES-Preis aus DB-Cache, Gamma API oder Polymarket."""
        price = self._db.get_last_known_price(condition_id)
        if price is not None:
            return price

        # Direct Gamma API lookup by conditionId (works for resolved markets too)
        try:
            import requests
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"conditionIds": condition_id},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                items = data if isinstance(data, list) else data.get("data", data.get("results", []))
                for item in items:
                    outcomes = item.get("outcomes")
                    prices = item.get("outcomePrices")
                    if isinstance(outcomes, str):
                        try:
                            import json as _json
                            outcomes = _json.loads(outcomes)
                            prices = _json.loads(prices) if isinstance(prices, str) else prices
                        except Exception:
                            pass
                    if isinstance(outcomes, list) and isinstance(prices, list):
                        for i, o in enumerate(outcomes):
                            if str(o).upper() == "YES" and i < len(prices):
                                return float(prices[i])
                    # Fallback: clobTokenIds / tokens array
                    tokens = item.get("tokens") or item.get("clobTokenIds") or []
                    if isinstance(tokens, list) and len(tokens) >= 2:
                        outcome_p = item.get("bestBid") or item.get("lastTradePrice")
                        if outcome_p is not None:
                            return float(outcome_p)
        except Exception as e:
            logger.debug(f"Gamma API direct lookup failed for {condition_id[:16]}: {e}")

        # Fallback: filtered markets list
        try:
            markets = polymarket_client.get_filtered_markets(
                max_results=500, min_volume=0
            )
            for m in markets:
                if m.condition_id == condition_id:
                    return m.yes_price
        except Exception:
            pass
        return None

    def _calc_pnl(self, bet_usdc: float, entry_price: float, won: bool) -> float:
        if entry_price <= 0:
            return 0.0
        shares = bet_usdc / entry_price
        return round(shares - bet_usdc, 2) if won else round(-bet_usdc, 2)

    def _save_learning_datapoint(self, trade: dict, outcome: str, pnl_usdc: float):
        """Extrahiert Features aus einem Trade-Datensatz und speichert sie."""
        ts_str = trade.get("timestamp", "")
        time_of_day = 12
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            time_of_day = ts.hour
        except Exception:
            pass

        self._db.save_learning_datapoint(
            trade_id=trade.get("id"),
            market_condition_id=trade.get("market_condition_id", ""),
            sport_category=trade.get("market_category", "unknown") or "unknown",
            confidence_at_bet=float(trade.get("confidence_at_bet") or 0),
            edge_at_bet=float(trade.get("edge_at_bet") or 0),
            weighted_score_at_bet=float(trade.get("weighted_score_at_bet") or 0),
            source_quality_score=float(trade.get("source_quality_at_bet") or 0),
            tier1_2_count=int(trade.get("tier1_2_count_at_bet") or 0),
            days_to_resolution_at_bet=int(trade.get("days_to_resolution_at_bet") or 0),
            market_volume_usd=float(trade.get("market_volume_usd") or 0),
            time_of_day_utc=time_of_day,
            outcome=outcome,
            pnl_usdc=pnl_usdc,
        )

    # =========================================================================
    # Threshold-Update
    # =========================================================================

    def should_update_thresholds(self) -> bool:
        """True wenn genug neue Outcomes fuer ein Threshold-Update vorliegen."""
        return self._new_outcomes_since_threshold >= THRESHOLD_UPDATE_INTERVAL

    def update_thresholds(self) -> list[str]:
        """
        Nach 10+ neuen aufgeloesten Bets:
        - Win-Rate per Kategorie berechnen → Blacklist / Bonus
        - Optimale Confidence/Edge Schwellenwerte finden
        - Aenderungen in adaptive_config speichern und via Telegram melden
        """
        logger.info("LearningEngine: Aktualisiere Schwellenwerte...")
        changes = []

        learning_data = self._db.get_learning_data()
        if not learning_data:
            logger.info("LearningEngine: Noch keine Daten fuer Schwellenwert-Update")
            self._new_outcomes_since_threshold = 0
            return changes

        # === 1. KATEGORIE-ANALYSE ===
        cat_stats: dict[str, dict] = {}
        for row in learning_data:
            cat = (row.get("sport_category") or "unknown").lower()
            if cat not in cat_stats:
                cat_stats[cat] = {"wins": 0, "total": 0}
            cat_stats[cat]["total"] += 1
            if row.get("outcome") == "WIN":
                cat_stats[cat]["wins"] += 1

        import config as cfg

        self._category_win_rates = {}
        self._category_sample_counts = {}

        for cat, stats in cat_stats.items():
            total = stats["total"]
            wins = stats["wins"]
            if total < MIN_CATEGORY_SAMPLES:
                continue

            win_rate = wins / total
            self._category_win_rates[cat] = win_rate
            self._category_sample_counts[cat] = total

            if win_rate < MIN_WIN_RATE_FOR_BLACKLIST:
                self._db.save_learning(
                    analysis_date=str(datetime.now(timezone.utc).date()),
                    insight_type="category_performance",
                    key=cat,
                    win_rate=win_rate,
                    total_bets=total,
                    recommendation="blacklist",
                    reasoning=(
                        f"Win-Rate {win_rate:.0%} unter {MIN_WIN_RATE_FOR_BLACKLIST:.0%} "
                        f"Schwellenwert ({total} Bets)"
                    ),
                    action_taken="auto_blacklisted",
                )
                changes.append(f"{cat} blacklisted ({win_rate:.0%} Win-Rate, {total} Bets)")
                logger.warning(f"  Kategorie '{cat}' blackgelistet: {win_rate:.0%}")

            elif win_rate > MIN_WIN_RATE_FOR_BONUS:
                self._db.save_learning(
                    analysis_date=str(datetime.now(timezone.utc).date()),
                    insight_type="category_performance",
                    key=cat,
                    win_rate=win_rate,
                    total_bets=total,
                    recommendation="increase_kelly",
                    reasoning=(
                        f"Win-Rate {win_rate:.0%} ueber {MIN_WIN_RATE_FOR_BONUS:.0%} "
                        f"— Bonus +{CATEGORY_CONFIDENCE_BONUS:.0%} Konfidenz"
                    ),
                    action_taken="category_bonus",
                )
                logger.info(f"  Kategorie '{cat}': Bonus ({win_rate:.0%} Win-Rate)")

        # === 2. OPTIMALER CONFIDENCE THRESHOLD ===
        conf_threshold = self._find_optimal_threshold(
            learning_data, "confidence_at_bet", min_win_rate=0.60
        )
        if conf_threshold is not None and self._adaptive_config:
            old_val = self._adaptive_config.get("MIN_CONFIDENCE", cfg.MIN_CONFIDENCE)
            new_val = round(max(0.75, min(0.95, conf_threshold)), 3)
            if abs(new_val - old_val) >= 0.01:
                self._adaptive_config.set(
                    "MIN_CONFIDENCE", new_val,
                    f"Learning: opt. Threshold (n={len(learning_data)} Bets)",
                )
                changes.append(f"New MIN_CONFIDENCE: {new_val:.2f} (war {old_val:.2f})")
                logger.info(f"  MIN_CONFIDENCE: {old_val:.3f} → {new_val:.3f}")

        # === 3. OPTIMALER EDGE THRESHOLD ===
        edge_threshold = self._find_optimal_threshold(
            learning_data, "edge_at_bet", min_win_rate=0.60
        )
        if edge_threshold is not None and self._adaptive_config:
            old_val = self._adaptive_config.get("MIN_EDGE", cfg.MIN_EDGE)
            new_val = round(max(0.08, min(0.25, edge_threshold)), 3)
            if abs(new_val - old_val) >= 0.01:
                self._adaptive_config.set(
                    "MIN_EDGE", new_val,
                    f"Learning: opt. Threshold (n={len(learning_data)} Bets)",
                )
                changes.append(f"New MIN_EDGE: {new_val:.2f} (war {old_val:.2f})")
                logger.info(f"  MIN_EDGE: {old_val:.3f} → {new_val:.3f}")

        self._new_outcomes_since_threshold = 0

        # Telegram-Benachrichtigung
        if changes and self._notifier:
            total_bets = self._db.get_learning_data_count()
            cat_summary = "; ".join(
                f"{cat}: {wr:.0%}"
                for cat, wr in list(self._category_win_rates.items())[:4]
            )
            msg = (
                f"<b>Learning Update ({total_bets} aufgeloeste Bets)</b>\n"
                + "\n".join(f"  {c}" for c in changes)
            )
            if cat_summary:
                msg += f"\n\nKategorien: {cat_summary}"
            self._notifier.send_message(msg)

        logger.info(
            f"Threshold-Update abgeschlossen: {len(changes)} Aenderungen | "
            f"{len(learning_data)} Datenpunkte"
        )
        return changes

    def _find_optimal_threshold(
        self, data: list, feature_key: str, min_win_rate: float = 0.60
    ) -> Optional[float]:
        """
        Findet den niedrigsten Feature-Wert, bei dem kumulierte Win-Rate >= min_win_rate.
        Sortiert absteigend und berechnet kumulierte Win-Rate von oben.
        Benoetigt mindestens 20 Datenpunkte.
        """
        if len(data) < 20:
            return None

        valid = [
            (row[feature_key], row["outcome"] == "WIN")
            for row in data
            if row.get(feature_key) is not None
        ]
        if len(valid) < 10:
            return None

        valid.sort(key=lambda x: x[0], reverse=True)

        cum_wins = 0
        optimal = None
        for i, (val, won) in enumerate(valid):
            if won:
                cum_wins += 1
            n = i + 1
            if n >= 10 and (cum_wins / n) >= min_win_rate:
                optimal = val  # Niedrigster Wert mit akzeptabler Win-Rate

        return optimal

    # =========================================================================
    # ML-Modell
    # =========================================================================

    def should_train_model(self) -> bool:
        """True wenn genug Daten fuer (Nach-)Training vorliegen."""
        total = self._db.get_learning_data_count()
        return total >= MODEL_TRAIN_THRESHOLD and self._new_outcomes_since_train >= MODEL_RETRAIN_INTERVAL

    def train_model(self):
        """
        Trainiert ein Logistisches Regressionsmodell nach 50+ Bets.
        Erfordert scikit-learn (optionale Abhaengigkeit).
        """
        try:
            from sklearn.linear_model import LogisticRegression
            from sklearn.preprocessing import StandardScaler
            from sklearn.model_selection import cross_val_score
            import numpy as np
        except ImportError:
            logger.warning("scikit-learn nicht installiert — ML-Modell nicht verfuegbar")
            return

        learning_data = self._db.get_learning_data()
        if len(learning_data) < MODEL_TRAIN_THRESHOLD:
            return

        features = []
        labels = []
        for row in learning_data:
            if row.get("outcome") not in ("WIN", "LOSS"):
                continue
            features.append([
                float(row.get("confidence_at_bet") or 0),
                float(row.get("edge_at_bet") or 0),
                float(row.get("weighted_score_at_bet") or 0),
                float(row.get("source_quality_score") or 0),
                float(row.get("days_to_resolution_at_bet") or 0),
            ])
            labels.append(1 if row["outcome"] == "WIN" else 0)

        if len(features) < MODEL_TRAIN_THRESHOLD:
            return

        logger.info(f"Trainiere ML-Modell mit {len(features)} Datenpunkten...")

        X = np.array(features)
        y = np.array(labels)

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        model = LogisticRegression(max_iter=1000, random_state=42)
        model.fit(X_scaled, y)

        # Cross-validation
        cv_folds = min(5, max(2, len(y) // 10))
        try:
            scores = cross_val_score(model, X_scaled, y, cv=cv_folds, scoring="accuracy")
            accuracy = float(scores.mean())
        except Exception:
            accuracy = float((model.predict(X_scaled) == y).mean())

        self._model = {"model": model, "scaler": scaler}
        self._model_accuracy = accuracy
        self._new_outcomes_since_train = 0

        try:
            with open(MODEL_PATH, "wb") as f:
                pickle.dump(self._model, f)
            logger.info(f"Modell gespeichert: {MODEL_PATH} | Accuracy: {accuracy:.1%}")
        except Exception as e:
            logger.error(f"Modell-Speicherung fehlgeschlagen: {e}")

        if self._notifier:
            win_rate = sum(labels) / len(labels) if labels else 0
            self._notifier.send_message(
                f"<b>ML-Modell trainiert</b>\n"
                f"Datenpunkte: {len(features)}\n"
                f"Accuracy (CV): {accuracy:.1%}\n"
                f"Win-Rate im Dataset: {win_rate:.1%}"
            )

    def _load_model(self):
        """Laedt ein gespeichertes Modell falls vorhanden."""
        try:
            if os.path.exists(MODEL_PATH):
                with open(MODEL_PATH, "rb") as f:
                    self._model = pickle.load(f)
                logger.info(f"ML-Modell geladen: {MODEL_PATH}")
        except Exception as e:
            logger.warning(f"Modell-Laden fehlgeschlagen (OK beim ersten Start): {e}")
            self._model = None

    def predict_win_probability(
        self,
        confidence: float,
        edge: float,
        weighted_score: float,
        source_quality: float,
        days_to_resolution: int,
    ) -> Optional[float]:
        """
        Sagt die WIN-Wahrscheinlichkeit voraus.
        Returns None wenn kein Modell verfuegbar.
        """
        if not self._model:
            return None

        try:
            import numpy as np
            scaler = self._model["scaler"]
            model = self._model["model"]
            X = np.array([[confidence, edge, weighted_score, source_quality, days_to_resolution]])
            X_scaled = scaler.transform(X)
            # Klasse 1 = WIN
            prob = model.predict_proba(X_scaled)[0][1]
            return float(prob)
        except Exception as e:
            logger.debug(f"Vorhersage fehlgeschlagen: {e}")
            return None

    # =========================================================================
    # Kategorie-Anpassungen
    # =========================================================================

    def _refresh_category_stats(self):
        """Laedt Kategorie-Win-Rates aus vorhandenen Learning-Daten."""
        try:
            data = self._db.get_learning_data()
            cat_stats: dict[str, dict] = {}
            for row in data:
                cat = (row.get("sport_category") or "unknown").lower()
                if cat not in cat_stats:
                    cat_stats[cat] = {"wins": 0, "total": 0}
                cat_stats[cat]["total"] += 1
                if row.get("outcome") == "WIN":
                    cat_stats[cat]["wins"] += 1

            for cat, stats in cat_stats.items():
                if stats["total"] >= MIN_CATEGORY_SAMPLES:
                    self._category_win_rates[cat] = stats["wins"] / stats["total"]
                    self._category_sample_counts[cat] = stats["total"]
        except Exception as e:
            logger.debug(f"Kategorie-Stats laden fehlgeschlagen: {e}")

    def get_category_adjustment(self, category: str) -> float:
        """
        Gibt Konfidenz-Anpassung fuer eine Kategorie zurueck.
        Bonus-Kategorie (>70% Win-Rate): +0.05
        Schlechte Kategorie (<45%): -0.05 (Blacklist-Schutz)
        Unbekannte Kategorie: 0.0
        """
        if not category:
            return 0.0

        cat_lower = category.lower()
        win_rate = self._category_win_rates.get(cat_lower)
        samples = self._category_sample_counts.get(cat_lower, 0)

        if win_rate is None or samples < MIN_CATEGORY_SAMPLES:
            return 0.0

        if win_rate > MIN_WIN_RATE_FOR_BONUS:
            return CATEGORY_CONFIDENCE_BONUS
        elif win_rate < MIN_WIN_RATE_FOR_BLACKLIST:
            return -0.05
        return 0.0

    def get_category_win_rates(self) -> dict[str, float]:
        """Gibt alle bekannten Kategorie-Win-Rates zurueck (fuer Pre-Filter)."""
        return dict(self._category_win_rates)

    def has_model(self) -> bool:
        return self._model is not None

    def get_model_accuracy(self) -> float:
        return self._model_accuracy
