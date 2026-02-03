#!/bin/bash
# Ensure HOME directory exists and is writable
if [ ! -d "$HOME" ]; then
    mkdir -p "$HOME" 2>/dev/null || true
fi
# Mozilla directory for Firefox
mkdir -p "$HOME/.mozilla" 2>/dev/null || true

if [ "$1" = "shell" ]; then
    shift
    if [ $# -eq 0 ]; then
        exec /bin/bash
    else
        exec /bin/bash "$@"
    fi
elif [ "$1" = "download_then_upload" ]; then
    for var in DATA_DIR IMMICH_API_KEY IMMICH_INSTANCE_URL; do
        if [ -z "${!var}" ]; then
            echo "ERROR: Required environment variable $var is not set" >&2
            exit 1
        fi
    done
    /usr/local/bin/flickr-docker.sh info
    /usr/local/bin/flickr-docker.sh download "${@:2}"
    rc_download=$?
    echo rc_download: $rc_download
    /usr/local/bin/upload-to-immich.sh
    rc_upload=$?
    echo rc_upload: $rc_upload
    exit $(( rc_download > rc_upload ? rc_download : rc_upload ))
else
    /usr/local/bin/flickr-docker.sh info &&
    exec /usr/local/bin/flickr-docker.sh "$@"
fi
