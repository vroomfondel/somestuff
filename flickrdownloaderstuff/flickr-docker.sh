#!/bin/bash
#
# Flickr Download Docker Script
# =============================
# Erstellt ein Docker-Image mit flickr_download und X11-Support,
# damit der Browser für die OAuth-Authentifizierung auf dem Host öffnet.
#
# Nutzung:
#   ./flickr-docker.sh build                    # Image bauen
#   ./flickr-docker.sh auth                     # Nur authentifizieren
#   ./flickr-docker.sh download <username>      # Fotos herunterladen
#   ./flickr-docker.sh shell                    # Shell im Container
#   ./flickr-docker.sh clean                    # Image und temp. Dateien löschen
#

set -e

# ============================================================================
# KONFIGURATION
# ============================================================================

IMAGE_NAME="flickr-download"
IMAGE_TAG="latest"
WORK_DIR="$(pwd)/flickr-backup"
CONFIG_DIR="$(pwd)/flickr-config"
CACHE_DIR="$(pwd)/flickr-cache"
XAUTH_FILE="/tmp/.flickr-docker.xauth"

# Container-Runtime (wird später erkannt)
CONTAINER_RUNTIME=""

# OS-Erkennung
detect_os() {
    case "$(uname -s)" in
        Linux*)
            # Prüfe ob WSL
            if grep -qi microsoft /proc/version 2>/dev/null; then
                echo "wsl"
            else
                echo "linux"
            fi
            ;;
        Darwin*)
            echo "mac"
            ;;
        CYGWIN*|MINGW*|MSYS*)
            echo "windows"
            ;;
        *)
            echo "unknown"
            ;;
    esac
}

HOST_OS="$(detect_os)"

# Browser-Konfiguration je nach OS
# Linux: Expliziter Browser für X11-Forwarding
# Mac/Windows: Leer lassen -> Python webbrowser nutzt System-Default
if [ "$HOST_OS" = "linux" ]; then
    BROWSER="${BROWSER:-chrome}"
else
    # Auf Mac/Windows: BROWSER leer = Python öffnet System-Browser
    BROWSER="${BROWSER:-}"
fi

# X11 nur unter Linux
X11_AVAILABLE=false
if [ "$HOST_OS" = "linux" ] && [ -n "$DISPLAY" ]; then
    X11_AVAILABLE=true
fi

# Farben für Output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ============================================================================
# HILFSFUNKTIONEN
# ============================================================================

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_dependencies() {
    log_info "Erkanntes OS: $HOST_OS"
    
    # Container-Runtime erkennen (Docker oder Podman)
    if command -v podman &> /dev/null && podman info &> /dev/null 2>&1; then
        CONTAINER_RUNTIME="podman"
        log_info "Container-Runtime: Podman erkannt"
    elif command -v docker &> /dev/null; then
        CONTAINER_RUNTIME="docker"
        log_info "Container-Runtime: Docker erkannt"
    else
        log_error "Weder Docker noch Podman gefunden!"
        exit 1
    fi
    
    # X11-Checks nur unter Linux
    if [ "$HOST_OS" = "linux" ]; then
        if [ -z "$DISPLAY" ]; then
            log_error "DISPLAY ist nicht gesetzt. X11 erforderlich!"
            log_info "Tipp: export DISPLAY=:0"
            exit 1
        fi
        
        if ! command -v xauth &> /dev/null; then
            log_error "xauth ist nicht installiert! (apt install xauth)"
            exit 1
        fi
    else
        log_info "Kein X11-Forwarding (Mac/Windows) - Browser öffnet auf Host"
    fi
}

setup_directories() {
    log_info "Erstelle Verzeichnisse..."
    mkdir -p "$WORK_DIR"
    mkdir -p "$CONFIG_DIR"
    mkdir -p "$CACHE_DIR"
    log_success "Verzeichnisse erstellt"
}

