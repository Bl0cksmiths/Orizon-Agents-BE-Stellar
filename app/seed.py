from __future__ import annotations

from .schemas import Agent
from .state import state

# id → (name, skills, price, rep, status, runs, real)
_SEED: list[tuple[str, str, list[str], float, float, str, int, bool]] = [
    ("agt_01h8", "copywrite.v3", ["copy", "seo", "en"], 0.012, 4.92, "online", 18420, True),
    ("agt_02k2", "design.figma", ["ui", "tokens", "figma"], 0.018, 4.87, "online", 7321, True),
    ("agt_03d9", "code.next", ["ts", "react", "next"], 0.066, 4.95, "online", 24610, False),
    ("agt_04m1", "sol-audit", ["solidity", "security"], 0.180, 4.78, "idle", 1204, True),
    ("agt_05x7", "seo.brief", ["seo", "research"], 0.009, 4.65, "online", 32012, True),
    ("agt_06q4", "vision.ocr", ["vision", "ocr"], 0.014, 4.71, "idle", 8811, True),
    ("agt_07w3", "ads.meta", ["ads", "meta"], 0.022, 4.58, "online", 5320, True),
    ("agt_08j2", "deploy.v0", ["deploy", "ci", "seal"], 0.011, 4.88, "online", 12980, True),
    ("agt_09l5", "research.pro", ["research", "citations"], 0.024, 4.83, "online", 9042, True),
    ("agt_10b6", "translate.42", ["i18n", "42 langs"], 0.007, 4.90, "online", 41200, True),
    ("agt_11c0", "code.gen", ["code", "html", "js", "build"], 0.054, 4.89, "online", 3021, True),
    ("agt_12r0", "code.critic", ["a11y", "polish", "review"], 0.052, 4.91, "online", 2218, True),
]


def seed_registry() -> None:
    for id_, name, skills, price, rep, status, runs, real in _SEED:
        state.add_agent(
            Agent(
                id=id_,
                name=name,
                skills=skills,
                price=price,
                rep=rep,
                status=status,  # type: ignore[arg-type]
                runs=runs,
                real=real,
            )
        )
