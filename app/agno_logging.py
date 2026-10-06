"""Keep agno's loggers under the app's JSON, request-id and redaction handler.

agno gives its loggers a Rich console handler of its own and switches their
propagation off, so its lines bypass the root handler: no JSON, no request id,
and no redaction — while it logs a provider's error text verbatim at ERROR, the
one line most likely to quote a key. Handing them back to the root handler puts
them under all three. Held at WARNING: agno's INFO chatter was only ever console
decoration, and its warnings and errors are what matters.

agno does this when `agno.utils.log` is IMPORTED, and it is imported lazily now
(app/agents/model_factory.py builds every agno Agent on first use, so ~0.5 s of
agno/openai imports leave the boot path). A hand-back done once at startup
would therefore be undone by the first plan. So `install` hands the loggers
back now AND after `agno.utils.log` executes, whenever and by whoever it is
first imported — a meta-path finder that wraps that one module's loader. A
module already imported is handed back at once.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import logging
import sys
from collections.abc import Sequence
from types import ModuleType
from typing import Any

# agno.utils.log's own names, restated so naming them imports nothing.
AGNO_LOGGER_NAMES = ("agno", "agno-team", "agno-workflow")
_AGNO_LOG_MODULE = "agno.utils.log"


def hand_back() -> None:
    """Route agno's loggers through the root handler, at WARNING."""
    for name in AGNO_LOGGER_NAMES:
        agno_logger = logging.getLogger(name)
        agno_logger.handlers.clear()
        agno_logger.propagate = True
        agno_logger.setLevel(logging.WARNING)


class _HandBackAfterImport(importlib.abc.MetaPathFinder):
    """Finds `agno.utils.log` like the normal path finder, and hands the
    loggers back right after the module's code has run."""

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname != _AGNO_LOG_MODULE:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or spec.loader is None:
            return spec
        loader: Any = spec.loader
        run_module = loader.exec_module

        def exec_module(module: ModuleType) -> None:
            run_module(module)
            hand_back()

        loader.exec_module = exec_module
        return spec


def install() -> None:
    """Hand agno's loggers back now and after any later import. Idempotent."""
    hand_back()
    if not any(isinstance(finder, _HandBackAfterImport) for finder in sys.meta_path):
        sys.meta_path.insert(0, _HandBackAfterImport())
