DOCKER_USERNAME="arley@somewhere.com"
DOCKER_PASSWORD="someweirdpassword"

DOCKER_TOKENUSER="dockerhubtokenuser"
DOCKER_TOKEN="dockerhubtokenforthatuser"

KUBECTL_CONTEXT="arley@local"

# echo \$0 in include.sh: $0

declare -a include_local_sh
include_local_sh[0]="include.local.sh"
include_local_sh[1]="scripts/include.local.sh"
include_local_sh[2]="$(dirname "$0")/scripts/include.local.sh"
include_local_sh[3]="$(dirname "$0")/../scripts/include.local.sh"
found=false

for path in "${include_local_sh[@]}"; do
  if [ -e "${path}" ]; then
    echo "${path} will be read..."
    source "${path}"
    found=true
    break
  fi
done

if [ "$found" = false ]; then
  echo "No include.local.sh file[s] found."
fi

lala="2d163634cb8957acf42544eb3fabbce7dfe44a8bce6bf605333714b9524040fc"