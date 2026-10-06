"""agno's loggers stay under the root handler however late agno is imported.

Each case runs in a fresh interpreter: in this one agno is long imported, and
what is pinned is exactly what happens on its FIRST import.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_PROBE = """
import json, logging, sys
{setup}
import agno.agent  # the first import of agno.utils.log, after the hand-back was installed
state = {{}}
for name in ("agno", "agno-team", "agno-workflow"):
    lg = logging.getLogger(name)
    state[name] = [len(lg.handlers), lg.propagate, lg.level]
print(json.dumps(state))
"""


def _probe(setup: str) -> dict[str, list[object]]:
    out = subprocess.run(
        [sys.executable, "-c", _PROBE.format(setup=setup)],
        cwd=ROOT,
        env={**os.environ, "OPENAI_API_KEY": "sk-test"},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_late_agno_import_is_handed_back_to_the_root_handler() -> None:
    state = _probe("from app.agno_logging import install; install()")
    assert state == {name: [0, True, 30] for name in ("agno", "agno-team", "agno-workflow")}


def test_without_the_hook_agno_takes_its_loggers_away() -> None:
    """What the hook prevents: agno's own console handler, propagation off."""
    state = _probe("pass")
    assert all(handlers == 1 and propagate is False for handlers, propagate, _level in state.values())


def test_install_is_idempotent_and_hands_back_an_already_imported_agno() -> None:
    from app import agno_logging

    before = list(sys.meta_path)
    agno_logging.install()
    agno_logging.install()
    hooks = [f for f in sys.meta_path if isinstance(f, agno_logging._HandBackAfterImport)]
    assert len(hooks) == 1
    assert [f for f in sys.meta_path if f not in before] == []
