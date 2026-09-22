MDFORMAT_EXCLUDES := --exclude '.superpowers/**' --exclude 'docs/internal/**' --exclude 'docs/superpowers/**'

.PHONY: all check format format-check install reinstall test type whitespace

install:
	uv tool install .

reinstall:
	uv tool install --reinstall .

format:
	ruff format .
	ruff check --fix .
	mdformat . $(MDFORMAT_EXCLUDES)
	pyproject-fmt pyproject.toml

format-check:
	ruff format --check .
	ruff check .
	mdformat --check . $(MDFORMAT_EXCLUDES)
	pyproject-fmt --check pyproject.toml

type:
	pyright

test:
	pytest -q

whitespace:
	git diff --check HEAD

check: format-check type test whitespace

all: check
