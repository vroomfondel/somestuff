DOCKER_USERNAME="arley@somewhere.com"
DOCKER_PASSWORD="someweirdpassword"

DOCKER_TOKENUSER="dockerhubtokenuser"
DOCKER_TOKEN="dockerhubtokenforthatuser"

# OK, public
DOCKER_IMAGE="xomoxcc/somestuff:latest"

KUBECTL_CONTEXT="arley@local"

# echo \$0: $0

include_local_sh="$(dirname "$0")/include.local.sh"
include_local_sh2="$(dirname "$0")/scripts/include.local.sh"

if [ -e "${include_local_sh}" ] ; then
  echo "${include_local_sh}" to be read...
  source "${include_local_sh}"
else
  # echo "${include_local_sh}" does not exist...
  if [ -e "${include_local_sh2}" ] ; then
    echo "${include_local_sh2}" to be read...
    source "${include_local_sh2}"
  else
    echo NEITHER "${include_local_sh}" NOR "${include_local_sh2}" exist...
  fi
fi