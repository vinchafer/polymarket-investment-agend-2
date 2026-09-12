# 🤖 Polymarket AI Trading Agent

Ein vollautomatischer KI-Agent der auf Polymarket investiert — basierend auf Internet-Recherche (Tavily) und Claude-Analyse. Läuft 24/7 auf einem VPS ohne dass dein PC eingeschaltet sein muss.

---

## 🏗️ Architektur

```
Cron-Schleife (alle 2h)
        ↓
1. Polymarket API  →  Märkte laden & filtern
        ↓
2. Tavily API      →  Internet-Recherche pro Markt
        ↓
3. Claude API      →  Analysiert & entscheidet (BET_YES / BET_NO / SKIP)
        ↓
4. Risk Manager    →  Prüft Limits & Einsatzgröße
        ↓
5. Polymarket API  →  Order platzieren
        ↓
6. SQLite + Telegram → Loggen & Benachrichtigen
```

---

## 📁 Projektstruktur

```
polymarket-agent/
├── main.py           # Haupt-Orchestrierung & CLI
├── config.py         # Alle Einstellungen (aus .env)
├── polymarket.py     # Polymarket CLOB API Wrapper
├── researcher.py     # Internet-Recherche via Tavily
├── agent.py          # Claude AI Entscheidungs-Engine
├── risk_manager.py   # Risikomanagement & Position Tracking
├── logger.py         # SQLite Trade-Logging
├── notifier.py       # Telegram Benachrichtigungen
├── requirements.txt  # Python-Pakete
├── .env.example      # Konfigurationsvorlage (kopieren zu .env)
├── deploy.sh         # Automatisches VPS-Setup Script
└── README.md         # Diese Datei
```

---

## ⚙️ Schritt-für-Schritt Setup

### Schritt 1: API Keys besorgen

**Claude API Key:**
1. Gehe zu https://console.anthropic.com/settings/keys
2. Klicke "Create Key"
3. Kopiere den Key (beginnt mit `sk-ant-...`)
4. Lade dein Konto mit mind. $5 auf

**Tavily API Key (kostenlos):**
1. Gehe zu https://app.tavily.com
2. Registriere dich kostenlos
3. Kopiere deinen API Key (beginnt mit `tvly-...`)
4. Kostenloses Tier: 1.000 Anfragen/Monat (reicht für ~200 Trades)