setup_xauth() {
    # Nur unter Linux mit X11
    if [ "$HOST_OS" != "linux" ]; then
        log_info "X11-Setup übersprungen (nicht Linux)"
        return 0
    fi
    
    log_info "Konfiguriere X11-Authentifizierung..."
    
    # Prüfe auf Wayland
    if [ -n "$WAYLAND_DISPLAY" ]; then
        log_info "Wayland erkannt (WAYLAND_DISPLAY=$WAYLAND_DISPLAY)"
        log_info "X11-Apps laufen über XWayland"
    fi
    
    # Alte xauth-Datei entfernen
    rm -f "$XAUTH_FILE"
    touch "$XAUTH_FILE"
    chmod 666 "$XAUTH_FILE"  # Muss für alle User im Container lesbar sein
    
    # X11 Cookie extrahieren
    # Für Podman mit keep-id brauchen wir das Cookie für den aktuellen User
    local xauth_source=""
    
    if [ -n "$XAUTHORITY" ] && [ -f "$XAUTHORITY" ]; then
        # Benutze existierende XAUTHORITY
        xauth_source="$XAUTHORITY"
        log_info "Verwende XAUTHORITY: $XAUTHORITY"
    elif [ -f "$HOME/.Xauthority" ]; then
        xauth_source="$HOME/.Xauthority"
        log_info "Verwende ~/.Xauthority"
    fi
    
    if [ -n "$xauth_source" ]; then
        # Methode 1: Direktes Kopieren (funktioniert meist am besten)
        cp "$xauth_source" "$XAUTH_FILE"
        chmod 666 "$XAUTH_FILE"
        log_info "xauth-Datei kopiert von: $xauth_source"
    else
        # Methode 2: Cookie aus xauth extrahieren
        # Das 'ffff' ersetzt die Display-Nummer für Wildcard-Matching
        log_info "Extrahiere Cookie mit xauth nlist..."
        xauth nlist "$DISPLAY" 2>/dev/null | sed -e 's/^..../ffff/' | xauth -f "$XAUTH_FILE" nmerge - 2>/dev/null || true
    fi
    
    if [ -s "$XAUTH_FILE" ]; then
        local cookie_count
        cookie_count=$(xauth -f "$XAUTH_FILE" list 2>/dev/null | wc -l)
        log_success "X11-Authentifizierung konfiguriert ($cookie_count Cookie(s))"
    else
        log_warn "Konnte X11-Cookie nicht extrahieren!"
        log_warn "Browser könnte nicht funktionieren."
        log_info "Debug: DISPLAY=$DISPLAY, XAUTHORITY=${XAUTHORITY:-nicht gesetzt}"
    fi
}

cleanup_xauth() {
    # Nur unter Linux
    if [ "$HOST_OS" != "linux" ]; then
        return 0
    fi
    
    if [ -f "$XAUTH_FILE" ]; then
        rm -f "$XAUTH_FILE"
        log_info "X11-Auth-Datei aufgeräumt"
    fi
}

check_config() {
    if [ ! -f "$CONFIG_DIR/.flickr_download" ]; then
        log_warn "Keine API-Konfiguration gefunden!"
        echo ""
        echo "Bitte erstelle $CONFIG_DIR/.flickr_download mit folgendem Inhalt:"
        echo ""
        echo "  api_key: DEIN_FLICKR_API_KEY"
        echo "  api_secret: DEIN_FLICKR_API_SECRET"
        echo ""
        echo "API-Key bekommst du hier: https://www.flickr.com/services/apps/create/"
        echo ""
        
        read -p "Soll ich die Datei jetzt erstellen? (j/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Jj]$ ]]; then
            read -p "API Key: " api_key
            read -p "API Secret: " api_secret
            echo "api_key: $api_key" > "$CONFIG_DIR/.flickr_download"
            echo "api_secret: $api_secret" >> "$CONFIG_DIR/.flickr_download"
            chmod 600 "$CONFIG_DIR/.flickr_download"
            log_success "Konfiguration erstellt"
        else
            exit 1
        fi
    fi
}

# ============================================================================
# DOCKERFILE (INLINE)
# ============================================================================

