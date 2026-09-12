#!/bin/bash
# ============================================================
# deploy.sh — Automatisches VPS-Setup für den Trading Agent
# Getestet auf: Ubuntu 22.04 (Hetzner Cloud CX22)
#
# Verwendung:
#   chmod +x deploy.sh
#   ./deploy.sh
# ============================================================

set -e  # Bei Fehler sofort abbrechen

# Farben für Output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}============================================================${NC}"
echo -e "${BLUE}   Polymarket AI Agent — VPS Setup${NC}"
echo -e "${BLUE}============================================================${NC}"

# ============================================================
# 1. System aktualisieren
# ============================================================
echo -e "\n${YELLOW}[1/8] System aktualisieren...${NC}"
sudo apt-get update -qq
sudo apt-get upgrade -y -qq
echo -e "${GREEN}✓ System aktualisiert${NC}"

# ============================================================
# 2. Python & Tools installieren
# ============================================================
echo -e "\n${YELLOW}[2/8] Python & Tools installieren...${NC}"
sudo apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    git \
    curl \
    htop \
    nano \
    ufw

# Python Version prüfen
PYTHON_VERSION=$(python3 --version | cut -d' ' -f2 | cut -d'.' -f1,2)
echo -e "${GREEN}✓ Python $PYTHON_VERSION installiert${NC}"

# ============================================================
# 3. Projektverzeichnis erstellen
# ============================================================
echo -e "\n${YELLOW}[3/8] Projektverzeichnis einrichten...${NC}"
PROJECT_DIR="$HOME/polymarket-agent"

if [ ! -d "$PROJECT_DIR" ]; then
    mkdir -p "$PROJECT_DIR"
    echo -e "${GREEN}✓ Verzeichnis erstellt: $PROJECT_DIR${NC}"
else
    echo -e "${YELLOW}⚠ Verzeichnis existiert bereits: $PROJECT_DIR${NC}"
fi

cd "$PROJECT_DIR"

# ============================================================
# 4. Python Virtual Environment erstellen
# ============================================================
echo -e "\n${YELLOW}[4/8] Virtual Environment erstellen...${NC}"
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
echo -e "${GREEN}✓ Virtual Environment aktiv${NC}"

# ============================================================
# 5. Dependencies installieren
# ============================================================
echo -e "\n${YELLOW}[5/8] Python-Pakete installieren...${NC}"
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo -e "${GREEN}✓ Alle Pakete installiert${NC}"

# ============================================================
# 6. .env Datei erstellen (falls noch nicht vorhanden)
# ============================================================
echo -e "\n${YELLOW}[6/8] Konfiguration prüfen...${NC}"
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo -e "${YELLOW}⚠  .env Datei aus Vorlage erstellt${NC}"
    echo -e "${RED}   → Bitte jetzt .env mit deinen Daten befüllen!${NC}"
    echo -e "   nano $PROJECT_DIR/.env"
else
    echo -e "${GREEN}✓ .env Datei vorhanden${NC}"
fi

# .env Dateiberechtigungen schützen
chmod 600 .env

# ============================================================
# 7. Systemd Service erstellen (läuft automatisch beim Neustart)
# ============================================================
echo -e "\n${YELLOW}[7/8] Systemd Service einrichten...${NC}"

SERVICE_FILE="/etc/systemd/system/polymarket-agent.service"

sudo bash -c "cat > $SERVICE_FILE" << EOF
[Unit]
Description=Polymarket AI Trading Agent
Documentation=https://github.com/yourname/polymarket-agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$PROJECT_DIR
Environment=PATH=$PROJECT_DIR/venv/bin:/usr/bin:/bin
ExecStart=$PROJECT_DIR/venv/bin/python main.py
Restart=always
RestartSec=60
StandardOutput=journal
StandardError=journal

# Sicherheits-Einstellungen
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
echo -e "${GREEN}✓ Systemd Service erstellt${NC}"

# ============================================================
# 8. Firewall konfigurieren
# ============================================================
echo -e "\n${YELLOW}[8/8] Firewall konfigurieren...${NC}"
sudo ufw allow ssh
sudo ufw --force enable
echo -e "${GREEN}✓ Firewall aktiv (SSH erlaubt)${NC}"

# ============================================================
# Zusammenfassung
# ============================================================
echo -e "\n${BLUE}============================================================${NC}"
echo -e "${GREEN}✅ Setup abgeschlossen!${NC}"
echo -e "${BLUE}============================================================${NC}"
echo ""
echo -e "Nächste Schritte:"
echo ""
echo -e "  ${YELLOW}1. .env Datei befüllen:${NC}"
echo -e "     nano $PROJECT_DIR/.env"
echo ""
echo -e "  ${YELLOW}2. Agent testen (DRY RUN):${NC}"
echo -e "     cd $PROJECT_DIR && source venv/bin/activate"
echo -e "     python main.py --once"
echo ""
echo -e "  ${YELLOW}3. Agent als Service starten:${NC}"
echo -e "     sudo systemctl start polymarket-agent"
echo -e "     sudo systemctl enable polymarket-agent"
echo ""
echo -e "  ${YELLOW}4. Logs verfolgen:${NC}"
echo -e "     sudo journalctl -u polymarket-agent -f"
echo -e "     # oder:"
echo -e "     tail -f $PROJECT_DIR/agent.log"
echo ""
echo -e "  ${YELLOW}5. Status prüfen:${NC}"
echo -e "     sudo systemctl status polymarket-agent"
echo ""
echo -e "  ${YELLOW}6. Auf LIVE Trading umschalten:${NC}"
echo -e "     nano $PROJECT_DIR/.env  # DRY_RUN=false setzen"
echo -e "     sudo systemctl restart polymarket-agent"
echo ""
echo -e "${RED}⚠️  WICHTIG: Erst ausgiebig im DRY RUN testen!${NC}"
echo -e "${BLUE}============================================================${NC}"
