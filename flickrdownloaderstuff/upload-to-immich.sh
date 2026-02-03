#!/bin/bash

cd "$(dirname "$0")"

IN_CONTAINER=false
if [ -f "/.dockerenv" ] || [ -f "/run/.containerenv" ] || [ -n "$KUBERNETES_SERVICE_HOST" ]; then
    IN_CONTAINER=true
fi

if [ "$IN_CONTAINER" = true ]; then
    echo "Running directly (in-container mode)..."
    DATA_DIR="${DATA_DIR:-$(pwd)/flickr-backup}" python3 "$(dirname "$0")/immich-uploader-wrapped.py" "$@"
else
    podman run -it --rm \
      -e IMMICH_INSTANCE_URL=${IMMICH_INSTANCE_URL} \
      -e IMMICH_API_KEY=${IMMICH_API_KEY} \
      -e DATA_DIR=/data \
      -v "$(pwd)/im.py:/immich-uploader-wrapped.py:ro" \
      -v "$(pwd)/flickr-backup:/data:ro" \
      nikolaik/python-nodejs:python3.12-nodejs22-alpine python3 /immich-uploader-wrapped.py "$@"
fi

echo DONE
