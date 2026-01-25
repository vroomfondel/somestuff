#!/bin/bash

medir=$(dirname "$0")
medir=$(realpath "${medir}")
cd "${medir}" || exit 123

python_version=3.14
pandas_version=2.2.3
debian_version=slim-trixie

DOCKER_IMAGE=docker.io/xomoxcc/pythonpandasmultiarch:python-${python_version}-pandas-${pandas_version}-${debian_version}
dockerfile=Dockerfile

buildtime=$(date +'%Y-%m-%d %H:%M:%S %Z')

dockerfile=Dockerfile

source ../scripts/include.sh

export DOCKER_CONFIG=$(realpath $(pwd)/../docker-config)

# Detect if "docker" is actually podman
dockerispodman=0
if docker --version 2>&1 | grep -qi podman; then
  dockerispodman=1
fi

export REGISTRY_AUTH_FILE="${DOCKER_CONFIG}/config.json"

if ! [ -e "${REGISTRY_AUTH_FILE}" ] ; then
  echo "${DOCKER_TOKEN}" | docker login --username "${DOCKER_TOKENUSER}" --password-stdin

  lsucc=$?
  if [ $lsucc -ne 0 ] ; then
    echo LOGIN FAILED
    exit 124
  fi
fi


export BUILDER_NAME=mbuilder
# --progress=plain --no-cache
# export BUILDKIT_PROGRESS=plain
# export DOCKER_CLI_EXPERIMENTAL=enabled
# apt -y install qemu-user-binfmt qemu-user binfmt-support

docker buildx inspect ${BUILDER_NAME} --bootstrap >/dev/null 2>&1
builder_found=$?

if [ $dockerispodman -eq 0 ] ; then
  if [ $builder_found -ne 0 ] ; then
    #BUILDER=$(docker ps | grep ${BUILDER_NAME} | cut -f1 -d' ')
    docker run --privileged --rm tonistiigi/binfmt --install all
    docker buildx create --name $BUILDER_NAME
    docker buildx use ${BUILDER_NAME}
  fi
fi

docker_base_args=("build" "-f" "${dockerfile}" "--build-arg" "buildtime=\"${buildtime}\"")
docker_tag_args=("-t" "${DOCKER_IMAGE}")

if ! [ "${DOCKER_IMAGE}" = *latest ] ; then
  echo "DOCKER_IMAGE ${DOCKER_IMAGE} not tagged :latest -> adding second tag with :latest"
  DOCKER_IMAGE_2=${DOCKER_IMAGE%\:*}\:latest
  docker_tag_args+=("-t" "${DOCKER_IMAGE_2}")
fi

if [ $# -eq 1 ] ; then
        if [ "$1" == "onlylocal" ] ; then
          export BUILDKIT_PROGRESS=plain  # plain|tty|auto
          docker "${docker_base_args[@]}" "${docker_tag_args[@]}" .
          exit $?
        fi
fi

PLATFORMS=("linux/arm64" "linux/amd64")
podmanvmstarted=0

if [ $dockerispodman -eq 1 ]; then
  # Podman multi-arch build using manifest workflow
  # Remove existing manifest if it exists
  docker manifest rm "${DOCKER_IMAGE}" 2>/dev/null || true

  # Build for each platform in parallel (separate images)
  platform_tags=()
  declare -A platform_connect_args  # Associative array: platform -> connect_arg
  for platform in "${PLATFORMS[@]}"; do
    arch="${platform#*/}"  # Extract arch from "linux/amd64" -> "amd64"

    podmaninvm=0
    connect_arg=""
    podman run --platform ${platform} alpine uname -m >/dev/null 2>&1
    if [ $? -ne 0 ] ; then
      echo "podman cannot execute successfully on ${platform} - using podman machine (vm) for that"
      podmaninvm=1
      if ! podman machine list | grep -q "podman-machine-default" ; then
        podman machine init --disk-size 100
        podman machine start
        if [ $? -ne 0 ] ; then
          echo "could not start podman machine (VM)"
          exit 123
        fi
        # export DOCKER_HOST='unix:///run/user/1000/podman/podman-machine-default-api.sock'
        podmanvmstarted=1
      else
        # Machine exists, check if running
        if ! podman machine list --format "{{.Running}}" | grep -q "true"; then
          podman machine start
          if [ $? -ne 0 ] ; then
            echo "could not start podman machine (VM)"
            exit 123
          fi
          # export DOCKER_HOST='unix:///run/user/1000/podman/podman-machine-default-api.sock'
          podmanvmstarted=1
        fi
      fi
      connect_arg="--connection podman-machine-default"
    fi
    platform_connect_args["${platform}"]="${connect_arg}"

    platform_tag="${DOCKER_IMAGE}-${arch}"
    platform_tags+=("${platform_tag}")
    echo "Building for ${platform} -> ${platform_tag}..."
    echo podman ${connect_arg} buildx "${docker_base_args[@]}" --platform "${platform}" -t "${platform_tag}" .
    podman ${connect_arg} buildx "${docker_base_args[@]}" --platform "${platform}" -t "${platform_tag}" .
    if [ $? -ne 0 ] ; then
      echo FAIL $?
      exit $?
    fi
  done

  # Wait for all builds to complete
  num_jobs=$(jobs -p | wc -l)
  if (( $num_jobs > 0 )) ; then
    echo "Warte auf ${num_jobs} parallele Builds..."
    wait
  fi

  # Copy images built in VM to host
  for platform in "${!platform_connect_args[@]}"; do
    if [[ -n "${platform_connect_args[$platform]}" ]]; then
      arch="${platform#*/}"
      platform_tag="${DOCKER_IMAGE}-${arch}"
      echo "Copying image from VM to host: ${platform_tag}"
      echo podman image scp podman-machine-default::${platform_tag}
      podman image scp podman-machine-default::${platform_tag}
    fi
  done

  # Create manifest and add all platform images
  echo podman manifest create "${DOCKER_IMAGE}" "${platform_tags[@]}"
  podman manifest create "${DOCKER_IMAGE}" "${platform_tags[@]}"

  # Tag with second name if set
  if [ -n "${DOCKER_IMAGE_2:-}" ]; then
    echo podman tag "${DOCKER_IMAGE}" "${DOCKER_IMAGE_2}"
    podman tag "${DOCKER_IMAGE}" "${DOCKER_IMAGE_2}"
  fi

  echo WOULD DO: podman manifest push "${DOCKER_IMAGE}" docker://"${DOCKER_IMAGE}"
else
  # Docker buildx multi-arch build

  printf -v PLATFORMS_CSV '%s,' "${PLATFORMS[@]}"
  PLATFORMS_CSV="${PLATFORMS_CSV%,}"  # Remove trailing comma

  docker buildx "${docker_base_args[@]}" "${docker_tag_args[@]}" --platform "${PLATFORMS_CSV}" --push .
fi

if [ $podmanvmstarted -eq 1 ] ; then
  podman machine stop
fi
