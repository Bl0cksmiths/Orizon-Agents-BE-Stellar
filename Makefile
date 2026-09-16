# Local dev loop — mirrors what CI runs. `make check` before pushing.
PY := .venv/bin/python

.PHONY: lint format type test cov drift check

lint:
	.venv/bin/ruff check .

format:
	.venv/bin/ruff format .

type:
	.venv/bin/mypy

test:
	$(PY) -m pytest -q

cov:
	$(PY) -m pytest -q --cov

# Cross-checks .env.example and render.yaml against the contracts repo's
# address book. Needs that repo cloned; the failure message says where it
# looked and how to point it elsewhere. Part of `check` because this file's
# job is to mirror CI — and a dev who cannot run this is the dev about to push
# a contract id nothing verified.
drift:
	$(PY) scripts/check_contract_drift.py

check: lint type cov drift
	.venv/bin/ruff format --check .
