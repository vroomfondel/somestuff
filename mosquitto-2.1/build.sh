#!/bin/bash

medir=$(dirname "$0")
medir=$(realpath "${medir}")
cd "${medir}" || exit 123

DOCKER_IMAGE=xomoxcc/mosquitto:2.1
dockerfile=Dockerfile

buildtime=$(date +'%Y-%m-%d %H:%M:%S %Z')

dockerfile=Dockerfile

source ../scripts/include.sh

export DOCKER_CONFIG=$(realpath $(pwd)/../docker-config)

if ! [ -e "${DOCKER_CONFIG}/config.json" ] ; then
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

if [ $builder_found -ne 0 ] ; then
  #BUILDER=$(docker ps | grep ${BUILDER_NAME} | cut -f1 -d' ')
  docker run --privileged --rm tonistiigi/binfmt --install all
  docker buildx create --name $BUILDER_NAME
  docker buildx use ${BUILDER_NAME}
fi

docker_base_args=("build" "-f" "${dockerfile}" "--build-arg" "buildtime=\"${buildtime}\"" "-t" "${DOCKER_IMAGE}")

if ! [ "${DOCKER_IMAGE}" = *latest ] ; then
  echo "DOCKER_IMAGE ${DOCKER_IMAGE} not tagged :latest -> adding second tag with :latest"
  DOCKER_IMAGE_2=${DOCKER_IMAGE%\:*}\:latest
  docker_base_args+=("-t" "${DOCKER_IMAGE_2}")
fi

if [ $# -eq 1 ] ; then
        if [ "$1" == "onlylocal" ] ; then
          export BUILDKIT_PROGRESS=plain  # plain|tty|auto
                docker "${docker_base_args[@]}" .
                exit $?
        fi
fi



# docker "${docker_base_args[@]}" . > docker_build_local.log 2>&1 &

docker buildx "${docker_base_args[@]}" --platform linux/amd64,linux/arm64 --push .
# docker buildx "${docker_base_args[@]}" --platform linux/amd64 --push .

