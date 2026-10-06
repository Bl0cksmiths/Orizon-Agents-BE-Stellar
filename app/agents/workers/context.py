"""The upstream handoff: what earlier steps of a run produced, for a later step.

A plan is a pipeline (research.pro → seo.brief → copywrite.v3 → design.figma →
code.gen → code.critic → deploy.v0), and a pipeline only beats one agent when
each step builds on what the steps before it made. The run loop already keeps
every step's output in `context`, keyed by worker name; this module is the one
place that turns those outputs into something a later step's prompt can carry:

  * **Typed.** Each producing role has a payload type (`ResearchHandoff`,
    `SeoHandoff`, `CopyHandoff`, `DesignHandoff`, `CodeHandoff`, `OcrHandoff`,
    `TranslationHandoff`, `AuditHandoff`, `AdsHandoff`) built from an
    allowlist of that role's output fields. Nothing else in the output — an
    artifact's HTML, a preview URL, a `source` tag, a key nobody expected — is
    read, so nothing else can reach a prompt.
  * **Keyed by role.** `CONSUMES` is the handoff map: for each consuming role,
    the upstream roles it reads, most useful first.
  * **Bounded.** Every field, list, item (`MAX_ITEM_CHARS`) and the whole
    handoff (`MAX_TOTAL_CHARS`) has a ceiling. Roles are taken in `CONSUMES`
    order, so when the total runs out it is the least useful item that is
    trimmed or left out.
  * **Fenced.** Every upstream output was written by a model that read the
    buyer's intent, so it carries the intent's taint: `Handoff.section` puts it
    inside one `prompt_safety` UNTRUSTED block, never bare.
  * **No secrets, no one else's data.** Text is scrubbed of anything shaped
    like a credential and of the values of this service's own configured
    secrets. The handoff reads only the `context` it is handed — the run's own
    dict, built fresh per run by `execution_svc._run` — never shared state, so
    it cannot carry another buyer's work.

What this module does NOT change: the `context` dict itself. External operator
agents receive `context` exactly as before (ADR 0001's envelope), and nothing
here adds a key to it, so building a handoff never widens what a third party
is sent. A bound operator's own output enters `context` already fenced
(`execution_svc._fenced_for_context`); a first-party consumer reads only its
`summary`, unwrapped from that fence and re-fenced inside the handoff block.

Output fields read per role (a new worker that wants to hand off must return
these keys; anything else falls back to `summary` only):

  research.pro   summary, findings[{claim, confidence}], sources[]
  seo.brief      summary, brand_name, tagline, keywords[], audiences[]
  copywrite.v3   hero{headline, subtitle}, sections[{title, body}]
  design.figma   palette{bg … danger}, typography{family_ui, family_display}
  code.gen / code.next / code.critic
                 artifact{title, summary, entry, files[{path, content}]}
                 (counts only — the HTML itself is never handed off)
  vision.ocr     text, language?  (or blocks[{text}])
  translate.42   translations[{lang|language, text}]  (or language + text)
  sol-audit      summary, findings[{severity, title, rationale}], cvss_estimate
  ads.meta       ads[{headline, primary_text, description?, cta?}] (or variants[])

Pure: no I/O, no clock, no network.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from ...config import settings
from .prompt_safety import fence_untrusted, sanitize_untrusted

# ── Roles ────────────────────────────────────────────────────────────────────
# Worker names: the keys `execution_svc` files each step's output under.

RESEARCH = "research.pro"
SEO = "seo.brief"
COPY = "copywrite.v3"
DESIGN = "design.figma"
CODE_GEN = "code.gen"
CODE_NEXT = "code.next"
CODE_CRITIC = "code.critic"
DEPLOY = "deploy.v0"
AUDIT = "sol-audit"
OCR = "vision.ocr"
ADS = "ads.meta"
TRANSLATE = "translate.42"

# Roles whose output carries a code artifact, drafts before reviews.
CODE_ROLES: tuple[str, ...] = (CODE_GEN, CODE_NEXT, CODE_CRITIC)

# A bound operator's output is filed under "external.<agent_id>".
EXTERNAL_PREFIX = "external."

# `context` keys that are not a step's output: the curated kit, the request,
# and the buyer's uploaded images (`vision_input.UPLOADED_IMAGES_KEY`).
META_KEYS = frozenset({"kit", "intent", "images"})

# The handoff map: consumer → the upstream roles it reads, most useful first.
# Bound operators' outputs follow these, in delivery order, for every consumer
# that reads upstream at all.
_CODE_INPUTS = (DESIGN, COPY, SEO, RESEARCH, TRANSLATE, OCR)
CONSUMES: dict[str, tuple[str, ...]] = {
    RESEARCH: (OCR, AUDIT, TRANSLATE),
    SEO: (RESEARCH, OCR, TRANSLATE),
    COPY: (SEO, RESEARCH, AUDIT, OCR, TRANSLATE),
    DESIGN: (SEO, COPY, RESEARCH),
    CODE_GEN: _CODE_INPUTS,
    CODE_NEXT: _CODE_INPUTS,
    CODE_CRITIC: (COPY, DESIGN, CODE_GEN, CODE_NEXT, SEO, RESEARCH, TRANSLATE),
    AUDIT: (OCR, RESEARCH),
    ADS: (COPY, SEO, RESEARCH, DESIGN, TRANSLATE),
    TRANSLATE: (COPY, ADS, RESEARCH, AUDIT, OCR, SEO),
    OCR: (),
    DEPLOY: (),  # seals an artifact rather than reading prose: see latest_output
}

# ── Bounds ───────────────────────────────────────────────────────────────────

# One field: a claim, a section body, a keyword, a translation line.
MAX_FIELD_CHARS = 400
# Long free text: OCR'd text, one translation.
MAX_TEXT_CHARS = 1_500
# Entries kept from any one list.
MAX_LIST_ITEMS = 12
# One role's rendered item.
MAX_ITEM_CHARS = 2_000
# The whole handoff — about 2 000 tokens, a small share of any worker's prompt.
MAX_TOTAL_CHARS = 8_000
# An item the remaining budget would cut below this is left out rather than
# handed on as a stub.
MIN_ITEM_CHARS = 160

FENCE_LABEL = "UPSTREAM_OUTPUTS"
_TRIMMED = " …[trimmed]"

# ── Secret scrubbing ─────────────────────────────────────────────────────────

_REDACTED = "[redacted secret]"
_SECRET_PATTERNS = (
    # PEM private keys, whole block.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.DOTALL),
    # Stellar secret seeds (S + 55 base32).
    re.compile(r"\bS[A-Z2-7]{55}\b"),
    # Provider API keys: Anthropic / OpenAI style, GitHub, AWS, Slack.
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    # JWTs and bearer tokens.
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{16,}"),
    # key = value / key: value for credential-named keys.
    re.compile(
        r"(?i)\b(api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token|password|passwd)"
        r"(\s*[:=]\s*)['\"]?[^\s'\",;]{6,}"
    ),
)
# This service's own credentials, by settings field. A configured value is
# redacted wherever it appears, whatever its shape.
_SECRET_SETTINGS = (
    "anthropic_api_key",
    "typesafe_api_key",
    "openai_api_key",
    "api_key",
    "frontend_proxy_token",
    "orizon_dispatch_signing_key",
    "stellar_signing_key",
    "pdax_password",
    "pdax_otp_secret",
    "pdax_webhook_secret",
)
_MIN_SECRET_CHARS = 8


def _configured_secrets() -> list[str]:
    values = (getattr(settings, name, "") for name in _SECRET_SETTINGS)
    return [v for v in values if isinstance(v, str) and len(v.strip()) >= _MIN_SECRET_CHARS]


def scrub_secrets(text: str) -> str:
    """`text` with credentials and this service's configured secrets redacted."""
    for value in _configured_secrets():
        text = text.replace(value.strip(), _REDACTED)
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 2:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", text)
        else:
            text = pattern.sub(_REDACTED, text)
    return text


