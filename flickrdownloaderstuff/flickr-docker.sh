#!/bin/bash
#
# Flickr Download Docker Script
# =============================
# Builds a Docker image with flickr_download and X11 support,
# so the browser opens on the host for OAuth authentication.
#
# Usage:
#   ./flickr-docker.sh build                    # Build image
#   ./flickr-docker.sh auth                     # Authenticate only
#   ./flickr-docker.sh download <username>      # Download photos
#   ./flickr-docker.sh shell                    # Shell in container
#   ./flickr-docker.sh clean                    # Remove image and temp files
#

set -e

# ============================================================================
# CONFIGURATION
# ============================================================================

IMAGE_NAME="flickr-download"
IMAGE_TAG="latest"
WORK_DIR="$(pwd)/flickr-backup"
CONFIG_DIR="$(pwd)/flickr-config"
CACHE_DIR="$(pwd)/flickr-cache"
XAUTH_FILE="/tmp/.flickr-docker.xauth"

# Rate-limit backoff (seconds); doubles on consecutive 429s, caps at max
BACKOFF_BASE=60
BACKOFF_MAX=600

# Container runtime (detected later)
CONTAINER_RUNTIME=""

# In-container mode (detected later)
IN_CONTAINER=false

# OS detection
detect_os() {
    case "$(uname -s)" in
        Linux*)
            # Check for WSL
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

# Container detection
detect_container() {
    # FLICKR_HOME is set exclusively by `make dstart` when flickr-config dir exists
    if [ -n "$FLICKR_HOME" ]; then
        IN_CONTAINER=true; return
    fi
    # Fallback: standard container marker files
    if [ -f "/.dockerenv" ] || [ -f "/run/.containerenv" ]; then
        IN_CONTAINER=true; return
    fi
}
detect_container

# Override paths when running inside the container
if [ "$IN_CONTAINER" = true ]; then
    CONFIG_DIR="${FLICKR_HOME:-$HOME/.flickr-config}"
    WORK_DIR="$HOME/flickr-backup"
    CACHE_DIR="$HOME/flickr-cache"
fi

# Browser configuration per OS
# Linux: explicit browser for X11 forwarding
# Mac/Windows: leave empty -> Python webbrowser uses system default
if [ "$HOST_OS" = "linux" ]; then
    BROWSER="${BROWSER:-chrome}"
else
    # On Mac/Windows: empty BROWSER = Python opens system browser
    BROWSER="${BROWSER:-}"
fi

# X11 only on Linux
X11_AVAILABLE=false
if [ "$HOST_OS" = "linux" ] && [ -n "$DISPLAY" ]; then
    X11_AVAILABLE=true
fi

# Output colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ============================================================================
# HELPER FUNCTIONS
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
    # No Docker/Podman detection needed inside a container
    if [ "$IN_CONTAINER" = true ]; then
        return 0
    fi

    log_info "Detected OS: $HOST_OS"

    # Detect container runtime (Docker or Podman)
    if command -v podman &> /dev/null && podman info &> /dev/null 2>&1; then
        CONTAINER_RUNTIME="podman"
        log_info "Container runtime: Podman detected"
    elif command -v docker &> /dev/null; then
        CONTAINER_RUNTIME="docker"
        log_info "Container runtime: Docker detected"
    else
        log_error "Neither Docker nor Podman found!"
        exit 1
    fi

    # X11 checks only on Linux
    if [ "$HOST_OS" = "linux" ]; then
        if [ -z "$DISPLAY" ]; then
            log_error "DISPLAY is not set. X11 required!"
            log_info "Hint: export DISPLAY=:0"
            exit 1
        fi

        if ! command -v xauth &> /dev/null; then
            log_error "xauth is not installed! (apt install xauth)"
            exit 1
        fi
    else
        log_info "No X11 forwarding (Mac/Windows) - browser opens on host"
    fi
}

setup_directories() {
    log_info "Creating directories..."
    mkdir -p "$WORK_DIR"
    mkdir -p "$CONFIG_DIR"
    mkdir -p "$CACHE_DIR"
    log_success "Directories created"
}

