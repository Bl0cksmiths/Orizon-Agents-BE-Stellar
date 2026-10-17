from fastapi import APIRouter

from ..schemas import Flow, FlowNode

router = APIRouter(tags=["flow"])


DEFAULT_FLOW = Flow(
    nodes=[
        FlowNode(id="in", label="intent", sub="user input", x=4, y=50),
        FlowNode(id="seo", label="seo.brief", sub="research", x=26, y=22),
        FlowNode(id="copy", label="copywrite.v3", sub="content", x=50, y=22),
        FlowNode(id="ads", label="ads.meta", sub="campaign", x=50, y=78),
        FlowNode(id="an", label="analytics.v2", sub="measure", x=74, y=50),
        FlowNode(id="out", label="outcome", sub="verified", x=96, y=50),
    ],
    edges=[
        ("in", "seo"),
        ("in", "ads"),
        ("seo", "copy"),
        ("copy", "an"),
        ("ads", "an"),
        ("an", "out"),
    ],
)


@router.get("/flow/default", summary="Default agent-graph flow layout")
async def default_flow() -> Flow:
    return DEFAULT_FLOW