**Polymarket Private Key:**
1. Öffne MetaMask (oder installiere es: https://metamask.io)
2. Erstelle eine neue Wallet NUR für den Trading Agent (nie deine Haupt-Wallet!)
3. Gehe zu: Konto → drei Punkte → "Account Details"
4. Klicke "Export Private Key" (Passwort eingeben)
5. Kopiere den Key (beginnt mit `0x...`)

> ⚠️ **WICHTIG:** Erstelle eine SEPARATE Wallet nur für den Agenten. Lade sie nur mit dem Betrag auf, den du maximal verlieren kannst!

### Schritt 2: USDC auf Polygon aufladen

Der Agent handelt mit USDC auf dem Polygon-Netzwerk:

1. Kaufe USDC auf einer Exchange (z.B. Coinbase, Kraken, Binance)
2. Sende USDC an deine MetaMask Wallet
3. Stelle sicher dass du auf dem **Polygon**-Netzwerk bist (nicht Ethereum!)
4. Empfehlung für Anfang: 50-100 USDC

> Polygon-USDC Addresse: `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`

### Schritt 3: Telegram Bot einrichten (empfohlen)

1. Öffne Telegram → suche `@BotFather`
2. Sende `/newbot`
3. Name und Username vergeben
4. Token kopieren (sieht aus wie: `1234567890:ABC...`)
5. Starte deinen Bot (klicke auf den Link den BotFather sendet)
6. Finde deine Chat-ID: suche `@userinfobot` → sende `/start`

### Schritt 4: Lokal testen

```bash
# Projekt klonen / Dateien ablegen
cd polymarket-agent/

# .env Datei erstellen
cp .env.example .env
nano .env  # Alle Keys eintragen

# Dependencies installieren
pip install -r requirements.txt

# Einmaliger Test-Durchlauf (DRY RUN)
python main.py --once

# Status anzeigen
python main.py --status
```

### Schritt 5: VPS aufsetzen (Hetzner empfohlen)

**VPS buchen:**
1. Gehe zu https://hetzner.com/cloud
2. Erstelle neuen Server: CX22 (2 vCPU, 4GB RAM) — ca. €4/Monat
3. Ubuntu 22.04 auswählen
4. SSH Key hinzufügen (empfohlen) oder Passwort merken
5. Server erstellen und IP-Adresse notieren

**Verbinden und Setup:**
```bash
# Mit VPS verbinden
ssh root@DEINE_VPS_IP

# Projektdateien hochladen (von deinem lokalen PC)
scp -r polymarket-agent/ root@DEINE_VPS_IP:~/

# Auf VPS: Setup-Script ausführen
cd ~/polymarket-agent
chmod +x deploy.sh
./deploy.sh
```

**Agent starten:**
```bash
# .env auf dem VPS befüllen
nano ~/polymarket-agent/.env

# Agent als Service starten (läuft auch nach Neustart)
sudo systemctl start polymarket-agent
sudo systemctl enable polymarket-agent

# Logs verfolgen (live)
sudo journalctl -u polymarket-agent -f
```

---

## 🎮 Bedienung

### Agent starten
```bash
# DRY RUN (kein echtes Geld) — Standard
python main.py

# Einmaliger Durchlauf
python main.py --once

# Live Trading (ECHTES GELD)
python main.py --live
```

### Status & Monitoring
```bash
# Aktuellen Status anzeigen
python main.py --status

# Performance der letzten 30 Tage
python main.py --performance

# Logs live verfolgen
tail -f agent.log

# VPS Service Status
sudo systemctl status polymarket-agent

# Live-Logs des Services
sudo journalctl -u polymarket-agent -f
```

### Agent stoppen
```bash
# Service stoppen
sudo systemctl stop polymarket-agent

# Autostart deaktivieren
sudo systemctl disable polymarket-agent
```

---

## ⚠️ Risikomanagement

Der Agent hat mehrere Schutzebenen:

| Schutz | Standard | Beschreibung |
|--------|----------|--------------|
| Max Bet | 5 USDC | Maximaler Einsatz pro Trade |
| Daily Loss Limit | 25 USDC | Agent stoppt wenn täglich mehr verloren |
| Max Positionen | 5 | Maximal 5 gleichzeitige Wetten |
| Min. Konfidenz | 75% | Claude muss mind. 75% sicher sein |
| Min. Edge | 7% | Agent braucht mind. 7% Vorteil gegenüber Markt |
| DRY RUN | true | Standard: kein echtes Geld |

Alle Werte können in `.env` angepasst werden.

**Empfohlene Strategie für den Start:**
1. Mindestens 1-2 Wochen im DRY RUN laufen lassen
2. Performance analysieren (`python main.py --performance`)
3. Erst dann auf LIVE umstellen mit kleinen Beträgen (2-5 USDC pro Bet)

---

## 💰 Geschätzte Kosten

| Komponente | Kosten |
|------------|--------|
| Hetzner VPS CX22 | ~€4/Monat |
| Claude API (Haiku) | ~$0.001 pro Analyse |
| Tavily API | Kostenlos (1k req/Monat) |
| Polygon Gas | Minimal (~$0.001 pro Trade) |
| **Gesamt** | **~€5-8/Monat** + Trading-Kapital |

Bei 15 Analysen/Durchlauf, alle 2h = ~180 Analysen/Tag = ~$0.18/Tag API-Kosten

---

## 🔧 Konfiguration anpassen

Alle Einstellungen in `.env`:

```bash
# Agressivere Strategie (mehr Bets)
MIN_CONFIDENCE=0.65
MIN_EDGE=0.05
MAX_MARKETS_TO_ANALYZE=25

# Konservativere Strategie (weniger, sichere Bets)
MIN_CONFIDENCE=0.85
MIN_EDGE=0.10
MAX_BET_USDC=2.0

# Häufiger analysieren
RUN_INTERVAL_MINUTES=60

# Nur Politik-Märkte
PREFERRED_CATEGORIES=politics
```

---

## 🐛 Häufige Probleme

**"POLYMARKET_PRIVATE_KEY ist nicht gesetzt"**
→ .env Datei fehlt oder Key nicht eingetragen

**"Polymarket Client Initialisierung fehlgeschlagen"**
→ Private Key falsch, oder keine Internetverbindung zum VPS

**"Keine Märkte gefunden"**
→ Filter zu streng (MIN_MARKET_VOLUME, MAX_DAYS_TO_RESOLUTION verringern)

**Telegram Nachrichten kommen nicht an**
→ Bot-Token oder Chat-ID falsch. Teste: `python -c "from notifier import TelegramNotifier; TelegramNotifier().test_connection()"`

---

## 📊 Performance verbessern

Tipps für bessere Ergebnisse:

1. **Kategorien filtern**: Fokussiere auf Bereiche wo du/Claude stärker ist
2. **Edge erhöhen**: Höheres `MIN_EDGE` = weniger, dafür qualitativ bessere Bets
3. **Claude Modell**: Wechsel zu `claude-sonnet-4-6` für bessere Analyse (teurer)
4. **Häufigkeit**: Öfter analysieren = mehr Gelegenheiten (mehr API-Kosten)
5. **Logs auswerten**: Welche Kategorien/Märkte performen am besten?

---

## ⚖️ Rechtliches & Haftungsausschluss

- Dieses Tool ist für **Bildungs- und Forschungszwecke** entwickelt
- Automatisierter Handel mit echtem Geld birgt erhebliches Verlustrisiko
- Ich bin kein Finanzberater — dies ist keine Anlageberatung
- Überprüfe die Nutzungsbedingungen von Polymarket in deiner Region
- Verwende **immer nur Kapital, dessen Totalverlust du dir leisten kannst**