setup_xauth() {
    # Only on Linux with X11
    if [ "$HOST_OS" != "linux" ]; then
        log_info "X11 setup skipped (not Linux)"
        return 0
    fi

    log_info "Configuring X11 authentication..."

    # Check for Wayland
    if [ -n "$WAYLAND_DISPLAY" ]; then
        log_info "Wayland detected (WAYLAND_DISPLAY=$WAYLAND_DISPLAY)"
        log_info "X11 apps run via XWayland"
    fi

    # Remove old xauth file
    rm -f "$XAUTH_FILE"
    touch "$XAUTH_FILE"
    chmod 666 "$XAUTH_FILE"  # Must be readable by all users in the container

    # Extract X11 cookie
    # For Podman with keep-id we need the cookie for the current user
    local xauth_source=""

    if [ -n "$XAUTHORITY" ] && [ -f "$XAUTHORITY" ]; then
        # Use existing XAUTHORITY
        xauth_source="$XAUTHORITY"
        log_info "Using XAUTHORITY: $XAUTHORITY"
    elif [ -f "$HOME/.Xauthority" ]; then
        xauth_source="$HOME/.Xauthority"
        log_info "Using ~/.Xauthority"
    fi

    if [ -n "$xauth_source" ]; then
        # Method 1: direct copy (usually works best)
        cp "$xauth_source" "$XAUTH_FILE"
        chmod 666 "$XAUTH_FILE"
        log_info "xauth file copied from: $xauth_source"
    else
        # Method 2: extract cookie from xauth
        # The 'ffff' replaces the display number for wildcard matching
        log_info "Extracting cookie with xauth nlist..."
        xauth nlist "$DISPLAY" 2>/dev/null | sed -e 's/^..../ffff/' | xauth -f "$XAUTH_FILE" nmerge - 2>/dev/null || true
    fi

    if [ -s "$XAUTH_FILE" ]; then
        local cookie_count
        cookie_count=$(xauth -f "$XAUTH_FILE" list 2>/dev/null | wc -l)
        log_success "X11 authentication configured ($cookie_count cookie(s))"
    else
        log_warn "Could not extract X11 cookie!"
        log_warn "Browser may not work."
        log_info "Debug: DISPLAY=$DISPLAY, XAUTHORITY=${XAUTHORITY:-not set}"
    fi
}

cleanup_xauth() {
    # Only on Linux
    if [ "$HOST_OS" != "linux" ]; then
        return 0
    fi

    if [ -f "$XAUTH_FILE" ]; then
        rm -f "$XAUTH_FILE"
        log_info "X11 auth file cleaned up"
    fi
}

check_config() {
    if [ ! -f "$CONFIG_DIR/.flickr_download" ]; then
        log_warn "No API configuration found!"
        echo ""
        echo "Please create $CONFIG_DIR/.flickr_download with the following content:"
        echo ""
        echo "  api_key: YOUR_FLICKR_API_KEY"
        echo "  api_secret: YOUR_FLICKR_API_SECRET"
        echo ""
        echo "Get your API key here: https://www.flickr.com/services/apps/create/"
        echo ""

        read -p "Create the file now? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            read -p "API Key: " api_key
            read -p "API Secret: " api_secret
            echo "api_key: $api_key" > "$CONFIG_DIR/.flickr_download"
            echo "api_secret: $api_secret" >> "$CONFIG_DIR/.flickr_download"
            chmod 600 "$CONFIG_DIR/.flickr_download"
            log_success "Configuration created"
        else
            exit 1
        fi
    fi
}

# ============================================================================
# DOCKERFILE (INLINE)
# ============================================================================

