#!/usr/bin/env bash
###############################################################################
#  diagnose-dhcp.sh
#  Diagnose-Script: Ungewolltes DHCP auf physischen Interfaces
#  Ziel-OS: Ubuntu Server 24.04 (noble)
#
#  Das Script sammelt alle relevanten Konfigurationsquellen, zeigt
#  Zwischenergebnisse farbig an und liefert am Ende ein Fazit mit
#  konkreten Handlungsempfehlungen.
#
#  Aufruf:  sudo bash diagnose-dhcp.sh
###############################################################################

set -euo pipefail

# ── Farben & Formatierung ────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

# ── Globale Variablen für das Fazit ──────────────────────────────────────────
declare -a FINDINGS=()       # gesammelte Probleme
declare -a RECOMMENDATIONS=() # zugehörige Empfehlungen

# ── Hilfsfunktionen ──────────────────────────────────────────────────────────
banner() {
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "${BOLD}  $1${RESET}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
}

info()    { echo -e "  ${CYAN}ℹ${RESET}  $*"; }
ok()      { echo -e "  ${GREEN}✔${RESET}  $*"; }
warn()    { echo -e "  ${YELLOW}⚠${RESET}  $*"; }
fail()    { echo -e "  ${RED}✖${RESET}  $*"; }
detail()  { echo -e "     ${DIM}$*${RESET}"; }

add_finding() {
    FINDINGS+=("$1")
    RECOMMENDATIONS+=("$2")
}

show_file() {
    local f="$1"
    if [[ -f "$f" ]]; then
        echo -e "  ${DIM}── $f ──${RESET}"
        sed 's/^/     /' "$f"
        echo ""
    fi
}

# ── Root-Check ───────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}Dieses Script muss als root ausgeführt werden (sudo).${RESET}"
    exit 1
fi

###############################################################################
banner "1 ▸ Systemübersicht"
###############################################################################
echo ""
info "Hostname:       $(hostname)"
info "OS:             $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || echo "unbekannt")"
info "Kernel:         $(uname -r)"
info "Datum:          $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo ""

# Physische Interfaces ermitteln
mapfile -t PHYS_IFACES < <(find /sys/class/net -mindepth 1 -maxdepth 1 \
    ! -name lo ! -name veth\* ! -name docker\* ! -name br-\* ! -name virbr\* \
    -exec basename {} \; | sort)

info "Erkannte (nicht-virtuelle) Interfaces: ${PHYS_IFACES[*]:-keine}"
echo ""

for iface in "${PHYS_IFACES[@]}"; do
    # Prüfe ob DHCP-Lease aktiv ist
    dhcp_active=false
    if ip -4 addr show dev "$iface" 2>/dev/null | grep -q 'dynamic'; then
        dhcp_active=true
    fi

    ip_line=$(ip -4 addr show dev "$iface" 2>/dev/null | grep 'inet ' | head -1 || true)
    state=$(ip link show dev "$iface" 2>/dev/null | head -1 | grep -oP 'state \K\S+' || echo "UNKNOWN")

    if $dhcp_active; then
        fail "Interface ${BOLD}$iface${RESET}: state=$state  ${RED}DHCP aktiv${RESET}  $ip_line"
    elif [[ -n "$ip_line" ]]; then
        ok "Interface ${BOLD}$iface${RESET}: state=$state  statisch  $ip_line"
    else
        info "Interface ${BOLD}$iface${RESET}: state=$state  keine IPv4-Adresse"
    fi
done

###############################################################################
banner "2 ▸ cloud-init Status"
###############################################################################
echo ""

CLOUD_INIT_INSTALLED=false
CLOUD_INIT_ACTIVE=false
CLOUD_INIT_NET_DISABLED=false