build_image() {
    log_info "Baue Docker-Image '$IMAGE_NAME:$IMAGE_TAG'..."
    
    # Temporäres Build-Verzeichnis
    BUILD_DIR=$(mktemp -d)
    trap "rm -rf $BUILD_DIR" EXIT
    
    # Dockerfile schreiben
    cat > "$BUILD_DIR/Dockerfile" << 'DOCKERFILE_END'
# ============================================================================
# Flickr Download Docker Image
# Mit Firefox und X11-Support für OAuth-Authentifizierung
# Kompatibel mit Docker und Podman
# ============================================================================

FROM python:3.14-slim

LABEL maintainer="Flickr Backup Script"
LABEL description="Flickr Download mit Browser-Support für OAuth"

# System-Pakete installieren
RUN apt-get update && apt-get install -y --no-install-recommends \
    # ExifTool für Metadaten
    libimage-exiftool-perl \
    # Browser (beide für Flexibilität)
    chromium \
    firefox-esr \
    # xdg-utils für xdg-open
    xdg-utils \
    # X11-Libraries
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    libgtk-3-0 \
    libdbus-glib-1-2 \
    libdbus-1-3 \
    dbus \
    libxt6 \
    libnss3 \
    libnspr4 \
    libasound2t64 \
    # Fonts
    fonts-liberation \
    fonts-dejavu-core \
    # Tools
    bash \
    procps \
    ca-certificates \
    git \
    # Aufräumen
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Symlinks für Browser-Namen ($BROWSER Kompatibilität)
# chrome/google-chrome -> chromium
# firefox bleibt firefox-esr
RUN ln -sf /usr/bin/chromium /usr/bin/chrome && \
    ln -sf /usr/bin/chromium /usr/bin/google-chrome && \
    ln -sf /usr/bin/firefox-esr /usr/bin/firefox

# Python-Pakete
RUN pip install --no-cache-dir \
    git+https://github.com/beaufour/flickr-download.git \
    PyYAML

# Arbeitsverzeichnis
WORKDIR /data

# Cache-Verzeichnis
RUN mkdir -p /cache && chmod 777 /cache

# Home-Verzeichnis für Podman (keep-id) - muss für alle User beschreibbar sein
RUN mkdir -p /home/poduser && chmod 777 /home/poduser

# Mozilla-Verzeichnis für Firefox-Profil (beide Homes)
RUN mkdir -p /root/.mozilla /home/poduser/.mozilla && \
    chmod -R 777 /root/.mozilla /home/poduser/.mozilla

# Umgebungsvariablen
ENV PYTHONUNBUFFERED=1
ENV HOME=/root

# Entrypoint-Script mit besserer Shell-Unterstützung
RUN echo '#!/bin/bash\n\
# Stelle sicher dass HOME-Verzeichnis existiert und beschreibbar ist\n\
if [ ! -d "$HOME" ]; then\n\
    mkdir -p "$HOME" 2>/dev/null || true\n\
fi\n\
# Mozilla-Verzeichnis für Firefox\n\
mkdir -p "$HOME/.mozilla" 2>/dev/null || true\n\
\n\
if [ "$1" = "shell" ]; then\n\
    shift\n\
    if [ $# -eq 0 ]; then\n\
        exec /bin/bash\n\
    else\n\
        exec /bin/bash "$@"\n\
    fi\n\
else\n\
    exec flickr_download "$@"\n\
fi' > /entrypoint.sh && chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["--help"]
DOCKERFILE_END

    # Image bauen
    # Container-Runtime erkennen
    local runtime="docker"
    if command -v podman &> /dev/null && podman info &> /dev/null 2>&1; then
        runtime="podman"
        log_info "Verwende Podman zum Bauen"
    else
        log_info "Verwende Docker zum Bauen"
    fi
    
    $runtime build -t "$IMAGE_NAME:$IMAGE_TAG" "$BUILD_DIR"
    
    log_success "Image erfolgreich gebaut ($runtime)"
}