build_image() {
    log_info "Building Docker image '$IMAGE_NAME:$IMAGE_TAG'..."

    # Temporary build directory
    BUILD_DIR=$(mktemp -d)
    trap "rm -rf $BUILD_DIR" EXIT

    # Write Dockerfile
    cat > "$BUILD_DIR/Dockerfile" << 'DOCKERFILE_END'
# ============================================================================
# Flickr Download Docker Image
# With Firefox and X11 support for OAuth authentication
# Compatible with Docker and Podman
# ============================================================================

FROM python:3.14-slim

LABEL maintainer="Flickr Backup Script"
LABEL description="Flickr Download with browser support for OAuth"

# Install system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    # ExifTool for metadata
    libimage-exiftool-perl \
    # Browsers (both for flexibility)
    chromium \
    firefox-esr \
    # xdg-utils for xdg-open
    xdg-utils \
    # X11 libraries
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
    # Clean up
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Browser name symlinks ($BROWSER compatibility)
# chrome/google-chrome -> chromium
# firefox stays firefox-esr
RUN ln -sf /usr/bin/chromium /usr/bin/chrome && \
    ln -sf /usr/bin/chromium /usr/bin/google-chrome && \
    ln -sf /usr/bin/firefox-esr /usr/bin/firefox

# Python packages
RUN pip install --no-cache-dir \
    git+https://github.com/beaufour/flickr-download.git \
    PyYAML

# Working directory
WORKDIR /data

# Cache directory
RUN mkdir -p /cache && chmod 777 /cache

# Home directory for Podman (keep-id) - must be writable by all users
RUN mkdir -p /home/poduser && chmod 777 /home/poduser

# Mozilla directory for Firefox profile (both homes)
RUN mkdir -p /root/.mozilla /home/poduser/.mozilla && \
    chmod -R 777 /root/.mozilla /home/poduser/.mozilla

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV HOME=/root

# Entrypoint script with improved shell support
RUN echo '#!/bin/bash\n\
# Ensure HOME directory exists and is writable\n\
if [ ! -d "$HOME" ]; then\n\
    mkdir -p "$HOME" 2>/dev/null || true\n\
fi\n\
# Mozilla directory for Firefox\n\
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

    # Build image
    # Detect container runtime
    local runtime="docker"
    if command -v podman &> /dev/null && podman info &> /dev/null 2>&1; then
        runtime="podman"
        log_info "Using Podman for build"
    else
        log_info "Using Docker for build"
    fi

    $runtime build -t "$IMAGE_NAME:$IMAGE_TAG" "$BUILD_DIR"

    log_success "Image built successfully ($runtime)"
}

# ============================================================================
# RUN CONTAINER
# ============================================================================

is_token_valid() {
    local token_file="$1"

    # File must exist
    [ -f "$token_file" ] || return 1

    # File must have content
    [ -s "$token_file" ] || return 1

    # File must have at least 2 non-empty lines (key + secret)
    local line_count
    line_count=$(grep -c '[^[:space:]]' "$token_file" 2>/dev/null || echo "0")
    [ "$line_count" -ge 2 ] || return 1

    return 0
}

# Convert username to Flickr URL if needed
# flickr_download works more reliably with URLs
flickr_user_to_url() {
    local user="$1"

    # If already a URL, return unchanged
    if [[ "$user" == http* ]]; then
        echo "$user"
    else
        # Convert username to URL
        echo "https://www.flickr.com/photos/${user}/"
    fi
}

build_container_args() {
    # Populates the CONTAINER_ARGS array (must be declared by the caller).
    # Does NOT include the "run" verb or interactive flags (-it / -i);
    # the caller prepends those.

    CONTAINER_ARGS+=(
        --rm
        --name flickr-download-run
        -v "$WORK_DIR:/data"
        -v "$CACHE_DIR:/cache"
    )

    # Network configuration per OS
    if [ "$HOST_OS" = "linux" ]; then
        # Linux: --network=host for OAuth callback
        CONTAINER_ARGS+=(--network=host)
    else
        # Mac/Windows: --network=host does not work in Docker Desktop
        # Publish ports for OAuth callback (flickr_download typically uses 8080-8100)
        log_info "Mac/Windows: publishing ports 8080-8100 for OAuth callback"
        CONTAINER_ARGS+=(-p 8080-8100:8080-8100)
    fi

    # X11 configuration only on Linux
    if [ "$HOST_OS" = "linux" ]; then
        CONTAINER_ARGS+=(
            -e "DISPLAY=$DISPLAY"
            -e "XAUTHORITY=/tmp/.xauth"
            -v "/tmp/.X11-unix:/tmp/.X11-unix:rw"
            -v "$XAUTH_FILE:/tmp/.xauth:ro"
        )
    fi

    # Podman-specific options (Linux only, Podman on Mac/Windows works differently)
    if [ "$CONTAINER_RUNTIME" = "podman" ] && [ "$HOST_OS" = "linux" ]; then
        log_info "Using Podman-specific options (userns=keep-id)"
        CONTAINER_ARGS+=(
            # Preserve user namespace for X11 access
            --userns=keep-id
            # Disable security label for X11 access (SELinux)
            --security-opt label=disable
        )
        # With Podman keep-id: user is NOT root!
        # Mount config to /home/poduser and set HOME accordingly
        # flickr_download looks for .flickr_download in $HOME
        CONTAINER_ARGS+=(
            -v "$CONFIG_DIR:/home/poduser"
            -e "HOME=/home/poduser"
        )
    else
        # Docker (all OS) or Podman on Mac: user is root, HOME=/root
        CONTAINER_ARGS+=(
            -v "$CONFIG_DIR:/root"
            -e "HOME=/root"
        )
    fi

    # BROWSER variable
    # Linux: explicit browser for X11 forwarding
    # Mac/Windows: empty -> Python webbrowser prints URL to stdout
    if [ -n "$BROWSER" ]; then
        CONTAINER_ARGS+=(-e "BROWSER=$BROWSER")
    fi
}

