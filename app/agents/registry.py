from __future__ import annotations

from .workers.ads_meta import AdsMeta
from .workers.base import Worker
from .workers.code_critic_worker import CodeCriticWorker
from .workers.code_gen import CodeGen
from .workers.code_next import CodeNext
from .workers.copywrite import Copywrite
from .workers.deploy_v0 import DeployV0
from .workers.design_tokens import DesignTokens
from .workers.research_pro import ResearchPro
from .workers.seo_brief import SeoBrief
from .workers.sol_audit import SolAudit
from .workers.translate import Translate42
from .workers.vision_ocr import VisionOcr

# Every seeded agent has a real worker. code.next, vision.ocr, ads.meta and
# translate.42 run on Claude only (`workers/claude_only.py`): with the provider
# on OpenAI their steps are not attempted rather than simulated, so a buyer is
# never charged for stand-in output.
_REAL: list[Worker] = [
    Copywrite(),  # agt_01h8 copywrite.v3
    DesignTokens(),  # agt_02k2 design.figma — kit-aware tokens
    CodeNext(),  # agt_03d9 code.next — React / Next.js files, Claude only
    SolAudit(),  # agt_04m1 sol-audit
    SeoBrief(),  # agt_05x7 seo.brief — kit-aware brand block
    VisionOcr(),  # agt_06q4 vision.ocr — Claude vision, Claude only
    AdsMeta(),  # agt_07w3 ads.meta — Meta ad set copy, Claude only
    DeployV0(),  # agt_08j2 deploy.v0 — seal + preview URL
    ResearchPro(),  # agt_09l5 research.pro — kit-aware feature brief
    Translate42(),  # agt_10b6 translate.42 — upstream text into the requested languages, Claude only
    CodeGen(),  # agt_11c0 code.gen — context-aware HTML draft
    CodeCriticWorker(),  # agt_12r0 code.critic — top-level polish step
]

WORKERS: dict[str, Worker] = {w.id: w for w in _REAL}


def get_worker(agent_id: str) -> Worker | None:
    return WORKERS.get(agent_id)