# ============================================================================
# CONTAINER STARTEN
# ============================================================================

is_token_valid() {
    local token_file="$1"
    
    # Datei muss existieren
    [ -f "$token_file" ] || return 1
    
    # Datei muss Inhalt haben
    [ -s "$token_file" ] || return 1
    
    # Datei muss mindestens 2 Zeilen mit Inhalt haben (key + secret)
    local line_count
    line_count=$(grep -c '[^[:space:]]' "$token_file" 2>/dev/null || echo "0")
    [ "$line_count" -ge 2 ] || return 1
    
    return 0
}

# Konvertiert Username zu Flickr-URL falls nötig
# flickr_download funktioniert zuverlässiger mit URLs
flickr_user_to_url() {
    local user="$1"
    
    # Wenn bereits eine URL, unverändert zurückgeben
    if [[ "$user" == http* ]]; then
        echo "$user"
    else
        # Username zu URL konvertieren
        echo "https://www.flickr.com/photos/${user}/"
    fi
}

run_container() {
    local CMD=("$@")
    
    check_dependencies
    setup_directories
    check_config
    setup_xauth
    
    log_info "Starte Container..."
    echo ""
    
    # Alten Container entfernen falls vorhanden
    $CONTAINER_RUNTIME rm -f flickr-download-run 2>/dev/null || true
    
    # Basis-Argumente
    local CONTAINER_ARGS=(
        run -it --rm
        --name flickr-download-run
        -v "$WORK_DIR:/data"
        -v "$CACHE_DIR:/cache"
    )
    
    # Netzwerk-Konfiguration je nach OS
    if [ "$HOST_OS" = "linux" ]; then
        # Linux: --network=host für OAuth-Callback
        CONTAINER_ARGS+=(--network=host)
    else
        # Mac/Windows: --network=host funktioniert nicht in Docker Desktop
        # Ports für OAuth-Callback publishen (flickr_download nutzt meist 8080-8100)
        log_info "Mac/Windows: Publishe Ports 8080-8100 für OAuth-Callback"
        CONTAINER_ARGS+=(-p 8080-8100:8080-8100)
    fi
    
    # X11-Konfiguration nur unter Linux
    if [ "$HOST_OS" = "linux" ]; then
        CONTAINER_ARGS+=(
            -e "DISPLAY=$DISPLAY"
            -e "XAUTHORITY=/tmp/.xauth"
            -v "/tmp/.X11-unix:/tmp/.X11-unix:rw"
            -v "$XAUTH_FILE:/tmp/.xauth:ro"
        )
    fi
    
    # Podman-spezifische Optionen (nur Linux, da Podman auf Mac/Windows anders läuft)
    if [ "$CONTAINER_RUNTIME" = "podman" ] && [ "$HOST_OS" = "linux" ]; then
        log_info "Verwende Podman-spezifische Optionen (userns=keep-id)"
        CONTAINER_ARGS+=(
            # User-Namespace beibehalten für X11-Zugriff
            --userns=keep-id
            # Security-Label für X11-Zugriff deaktivieren (SELinux)
            --security-opt label=disable
        )
        # Bei Podman mit keep-id: User ist NICHT root!
        # Wir mounten config nach /home/poduser und setzen HOME darauf
        # flickr_download sucht .flickr_download in $HOME
        CONTAINER_ARGS+=(
            -v "$CONFIG_DIR:/home/poduser"
            -e "HOME=/home/poduser"
        )
    else
        # Docker (alle OS) oder Podman auf Mac: User ist root, HOME=/root
        CONTAINER_ARGS+=(
            -v "$CONFIG_DIR:/root"
            -e "HOME=/root"
        )
    fi
    
    # BROWSER Variable
    # Linux: Expliziter Browser für X11-Forwarding
    # Mac/Windows: Leer -> Python webbrowser gibt URL auf stdout aus
    if [ -n "$BROWSER" ]; then
        CONTAINER_ARGS+=(-e "BROWSER=$BROWSER")
    fi
    
    $CONTAINER_RUNTIME "${CONTAINER_ARGS[@]}" "$IMAGE_NAME:$IMAGE_TAG" "${CMD[@]}"
    
    local exit_code=$?
    
    cleanup_xauth
    
    return $exit_code
}

