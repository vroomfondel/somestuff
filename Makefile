.PHONY: tests help install venv lint dstart isort tcheck build build-nfs update-all-dockerhub-readmes commit-checks prepare flickrstuffpipe %
SHELL := /usr/bin/bash
.ONESHELL:


help:
	@printf "\ninstall\n\tinstall requirements\n"
	@printf "\nisort\n\tmake isort import corrections\n"
	@printf "\nlint\n\tmake linter check with black\n"
	@printf "\ntcheck\n\tmake static type checks with mypy\n"
	@printf "\ntests\n\tLaunch tests\n"
	@printf "\nprepare\n\tLaunch tests and commit-checks\n"
	@printf "\ncommit-checks\n\trun pre-commit checks on all files\n"
	# @printf "\nstart \n\tstart app in gunicorn - listening on port 8055\n"
	@printf "\nbuild \n\tbuild docker image\n"
	@printf "\nbuild-nfs \n\tbuild nfs-subdir-external-provisioner (applies overlay)\n"
	@printf "\nupdate-all-dockerhub-readmes \n\tupdate ALL Docker Hub repo descriptions from DOCKERHUB_OVERVIEW.md resp */DOCKERHUB_OVERVIEW.md\n"
	@printf "\ndstart \n\tlaunch \"app\" in docker\n"
	@printf "\nflickrstuffpipe \n\textract and run flickr-docker.sh from container (no git clone needed)\n"



# check for "CI" not in os.environ || "GITHUB_RUN_ID" not in os.environ
venv_activated=if [ -z $${VIRTUAL_ENV+x} ] && [ -z $${GITHUB_RUN_ID+x} ] ; then printf "activating venv...\n" ; source .venv/bin/activate ; else printf "venv already activated or GITHUB_RUN_ID=$${GITHUB_RUN_ID} is set\n"; fi

install: venv

venv: .venv/touchfile

.venv/touchfile: requirements.txt requirements-dev.txt
	@if [ -z "$${GITHUB_RUN_ID}" ]; then \
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
	black -l 120 --extend-exclude nfs-subdir-external-provisioner .

dstart:
	# map config.local.yaml, gcal credentials, kubeconfig, ssh keys, and flickr config/data into container
	# detect podman: add userns mapping so bind-mounted host files are owned by pythonuser (UID 1200)
	if docker --version 2>&1 | grep -qi podman; then \
		USERNS_FLAG="--userns=keep-id:uid=1200,gid=1201"; \
	else \
		USERNS_FLAG=""; \
	fi
	# only mount flickr directories when they exist on the host
	FLICKR_FLAGS=""
	if [ -d flickrdownloaderstuff/flickr-config ]; then \
		FLICKR_FLAGS="$$FLICKR_FLAGS -v $$(pwd)/flickrdownloaderstuff/flickr-config:/home/pythonuser/.flickr-config:ro -e FLICKR_HOME=/home/pythonuser/.flickr-config"; \
	fi
	if [ -d flickrdownloaderstuff/flickr-backup ]; then \
		FLICKR_FLAGS="$$FLICKR_FLAGS -v $$(pwd)/flickrdownloaderstuff/flickr-backup:/home/pythonuser/flickr-backup"; \
	fi
	if [ -d flickrdownloaderstuff/flickr-cache ]; then \
		FLICKR_FLAGS="$$FLICKR_FLAGS -v $$(pwd)/flickrdownloaderstuff/flickr-cache:/home/pythonuser/flickr-cache"; \
	fi
	docker run --network=host -it --rm --name somestuffephemeral \
		$$USERNS_FLAG \
		-v $$(pwd)/config.local.yaml:/app/config.local.yaml \
		-v ~/.config/gcal:/home/pythonuser/.config/gcal \
		-v ~/.kube:/home/pythonuser/.kube \
		-v ~/.ssh:/home/pythonuser/.ssh:ro \
		$$FLICKR_FLAGS \
		xomoxcc/somestuff:latest /bin/bash

flickrstuffpipe:
	docker run --rm xomoxcc/somestuff:latest cat flickrdownloaderstuff/flickr-docker.sh | /bin/bash -s -- $(filter-out $@,$(MAKECMDGOALS))

isort: venv
	@$(venv_activated)
	isort .

tcheck: venv
	@$(venv_activated)
	mypy *.py ecowittstuff/*.py llmstuff/*.py dnsstuff/*.py netatmostuff/*.py hydromailstuff/*.py k3shelperstuff/*.py gcalstuff/*.py

build: venv
	git submodule update --remote
	# git submodule update --init --recursive
	./build.sh

build-nfs:
	git submodule update --init nfs-subdir-external-provisioner
	cp overlays/nfs-subdir-external-provisioner/* nfs-subdir-external-provisioner/
	cd nfs-subdir-external-provisioner && make && ./build.sh
	# cd nfs-subdir-external-provisioner && make clean && make && ./build.sh

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
	  "tangstuff:xomoxcc/tang"; do \
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

# Catch-all target to allow arguments after flickrstuffpipe
%:
	@:

#pypibuild: .venv
#	@$(venv_activated)
#	pip install -r requirements-build.txt
#	pip install --upgrade twine build
#	python3 -m build

# AUTH=$(jq -r '.auths["https://index.docker.io/v1/"].auth' ~/.docker/config.json | base64 -d) && USERNAME=$(echo "$AUTH" | cut -d: -f1) && PASSWORD=$(echo "$AUTH" | cut -d: -f2-)
# TOKEN=$(curl -s -X POST https://hub.docker.com/v2/users/login/ -H "Content-Type: application/json" -d '{"username":"'"$USERNAME"'","password":"'"$PASSWORD"'"}' | jq -r .token)

