#!/bin/bash

# Farben für die Ausgabe
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Prüfen, ob ein Argument übergeben wurde
if [ -z "$1" ]; then
    echo -e "${YELLOW}Verwendung: sudo $0 <Pfad-zum-LUKS-Gerät>${NC}"
    echo "Beispiel: sudo $0 /dev/sda2"
    exit 1
fi

DEVICE=$1

# Root-Rechte prüfen
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}Bitte führen Sie dieses Skript als root (sudo) aus.${NC}"
  exit 1
fi

# Prüfen, ob das Gerät existiert
if [ ! -b "$DEVICE" ]; then
    echo -e "${RED}Fehler: Gerät $DEVICE nicht gefunden.${NC}"
    exit 1
fi

echo "Prüfe Clevis-Bindungen auf $DEVICE..."

# Liste der Clevis-Slots abrufen
# Das Format ist "Slot: Pin: Config"
SLOTS_INFO=$(clevis luks list -d "$DEVICE" 2>/dev/null)

if [ -z "$SLOTS_INFO" ]; then
    echo -e "${RED}Keine Clevis-Bindung auf $DEVICE gefunden!${NC}"
    echo "Haben Sie 'clevis luks bind' ausgeführt?"
    exit 1
fi

echo -e "Gefundene Bindungen:\n$SLOTS_INFO\n"

# Durch die Slots iterieren und testen
while IFS= read -r line; do
    # Wir nutzen awk, um die Felder sicher zu trennen.
    # $1 ist der Slot (z.B. "1:"), $2 ist der Pin (z.B. "sss")
    
    # Slot-Nummer: Erstes Feld nehmen und den Doppelpunkt entfernen
    SLOT=$(echo "$line" | awk '{print $1}' | tr -d :)
    
    # Pin-Typ: Zweites Feld nehmen (schneidet alles ab dem ersten Leerzeichen ab)
    PIN=$(echo "$line" | awk '{print $2}')

    if [ "$PIN" == "tang" ] || [ "$PIN" == "sss" ]; then
        echo -e "Teste Entschlüsselung für Slot ${YELLOW}$SLOT${NC} (Typ: $PIN)..."
        
        # Der eigentliche Test:
        START_TIME=$(date +%s%N)
        if timeout 5s clevis luks pass -d "$DEVICE" -s "$SLOT" > /dev/null; then
            END_TIME=$(date +%s%N)
            DURATION=$((($END_TIME - $START_TIME)/1000000))
            echo -e "${GREEN}[OK] Erfolgreich authentifiziert.${NC} (Dauer: ${DURATION}ms)"
        else
            echo -e "${RED}[FEHLER] Konnte nicht authentifizieren.${NC}"
            echo "Mögliche Ursachen:"
            echo " - Tang Server nicht erreichbar"
            echo " - DNS-Probleme"
            echo " - Firewall blockiert Port"
        fi
    else
        echo "Überspringe Slot $SLOT (Typ: $PIN ist nicht relevant)"
    fi
    echo "---------------------------------------------------"
done <<< "$SLOTS_INFO"