# ============================================================================
# KOMMANDOS
# ============================================================================

cmd_build() {
    check_dependencies
    build_image
}

cmd_auth() {
    log_info "Starte Authentifizierung..."
    log_info "OS: $HOST_OS"
    
    # Ungültiges Token vorher löschen
    if [ -f "$CONFIG_DIR/.flickr_token" ] && ! is_token_valid "$CONFIG_DIR/.flickr_token"; then
        log_warn "Ungültiges Token gefunden - wird gelöscht"
        rm -f "$CONFIG_DIR/.flickr_token"
    fi
    
    echo ""
    if [ "$HOST_OS" = "linux" ]; then
        log_info "Browser: ${BROWSER:-<system-default>}"
        echo "Ein Browser-Fenster wird sich öffnen."
    else
        echo "HINWEIS für Mac/Windows:"
        echo "  - Eine URL wird im Terminal angezeigt"
        echo "  - Öffne diese URL manuell in deinem Browser"
        echo "  - Nach dem Login wird der Callback automatisch verarbeitet"
        echo ""
    fi
    echo "Bitte bei Flickr einloggen und die App autorisieren."
    echo ""
    
    run_container -t
    
    if is_token_valid "$CONFIG_DIR/.flickr_token"; then
        log_success "Authentifizierung erfolgreich!"
        log_info "Token gespeichert in: $CONFIG_DIR/.flickr_token"
    else
        log_error "Authentifizierung fehlgeschlagen oder abgebrochen!"
        rm -f "$CONFIG_DIR/.flickr_token"
    fi
}

cmd_download() {
    local USERNAME="$1"
    
    if [ -z "$USERNAME" ]; then
        log_error "Benutzername fehlt!"
        echo "Nutzung: $0 download <flickr-username>"
        exit 1
    fi
    
    if ! is_token_valid "$CONFIG_DIR/.flickr_token"; then
        log_warn "Noch nicht authentifiziert (kein gültiges Token)!"
        echo "Führe zuerst '$0 auth' aus."
        exit 1
    fi
    
    # Username zu URL konvertieren
    local FLICKR_USER
    FLICKR_USER=$(flickr_user_to_url "$USERNAME")
    
    log_info "Starte Download für: $FLICKR_USER"
    log_info "Zielverzeichnis: $WORK_DIR"
    echo ""
    
    run_container \
        -t \
        --download_user "$FLICKR_USER" \
        --save_json \
        --cache /cache/api_cache \
        --metadata_store
    
    log_success "Download abgeschlossen!"
    log_info "Fotos gespeichert in: $WORK_DIR"
}

cmd_download_album() {
    local ALBUM_ID="$1"
    
    if [ -z "$ALBUM_ID" ]; then
        log_error "Album-ID fehlt!"
        echo "Nutzung: $0 album <album-id>"
        echo ""
        echo "Album-IDs findest du mit: $0 list <username>"
        exit 1
    fi
    
    log_info "Starte Download für Album: $ALBUM_ID"
    
    run_container \
        -t \
        --download "$ALBUM_ID" \
        --save_json \
        --cache /cache/api_cache \
        --metadata_store
}

cmd_list() {
    local USERNAME="$1"
    
    if [ -z "$USERNAME" ]; then
        log_error "Benutzername fehlt!"
        echo "Nutzung: $0 list <flickr-username>"
        exit 1
    fi
    
    # Username zu URL konvertieren
    local FLICKR_USER
    FLICKR_USER=$(flickr_user_to_url "$USERNAME")
    
    log_info "Liste Alben für: $FLICKR_USER"
    echo ""
    
    run_container -t --list "$FLICKR_USER"
}

cmd_shell() {
    log_info "Starte Shell im Container..."
    run_container shell
}

