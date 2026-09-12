# Polymarket Investment Agents (A1 + A2)

Zwei-Arm-Setup für automatisiertes LLM-gestütztes Trading auf Polymarket, betrieben auf einem Hetzner-VPS (mittlerweile stillgelegt, Stand 2026-09-12).

- **agent1/** — A1, LLM-Analyst-Arm. Bewertet Märkte via Groq/Gemini/GitHub-Models, trifft eigene Entscheidungen (Scout → Analyst → Risk → Devil's-Advocate → Portfolio).
- **agent2/** — A2, deterministischer Copy-Arm (Klon von A1). Kopiert Fills unabhängig von A1s Analyst/Devil-Veto, fixe mittlere Positionsgröße. Diente als A/B-Vergleich gegen A1.
- **compare.sh / ab_compare.sh** — Vergleichsreports A1 vs. A2 (P&L, offene Positionen, Divergence-Set).
- **deploy/** — systemd unit files + nginx config, wie zuletzt auf dem Server aktiv.

Beide Arme liefen durchgängig mit `DRY_RUN=true` (Paper-Trading, kein Live-Kapital im Risiko).

## Setup

```bash
cd agent1  # oder agent2
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env   # Werte eintragen
venv/bin/python main.py
```

Env-Variablen: siehe `.env.example` je Agent (nur Namen, keine Werte im Repo).

## Letzter Stand vor Decommission (2026-09-12)

| Arm | Trades | Resolved | Wins | Losses | P&L (USDC) |
|---|---|---|---|---|---|
| A1-LLM | 135 | 135 | 83 | 52 | -8.67 |
| A2-COPY | 309 | 300 | 170 | 130 | -99.91 |

Divergence-Set (Effekt von A1s Analyst/Devil-Veto auf A2s Fills): 189 divergente Fills, 180 resolved, A2-P&L auf diesen: -93.14 USDC (negativ = A1-Veto hat effektiv Geld gespart).

Server wurde nach diesem Stand gestoppt, alle Daten (DBs, Logs, Env-Werte) liegen im lokalen Backup-Archiv, nicht in diesem Repo.