run_container() {
    local CMD=("$@")

    check_dependencies
    setup_directories
    check_config
    setup_xauth

    log_info "Starting container..."
    echo ""

    # Remove old container if present
    $CONTAINER_RUNTIME rm -f flickr-download-run 2>/dev/null || true

    local CONTAINER_ARGS=(run -it)
    build_container_args

    $CONTAINER_RUNTIME "${CONTAINER_ARGS[@]}" "$IMAGE_NAME:$IMAGE_TAG" "${CMD[@]}"

    local exit_code=$?

    cleanup_xauth

    return $exit_code
}

run_direct() {
    check_config
    log_info "Running flickr_download directly (in-container mode)..."
    echo ""
    run_with_backoff env HOME="$CONFIG_DIR" flickr_download "$@"
}

run_with_backoff() {
    local fifo
    fifo=$(mktemp -u)
    mkfifo "$fifo"
    trap "rm -f '$fifo'" RETURN

    # Run the actual command in the background, merge stderr into stdout via FIFO
    "$@" > "$fifo" 2>&1 &
    local pid=$!

    local consecutive_429=0

    while IFS= read -r line; do
        # Re-colorize Python logging level prefix (lost TTY due to FIFO)
        case "$line" in
            ERROR:*)   echo -e "${RED}[ERROR]${NC}${line#ERROR}" ;;
            WARNING:*) echo -e "${YELLOW}[WARN]${NC}${line#WARNING}" ;;
            INFO:*)    echo -e "${BLUE}[INFO]${NC}${line#INFO}" ;;
            *)         echo "$line" ;;
        esac
        if [[ "$line" == *"HTTP Error 429"* ]]; then
            consecutive_429=$((consecutive_429 + 1))
            local wait=$((BACKOFF_BASE * consecutive_429))
            [ "$wait" -gt "$BACKOFF_MAX" ] && wait=$BACKOFF_MAX
            log_warn "Rate limit hit (#$consecutive_429), suspending for ${wait}s..."
            kill -STOP "$pid" 2>/dev/null
            sleep "$wait"
            kill -CONT "$pid" 2>/dev/null
            log_info "Resuming download..."
        else
            consecutive_429=0
        fi
    done < "$fifo"

    wait "$pid"
}

run_container_with_backoff() {
    local CMD=("$@")

    check_dependencies
    setup_directories
    check_config
    setup_xauth

    log_info "Starting container..."
    echo ""

    $CONTAINER_RUNTIME rm -f flickr-download-run 2>/dev/null || true

    # Use -i (no TTY) so stdout can be piped through run_with_backoff
    local CONTAINER_ARGS=(run -i)
    build_container_args

    run_with_backoff \
        $CONTAINER_RUNTIME "${CONTAINER_ARGS[@]}" "$IMAGE_NAME:$IMAGE_TAG" "${CMD[@]}"

    local exit_code=$?
    cleanup_xauth
    return $exit_code
}

# ============================================================================
# COMMANDS
# ============================================================================

cmd_build() {
    if [ "$IN_CONTAINER" = true ]; then
        log_info "flickr_download is already installed (in-container mode)"
        flickr_download --version 2>/dev/null || true
        return 0
    fi
    check_dependencies
    build_image
}

