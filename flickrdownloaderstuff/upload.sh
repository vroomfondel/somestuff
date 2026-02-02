#!/bin/bash

cd "$(dirname "$0")"

IN_CONTAINER=false
if [ -f "/.dockerenv" ] || [ -f "/run/.containerenv" ] || [ -n "$KUBERNETES_SERVICE_HOST" ]; then
    IN_CONTAINER=true
fi

cat >im.sh <<'EOFIMSH'
echo START

npm install -g @immich/cli

for dir in ${DATA_DIR}/*/; do
  album="$(basename "$dir")"
  find "$dir" -type f \( -iname "*.jpg" -o -iname "*.png" -o -iname "*.mp4" \) | xargs -I {} immich upload {} --album "$album"
done

echo
echo DONE
EOFIMSH

chmod a+x im.sh

if [ "$IN_CONTAINER" = true ]; then
    echo "Running directly (in-container mode)..."
    DATA_DIR="${DATA_DIR:-$(pwd)/flickr-backup}" /bin/sh im.sh
else
    podman run -it --rm \
      -e IMMICH_INSTANCE_URL=${IMMICH_INSTANCE_URL} \
      -e IMMICH_API_KEY=${IMMICH_API_KEY} \
      -e DATA_DIR=/data \
      -v "$(pwd)/im.sh:/im.sh" \
      -v "$(pwd)/flickr-backup:/data:ro" \
      --entrypoint /bin/sh \
      node:lts-alpine -c /im.sh
fi

echo DONE
