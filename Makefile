.PHONY: tests help install venv lint dstart isort tcheck build build-nfs commit-checks prepare
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
	@printf "\ndstart \n\tlaunch \"app\" in docker\n"



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
	black -l 120 .

dstart:
	# map config.local.yaml from current workdirectory into container
	docker run --network=host -it --rm --name somestuffephemeral -v $(pwd)/config.local.yaml:/app/config.local.yaml xomoxcc/somestuff:latest /bin/bash

isort: venv
	@$(venv_activated)
	isort .

tcheck: venv
	@$(venv_activated)
	mypy *.py ecowittstuff/*py llmstuff/*.py dnsstuff/*.py netatmostuff/*.py hydromailstuff/*.py
	# mypy -p ecowittstuff -p llmstuff -p dnsstuff -p netatmostuff -p hydromailstuff -p mqttstuff -m config -m Helper

build: venv
	git submodule update --remote
	# git submodule update --init --recursive
	./build.sh

build-nfs:
	git submodule update --init --remote nfs-subdir-external-provisioner
	cp overlays/nfs-subdir-external-provisioner/* nfs-subdir-external-provisioner/
	cd nfs-subdir-external-provisioner && make && ./build.sh
	# cd nfs-subdir-external-provisioner && make clean && make && ./build.sh

.git/hooks/pre-commit: venv
	@$(venv_activated)
	pre-commit install

commit-checks: .git/hooks/pre-commit
	@$(venv_activated)
	pre-commit run --all-files

prepare: tests commit-checks

#pypibuild: .venv
#	@$(venv_activated)
#	pip install -r requirements-build.txt
#	pip install --upgrade twine build
#	python3 -m build