if command -v cloud-init &>/dev/null; then
    CLOUD_INIT_INSTALLED=true
    warn "cloud-init ist ${BOLD}installiert${RESET}"

    ci_status=$(cloud-init status 2>/dev/null | head -1 || echo "unbekannt")
    detail "Status: $ci_status"

    if [[ -f /etc/cloud/cloud-init.disabled ]]; then
        ok "cloud-init ist via /etc/cloud/cloud-init.disabled deaktiviert"
    elif echo "$ci_status" | grep -qi 'disabled'; then
        ok "cloud-init meldet sich als disabled"
    else
        CLOUD_INIT_ACTIVE=true
        fail "cloud-init ist ${BOLD}aktiv${RESET}"
        add_finding \
            "cloud-init ist aktiv und kann bei jedem Boot eine DHCP-Config generieren." \
            "Deaktivieren:  touch /etc/cloud/cloud-init.disabled\n     Oder komplett entfernen:  apt purge cloud-init && rm -rf /etc/cloud /var/lib/cloud"
    fi
else
    ok "cloud-init ist ${BOLD}nicht installiert${RESET}"
fi

echo ""
info "Prüfe Netzwerk-Deaktivierung in cloud.cfg.d/:"

found_net_disabled=false
for f in /etc/cloud/cloud.cfg.d/*.cfg /etc/cloud/cloud.cfg.d/*.conf; do
    [[ -f "$f" ]] || continue
    if grep -qE 'network:\s*\{?\s*config:\s*disabled' "$f" 2>/dev/null; then
        ok "Netzwerk-Config disabled in: $f"
        found_net_disabled=true
        CLOUD_INIT_NET_DISABLED=true
    fi
done

if ! $found_net_disabled; then
    if $CLOUD_INIT_INSTALLED; then
        fail "Keine cloud.cfg.d-Datei deaktiviert die Netzwerk-Generierung"
        add_finding \
            "cloud-init Netzwerk-Generierung ist nicht explizit deaktiviert." \
            "echo 'network: {config: disabled}' > /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg"
    else
        ok "Nicht relevant (cloud-init nicht installiert)"
    fi
fi

###############################################################################
banner "3 ▸ Netplan-Konfigurationen"
###############################################################################
echo ""

NETPLAN_DIRS=("/etc/netplan" "/run/netplan" "/lib/netplan" "/usr/lib/netplan")
declare -a DHCP_NETPLAN_FILES=()

for dir in "${NETPLAN_DIRS[@]}"; do
    if [[ -d "$dir" ]] && ls "$dir"/*.yaml &>/dev/null 2>&1; then
        info "Verzeichnis: ${BOLD}$dir${RESET}"
        for f in "$dir"/*.yaml; do
            [[ -f "$f" ]] || continue
            show_file "$f"

            # Prüfe ob DHCP aktiviert wird
            if grep -qiE 'dhcp4:\s*(true|yes)' "$f" 2>/dev/null; then
                ifaces_in_file=$(grep -E '^\s{4,}\S+:' "$f" | sed 's/://;s/^[[:space:]]*//' | tr '\n' ', ' | sed 's/,$//')
                fail "DHCP4 aktiviert in ${BOLD}$f${RESET}  (Interfaces: ${ifaces_in_file:-unklar})"
                DHCP_NETPLAN_FILES+=("$f")

                # Ist es eine cloud-init-generierte Datei?
                if head -3 "$f" | grep -qi 'cloud-init'; then
                    add_finding \
                        "Netplan-Datei $f wurde von cloud-init generiert und enthält DHCP." \
                        "Löschen:  rm $f\nUnd cloud-init Netzwerk deaktivieren (siehe oben)."
                else
                    add_finding \
                        "Netplan-Datei $f enthält DHCP4=true." \
                        "Datei prüfen/anpassen oder entfernen:  nano $f && netplan apply"
                fi
            else
                ok "Kein DHCP4 in $f"
            fi
        done
    else
        detail "Verzeichnis $dir: leer oder nicht vorhanden"
    fi
done

