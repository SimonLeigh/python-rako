.DEFAULT_GOAL := help
export VENV := $(abspath venv)
export PATH := ${VENV}/bin:${PATH}

define BROWSER_PYSCRIPT
import os, webbrowser, sys

try:
	from urllib import pathname2url
except:
	from urllib.request import pathname2url

webbrowser.open("file://" + pathname2url(os.path.abspath(sys.argv[1])))
endef
export BROWSER_PYSCRIPT

BROWSER := python -c "$$BROWSER_PYSCRIPT"

.PHONY: help
help: ## Shows this message.
	@echo "Asynchronous Python client for Rako Controls."; \
	echo; \
	echo "Usage:"; \
	awk -F ':|##' '/^[^\t].+?:.*?##/ {\
		printf "\033[36m  make %-30s\033[0m %s\n", $$1, $$NF \
	}' $(MAKEFILE_LIST)

.PHONY: dev
dev: install-dev install ## Set up a development environment.

.PHONY: lint
lint: ## Run Ruff linting and formatting checks.
	python -m ruff check
	python -m ruff format --check

.PHONY: format
format: ## Format code with Ruff.
	python -m ruff format
	python -m ruff check --fix

.PHONY: typecheck
typecheck: ## Run type checking with MyPy.
	python -m mypy python_rako/

.PHONY: check
check: lint typecheck ## Run all checks (linting, formatting, and type checking).

.PHONY: test
test: ## Run tests quickly with the default Python.
	pytest --cov-report html --cov-report term --cov-report xml:cov.xml --cov=python_rako .;

.PHONY: coverage
coverage: test ## Check code coverage quickly with the default Python.
	$(BROWSER) htmlcov/index.html

.PHONY: install
install: clean ## Install the package to the active Python's site-packages.
	pip install -Ur requirements.txt; \
	pip install -e .;

.PHONY: clean clean-all
clean: clean-build clean-pyc clean-test ## Removes build, test, coverage and Python artifacts.
clean-all: clean-build clean-pyc clean-test clean-venv ## Removes all venv, build, test, coverage and Python artifacts.

.PHONY: clean-build
clean-build: ## Removes build artifacts.
	rm -fr build/; \
	rm -fr dist/; \
	rm -fr .eggs/; \
	find . -name '*.egg-info' -exec rm -fr {} +; \
	find . -name '*.egg' -exec rm -fr {} +;

.PHONY: clean-pyc
clean-pyc: ## Removes Python file artifacts.
	find . -name '*.pyc' -delete; \
	find . -name '*.pyo' -delete; \
	find . -name '*~' -delete; \
	find . -name '__pycache__' -exec rm -fr {} +;

.PHONY: clean-test
clean-test: ## Removes test and coverage artifacts.
	rm -f .coverage; \
	rm -fr htmlcov/; \
	rm -fr .pytest_cache;

.PHONY: clean-venv
clean-venv: ## Removes Python virtual environment artifacts.
	rm -fr venv/;

.PHONY: dist
dist: clean ## Builds source and wheel package.
	python -m build; \
	ls -l dist;

.PHONY: release
release:  ## Release build on PyP
	twine upload dist/*

.PHONY: venv
venv: clean-venv ## Create Python venv environment.
	python3 -m venv venv;

.PHONY: install-dev
install-dev: clean
	pip install -Ur requirements_dev.txt; \
	pre-commit install;

.PHONY: bump-patch
bump-patch: ## Bump patch version (x.y.Z) and commit
	@python -c "import re; content = open('python_rako/__version__.py').read().strip(); match = re.search(r'(\d+)\.(\d+)\.(\d+)', content); major, minor, patch = match.groups(); new_version = f'{major}.{minor}.{int(patch) + 1}'; open('python_rako/__version__.py', 'w').write(f'__version__ = \"{new_version}\"\n'); print(f'Bumped version to {new_version}')"
	@git add python_rako/__version__.py
	@git commit -m "Bump version to $$(python -c "import re; content = open('python_rako/__version__.py').read(); match = re.search(r'\"([^\"]+)\"', content); print(match.group(1))")"

.PHONY: bump-minor
bump-minor: ## Bump minor version (x.Y.z) and commit
	@python -c "import re; content = open('python_rako/__version__.py').read().strip(); match = re.search(r'(\d+)\.(\d+)\.(\d+)', content); major, minor, patch = match.groups(); new_version = f'{major}.{int(minor) + 1}.0'; open('python_rako/__version__.py', 'w').write(f'__version__ = \"{new_version}\"\n'); print(f'Bumped version to {new_version}')"
	@git add python_rako/__version__.py
	@git commit -m "Bump version to $$(python -c "import re; content = open('python_rako/__version__.py').read(); match = re.search(r'\"([^\"]+)\"', content); print(match.group(1))")"

.PHONY: bump-major
bump-major: ## Bump major version (X.y.z) and commit
	@python -c "import re; content = open('python_rako/__version__.py').read().strip(); match = re.search(r'(\d+)\.(\d+)\.(\d+)', content); major, minor, patch = match.groups(); new_version = f'{int(major) + 1}.0.0'; open('python_rako/__version__.py', 'w').write(f'__version__ = \"{new_version}\"\n'); print(f'Bumped version to {new_version}')"
	@git add python_rako/__version__.py
	@git commit -m "Bump version to $$(python -c "import re; content = open('python_rako/__version__.py').read(); match = re.search(r'\"([^\"]+)\"', content); print(match.group(1))")"
