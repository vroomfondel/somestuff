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
else
    /usr/local/bin/flickr-docker.sh info &&
    exec /usr/local/bin/flickr-docker.sh "$@"
fi