###############################################################################
banner "4 ▸ systemd-networkd Konfigurationen"
###############################################################################
echo ""

NETWORKD_DIRS=(
    "/run/systemd/network"
    "/etc/systemd/network"
    "/usr/lib/systemd/network"
    "/lib/systemd/network"
)

for dir in "${NETWORKD_DIRS[@]}"; do
    if [[ -d "$dir" ]] && ls "$dir"/*.network &>/dev/null 2>&1; then
        info "Verzeichnis: ${BOLD}$dir${RESET}"
        for f in "$dir"/*.network; do
            [[ -f "$f" ]] || continue
            show_file "$f"

            if grep -qiE '^\s*DHCP\s*=\s*(yes|ipv4|true)' "$f" 2>/dev/null; then
                fail "DHCP aktiviert in ${BOLD}$f${RESET}"
                add_finding \
                    "systemd-networkd Datei $f enthält DHCP=yes/ipv4." \
                    "Prüfe ob diese Datei beim Boot von netplan regeneriert wird.\nWenn ja: Netplan-Quelle fixen. Wenn manuell angelegt: Datei anpassen/entfernen."
            else
                ok "Kein DHCP in $f"
            fi
        done
    else
        detail "Verzeichnis $dir: keine .network-Dateien"
    fi
done

# Auch .link-Dateien prüfen (weniger wahrscheinlich, aber vollständig)
echo ""
info "Prüfe .link-Dateien (zur Vollständigkeit):"
for dir in "${NETWORKD_DIRS[@]}"; do
    for f in "$dir"/*.link; do
        [[ -f "$f" ]] || continue
        detail "Gefunden: $f"
    done
done

###############################################################################
banner "5 ▸ NetworkManager-Check"
###############################################################################
echo ""

if systemctl is-active --quiet NetworkManager 2>/dev/null; then
    warn "NetworkManager ist ${BOLD}aktiv${RESET}"
    detail "NM kann eigenständig DHCP auf unmanaged Interfaces starten."

    if command -v nmcli &>/dev/null; then
        info "Aktive NM-Verbindungen:"
        nmcli -t -f NAME,TYPE,DEVICE,STATE connection show --active 2>/dev/null | while IFS=: read -r name type dev state; do
            if [[ "$type" == *"ethernet"* ]]; then
                # Prüfe ob DHCP
                method=$(nmcli -t -f ipv4.method connection show "$name" 2>/dev/null | cut -d: -f2)
                if [[ "$method" == "auto" ]]; then
                    fail "NM-Verbindung '$name' auf $dev: IPv4-Methode = auto (DHCP)"
                    add_finding \
                        "NetworkManager-Verbindung '$name' verwendet DHCP auf $dev." \
                        "nmcli con mod '$name' ipv4.method manual ipv4.addresses <IP>/<PREFIX> ipv4.gateway <GW>\nOder NM deaktivieren falls systemd-networkd genutzt wird."
                else
                    ok "NM-Verbindung '$name' auf $dev: IPv4-Methode = $method"
                fi
            fi
        done
    fi
else
    ok "NetworkManager ist nicht aktiv (systemd-networkd wird vermutlich genutzt)"
fi

###############################################################################
banner "6 ▸ DHCP-Client-Prozesse"
###############################################################################
echo ""

dhcp_procs=$(ps aux 2>/dev/null | grep -iE '(dhclient|dhcpcd|udhcpc|systemd-networkd)' | grep -v grep || true)

if [[ -n "$dhcp_procs" ]]; then
    info "Laufende DHCP-bezogene Prozesse:"
    echo "$dhcp_procs" | while read -r line; do
        detail "$line"
    done

    # Speziell dhclient prüfen – sollte auf Ubuntu 24.04 eigentlich nicht laufen
    if echo "$dhcp_procs" | grep -q 'dhclient'; then
        fail "dhclient läuft – ungewöhnlich für Ubuntu 24.04 (nutzt normalerweise systemd-networkd)"
        add_finding \
            "dhclient-Prozess läuft. Möglicherweise durch ein Legacy-Script gestartet." \
            "Prüfen: dpkg -l | grep dhclient\nKillall: killall dhclient\nEntfernen: apt purge isc-dhcp-client"
    fi
else
    ok "Keine externen DHCP-Client-Prozesse gefunden (normal bei systemd-networkd)"
fi

###############################################################################
banner "7 ▸ DHCP-Leases"
###############################################################################
echo ""

LEASE_DIRS=(
    "/run/systemd/netif/leases"
    "/var/lib/dhcp"
    "/var/lib/NetworkManager"
)

for dir in "${LEASE_DIRS[@]}"; do
    if [[ -d "$dir" ]]; then
        leases=$(find "$dir" -type f -name '*lease*' -o -name '*.lease' 2>/dev/null || true)
        if [[ -n "$leases" ]]; then
            warn "Lease-Dateien in ${BOLD}$dir${RESET}:"
            echo "$leases" | while read -r lf; do
                detail "$lf"
                if [[ -f "$lf" && -s "$lf" ]]; then
                    # Zeige die wichtigsten Felder
                    grep -iE '(ADDRESS|SERVER|ROUTER|DNS|INTERFACE)' "$lf" 2>/dev/null | head -8 | sed 's/^/          /'
                fi
            done
        else
            ok "Keine Leases in $dir"
        fi
    fi
done

# systemd-networkd spezifisch
if [[ -d /run/systemd/netif/leases ]]; then
    for lf in /run/systemd/netif/leases/*; do
        [[ -f "$lf" ]] || continue
        ifindex=$(basename "$lf")
        iface_name=$(ip -o link show 2>/dev/null | awk -v idx="$ifindex" -F'[ :]+' '$1 == idx {print $2}' || echo "?")
        fail "Aktiver DHCP-Lease für Interface-Index $ifindex ($iface_name)"
        show_file "$lf"
    done
fi

###############################################################################
banner "8 ▸ Initramfs / Kernel-Cmdline Netzwerk-Parameter"
###############################################################################
echo ""

cmdline=$(cat /proc/cmdline 2>/dev/null || echo "")
info "Kernel-Cmdline:"
detail "$cmdline"
echo ""

if echo "$cmdline" | grep -qiE 'ip=dhcp|ip=::dhcp'; then
    fail "Kernel-Cmdline enthält ip=dhcp – erzwingt DHCP beim Boot!"
    add_finding \
        "Kernel-Cmdline enthält 'ip=dhcp'. Das erzwingt DHCP vor dem Userspace." \
        "Entferne 'ip=dhcp' aus /etc/default/grub (GRUB_CMDLINE_LINUX) und führe update-grub aus."
elif echo "$cmdline" | grep -qiE 'ip='; then
    warn "Kernel-Cmdline enthält ip= Parameter"
else
    ok "Keine DHCP-relevanten Kernel-Parameter"
fi

# Initramfs Netzwerk-Hooks prüfen
if [[ -d /usr/share/initramfs-tools/scripts ]]; then
    initramfs_net=$(grep -rl 'configure_networking\|dhclient\|DHCP' /usr/share/initramfs-tools/scripts/ 2>/dev/null || true)
    if [[ -n "$initramfs_net" ]]; then
        info "Initramfs-Scripts mit Netzwerk-Referenzen:"
        echo "$initramfs_net" | while read -r s; do detail "$s"; done
    fi
fi

###############################################################################
banner "9 ▸ udev-Regeln & systemd-Einheiten"
###############################################################################
echo ""

# udev-Regeln die Netzwerk-Interfaces betreffen
info "Prüfe udev-Regeln für Netzwerk:"
udev_net=$(find /etc/udev/rules.d /usr/lib/udev/rules.d -name '*net*' -type f 2>/dev/null || true)
if [[ -n "$udev_net" ]]; then
    echo "$udev_net" | while read -r r; do detail "$r"; done
else
    ok "Keine besonderen Netzwerk-udev-Regeln"
fi

echo ""
info "Prüfe auf Hook-Scripts die DHCP starten könnten:"
for hook_dir in /etc/networkd-dispatcher/routable.d \
                /etc/networkd-dispatcher/carrier.d \
                /etc/networkd-dispatcher/configured.d \
                /etc/network/if-up.d \
                /etc/network/if-pre-up.d; do
    if [[ -d "$hook_dir" ]]; then
        hooks=$(find "$hook_dir" -type f -executable 2>/dev/null || true)
        if [[ -n "$hooks" ]]; then
            info "Hooks in ${BOLD}$hook_dir${RESET}:"
            echo "$hooks" | while read -r h; do
                detail "$h"
                if grep -qiE 'dhclient|dhcpcd|udhcpc' "$h" 2>/dev/null; then
                    fail "  → enthält DHCP-Client-Aufruf!"
                    add_finding \
                        "Hook-Script $h ruft einen DHCP-Client auf." \
                        "Script prüfen und ggf. entfernen oder anpassen."
                fi
            done
        fi
    fi
done

###############################################################################
banner "10 ▸ Zusammenfassung: networkctl"
###############################################################################
echo ""

if command -v networkctl &>/dev/null; then
    networkctl list 2>/dev/null || true
    echo ""
    for iface in "${PHYS_IFACES[@]}"; do
        echo -e "  ${DIM}── networkctl status $iface ──${RESET}"
        networkctl status "$iface" 2>/dev/null | head -20 | sed 's/^/     /'
        echo ""
    done
else
    detail "networkctl nicht verfügbar"
fi

###############################################################################
banner "═══  F A Z I T  ═══"
###############################################################################
echo ""

if [[ ${#FINDINGS[@]} -eq 0 ]]; then
    echo -e "  ${GREEN}${BOLD}Keine offensichtlichen Probleme gefunden.${RESET}"
    echo ""
    echo -e "  Falls DHCP trotzdem aktiv ist, prüfe manuell:"
    echo -e "    • journalctl -b -u systemd-networkd | grep -i dhcp"
    echo -e "    • Ob ein anderer Dienst (z.B. LXD, Docker) Interfaces verwaltet"
    echo -e "    • Ob ein Management-Controller (IPMI/iLO/iDRAC) ein USB-NIC bereitstellt"
    echo ""
else
    echo -e "  ${RED}${BOLD}${#FINDINGS[@]} Problem(e) gefunden:${RESET}"
    echo ""

    for i in "${!FINDINGS[@]}"; do
        num=$((i + 1))
        echo -e "  ${RED}${BOLD}Problem $num:${RESET}"
        echo -e "    ${FINDINGS[$i]}"
        echo ""
        echo -e "    ${GREEN}→ Empfehlung:${RESET}"
        echo -e "    ${RECOMMENDATIONS[$i]}" | sed 's/^/    /'
        echo ""
        if [[ $i -lt $((${#FINDINGS[@]} - 1)) ]]; then
            echo -e "  ${DIM}─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─${RESET}"
        fi
    done

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""
    echo -e "  ${BOLD}Empfohlene Reihenfolge:${RESET}"
    echo ""
    echo -e "    1. cloud-init deaktivieren/entfernen (häufigste Ursache)"
    echo -e "    2. Generierte Netplan-Dateien löschen (50-cloud-init.yaml etc.)"
    echo -e "    3. Installer-Config prüfen (00-installer-config.yaml)"
    echo -e "    4. Kernel-Cmdline bereinigen falls nötig"
    echo -e "    5. netplan apply && reboot"
    echo ""
    echo -e "  ${YELLOW}Nach den Änderungen dieses Script erneut ausführen zur Kontrolle.${RESET}"
fi

echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
