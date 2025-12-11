#!/bin/bash

cd "$(dirname "$0")" || exit 123

echo "$(basename "$0")"::PWD: "$(pwd)"

for penv in /python_venv "$(pwd)/venv" "$(pwd)/.venv" ; do
  if [ -e "${penv}" ] ; then
    . "${penv}/bin/activate"
    echo ACTIVATED VENV: "${penv}"
    break
  fi
done

PYTHONPATH=$PYTHONPATH:$(pwd)
export PYTHONPATH

exec python3 "$@"