# ── Field helpers ────────────────────────────────────────────────────────────


def _text(value: Any, limit: int = MAX_FIELD_CHARS) -> str:
    """A bounded one-line string from `value`, or "" when it is not text."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    flat = " ".join(str(value).split())
    return _clip(flat, limit)


def _block(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    """A bounded multi-line string (line breaks kept, blank runs collapsed)."""
    if not isinstance(value, str):
        return ""
    lines = [" ".join(line.split()) for line in value.splitlines()]
    joined = "\n".join(line for line in lines if line)
    return _clip(joined, limit)


def _clip(text: str, limit: int) -> str:
    """`text` cut to `limit` characters, at a line or word boundary when one is near."""
    if len(text) <= limit:
        return text
    budget = max(limit - len(_TRIMMED), 0)
    cut = text.rfind("\n", 0, budget + 1)
    if cut < budget // 2:
        cut = text.rfind(" ", 0, budget + 1)
    if cut < budget // 2:
        cut = budget
    return text[:cut].rstrip() + _TRIMMED


def _items(value: Any) -> list[Any]:
    """The first MAX_LIST_ITEMS entries of a list, or none when it is not one."""
    return list(value[:MAX_LIST_ITEMS]) if isinstance(value, list) else []


def _strings(value: Any, limit: int = MAX_FIELD_CHARS) -> tuple[str, ...]:
    return tuple(s for s in (_text(v, limit) for v in _items(value)) if s)


def _dict(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# ── Payloads ─────────────────────────────────────────────────────────────────


class Payload(Protocol):
    kind: ClassVar[str]

    def lines(self) -> list[str]: ...


@dataclass(frozen=True)
class SummaryHandoff:
    """Any output this module has no richer type for: its summary alone."""

    kind: ClassVar[str] = "summary"
    summary: str

    def lines(self) -> list[str]:
        return [f"Summary: {self.summary}"]


@dataclass(frozen=True)
class ResearchHandoff:
    kind: ClassVar[str] = "research findings"
    summary: str
    findings: tuple[tuple[str, float | None], ...]
    sources: tuple[str, ...]

    def lines(self) -> list[str]:
        out = [f"Summary: {self.summary}"] if self.summary else []
        if self.findings:
            out.append("Findings (background, not facts the buyer stated):")
            for claim, conf in self.findings:
                out.append(f"- {claim}" + (f" (confidence {conf:.2f})" if conf is not None else ""))
        if self.sources:
            out.append("Sources: " + "; ".join(self.sources))
        return out


@dataclass(frozen=True)
class SeoHandoff:
    kind: ClassVar[str] = "SEO brief"
    summary: str
    brand_name: str
    tagline: str
    keywords: tuple[str, ...]
    audiences: tuple[str, ...]

    def lines(self) -> list[str]:
        out: list[str] = []
        if self.brand_name:
            out.append(f"Brand name: {self.brand_name}")
        if self.tagline:
            out.append(f"Tagline: {self.tagline}")
        if self.keywords:
            out.append("Keywords: " + ", ".join(self.keywords))
        if self.audiences:
            out.append("Audiences: " + ", ".join(self.audiences))
        if self.summary:
            out.append(f"Summary: {self.summary}")
        return out


@dataclass(frozen=True)
class CopyHandoff:
    kind: ClassVar[str] = "page copy"
    headline: str
    subtitle: str
    sections: tuple[tuple[str, str], ...]

    def lines(self) -> list[str]:
        out: list[str] = []
        if self.headline:
            out.append(f"Hero headline: {self.headline}")
        if self.subtitle:
            out.append(f"Hero subtitle: {self.subtitle}")
        for title, body in self.sections:
            out.append(f"Section — {title}: {body}" if title else f"Section: {body}")
        return out


PALETTE_KEYS = ("bg", "surface", "surface_2", "border", "text", "muted", "primary", "accent", "danger")
# What a design token may look like. Values that are not a plain colour or a
# plain font stack are dropped, so a token can never smuggle prose.
_COLOR_RE = re.compile(r"^(#[0-9a-fA-F]{3,8}|(?:rgb|rgba|hsl|hsla)\([0-9.,%\s/]{1,40}\)|[a-zA-Z]{3,20})$")
_FONT_STACK_RE = re.compile(r"^[A-Za-z0-9 ,'\"\-]{1,120}$")


@dataclass(frozen=True)
class DesignHandoff:
    kind: ClassVar[str] = "design tokens"
    palette: tuple[tuple[str, str], ...]
    family_ui: str
    family_display: str

    def css_vars(self) -> str:
        """The palette as a `:root` block, rebuilt from the validated tokens."""
        body = "\n".join(f"  --{k.replace('_', '-')}: {v};" for k, v in self.palette)
        return ":root {\n" + body + "\n}"

    def lines(self) -> list[str]:
        out: list[str] = []
        if self.palette:
            out.append("Palette (use as the literal CSS variable values):")
            out.extend(self.css_vars().splitlines())
        if self.family_ui:
            out.append(f"Body font stack (family_ui): {self.family_ui}")
        if self.family_display:
            out.append(f"Display font stack (family_display): {self.family_display}")
        return out


@dataclass(frozen=True)
class CodeHandoff:
    kind: ClassVar[str] = "code artifact"
    title: str
    summary: str
    entry: str
    files: int
    lines_of_code: int

    def lines(self) -> list[str]:
        out = [f"Title: {self.title}"] if self.title else []
        if self.summary:
            out.append(f"Summary: {self.summary}")
        out.append(f"Files: {self.files} (entry {self.entry or 'index.html'}), {self.lines_of_code:,} lines")
        return out


@dataclass(frozen=True)
class OcrHandoff:
    kind: ClassVar[str] = "extracted text"
    text: str
    language: str

    def lines(self) -> list[str]:
        head = f"Text read from the image{f' ({self.language})' if self.language else ''}:"
        return [head, *self.text.splitlines()]


@dataclass(frozen=True)
class TranslationHandoff:
    kind: ClassVar[str] = "translations"
    translations: tuple[tuple[str, str], ...]

    def lines(self) -> list[str]:
        out: list[str] = []
        for lang, text in self.translations:
            out.append(f"[{lang or 'translation'}]")
            out.extend(text.splitlines())
        return out


@dataclass(frozen=True)
class AuditHandoff:
    kind: ClassVar[str] = "audit findings"
    summary: str
    findings: tuple[tuple[str, str, str], ...]
    cvss: float | None

    def lines(self) -> list[str]:
        out = [f"Summary: {self.summary}"] if self.summary else []
        if self.cvss is not None:
            out.append(f"CVSS-style estimate: {self.cvss:.1f} / 10")
        for severity, title, rationale in self.findings:
            out.append(f"- [{severity}] {title}" + (f" — {rationale}" if rationale else ""))
        return out


@dataclass(frozen=True)
class AdsHandoff:
    kind: ClassVar[str] = "ad variants"
    ads: tuple[tuple[str, str, str, str], ...]

    def lines(self) -> list[str]:
        out: list[str] = []
        for i, (headline, primary, description, cta) in enumerate(self.ads, 1):
            parts = [f"Ad {i}: {headline}"]
            if primary:
                parts.append(f"text: {primary}")
            if description:
                parts.append(f"description: {description}")
            if cta:
                parts.append(f"CTA: {cta}")
            out.append(" · ".join(parts))
        return out


# ── Extractors: output dict → payload, allowlisted fields only ──────────────


def _confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return min(max(float(value), 0.0), 1.0)


def _research(out: Mapping[str, Any]) -> Payload | None:
    findings = tuple(
        (claim, _confidence(f.get("confidence")))
        for f in _items(out.get("findings"))
        if isinstance(f, Mapping) and (claim := _text(f.get("claim")))
    )
    summary = _text(out.get("summary"))
    if not (findings or summary):
        return None
    return ResearchHandoff(summary=summary, findings=findings, sources=_strings(out.get("sources"), 120))


def _seo(out: Mapping[str, Any]) -> Payload | None:
    payload = SeoHandoff(
        summary=_text(out.get("summary")),
        brand_name=_text(out.get("brand_name"), 80),
        tagline=_text(out.get("tagline"), 160),
        keywords=_strings(out.get("keywords"), 80),
        audiences=_strings(out.get("audiences"), 120),
    )
    return payload if payload.lines() else None


def _copy(out: Mapping[str, Any]) -> Payload | None:
    hero = _dict(out.get("hero"))
    sections = tuple(
        (_text(s.get("title"), 120), body)
        for s in _items(out.get("sections"))
        if isinstance(s, Mapping) and (body := _text(s.get("body")))
    )
    payload = CopyHandoff(
        headline=_text(hero.get("headline"), 160), subtitle=_text(hero.get("subtitle"), 240), sections=sections
    )
    return payload if payload.lines() else None


def _font_stack(value: Any) -> str:
    text = _text(value, 120)
    return text if _FONT_STACK_RE.match(text) else ""


def _design(out: Mapping[str, Any]) -> Payload | None:
    palette = _dict(out.get("palette"))
    typography = _dict(out.get("typography"))
    tokens = tuple(
        (key, value) for key in PALETTE_KEYS if (value := _text(palette.get(key), 60)) and _COLOR_RE.match(value)
    )
    payload = DesignHandoff(
        palette=tokens,
        family_ui=_font_stack(typography.get("family_ui")),
        family_display=_font_stack(typography.get("family_display")),
    )
    return payload if payload.lines() else None


def _code(out: Mapping[str, Any]) -> Payload | None:
    artifact = out.get("artifact")
    if not isinstance(artifact, Mapping):
        summary = _text(out.get("summary"))
        return SummaryHandoff(summary=summary) if summary else None
    files = [f for f in _items(artifact.get("files")) if isinstance(f, Mapping)]
    lines = sum(str(f.get("content", "")).count("\n") + 1 for f in files)
    return CodeHandoff(
        title=_text(artifact.get("title"), 120),
        summary=_text(artifact.get("summary")),
        entry=_text(artifact.get("entry"), 120),
        files=len(files),
        lines_of_code=lines,
    )


def _ocr(out: Mapping[str, Any]) -> Payload | None:
    text = _block(out.get("text"))
    if not text:
        blocks = [b.get("text") for b in _items(out.get("blocks")) if isinstance(b, Mapping)]
        text = _block("\n".join(b for b in blocks if isinstance(b, str)))
    if not text:
        return None
    return OcrHandoff(text=text, language=_text(out.get("language"), 40))


def _translation(out: Mapping[str, Any]) -> Payload | None:
    pairs: list[tuple[str, str]] = []
    for t in _items(out.get("translations")):
        if isinstance(t, Mapping) and (text := _block(t.get("text"), MAX_TEXT_CHARS // 2)):
            pairs.append((_text(t.get("lang") or t.get("language"), 40), text))
    if not pairs and (text := _block(out.get("text"))):
        pairs.append((_text(out.get("language") or out.get("lang"), 40), text))
    return TranslationHandoff(translations=tuple(pairs)) if pairs else None


_SEVERITIES = {"info", "low", "medium", "high", "critical"}


def _audit(out: Mapping[str, Any]) -> Payload | None:
    findings = tuple(
        (severity, title, _text(f.get("rationale"), 240))
        for f in _items(out.get("findings"))
        if isinstance(f, Mapping)
        and (severity := _text(f.get("severity"), 12).lower()) in _SEVERITIES
        and (title := _text(f.get("title"), 160))
    )
    summary = _text(out.get("summary"))
    cvss = out.get("cvss_estimate")
    score = min(max(float(cvss), 0.0), 10.0) if isinstance(cvss, (int, float)) and not isinstance(cvss, bool) else None
    if not (findings or summary):
        return None
    return AuditHandoff(summary=summary, findings=findings, cvss=score)


def _ads(out: Mapping[str, Any]) -> Payload | None:
    raw = out.get("ads") if isinstance(out.get("ads"), list) else out.get("variants")
    ads = tuple(
        (headline, _text(a.get("primary_text")), _text(a.get("description"), 200), _text(a.get("cta"), 40))
        for a in _items(raw)
        if isinstance(a, Mapping) and (headline := _text(a.get("headline"), 160))
    )
    if ads:
        return AdsHandoff(ads=ads)
    return _summary(out)


def _summary(out: Mapping[str, Any]) -> Payload | None:
    summary = _text(out.get("summary"))
    return SummaryHandoff(summary=summary) if summary else None


EXTRACTORS: dict[str, Callable[[Mapping[str, Any]], Payload | None]] = {
    RESEARCH: _research,
    SEO: _seo,
    COPY: _copy,
    DESIGN: _design,
    CODE_GEN: _code,
    CODE_NEXT: _code,
    CODE_CRITIC: _code,
    OCR: _ocr,
    TRANSLATE: _translation,
    AUDIT: _audit,
    ADS: _ads,
}


# A bound operator's summary sits in `context` already fenced; the frame is
# found by fencing a placeholder, so it tracks prompt_safety's format exactly.
def _operator_frame() -> tuple[str, str]:
    framed = fence_untrusted("operator-sentinel", label="OPERATOR_OUTPUT")
    head, _, tail = framed.partition("operator-sentinel")
    return head, tail


def _unfenced(text: str) -> str:
    """The body of an operator-output fence, or `text` unchanged."""
    head, tail = _operator_frame()
    if text.startswith(head) and text.endswith(tail):
        return text[len(head) : len(text) - len(tail)]
    return text


def _external(out: Mapping[str, Any]) -> Payload | None:
    summary = out.get("summary")
    if not isinstance(summary, str):
        return None
    text = _text(_unfenced(summary), MAX_FIELD_CHARS * 2)
    return SummaryHandoff(summary=text) if text else None


def payload_for(role: str, output: Any) -> Payload | None:
    """`role`'s output as its typed payload, or None when it holds nothing to hand on."""
    if not isinstance(output, Mapping):
        return None
    if role.startswith(EXTERNAL_PREFIX):
        return _external(output)
    return EXTRACTORS.get(role, _summary)(output)