cmd_auth() {
    log_info "Starting authentication..."

    # Remove invalid token first
    if [ -f "$CONFIG_DIR/.flickr_token" ] && ! is_token_valid "$CONFIG_DIR/.flickr_token"; then
        log_warn "Invalid token found - removing"
        rm -f "$CONFIG_DIR/.flickr_token"
    fi

    echo ""
    if [ "$IN_CONTAINER" = true ]; then
        echo "NOTE (in-container mode):"
        echo "  - A URL will be displayed in the terminal"
        echo "  - Open this URL in a browser on the host"
        echo "  - After login the callback will be processed automatically"
        echo ""
        echo "Please log in to Flickr and authorize the app."
        echo ""
        run_direct -t
    else
        log_info "OS: $HOST_OS"
        if [ "$HOST_OS" = "linux" ]; then
            log_info "Browser: ${BROWSER:-<system-default>}"
            echo "A browser window will open."
        else
            echo "NOTE for Mac/Windows:"
            echo "  - A URL will be displayed in the terminal"
            echo "  - Open this URL manually in your browser"
            echo "  - After login the callback will be processed automatically"
            echo ""
        fi
        echo "Please log in to Flickr and authorize the app."
        echo ""
        run_container -t
    fi

    if is_token_valid "$CONFIG_DIR/.flickr_token"; then
        log_success "Authentication successful!"
        log_info "Token saved in: $CONFIG_DIR/.flickr_token"
    else
        log_error "Authentication failed or was cancelled!"
        rm -f "$CONFIG_DIR/.flickr_token"
    fi
}

cmd_download() {
    local USERNAME="$1"

    if [ -z "$USERNAME" ]; then
        log_error "Username missing!"
        echo "Usage: $0 download <flickr-username>"
        exit 1
    fi

    if ! is_token_valid "$CONFIG_DIR/.flickr_token"; then
        log_warn "Not yet authenticated (no valid token)!"
        if [ "$IN_CONTAINER" = true ]; then
            echo "Run on the host: cd flickrdownloaderstuff && ./flickr-docker.sh auth"
        else
            echo "Run '$0 auth' first."
        fi
        exit 1
    fi

    # Convert username to URL
    local FLICKR_USER
    FLICKR_USER=$(flickr_user_to_url "$USERNAME")

    log_info "Starting download for: $FLICKR_USER"
    log_info "Target directory: $WORK_DIR"
    echo ""

    if [ "$IN_CONTAINER" = true ]; then
        cd "$WORK_DIR"
        run_direct \
            -t \
            --download_user "$FLICKR_USER" \
            --save_json \
            --cache "$CACHE_DIR/api_cache" \
            --metadata_store
    else
        run_container_with_backoff \
            -t \
            --download_user "$FLICKR_USER" \
            --save_json \
            --cache /cache/api_cache \
            --metadata_store
    fi

    log_success "Download complete!"
    log_info "Photos saved in: $WORK_DIR"
}

cmd_download_album() {
    local ALBUM_ID="$1"

    if [ -z "$ALBUM_ID" ]; then
        log_error "Album ID missing!"
        echo "Usage: $0 album <album-id>"
        echo ""
        echo "Find album IDs with: $0 list <username>"
        exit 1
    fi

    log_info "Starting download for album: $ALBUM_ID"

    if [ "$IN_CONTAINER" = true ]; then
        cd "$WORK_DIR"
        run_direct \
            -t \
            --download "$ALBUM_ID" \
            --save_json \
            --cache "$CACHE_DIR/api_cache" \
            --metadata_store
    else
        run_container_with_backoff \
            -t \
            --download "$ALBUM_ID" \
            --save_json \
            --cache /cache/api_cache \
            --metadata_store
    fi
}

cmd_list() {
    local USERNAME="$1"

    if [ -z "$USERNAME" ]; then
        log_error "Username missing!"
        echo "Usage: $0 list <flickr-username>"
        exit 1
    fi

    # Convert username to URL
    local FLICKR_USER
    FLICKR_USER=$(flickr_user_to_url "$USERNAME")

    log_info "Listing albums for: $FLICKR_USER"
    echo ""

    if [ "$IN_CONTAINER" = true ]; then
        run_direct -t --list "$FLICKR_USER"
    else
        run_container -t --list "$FLICKR_USER"
    fi
}

cmd_shell() {
    if [ "$IN_CONTAINER" = true ]; then
        log_info "You are already inside the container."
        echo "Use the shell directly or run flickr_download manually:"
        echo "  HOME=$CONFIG_DIR flickr_download --help"
        return 0
    fi
    log_info "Starting shell in container..."
    run_container shell
}

