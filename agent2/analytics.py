"""
analytics.py — Wochentliche Selbst-Analyse & Performance-Tracking

Jeden Montag 08:00 UTC:
1. Lade alle aufgeloesten Trades der letzten 7 Tage
2. Berechne Performance-Statistiken (nach Kategorie, Konfidenz, Edge, etc.)
3. Claude analysiert und generiert Handlungsempfehlungen auf Deutsch
4. Speichere Erkenntnisse in agent_learnings Tabelle
5. Blackliste Kategorien mit <45% Win-Rate ueber 20+ Bets
6. Aktualisiere agent_overrides.json fuer dynamische Schwellenwerte
7. Sende vollstaendigen Report via Telegram
"""

import json
import logging
import os
from datetime import datetime, timezone, date
from typing import Optional

from groq import Groq

import config

logger = logging.getLogger(__name__)

OVERRIDES_FILE = "agent_overrides.json"

WEEKLY_ANALYSIS_PROMPT = """Du bist der Performance-Analyst eines Polymarket AI Trading Agents.
Analysiere die folgenden Daten der letzten 7 Tage und generiere praezise, umsetzbare Erkenntnisse auf Deutsch.

TRADING-PERFORMANCE LETZTE 7 TAGE:
{performance_data}

PERFORMANCE NACH KATEGORIE:
{category_data}

PERFORMANCE NACH KONFIDENZ-BUCKET:
{confidence_data}

PERFORMANCE NACH EDGE-GROESSE:
{edge_data}

AKTUELLE AGENT-KONFIGURATION:
- Min. Konfidenz: {min_confidence:.0%}
- Min. Edge: {min_edge:.0%}
- Min. Weighted Score: 7.5/10
- Portfolio: {portfolio_usdc} USDC

AUFGABE:
Analysiere systematisch und generiere ein JSON-Objekt mit:

1. "summary": Kurze Gesamtbewertung (2-3 Saetze auf Deutsch)

2. "category_insights": Liste von Kategorien-Erkenntnissen:
   - category: Name der Kategorie
   - win_rate: Win-Rate als Dezimalzahl
   - total_bets: Anzahl Bets
   - recommendation: "blacklist" | "reduce_kelly" | "increase_kelly" | "ok" | "insufficient_data"
   - reasoning: Erklaerung auf Deutsch (1-2 Saetze)

3. "threshold_adjustments": Vorgeschlagene Konfigurationsaenderungen:
   - min_confidence_adjustment: float (z.B. +0.05 oder -0.03, Bereich 0.75-0.95)
   - min_edge_adjustment: float (z.B. +0.02 oder 0.0)
   - reasoning: Erklaerung auf Deutsch

4. "key_insights": Liste von 3-5 wichtigsten Erkenntnissen auf Deutsch
   (z.B. "NFL-Bets haben 34% Win-Rate - deutlich unter Zufall. Sofort blacklisten.")

5. "action_items": Liste konkreter Massnahmen

Antworte NUR mit validem JSON, kein anderer Text.
"""


