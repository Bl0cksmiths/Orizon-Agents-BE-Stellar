from __future__ import annotations

from .workers.base import Worker
from .workers.code_critic_worker import CodeCriticWorker
from .workers.code_gen import CodeGen
from .workers.copywrite import Copywrite
from .workers.deploy_v0 import DeployV0
from .workers.design_tokens import DesignTokens
from .workers.mock import MockWorker
from .workers.research_pro import ResearchPro
from .workers.seo_brief import SeoBrief
from .workers.sol_audit import SolAudit
from .workers.vision_ocr import VisionOcr

# Real Agno workers
_REAL: list[Worker] = [
    Copywrite(),  # agt_01h8 copywrite.v3
    DesignTokens(),  # agt_02k2 design.figma — kit-aware tokens
    SolAudit(),  # agt_04m1 sol-audit
    SeoBrief(),  # agt_05x7 seo.brief — kit-aware brand block
    DeployV0(),  # agt_08j2 deploy.v0 — seal + preview URL
    ResearchPro(),  # agt_09l5 research.pro — kit-aware feature brief
    CodeGen(),  # agt_11c0 code.gen — context-aware HTML draft
    CodeCriticWorker(),  # agt_12r0 code.critic — top-level polish step
    VisionOcr(),  # agt_06q4 vision.ocr — Claude vision, Claude only
]

# Mock workers for the remaining agents in the registry
_MOCK: list[Worker] = [
    MockWorker("agt_03d9", "code.next"),
    MockWorker("agt_07w3", "ads.meta"),
    MockWorker("agt_10b6", "translate.42"),
]

WORKERS: dict[str, Worker] = {w.id: w for w in (_REAL + _MOCK)}


def get_worker(agent_id: str) -> Worker | None:
    return WORKERS.get(agent_id)