cmd_test_browser() {
    local URL="${1:-https://www.flickr.com/}"
    
    # Nur unter Linux mit X11 sinnvoll
    if [ "$HOST_OS" != "linux" ]; then
        log_warn "test-browser ist nur unter Linux mit X11 verfügbar"
        log_info "Auf $HOST_OS öffnet Python's webbrowser Modul den System-Browser automatisch"
        log_info "Teste stattdessen ob Container funktioniert..."
        echo ""
        
        check_dependencies
        $CONTAINER_RUNTIME rm -f flickr-download-run 2>/dev/null || true
        
        $CONTAINER_RUNTIME run -it --rm \
            --name flickr-download-run \
            "$IMAGE_NAME:$IMAGE_TAG" \
            shell -c "echo 'Container läuft!' && echo 'Python:' && python --version && echo 'flickr_download:' && flickr_download --version 2>/dev/null || echo 'installiert'"
        
        return 0
    fi
    
    log_info "Teste X11-Verbindung..."
    log_info "Öffne Browser mit: $URL"
    echo ""
    
    check_dependencies
    setup_xauth
    
    # Alten Container entfernen falls vorhanden
    $CONTAINER_RUNTIME rm -f flickr-download-run 2>/dev/null || true
    
    # Basis-Argumente
    local CONTAINER_ARGS=(
        run -it --rm
        --name flickr-download-run
        --network=host
        -e "DISPLAY=$DISPLAY"
        -e "XAUTHORITY=/tmp/.xauth"
        -v "/tmp/.X11-unix:/tmp/.X11-unix:rw"
        -v "$XAUTH_FILE:/tmp/.xauth:ro"
    )
    
    # Podman-spezifische Optionen
    if [ "$CONTAINER_RUNTIME" = "podman" ]; then
        log_info "Podman: verwende --userns=keep-id"
        CONTAINER_ARGS+=(
            --userns=keep-id
            --security-opt label=disable
            -e "HOME=/home/poduser"
        )
    else
        CONTAINER_ARGS+=(-e "HOME=/root")
    fi
    
    # BROWSER Variable
    if [ -n "$BROWSER" ]; then
        CONTAINER_ARGS+=(-e "BROWSER=$BROWSER")
    fi
    
    log_info "Teste Browser im Container..."
    log_info "Container-Runtime: $CONTAINER_RUNTIME"
    log_info "BROWSER: ${BROWSER:-<nicht gesetzt>}"
    log_info "DISPLAY: $DISPLAY"
    log_info "XAUTH_FILE: $XAUTH_FILE"
    
    # Browser direkt aufrufen
    $CONTAINER_RUNTIME "${CONTAINER_ARGS[@]}" "$IMAGE_NAME:$IMAGE_TAG" \
        shell -c "echo 'User: '\$(whoami) && echo 'UID: '\$(id -u) && echo 'HOME: '\$HOME && echo 'DISPLAY: '\$DISPLAY && echo 'BROWSER: '\${BROWSER:-nicht gesetzt} && echo '---' && echo 'Starte Browser...' && \${BROWSER:-firefox} '$URL' 2>&1 || echo 'Browser fehlgeschlagen'"
    
    cleanup_xauth
}

