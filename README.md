# Polymarket Investment Agent 2

Event-driven Multi-Agent Trading-System fuer Polymarket mit SQLite Event Bus, Risk-Limits und 24/7 Betrieb.

**Repository:** [github.com/vinchafer/polymarket-investment-agend-2](https://github.com/vinchafer/polymarket-investment-agend-2)

**Vercel-Dashboard (öffentlich):** [vercel-dashboard-lyart-pi.vercel.app](https://vercel-dashboard-lyart-pi.vercel.app) — setze dort `NEXT_PUBLIC_API_BASE` auf deine VPS-API-URL, sonst zeigt die Seite einen Hinweis.

## Was umgesetzt ist

- 6-Agent Architektur:
  - Agent 0: Efficiency Check (Preisdifferenz-Gate)
  - Agent 1: Scout (Top-Wallet Signals)
  - Agent 2: Analyst
  - Agent 3: Devil's Advocate
  - Agent 4: Risk Officer + Execution Gate
  - Agent 5: Position Monitor
  - Agent 6: Portfolio Manager (Telegram Report)
- Zentrale `agent_events` Tabelle als einziges Kommunikationsmedium.
- Risk-Limits:
  - Max 15 offene Positionen
  - Max 5 Positionen pro Sektor
  - Max 35 USDC Capital at Risk
  - Max 5 USDC pro Bet
  - Daily Loss Stop bei -10 USDC
- Dry-Run Modus fuer sicheres Testen.
- systemd Service-Unit fuer VPS Deploy.
- Phase-2 Core:
  - Gemini + Groq Integrationen mit JSON-Outputs und Fallback
  - externe Research-Adapter (ESPN, Serper, NewsAPI, Tavily)
  - The-Odds-basierter Effizienz-Hinweis fuer Sports-Maerkte
  - in-memory API-Budget-Guards fuer Free-Tier-Schutz
  - Paper-Execution-Log (`executions`) mit Slippage-Simulation

## Wichtiger Hinweis

Dieses System ist technisch aufgesetzt, aber **kein Garant fuer Gewinne**. Prediction Markets tragen Totalverlustrisiko. Verwende zuerst Dry-Run, dann Paper-Trade, erst danach echtes Kapital.

## Setup lokal / VPS

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Dann `.env` befuellen.

Start:

```bash
polymarket-agent
```

## Monitoring (Dashboard)

Lokal im zweiten Terminal:

```bash
polymarket-dashboard
```

Dann im Browser: `http://127.0.0.1:8765` (JSON unter `/api/summary`).

Auf dem VPS ohne öffentliches Exponieren: per SSH-Tunnel vom Laptop:

```bash
ssh -L 8765:127.0.0.1:8765 user@dein-vps
```

Danach ebenfalls `http://127.0.0.1:8765` lokal öffnen.

### Vercel (iPhone / unterwegs)

- Das Python-Dashboard muss unter **HTTPS öffentlich** erreichbar sein (Reverse-Proxy empfohlen), z.B. `https://agent.deine-domain.tld` → Port 8765.
- Setze `DASHBOARD_CORS_ORIGINS=https://dein-projekt.vercel.app` und optional `DASHBOARD_READ_TOKEN` (gleicher Wert als `NEXT_PUBLIC_DASHBOARD_TOKEN` im Vercel-Projekt).
- Vercel-App liegt in `vercel-dashboard/` (Root in Vercel auf diesen Ordner stellen). Siehe `vercel-dashboard/README.md`.

## Scout: dynamische Top-Wallets

Der **Wallet Curator** holt regelmässig die Top-Adressen von `GET /v1/leaderboard` (Polymarket Data API) und speichert sie in `tracked_wallets`.
Der Scout nutzt echte `0x`-Adressen und `GET /positions?user=…`.

## Paper-Attribution

Pro ausgeführtem Paper-Trade schreibt das System eine Zeile in `attribution_signals` (Signalpreis, Fill, Slippage, Stake, ETA bis Resolution).

## Order-Typen (Empfehlung)

Für illiquide Märkte sind **Limit- / Post-Only-Orders** mit explizitem Slippage-Cap meist robuster als reine Market-Orders.
Für Live-CLOB setzen wir später standardmässig **limit nahe Mid** mit Schutzband — nicht „eine Orderart für alles“, sondern abhängig von Spread und Liquidität.

## API Keys / env.txt

Das Projekt lädt automatisch `.env`, `env.txt` im Repo-Root sowie (falls vorhanden)
`%USERPROFILE%\.cursor\projects\c-Users-vinch-polymarket-investment-agend-2\env.txt`.
Alternativ setze `EXTRA_ENV_FILE` auf den Pfad deiner Secrets-Datei.

**Wichtig:** Committe niemals API-Keys. `env.txt` und `.env` stehen in `.gitignore`.

Empfohlene Modi:

- `EXECUTION_MODE=paper` fuer Shadow/Paper
- `EXECUTION_MODE=live` nur nach verifizierter Paper-Phase

## systemd Deployment (Ubuntu)

```bash
sudo cp deploy/polymarket-agent.service /etc/systemd/system/polymarket-agent.service
sudo systemctl daemon-reload
sudo systemctl enable polymarket-agent
sudo systemctl start polymarket-agent
sudo journalctl -u polymarket-agent -f
```

## Architektur-Flow

1. Scout findet neue Wallet-Positionen (max 2h alt)
2. Efficiency Check filtert effiziente Maerkte raus
3. Analyst bewertet Value + Confidence
4. Devil's Advocate versucht Entscheidung zu kippen
5. Risk Officer prueft Portfolio-Limits
6. Execution platziert Bet (oder Dry-Run-Record)
7. Monitor + Portfolio Manager erstellen Alerts/Reports

## Naechste Ausbaustufen (empfohlen)

- Echte Gemini/Groq JSON-Prompts mit strict schema validation
- Echte Odds/ESPN/News/Tavily Adapter inkl. Rate Limit Layer
- Kalshi-Pricing als zweiter Effizienz-Kanal
- Real CLOB Order Routing + signed transaction path
- Backtest + Walk-forward Validation + Performance Attribution
- Learning Loop (welche Wallets/Sektoren wirklich Edge liefern)