class PerformanceAnalytics:
    """
    Wochentliche Selbst-Analyse und Performance-Tracking.
    Generiert Erkenntnisse und passt Konfiguration dynamisch an.
    """

    def __init__(self, db, notifier=None):
        """
        Args:
            db: TradeLogger Instanz
            notifier: TelegramNotifier (optional)
        """
        self.db = db
        self.notifier = notifier
        self.groq_client = Groq(api_key=config.GROQ_API_KEY) if config.GROQ_API_KEY else None
        self._last_weekly_analysis_date: Optional[date] = None

    def should_run_weekly_analysis(self) -> bool:
        """Prueft ob die wochentliche Analyse jetzt laufen soll (Montag 08:00 UTC)."""
        now = datetime.now(timezone.utc)
        today = now.date()
        is_monday_morning = (now.weekday() == 0 and now.hour >= 8)
        already_ran_today = (self._last_weekly_analysis_date == today)
        return is_monday_morning and not already_ran_today

    def run_weekly_analysis(self) -> dict:
        """
        Fuehrt die vollstaendige wochentliche Selbst-Analyse durch.
        Gibt strukturierten Report zurueck.
        """
        logger.info("=== Starte wochentliche Selbst-Analyse ===")
        analysis_date = str(date.today())
        self._last_weekly_analysis_date = date.today()

        try:
            # 1. Performance-Daten sammeln
            perf = self.db.get_performance_summary(days=7)
            by_category = self.db.get_performance_by_category(days=7)
            by_confidence = self.db.get_performance_by_confidence_bucket(days=7)
            by_edge = self.db.get_performance_by_edge_bucket(days=7)
            trades = self.db.get_resolved_trades_for_analysis(days=7)

            if not trades:
                logger.info("Keine Trades in den letzten 7 Tagen fuer Analyse")
                return {"status": "no_data", "message": "Keine aufgeloesten Trades gefunden"}

            # 2. Stats berechnen
            performance_text = self._format_performance_for_prompt(perf, trades)
            category_text = self._format_category_stats(by_category)
            confidence_text = self._format_bucket_stats(by_confidence, "Konfidenz")
            edge_text = self._format_bucket_stats(by_edge, "Edge")

            # 3. Claude-Analyse
            prompt = WEEKLY_ANALYSIS_PROMPT.format(
                performance_data=performance_text,
                category_data=category_text,
                confidence_data=confidence_text,
                edge_data=edge_text,
                min_confidence=config.MIN_CONFIDENCE,
                min_edge=config.MIN_EDGE,
                portfolio_usdc=config.PORTFOLIO_USDC,
            )

            try:
                raw = self._call_llm(prompt)
            except Exception as e:
                logger.warning(f"Wochentlicher Report uebersprungen — Groq fehlgeschlagen: {e}")
                return {"status": "skipped", "reason": str(e)}

            insights = self._parse_insights(raw)

            if not insights:
                logger.error("Konnte Insights nicht parsen")
                return {"status": "parse_error", "raw": raw[:500]}

            # 4. Erkenntnisse in DB speichern
            self._save_insights(insights, analysis_date, by_category)

            # 5. Dynamische Konfiguration anpassen
            overrides = self._apply_threshold_adjustments(insights)

            # 6. Telegram-Report senden
            if self.notifier:
                self._send_weekly_report(insights, perf, overrides)

            logger.info(f"Wochentliche Analyse abgeschlossen: {len(insights.get('category_insights', []))} Kategorien analysiert")
            return {"status": "ok", "insights": insights, "overrides_applied": overrides}

        except Exception as e:
            logger.error(f"Fehler bei wochentlicher Analyse: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    def _call_llm(self, prompt: str) -> str:
        """Calls Groq. Raises on failure — caller should skip the report."""
        if not self.groq_client:
            raise RuntimeError("Groq client nicht konfiguriert")

        resp = self.groq_client.chat.completions.create(
            model=config.GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        logger.info(f"  Analytics: Groq ({config.GROQ_MODEL})")
        return resp.choices[0].message.content

    def _format_performance_for_prompt(self, perf: dict, trades: list) -> str:
        """Formatiert Performance-Daten als Text fuer den Prompt."""
        if "error" in perf:
            return "Keine Daten verfuegbar"

        t = perf.get("trades", {})
        f = perf.get("financials", {})

        lines = [
            f"Trades gesamt: {t.get('total', 0)}",
            f"Aufgeloest: {t.get('resolved', 0)} (Wins: {t.get('wins', 0)}, Losses: {t.get('losses', 0)})",
            f"Win-Rate: {t.get('win_rate', '0%')}",
            f"Investiert: {f.get('total_invested_usdc', 0):.2f} USDC",
            f"P&L: {f.get('total_pnl_usdc', 0):+.2f} USDC",
            f"ROI: {f.get('roi_pct', '0%')}",
            f"Avg P&L pro Trade: {f.get('avg_pnl_per_trade', 0):+.3f} USDC",
        ]
        return "\n".join(lines)

    def _format_category_stats(self, by_category: list) -> str:
        if not by_category:
            return "Keine Kategorie-Daten verfuegbar"
        lines = []
        for c in by_category:
            total = c.get("total_bets", 0)
            wins = c.get("wins", 0)
            wr = wins / max(total, 1)
            pnl = c.get("total_pnl", 0) or 0
            lines.append(
                f"  {c.get('market_category', '?')}: {wr:.0%} Win-Rate | "
                f"{total} Bets | P&L: {pnl:+.2f} USDC"
            )
        return "\n".join(lines) if lines else "Keine Daten"

    def _format_bucket_stats(self, buckets: list, label: str) -> str:
        if not buckets:
            return f"Keine {label}-Daten verfuegbar"
        lines = []
        for b in buckets:
            bucket_key = list(b.keys())[0]
            total = b.get("total_bets", 0)
            wins = b.get("wins", 0)
            wr = wins / max(total, 1)
            avg_pnl = b.get("avg_pnl", 0) or 0
            lines.append(
                f"  {b.get(bucket_key, '?')}: {wr:.0%} Win-Rate | "
                f"{total} Bets | Avg P&L: {avg_pnl:+.3f} USDC"
            )
        return "\n".join(lines) if lines else "Keine Daten"

    def _parse_insights(self, raw: str) -> Optional[dict]:
        """Parst Claude's JSON-Antwort."""
        import re
        try:
            return json.loads(raw.strip())
        except Exception:
            pass
        code = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if code:
            try:
                return json.loads(code.group(1))
            except Exception:
                pass
        obj = re.search(r'\{.*"summary".*\}', raw, re.DOTALL)
        if obj:
            try:
                return json.loads(obj.group(0))
            except Exception:
                pass
        return None

    def _save_insights(self, insights: dict, analysis_date: str, by_category: list):
        """Speichert Erkenntnisse in der Datenbank."""
        # Kategorie-Erkenntnisse
        for cat_insight in insights.get("category_insights", []):
            category = cat_insight.get("category", "unknown")
            win_rate = float(cat_insight.get("win_rate", 0))
            total_bets = int(cat_insight.get("total_bets", 0))
            recommendation = cat_insight.get("recommendation", "ok")
            reasoning = cat_insight.get("reasoning", "")

            self.db.save_learning(
                analysis_date=analysis_date,
                insight_type="category_performance",
                key=category,
                win_rate=win_rate,
                total_bets=total_bets,
                recommendation=recommendation,
                reasoning=reasoning,
                action_taken=f"Blacklisted: {recommendation == 'blacklist'}",
            )

        # Schwellenwert-Anpassungen als Erkenntnisse speichern
        adj = insights.get("threshold_adjustments", {})
        if adj:
            self.db.save_learning(
                analysis_date=analysis_date,
                insight_type="threshold_adjustment",
                key="config",
                win_rate=0,
                total_bets=0,
                recommendation="adjust",
                reasoning=adj.get("reasoning", ""),
                action_taken=json.dumps(adj),
            )

        logger.info(f"Erkenntnisse gespeichert: {len(insights.get('category_insights', []))} Kategorien")

    def _apply_threshold_adjustments(self, insights: dict) -> dict:
        """
        Passt Konfigurationsparameter dynamisch an via agent_overrides.json.
        Gibt angewendete Aenderungen zurueck.
        """
        adj = insights.get("threshold_adjustments", {})
        if not adj:
            return {}

        # Aktuelle Overrides laden
        overrides = {}
        if os.path.exists(OVERRIDES_FILE):
            try:
                with open(OVERRIDES_FILE) as f:
                    overrides = json.load(f)
            except Exception:
                pass

        changes = {}

        # Min. Konfidenz anpassen
        conf_adj = float(adj.get("min_confidence_adjustment", 0))
        if abs(conf_adj) >= 0.01:
            current_conf = overrides.get("MIN_CONFIDENCE", config.MIN_CONFIDENCE)
            new_conf = max(0.75, min(0.95, current_conf + conf_adj))
            overrides["MIN_CONFIDENCE"] = round(new_conf, 3)
            changes["MIN_CONFIDENCE"] = f"{current_conf:.3f} -> {new_conf:.3f}"

        # Min. Edge anpassen
        edge_adj = float(adj.get("min_edge_adjustment", 0))
        if abs(edge_adj) >= 0.01:
            current_edge = overrides.get("MIN_EDGE", config.MIN_EDGE)
            new_edge = max(0.08, min(0.25, current_edge + edge_adj))
            overrides["MIN_EDGE"] = round(new_edge, 3)
            changes["MIN_EDGE"] = f"{current_edge:.3f} -> {new_edge:.3f}"

        # Zeitstempel
        overrides["last_updated"] = datetime.now(timezone.utc).isoformat()
        overrides["last_analysis_date"] = str(date.today())

        # Speichern
        try:
            with open(OVERRIDES_FILE, "w") as f:
                json.dump(overrides, f, indent=2)
            if changes:
                logger.info(f"agent_overrides.json aktualisiert: {changes}")
        except Exception as e:
            logger.error(f"Fehler beim Speichern der Overrides: {e}")

        return changes

    def _send_weekly_report(self, insights: dict, perf: dict, overrides: dict):
        """Sendet den wochentlichen Report via Telegram."""
        t = perf.get("trades", {})
        f = perf.get("financials", {})

        summary = insights.get("summary", "Keine Zusammenfassung verfuegbar")
        key_insights = insights.get("key_insights", [])
        action_items = insights.get("action_items", [])

        # Blacklistete Kategorien
        blacklisted = [
            c["category"]
            for c in insights.get("category_insights", [])
            if c.get("recommendation") == "blacklist"
        ]

        text = (
            f"<b>Wochentlicher Performance-Report</b>\n"
            f"{datetime.now(timezone.utc).strftime('%d.%m.%Y')}\n\n"
            f"<b>Letzte 7 Tage:</b>\n"
            f"  Trades: {t.get('wins', 0)}W / {t.get('losses', 0)}L ({t.get('win_rate', '?')})\n"
            f"  P&L: {f.get('total_pnl_usdc', 0):+.2f} USDC\n\n"
            f"<b>Zusammenfassung:</b>\n{summary}\n\n"
        )

        if key_insights:
            text += "<b>Wichtigste Erkenntnisse:</b>\n"
            for insight in key_insights[:4]:
                text += f"  - {insight}\n"
            text += "\n"

        if blacklisted:
            text += f"<b>Blackgelistet:</b> {', '.join(blacklisted)}\n\n"

        if overrides:
            text += f"<b>Konfigurationsanpassungen:</b>\n"
            for k, v in overrides.items():
                if k not in ("last_updated", "last_analysis_date"):
                    text += f"  {k}: {v}\n"

        if action_items:
            text += "\n<b>Massnahmen:</b>\n"
            for item in action_items[:3]:
                text += f"  - {item}\n"

        self.notifier.send_message(text)

    def get_category_info(self, category: str) -> dict:
        """
        Gibt historische Performance-Info fuer eine Kategorie zurueck.
        Wird vom Agent-Prompt verwendet.
        """
        learnings = self.db.get_active_learnings("category_performance")
        for l in learnings:
            if l["key"].lower() == category.lower():
                return {
                    "win_rate": l["win_rate"] or 0,
                    "total_bets": l["total_bets"] or 0,
                    "recommendation": l["recommendation"],
                    "blacklisted": l["recommendation"] == "blacklist",
                    "reasoning": l["reasoning"],
                }
        return {"win_rate": 0, "total_bets": 0, "recommendation": "insufficient_data", "blacklisted": False}

    def get_blacklisted_categories(self) -> set:
        """Gibt alle aktuell blackgelisteten Kategorien zurueck."""
        return self.db.get_blacklisted_categories()

    def analyze_losses(self) -> dict:
        """
        Analyse aller verloren Trades (won=0, resolved=1).
        Liefert Muster nach Kategorie, Konfidenz und Quellqualitaet.
        Kann in jedem 6h-Report enthalten sein.

        Returns dict with:
            total_losses, avg_confidence, avg_edge, avg_source_quality,
            worst_categories [{category, losses, avg_pnl}],
            high_conf_losses (Verluste mit Konfidenz > 0.85),
            summary (1-2 Saetze)
        """
        try:
            import sqlite3
            conn = sqlite3.connect(self.db.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT market_category, confidence_at_bet, edge_at_bet,
                       source_quality_at_bet, weighted_score_at_bet, pnl_usdc
                FROM trades
                WHERE won = 0 AND resolved = 1
                ORDER BY timestamp DESC
            """).fetchall()
            conn.close()

            if not rows:
                return {"total_losses": 0, "summary": "Keine Verluste bisher."}

            losses = [dict(r) for r in rows]
            total = len(losses)

            avg_conf = sum(r["confidence_at_bet"] or 0 for r in losses) / total
            avg_edge = sum(abs(r["edge_at_bet"] or 0) for r in losses) / total
            avg_sq = sum(r["source_quality_at_bet"] or 0 for r in losses) / total
            total_pnl = sum(r["pnl_usdc"] or 0 for r in losses)

            # Category breakdown
            cat_stats: dict[str, dict] = {}
            for r in losses:
                cat = (r["market_category"] or "unknown").lower()
                if cat not in cat_stats:
                    cat_stats[cat] = {"losses": 0, "pnl": 0.0}
                cat_stats[cat]["losses"] += 1
                cat_stats[cat]["pnl"] += r["pnl_usdc"] or 0

            worst_categories = sorted(
                [{"category": k, "losses": v["losses"], "avg_pnl": v["pnl"] / v["losses"]}
                 for k, v in cat_stats.items()],
                key=lambda x: x["losses"],
                reverse=True,
            )[:5]

            # High-confidence losses (>85%) — concerning
            high_conf_losses = sum(1 for r in losses if (r["confidence_at_bet"] or 0) > 0.85)

            # Pattern summary
            top_cat = worst_categories[0]["category"] if worst_categories else "unknown"
            summary_parts = [f"{total} Verluste gesamt (P&L: {total_pnl:+.2f} USDC)."]
            if avg_conf > 0.85:
                summary_parts.append(f"Durchschn. Konfidenz war hoch ({avg_conf:.0%}) — Modell war ueberconfident.")
            if high_conf_losses > total * 0.4:
                summary_parts.append(f"{high_conf_losses} Verluste bei Konfidenz >85% — besorgniserregend.")
            if worst_categories:
                summary_parts.append(f"Meiste Verluste in Kategorie: {top_cat} ({worst_categories[0]['losses']}x).")

            return {
                "total_losses": total,
                "total_pnl": total_pnl,
                "avg_confidence": avg_conf,
                "avg_edge": avg_edge,
                "avg_source_quality": avg_sq,
                "worst_categories": worst_categories,
                "high_conf_losses": high_conf_losses,
                "summary": " ".join(summary_parts),
            }
        except Exception as e:
            logger.warning(f"analyze_losses failed: {e}")
            return {"total_losses": 0, "summary": f"Analyse fehlgeschlagen: {e}"}


def load_overrides() -> dict:
    """Laedt dynamische Konfigurationsoverrides aus agent_overrides.json."""
    if os.path.exists(OVERRIDES_FILE):
        try:
            with open(OVERRIDES_FILE) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Konnte {OVERRIDES_FILE} nicht laden: {e}")
    return {}
