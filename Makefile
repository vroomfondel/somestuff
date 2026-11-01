PUBLISHABLE := dnsstuff ucmstuff oepnvstuff mqttwebstuff uptimekumastuff k3shelperstuff dhcpstuff broadlinkstuff
PYPI_LOCAL  := scripts/include.local.sh
.PHONY: tests help install venv lint dstart isort tcheck gitleaks build build-nfs update-all-dockerhub-readmes check-dockerhub-token commit-checks prepare dist-all clean-dist %
# Pattern-built stage-/dist-/distcheck-<pkg> targets are not files; keep make from printing `rm` for them.
.SECONDARY:
# `make pypipush-<pkg>` uploads only to upload.pypi.org or test.pypi.org; URL + token come from
# scripts/include.local.sh (gitignored): PYPI_REPO_URL, PYPI_USER (__token__), PYPI_PASSWORD. distcheck gates every push.
SHELL := /usr/bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:


help:
	@printf "\ninstall\n\tinstall requirements\n"
	@printf "\nisort\n\tmake isort import corrections\n"
	@printf "\nlint\n\tmake linter check with black\n"
	@printf "\ntcheck\n\tmake static type checks with mypy\n"
	@printf "\ngitleaks\n\tscan for hardcoded secrets with gitleaks\n"
	@printf "\ntests\n\tLaunch tests\n"
	@printf "\nprepare\n\tLaunch tests and commit-checks\n"
	@printf "\ncommit-checks\n\trun pre-commit checks on all files\n"
	# @printf "\nstart \n\tstart app in gunicorn - listening on port 8055\n"
	@printf "\nbuild \n\tbuild docker image\n"
	@printf "\nbuild-nfs \n\tbuild nfs-subdir-external-provisioner (applies overlay)\n"
	@printf "\nupdate-all-dockerhub-readmes \n\tupdate ALL Docker Hub repo descriptions from DOCKERHUB_OVERVIEW.md resp */DOCKERHUB_OVERVIEW.md\n"
	@printf "\ncheck-dockerhub-token\n\tcheck Docker Hub token permissions (credentials from include.sh)\n"
	@printf "\ndstart \n\tlaunch \"app\" in docker\n"
	@printf "\ndistcheck-<pkg>\n\tstage + hatch build somestuff-<pkg> into build/<pkg>/ and verify the artifacts (pypibuild-<pkg> is an alias)\n"
	@printf "\npypipush-<pkg>\n\tupload build/<pkg>/dist/* to PYPI_REPO_URL from scripts/include.local.sh (pypi.org / test.pypi.org only)\n"
	@printf "\ndist-all\n\tdistcheck for every publishable package: $(PUBLISHABLE)\n"
	@printf "\nclean-dist\n\tremove build/\n"



# check for "CI" not in os.environ || "GITHUB_RUN_ID" not in os.environ
venv_activated=if [ -z $${VIRTUAL_ENV+x} ] && [ -z $${GITHUB_RUN_ID+x} ] ; then printf "activating venv...\n" ; source .venv/bin/activate ; else printf "venv already activated or GITHUB_RUN_ID=$${GITHUB_RUN_ID:-} is set\n"; fi

install: venv

venv: .venv/touchfile

.venv/touchfile: requirements.txt requirements-dev.txt
	@if [ -z "$${GITHUB_RUN_ID:-}" ]; then \
		test -d .venv || python3.14 -m venv .venv; \
		source .venv/bin/activate; \
		pip install -r requirements-dev.txt; \
		touch .venv/touchfile; \
	else \
  		echo "Skipping venv setup because GITHUB_RUN_ID is set"; \
  	fi


tests: venv
	@$(venv_activated)
	pytest .

lint: venv
	@$(venv_activated)
	black --extend-exclude nfs-subdir-external-provisioner $(ARGS) .

dstart:
	# map config.local.yaml, gcal credentials, kubeconfig, and ssh keys into container
	# detect podman: add userns mapping so bind-mounted host files are owned by pythonuser (UID 1200)
	if docker --version 2>&1 | grep -qi podman; then \
		USERNS_FLAG="--userns=keep-id:uid=1200,gid=1201"; \
	else \
		USERNS_FLAG=""; \
	fi
	docker run --network=host -it --rm --name somestuffephemeral \
		$$USERNS_FLAG \
		-v $$(pwd)/config.local.yaml:/app/config.local.yaml \
		-v ~/.config/gcal:/home/pythonuser/.config/gcal \
		-v ~/.kube:/home/pythonuser/.kube \
		-v ~/.ssh:/home/pythonuser/.ssh:ro \
		xomoxcc/somestuff:latest /bin/bash

isort: venv
	@$(venv_activated)
	isort .

tcheck: venv
	@$(venv_activated)
	mypy .

gitleaks: venv .git/hooks/pre-commit
	@$(venv_activated)
	pre-commit run gitleaks --all-files
	# gitleaks dir . -v

build: venv
	git submodule update --remote
	# git submodule update --init --recursive
	./build.sh