cmd_test_browser() {
    if [ "$IN_CONTAINER" = true ]; then
        log_info "No browser available in container."
        echo "Please run browser tests on the host:"
        echo "  cd flickrdownloaderstuff && ./flickr-docker.sh test-browser"
        return 0
    fi

    local URL="${1:-https://www.flickr.com/}"

    # Only useful on Linux with X11
    if [ "$HOST_OS" != "linux" ]; then
        log_warn "test-browser is only available on Linux with X11"
        log_info "On $HOST_OS Python's webbrowser module opens the system browser automatically"
        log_info "Testing whether container works instead..."
        echo ""

        check_dependencies
        $CONTAINER_RUNTIME rm -f flickr-download-run 2>/dev/null || true

        $CONTAINER_RUNTIME run -it --rm \
            --name flickr-download-run \
            "$IMAGE_NAME:$IMAGE_TAG" \
            shell -c "echo 'Container is running!' && echo 'Python:' && python --version && echo 'flickr_download:' && flickr_download --version 2>/dev/null || echo 'installed'"

        return 0
    fi

    log_info "Testing X11 connection..."
    log_info "Opening browser with: $URL"
    echo ""

    check_dependencies
    setup_xauth

    # Remove old container if present
    $CONTAINER_RUNTIME rm -f flickr-download-run 2>/dev/null || true

    # Base arguments
    local CONTAINER_ARGS=(
        run -it --rm
        --name flickr-download-run
        --network=host
        -e "DISPLAY=$DISPLAY"
        -e "XAUTHORITY=/tmp/.xauth"
        -v "/tmp/.X11-unix:/tmp/.X11-unix:rw"
        -v "$XAUTH_FILE:/tmp/.xauth:ro"
    )

    # Podman-specific options
    if [ "$CONTAINER_RUNTIME" = "podman" ]; then
        log_info "Podman: using --userns=keep-id"
        CONTAINER_ARGS+=(
            --userns=keep-id
            --security-opt label=disable
            -e "HOME=/home/poduser"
        )
    else
        CONTAINER_ARGS+=(-e "HOME=/root")
    fi

    # BROWSER variable
    if [ -n "$BROWSER" ]; then
        CONTAINER_ARGS+=(-e "BROWSER=$BROWSER")
    fi

    log_info "Testing browser in container..."
    log_info "Container runtime: $CONTAINER_RUNTIME"
    log_info "BROWSER: ${BROWSER:-<not set>}"
    log_info "DISPLAY: $DISPLAY"
    log_info "XAUTH_FILE: $XAUTH_FILE"

    # Launch browser directly
    $CONTAINER_RUNTIME "${CONTAINER_ARGS[@]}" "$IMAGE_NAME:$IMAGE_TAG" \
        shell -c "echo 'User: '\$(whoami) && echo 'UID: '\$(id -u) && echo 'HOME: '\$HOME && echo 'DISPLAY: '\$DISPLAY && echo 'BROWSER: '\${BROWSER:-not set} && echo '---' && echo 'Starting browser...' && \${BROWSER:-firefox} '$URL' 2>&1 || echo 'Browser failed'"

    cleanup_xauth
}