cmd_info() {
    echo ""
    echo "╔═══════════════════════════════════════════════════════════════════════════╗"
    echo "║                         SYSTEM-INFORMATIONEN                              ║"
    echo "╚═══════════════════════════════════════════════════════════════════════════╝"
    echo ""
    
    # OS
    echo -e "${BLUE}Betriebssystem:${NC}"
    echo "  Erkannt: $HOST_OS"
    echo "  uname: $(uname -s)"
    echo ""
    
    # Container-Runtime
    echo -e "${BLUE}Container-Runtime:${NC}"
    if command -v podman &> /dev/null && podman info &> /dev/null 2>&1; then
        echo "  Podman: $(podman --version)"
    fi
    if command -v docker &> /dev/null; then
        echo "  Docker: $(docker --version 2>/dev/null || echo 'nicht verfügbar')"
    fi
    echo ""
    
    # X11 (nur unter Linux relevant)
    echo -e "${BLUE}X11-Konfiguration:${NC}"
    if [ "$HOST_OS" = "linux" ]; then
        echo "  DISPLAY: ${DISPLAY:-NICHT GESETZT!}"
        echo "  XAUTHORITY: ${XAUTHORITY:-~/.Xauthority}"
        if [ -f "${XAUTHORITY:-$HOME/.Xauthority}" ]; then
            echo "  Xauthority-Datei: vorhanden"
        else
            echo "  Xauthority-Datei: nicht gefunden"
        fi
    else
        echo "  X11: nicht verwendet (Mac/Windows)"
        echo "  Browser öffnet auf Host automatisch"
    fi
    echo ""
    
    # Browser
    echo -e "${BLUE}Browser:${NC}"
    if [ -n "$BROWSER" ]; then
        echo "  BROWSER: $BROWSER"
    else
        echo "  BROWSER: <nicht gesetzt> (System-Default)"
    fi
    if [ "$HOST_OS" = "linux" ]; then
        echo "  (Ändern mit: BROWSER=firefox ./flickr-docker.sh ...)"
    fi
    echo ""
    
    # xauth (nur unter Linux)
    if [ "$HOST_OS" = "linux" ]; then
        echo -e "${BLUE}xauth:${NC}"
        if command -v xauth &> /dev/null; then
            echo "  xauth: installiert"
            echo "  Cookies für \$DISPLAY:"
            xauth list "$DISPLAY" 2>/dev/null | head -3 || echo "    Keine Cookies gefunden"
        else
            echo "  xauth: NICHT INSTALLIERT!"
        fi
        echo ""
    fi
    
    # Verzeichnisse
    echo -e "${BLUE}Verzeichnisse:${NC}"
    echo "  Config: $CONFIG_DIR"
    echo "  Downloads: $WORK_DIR"
    echo "  Cache: $CACHE_DIR"
    echo ""
    
    # Token-Status
    echo -e "${BLUE}Flickr-Status:${NC}"
    if [ -f "$CONFIG_DIR/.flickr_download" ]; then
        echo "  API-Config: vorhanden"
    else
        echo "  API-Config: NICHT KONFIGURIERT"
    fi
    if is_token_valid "$CONFIG_DIR/.flickr_token"; then
        echo "  Token: gültig"
    else
        echo "  Token: nicht vorhanden oder ungültig"
    fi
    echo ""
    
    # Image-Status
    echo -e "${BLUE}Docker-Image:${NC}"
    local runtime="docker"
    if command -v podman &> /dev/null && podman info &> /dev/null 2>&1; then
        runtime="podman"
    fi
    if $runtime image inspect "$IMAGE_NAME:$IMAGE_TAG" &> /dev/null 2>&1; then
        echo "  $IMAGE_NAME:$IMAGE_TAG: vorhanden"
    else
        echo "  $IMAGE_NAME:$IMAGE_TAG: NICHT GEBAUT (führe 'build' aus)"
    fi
    echo ""
}

cmd_clean() {
    log_info "Räume auf..."
    
    # Container-Runtime erkennen
    local runtime="docker"
    if command -v podman &> /dev/null && podman info &> /dev/null 2>&1; then
        runtime="podman"
    fi
    
    # Image löschen
    if $runtime image inspect "$IMAGE_NAME:$IMAGE_TAG" &> /dev/null; then
        $runtime rmi "$IMAGE_NAME:$IMAGE_TAG"
        log_success "Image gelöscht ($runtime)"
    else
        log_info "Image nicht vorhanden"
    fi
    
    # xauth aufräumen
    cleanup_xauth
    
    log_success "Aufräumen abgeschlossen"
    
    echo ""
    echo "Folgende Verzeichnisse wurden NICHT gelöscht:"
    echo "  - $WORK_DIR (deine Downloads)"
    echo "  - $CONFIG_DIR (deine Konfiguration)"
    echo "  - $CACHE_DIR (API-Cache)"
    echo ""
    echo "Lösche diese manuell, wenn nicht mehr benötigt."
}