build-nfs:
	git submodule update --init nfs-subdir-external-provisioner
	cp overlays/nfs-subdir-external-provisioner/* nfs-subdir-external-provisioner/
	cd nfs-subdir-external-provisioner && make && ./build.sh
	# cd nfs-subdir-external-provisioner && make clean && make && ./build.sh

check-dockerhub-token:
	source scripts/include.sh
	NS_ARGS=""
	for ns in "$${DOCKERHUB_NAMESPACES[@]}"; do NS_ARGS+=" -n $$ns"; done
	python3 scripts/check_dockerhub_token.py "$${DOCKER_TOKENUSER}" "$${DOCKER_TOKEN}" $$NS_ARGS

update-all-dockerhub-readmes:
	@AUTH=$$(jq -r '.auths["https://index.docker.io/v1/"].auth' docker-config/config.json | base64 -d) && \
	USERNAME=$$(echo "$$AUTH" | cut -d: -f1) && \
	PASSWORD=$$(echo "$$AUTH" | cut -d: -f2-) && \
	TOKEN=$$(curl -s -X POST https://hub.docker.com/v2/users/login/ \
	  -H "Content-Type: application/json" \
	  -d '{"username":"'"$$USERNAME"'","password":"'"$$PASSWORD"'"}' \
	  | jq -r .token) && \
	for mapping in \
	  ".:xomoxcc/somestuff" \
	  "python314jit:xomoxcc/python314-jit" \
	  "python314pandasmultiarch:xomoxcc/pythonpandasmultiarch" \
	  "mosquitto-2.1:xomoxcc/mosquitto" \
	  "tangstuff:xomoxcc/tang" \
	  "f2bapistuff:xomoxcc/f2b-api" \
	  "postfixpgsqlstuff:xomoxcc/postfix-pgsql"; do \
	  DIR=$$(echo "$$mapping" | cut -d: -f1) && \
	  REPO=$$(echo "$$mapping" | cut -d: -f2) && \
	  FILE="$$DIR/DOCKERHUB_OVERVIEW.md" && \
	  if [ -f "$$FILE" ]; then \
	    echo "Updating $$REPO from $$FILE..." && \
	    curl -s -X PATCH "https://hub.docker.com/v2/repositories/$$REPO/" \
	      -H "Authorization: Bearer $$TOKEN" \
	      -H "Content-Type: application/json" \
	      -d "{\"full_description\": $$(jq -Rs . "$$FILE")}" \
	      | jq -r '.full_description | length | "  Updated: \(.) chars"'; \
	  else \
	    echo "Skipping $$REPO - $$FILE not found"; \
	  fi; \
	done

.git/hooks/pre-commit: venv
	@$(venv_activated)
	pre-commit install

commit-checks: .git/hooks/pre-commit
	@$(venv_activated)
	pre-commit run --all-files

prepare: tests commit-checks

stage-%: venv
	rm -rf build/$*
	mkdir -p build/$*/somestuff
	cp -r somestuff/$* build/$*/somestuff/
	find build/$*/somestuff -name '__pycache__' -type d -prune -exec rm -rf {} +
	cp packaging/$*.toml build/$*/pyproject.toml
	cp somestuff/$*/README.md build/$*/README.md
	cp LICENSE.md build/$*/LICENSE.md
	test ! -e build/$*/somestuff/__init__.py

dist-%: stage-%
	@$(venv_activated)
	ver=$$(sed -n 's/^__version__ = "\(.*\)"$$/\1/p' somestuff/$*/__init__.py)
	[ -n "$$ver" ] || { printf "refusing: no __version__ in somestuff/$*/__init__.py\n" >&2; exit 2; }
	printf "somestuff-$*  VERSION: %s\n" "$$ver"
	cd build/$* && hatch build --clean

distcheck-%: dist-%
	@$(venv_activated)
	python scripts/check_dist_secrets.py --root build/$* --namespace somestuff

pypibuild-%: distcheck-%

pypipush-%: distcheck-%
	@$(venv_activated)
	[ -f "$(PYPI_LOCAL)" ] || { printf "refusing: %s missing (PYPI_REPO_URL, PYPI_USER, PYPI_PASSWORD)\n" "$(PYPI_LOCAL)" >&2; exit 2; }
	source scripts/include.sh > /dev/null
	case "$${PYPI_REPO_URL:-}" in
		https://upload.pypi.org/legacy/|https://test.pypi.org/legacy/) ;;
		*) printf "refusing: PYPI_REPO_URL='%s' is not pypi.org or test.pypi.org\n" "$${PYPI_REPO_URL:-}" >&2; exit 2;;
	esac
	[ "$${PYPI_USER:-}" = "__token__" ] && [ -n "$${PYPI_PASSWORD:-}" ] && [ "$$PYPI_PASSWORD" != dummy ] || { printf "refusing: PYPI_USER must be __token__ and PYPI_PASSWORD set in %s\n" "$(PYPI_LOCAL)" >&2; exit 2; }
	ver=$$(sed -n 's/^__version__ = "\(.*\)"$$/\1/p' somestuff/$*/__init__.py)
	printf "publishing somestuff-$* %s to %s\n" "$$ver" "$$PYPI_REPO_URL"
	@hatch publish -n -r "$$PYPI_REPO_URL" -u "$$PYPI_USER" -a "$$PYPI_PASSWORD" build/$*/dist/*
	idx=$$( [ "$$PYPI_REPO_URL" = https://test.pypi.org/legacy/ ] && echo https://test.pypi.org || echo https://pypi.org )
	curl -sf "$$idx/simple/somestuff-$*/" | grep -o -E "somestuff_$*-$$ver[^\"<#]*" | sort -u

dist-all: $(addprefix distcheck-,$(PUBLISHABLE))

clean-dist:
	rm -rf build

# Catch-all target to allow arguments after flickrstuffpipe
%:
	@:

# AUTH=$(jq -r '.auths["https://index.docker.io/v1/"].auth' ~/.docker/config.json | base64 -d) && USERNAME=$(echo "$AUTH" | cut -d: -f1) && PASSWORD=$(echo "$AUTH" | cut -d: -f2-)
# TOKEN=$(curl -s -X POST https://hub.docker.com/v2/users/login/ -H "Content-Type: application/json" -d '{"username":"'"$USERNAME"'","password":"'"$PASSWORD"'"}' | jq -r .token)