cmd_info() {
    echo ""
    echo "╔═══════════════════════════════════════════════════════════════════════════╗"
    echo "║                          SYSTEM INFORMATION                               ║"
    echo "╚═══════════════════════════════════════════════════════════════════════════╝"
    echo ""

    if [ "$IN_CONTAINER" = true ]; then
        echo -e "${BLUE}Mode:${NC}"
        echo "  In-container mode (flickr_download runs directly)"
        echo ""

        echo -e "${BLUE}Directories:${NC}"
        echo "  Config:    $CONFIG_DIR $([ -d "$CONFIG_DIR" ] && echo '(exists)' || echo '(MISSING)')"
        echo "  Downloads: $WORK_DIR $([ -d "$WORK_DIR" ] && echo '(exists)' || echo '(MISSING)')"
        echo "  Cache:     $CACHE_DIR $([ -d "$CACHE_DIR" ] && echo '(exists)' || echo '(MISSING)')"
        echo ""

        echo -e "${BLUE}Flickr status:${NC}"
        if [ -f "$CONFIG_DIR/.flickr_download" ]; then
            echo "  API config: present"
        else
            echo "  API config: NOT CONFIGURED"
        fi
        if is_token_valid "$CONFIG_DIR/.flickr_token"; then
            echo "  Token: valid"
        else
            echo "  Token: missing or invalid"
        fi
        echo ""

        echo -e "${BLUE}Tools:${NC}"
        echo "  flickr_download: $(flickr_download --version 2>/dev/null || echo 'not found')"
        echo "  Python: $(python3 --version 2>/dev/null || echo 'not found')"
        echo "  ExifTool: $(exiftool -ver 2>/dev/null || echo 'not found')"
        echo ""
        return 0
    fi

    # OS
    echo -e "${BLUE}Operating system:${NC}"
    echo "  Detected: $HOST_OS"
    echo "  uname: $(uname -s)"
    echo ""

    # Container runtime
    echo -e "${BLUE}Container runtime:${NC}"
    if command -v podman &> /dev/null && podman info &> /dev/null 2>&1; then
        echo "  Podman: $(podman --version)"
    fi
    if command -v docker &> /dev/null; then
        echo "  Docker: $(docker --version 2>/dev/null || echo 'not available')"
    fi
    echo ""

    # X11 (only relevant on Linux)
    echo -e "${BLUE}X11 configuration:${NC}"
    if [ "$HOST_OS" = "linux" ]; then
        echo "  DISPLAY: ${DISPLAY:-NOT SET!}"
        echo "  XAUTHORITY: ${XAUTHORITY:-~/.Xauthority}"
        if [ -f "${XAUTHORITY:-$HOME/.Xauthority}" ]; then
            echo "  Xauthority file: present"
        else
            echo "  Xauthority file: not found"
        fi
    else
        echo "  X11: not used (Mac/Windows)"
        echo "  Browser opens on host automatically"
    fi
    echo ""

    # Browser
    echo -e "${BLUE}Browser:${NC}"
    if [ -n "$BROWSER" ]; then
        echo "  BROWSER: $BROWSER"
    else
        echo "  BROWSER: <not set> (system default)"
    fi
    if [ "$HOST_OS" = "linux" ]; then
        echo "  (Change with: BROWSER=firefox ./flickr-docker.sh ...)"
    fi
    echo ""

    # xauth (only on Linux)
    if [ "$HOST_OS" = "linux" ]; then
        echo -e "${BLUE}xauth:${NC}"
        if command -v xauth &> /dev/null; then
            echo "  xauth: installed"
            echo "  Cookies for \$DISPLAY:"
            xauth list "$DISPLAY" 2>/dev/null | head -3 || echo "    No cookies found"
        else
            echo "  xauth: NOT INSTALLED!"
        fi
        echo ""
    fi

    # Directories
    echo -e "${BLUE}Directories:${NC}"
    echo "  Config: $CONFIG_DIR"
    echo "  Downloads: $WORK_DIR"
    echo "  Cache: $CACHE_DIR"
    echo ""

    # Token status
    echo -e "${BLUE}Flickr status:${NC}"
    if [ -f "$CONFIG_DIR/.flickr_download" ]; then
        echo "  API config: present"
    else
        echo "  API config: NOT CONFIGURED"
    fi
    if is_token_valid "$CONFIG_DIR/.flickr_token"; then
        echo "  Token: valid"
    else
        echo "  Token: missing or invalid"
    fi
    echo ""

    # Image status
    echo -e "${BLUE}Docker image:${NC}"
    local runtime="docker"
    if command -v podman &> /dev/null && podman info &> /dev/null 2>&1; then
        runtime="podman"
    fi
    if $runtime image inspect "$IMAGE_NAME:$IMAGE_TAG" &> /dev/null 2>&1; then
        echo "  $IMAGE_NAME:$IMAGE_TAG: present"
    else
        echo "  $IMAGE_NAME:$IMAGE_TAG: NOT BUILT (run 'build' first)"
    fi
    echo ""
}