# ── The handoff ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HandoffItem:
    role: str
    payload: Payload
    text: str  # rendered, scrubbed and bounded


@dataclass(frozen=True)
class Handoff:
    """What `consumer` receives from the steps before it."""

    consumer: str
    items: tuple[HandoffItem, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.items)

    def __iter__(self) -> Iterator[HandoffItem]:
        return iter(self.items)

    @property
    def sources(self) -> list[str]:
        """The roles this handoff carries output from, in the order it carries them."""
        return [item.role for item in self.items]

    def get(self, role: str) -> HandoffItem | None:
        return next((item for item in self.items if item.role == role), None)

    def text(self) -> str:
        return "\n\n".join(item.text for item in self.items)

    def section(self, guidance: str = "") -> str:
        """The handoff as a prompt section: a trusted line naming the sources
        and saying how to use them, then the outputs inside one UNTRUSTED
        block. Empty when there is nothing upstream, so a caller can always
        pass it to `worker_prompt(sections=…)`."""
        if not self.items:
            return ""
        preface = f"UPSTREAM OUTPUTS from earlier agents in this pipeline ({', '.join(self.sources)})."
        if guidance:
            preface += " " + guidance
        # The clamp is a backstop: `upstream` already holds the text inside
        # MAX_TOTAL_CHARS, plus the separators between items.
        fenced = fence_untrusted(self.text(), label=FENCE_LABEL, max_chars=MAX_TOTAL_CHARS + 64)
        return f"{preface}\n{fenced}"