cmd_help() {
    cat << 'HELP_END'

╔═══════════════════════════════════════════════════════════════════════════╗
║                     FLICKR DOWNLOAD DOCKER SCRIPT                         ║
╚═══════════════════════════════════════════════════════════════════════════╝

Nutzung: ./flickr-docker.sh <befehl> [optionen]

BEFEHLE:

  build                     Docker-Image bauen
  
  auth                      Bei Flickr authentifizieren
                            Linux: öffnet Browser automatisch
                            Mac/Win: URL wird angezeigt, manuell öffnen
  
  download <username>       Alle Alben eines Benutzers herunterladen
  
  album <album-id>          Einzelnes Album herunterladen
  
  list <username>           Alben eines Benutzers auflisten
  
  shell                     Interaktive Shell im Container starten
  
  test-browser [url]        X11-Verbindung testen (nur Linux)
                            Auf Mac/Windows: testet nur Container
                            Standard-URL: https://www.flickr.com/
  
  info                      System-Informationen anzeigen (Debugging)
  
  clean                     Docker-Image und temp. Dateien löschen

BEISPIELE:

  # X11-Verbindung testen
  ./flickr-docker.sh test-browser
  ./flickr-docker.sh test-browser https://example.com
  
  # Erstmaliges Setup
  ./flickr-docker.sh build
  ./flickr-docker.sh auth
  
  # Alle Fotos herunterladen
  ./flickr-docker.sh download mein_flickr_name
  
  # Nur bestimmtes Album
  ./flickr-docker.sh list mein_flickr_name
  ./flickr-docker.sh album 72157622764287329

VERZEICHNISSE:

  ./flickr-backup/          Heruntergeladene Fotos
  ./flickr-config/          API-Keys und Token
  ./flickr-cache/           Cache für Resume-Funktion

VORAUSSETZUNGEN:

  - Docker oder Podman (wird automatisch erkannt)
  - Linux: X11 (DISPLAY muss gesetzt sein), xauth
  - Mac/Windows: Nur Docker/Podman erforderlich

PLATTFORM-UNTERSTÜTZUNG:

  Linux:      Volle Unterstützung mit X11-Browser-Forwarding
              Browser öffnet automatisch im Container
              
  Mac:        Browser-URL wird im Terminal angezeigt
              Manuell öffnen, OAuth-Callback funktioniert
              
  Windows:    Wie Mac (via WSL2 oder Git Bash)
              Alternativ: WSL2 verhält sich wie Linux

UMGEBUNGSVARIABLEN:

  BROWSER                   Browser für OAuth
                            Linux-Default: chrome
                            Mac/Windows: nicht gesetzt (System-Default)
                            Optionen: chrome, chromium, firefox
                            Beispiel: BROWSER=firefox ./flickr-docker.sh auth

PODMAN-HINWEISE (nur Linux):

  Das Script erkennt Podman automatisch und verwendet:
  - --userns=keep-id (für X11-Zugriff)
  - --security-opt label=disable (für SELinux)

HELP_END
}

# ============================================================================
# HAUPTPROGRAMM
# ============================================================================

main() {
    local COMMAND="${1:-help}"
    shift || true
    
    case "$COMMAND" in
        build)
            cmd_build
            ;;
        auth)
            cmd_auth
            ;;
        download)
            cmd_download "$@"
            ;;
        album)
            cmd_download_album "$@"
            ;;
        list)
            cmd_list "$@"
            ;;
        shell)
            cmd_shell
            ;;
        test-browser)
            cmd_test_browser "$@"
            ;;
        info)
            cmd_info
            ;;
        clean)
            cmd_clean
            ;;
        help|--help|-h)
            cmd_help
            ;;
        *)
            log_error "Unbekannter Befehl: $COMMAND"
            echo "Nutze '$0 help' für Hilfe."
            exit 1
            ;;
    esac
}

main "$@"