cmd_clean() {
    if [ "$IN_CONTAINER" = true ]; then
        log_info "No Docker image to remove (in-container mode)."
        return 0
    fi

    log_info "Cleaning up..."

    # Detect container runtime
    local runtime="docker"
    if command -v podman &> /dev/null && podman info &> /dev/null 2>&1; then
        runtime="podman"
    fi

    # Remove image
    if $runtime image inspect "$IMAGE_NAME:$IMAGE_TAG" &> /dev/null; then
        $runtime rmi "$IMAGE_NAME:$IMAGE_TAG"
        log_success "Image removed ($runtime)"
    else
        log_info "Image not present"
    fi

    # Clean up xauth
    cleanup_xauth

    log_success "Cleanup complete"

    echo ""
    echo "The following directories were NOT removed:"
    echo "  - $WORK_DIR (your downloads)"
    echo "  - $CONFIG_DIR (your configuration)"
    echo "  - $CACHE_DIR (API cache)"
    echo ""
    echo "Remove them manually if no longer needed."
}

cmd_help() {
    if [ "$IN_CONTAINER" = true ]; then
        cat << HELP_CONTAINER_END

╔═══════════════════════════════════════════════════════════════════════════╗
║               FLICKR DOWNLOAD — IN-CONTAINER MODE                         ║
╚═══════════════════════════════════════════════════════════════════════════╝

Usage: $0 <command> [options]

AVAILABLE COMMANDS:

  auth                      Authenticate with Flickr
                            URL is displayed, open in a browser on the host
  download <username>       Download all albums for a user
  album <album-id>          Download a single album
  list <username>           List albums for a user
  info                      Show paths and tool versions
  help                      Show this help

NOT AVAILABLE IN CONTAINER (run on the host):

  build                     (flickr_download is already installed)
  test-browser              (no browser available)
  shell                     (already in the container)
  clean                     (no image present)

EXAMPLES:

  $0 list my_flickr_name
  $0 download my_flickr_name
  $0 album 72157622764287329

HELP_CONTAINER_END
        return 0
    fi

    cat << 'HELP_END'

╔═══════════════════════════════════════════════════════════════════════════╗
║                     FLICKR DOWNLOAD DOCKER SCRIPT                         ║
╚═══════════════════════════════════════════════════════════════════════════╝

Usage: ./flickr-docker.sh <command> [options]

COMMANDS:

  build                     Build Docker image

  auth                      Authenticate with Flickr
                            Linux: opens browser automatically
                            Mac/Win: URL is displayed, open manually

  download <username>       Download all albums for a user

  album <album-id>          Download a single album

  list <username>           List albums for a user

  shell                     Open interactive shell in container

  test-browser [url]        Test X11 connection (Linux only)
                            On Mac/Windows: tests container only
                            Default URL: https://www.flickr.com/

  info                      Show system information (debugging)

  clean                     Remove Docker image and temp files

EXAMPLES:

  # Test X11 connection
  ./flickr-docker.sh test-browser
  ./flickr-docker.sh test-browser https://example.com

  # First-time setup
  ./flickr-docker.sh build
  ./flickr-docker.sh auth

  # Download all photos
  ./flickr-docker.sh download my_flickr_name

  # Specific album only
  ./flickr-docker.sh list my_flickr_name
  ./flickr-docker.sh album 72157622764287329

DIRECTORIES:

  ./flickr-backup/          Downloaded photos
  ./flickr-config/          API keys and token
  ./flickr-cache/           Cache for resume functionality

PREREQUISITES:

  - Docker or Podman (auto-detected)
  - Linux: X11 (DISPLAY must be set), xauth
  - Mac/Windows: Docker/Podman only

PLATFORM SUPPORT:

  Linux:      Full support with X11 browser forwarding
              Browser opens automatically in container

  Mac:        Browser URL is displayed in terminal
              Open manually, OAuth callback works

  Windows:    Same as Mac (via WSL2 or Git Bash)
              Alternatively: WSL2 behaves like Linux

ENVIRONMENT VARIABLES:

  BROWSER                   Browser for OAuth
                            Linux default: chrome
                            Mac/Windows: not set (system default)
                            Options: chrome, chromium, firefox
                            Example: BROWSER=firefox ./flickr-docker.sh auth

PODMAN NOTES (Linux only):

  The script detects Podman automatically and uses:
  - --userns=keep-id (for X11 access)
  - --security-opt label=disable (for SELinux)

HELP_END
}

# ============================================================================
# MAIN
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
            log_error "Unknown command: $COMMAND"
            echo "Use '$0 help' for help."
            exit 1
            ;;
    esac
}

main "$@"