def _render(role: str, payload: Payload) -> str:
    lines = [f"## {role} — {payload.kind}", *payload.lines()]
    # Sanitised here as well as at the fence, so the budget is measured on the
    # text that will actually ship.
    return _clip(sanitize_untrusted(scrub_secrets("\n".join(lines))), MAX_ITEM_CHARS)


def _external_roles(context: Mapping[str, Any]) -> list[str]:
    return [k for k in context if isinstance(k, str) and k.startswith(EXTERNAL_PREFIX)]


def upstream(
    context: Mapping[str, Any] | None,
    consumer: str,
    *,
    roles: Sequence[str] | None = None,
    exclude: Iterable[str] = (),
) -> Handoff:
    """What `consumer` should be handed from this run's earlier steps.

    `roles` overrides the handoff map's list for `consumer`; `exclude` drops
    roles from it (code.gen leaves out the briefs a curated kit already
    supplies). Bound operators' outputs follow the mapped roles. A consumer
    with no mapped roles reads nothing — including operators.
    """
    if not context:
        return Handoff(consumer=consumer)
    wanted = list(roles if roles is not None else CONSUMES.get(consumer, ()))
    if wanted:
        wanted += _external_roles(context)
    skip = set(exclude) | {consumer} | META_KEYS
    items: list[HandoffItem] = []
    used = 0
    for role in dict.fromkeys(wanted):
        if role in skip:
            continue
        payload = payload_for(role, context.get(role))
        if payload is None:
            continue
        text = _render(role, payload)
        room = MAX_TOTAL_CHARS - used - (2 if items else 0)
        if len(text) > room:
            if room < MIN_ITEM_CHARS:
                continue
            text = _clip(text, room)
        items.append(HandoffItem(role=role, payload=payload, text=text))
        used += len(text) + (2 if len(items) > 1 else 0)
    return Handoff(consumer=consumer, items=tuple(items))


def latest_output(
    context: Mapping[str, Any] | None,
    roles: Iterable[str],
    *,
    include_external: bool = False,
) -> tuple[str, dict[str, Any]] | None:
    """The most recently delivered output among `roles` (and, with
    `include_external`, bound operators) that carries an artifact, with its
    role — e.g. the draft code.critic reviews, or the build deploy.v0 seals.
    "Most recent" is `context` order: a step's output is filed when it
    delivers (one role filed twice keeps its first position)."""
    if not context:
        return None
    wanted = set(roles)
    found: tuple[str, dict[str, Any]] | None = None
    for role, output in context.items():
        listed = role in wanted or (include_external and isinstance(role, str) and role.startswith(EXTERNAL_PREFIX))
        if listed and isinstance(output, dict) and isinstance(output.get("artifact"), dict):
            found = (role, output)
    return found


def delivered_roles(context: Mapping[str, Any] | None) -> list[str]:
    """Every role with an output in `context`, in delivery order — what a bound
    operator's dispatch envelope carries (it is sent the whole `context`)."""
    if not context:
        return []
    return [k for k, v in context.items() if k not in META_KEYS and isinstance(v, Mapping)]
