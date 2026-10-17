"""conftest's `hermetic_settings` gives back what the environment set.

The fixture blanks the live ids `.env` or the shell may provide and restores
them after each test. It used to restore them by hand, after a yield, and
whenever monkeypatch had been set up before it (another autouse fixture asking
for monkeypatch is enough) that ran before monkeypatch's own undo. A test that
patched one of these settings then left behind the fixture's blank, and every
test after it saw '' where the environment had set a value.

Only a real session shows that, with the variable set before the settings are
read, so this runs one in a subprocess against the suite's own conftest.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_ROOT = _TESTS.parent

_PATCHING_TEST = """\
from app.config import settings


def test_patches_the_registry(monkeypatch):
    assert settings.stellar_agent_registry == ""
    monkeypatch.setattr(settings, "stellar_agent_registry", "CTEST")
"""

_RUN = """\
import pytest

code = pytest.main(["-q", "-p", "conftest", "-p", "no:cacheprovider", "test_patching.py"])
from app.config import settings

print(int(code), settings.stellar_agent_registry)
"""


def test_a_patched_setting_comes_back_as_the_environment_set_it(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_patching.py").write_text(_PATCHING_TEST)
    # The inner session collects no Postgres tests on purpose, so CI's
    # "every real-Postgres test must run" gate would fail it; leave it out.
    outer_env = {k: v for k, v in os.environ.items() if not k.startswith("ORIZON_TEST_PG_")}
    out = subprocess.run(
        [sys.executable, "-c", _RUN],
        cwd=tmp_path,
        env={
            **outer_env,
            "PYTHONPATH": os.pathsep.join([str(_ROOT), str(_TESTS)]),
            "OPENAI_API_KEY": "sk-test",
            "STELLAR_AGENT_REGISTRY": "CREG",
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert out.stdout.strip().splitlines()[-1] == "0 CREG", out.stdout
