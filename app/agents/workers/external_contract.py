"""The operator response contract — what we are willing to accept back from a
bound external agent (ADR 0004, D1).

`ExternalHttpWorker._parse` checks that the body is a JSON object carrying a
non-empty `summary` and then returns the operator's whole dict. Whatever they
put beside `summary` lands in `context[worker.name]`, is forwarded verbatim to
every later operator in the plan, and reaches billing, on-chain reputation, the
SSE trace and the buyer's artifact viewer. An untrusted third party choosing
the bytes of all of that is the hole this module closes.

**Nothing is passed through.** `parse_operator_output` builds a NEW dict out of
an allowlist of keys, each type-checked and clamped, and every key it does not
recognise is dropped. That is stronger than a block-list in two ways worth
naming: a key we have not thought of yet is dropped by default rather than
forwarded by default, and the returned object contains no operator-owned
container — every value in it is a `str`, a `list[str]`, or a small dict we
built ourselves out of `str`s. So nothing operator-shaped survives to be
re-serialised into the next dispatch envelope or the trace.

Refusal vs. drop — the line, stated once:

  * A response is REFUSED when what is wrong is the *deliverable*: the body is
    not an object at all, or there is no summary, or `artifact` — the payload
    the buyer is paying for — is not even a dict. Refusal raises
    `ExternalOutputError`, which the worker turns into a failed step: skipped
    and unbilled. It is never a run failure; that is the existing `continue`
    discipline, and it is what stops one hostile response from denying
    settlement to every honest agent in the plan.
  * A malformed *decoration* around a usable deliverable is DROPPED. Failing an
    already-executed step because `critic_notes` arrived as a dict would charge
    the buyer nothing for work that was actually done, and hand any operator a
    way to fail their own step after the fact. Each drop site says why.

Every refusal names a `rule` from `OUTPUT_RULES`, a closed vocabulary. The rule
— not the prose — is the machine-readable part: it is what a caller maps to an
error code and what a test asserts on, leaving the message free to say
something useful to a human. The pattern, including the constructor that
refuses an unknown rule so a typo at a raise site fails loudly here rather than
travelling to a caller as an error code nothing handles, is
`app.services.endpoint_policy`'s, deliberately: two vocabularies that behave
differently would be two things to learn.

Bounds mirror the local path (`code_gen`) rather than inventing a second scale
— external output must not be allowed more room than our own workers get — and
`MAX_ARTIFACT_CHARS` and the HTML clamp are *imported* from it rather than
restated, because a bound with two copies is a bound that drifts.

Not this module's problem, stated so it is not assumed: the caller does
`json.loads` on the response bytes, and a ~1 MiB body of `[[[[…` raises
`RecursionError` there — a `RuntimeError` subclass that the call site's
`(json.JSONDecodeError, UnicodeDecodeError)` handler does not catch, so it
escapes as a run failure rather than a step failure. That is a *parse*-site
concern and the parse site belongs to the worker, not here: this module is
handed an already-decoded object. It gets no rule in `OUTPUT_RULES`, because a
rule nobody here can raise is a dead error code a caller would nonetheless
write a branch for. The half of that hazard that IS ours — an operator's deep
structure surviving into `context` and then blowing up on the way back OUT, at
`json.dumps` of the next envelope — is closed structurally by the allowlist
above: no operator container is ever copied into the result.

Pure: no I/O, no clock, no network.
"""

from __future__ import annotations

from typing import Any

from .code_gen import MAX_ARTIFACT_CHARS, clamp_artifact_content
from .code_validator import harden_artifact

# Bounds, mirroring code_gen's scale. A summary is a trace line and a receipt
# field, not a document; a title is a heading. MAX_ARTIFACT_CHARS is code_gen's
# own per-file ceiling, imported rather than restated.
MAX_SUMMARY_CHARS = 2_000
MAX_TITLE_CHARS = 120
MAX_FILES = 24
MAX_NOTES = 16
MAX_NOTE_CHARS = 500
# Mirrors ArtifactFile.path's max_length.
MAX_PATH_CHARS = 200
# Roughly the shortest ceiling any mainstream browser enforces on a URL, and
# far more than a preview link needs. It exists so a megabyte of "URL" cannot
# be laundered into a trace line.
MAX_PREVIEW_URL_CHARS = 2_048

# What a clamped HTML payload can weigh: the ceiling plus the truncation note
# and any closing tags `clamp_artifact_content` re-appends after the cut. Same
# derivation as code_gen._MAX_STORED_CHARS, for the same reason.
_MAX_STORED_CHARS = MAX_ARTIFACT_CHARS + 256

# The closed vocabulary of refusal reasons. Closed on purpose: ExternalOutputError
# rejects anything outside it, so a typo at a raise site fails loudly here rather
# than reaching a caller as an error code nobody handles.
#
# Short on purpose too. Every entry here is a way the *deliverable* can be
# missing; everything else an operator can get wrong is a drop, not a refusal,
# and a drop needs no vocabulary because it is not reported.
OUTPUT_RULES: frozenset[str] = frozenset(
    {
        "not_an_object",
        "summary_missing",
        "artifact_not_an_object",
    }
)


class ExternalOutputError(ValueError):
    """An operator response was refused, naming the rule that refused it.

    `rule` is one of `OUTPUT_RULES`; the constructor refuses any other value so
    the vocabulary cannot quietly grow a synonym. ValueError rather than a
    bespoke base because callers that do not care which rule fired — the
    worker, which re-raises everything as ExternalDispatchError, or a test —
    still get a sensible except clause.
    """

    def __init__(self, rule: str, message: str) -> None:
        if rule not in OUTPUT_RULES:
            raise ValueError(f"unknown operator output rule {rule!r}")
        super().__init__(message)
        self.rule = rule


# What a clamped short text ends with, so a truncated summary reads as truncated
# in the trace rather than as a sentence that simply stops.
_TRUNCATION_SUFFIX = " …[truncated]"


def _clamp_text(value: str, limit: int) -> str:
    """Bound one short text field, degrading instead of raising.

    Oversize is a quality failure, not a protocol violation: the step has
    already run, so the field is cut back and marked rather than costing the
    buyer the whole step. Same trade `clamp_artifact_content` makes for
    generated HTML, applied to text where that function's HTML repair (closing
    `</script>`, `</body>`) would be noise.
    """
    if len(value) <= limit:
        return value
    if limit <= len(_TRUNCATION_SUFFIX):  # pragma: no cover — no bound is this small
        return value[:limit]
    return value[: limit - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX


def _clean_text(value: object, limit: int) -> str | None:
    """`value` as a non-empty clamped `str`, or None if it is neither.

    Returning None rather than raising is what lets one call site refuse
    (`summary`) and another drop (`title`) on the same check.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return _clamp_text(text, limit)


def _clean_html(value: object) -> str | None:
    """`value` as a bounded HTML payload, or None if it is not a str.

    `clamp_artifact_content` is code_gen's own clamp, reused rather than
    reimplemented: it cuts at a line boundary and re-closes a dangling
    `<script>`/`<body>`/`<html>` so the truncated document still parses in the
    viewer's iframe. Not stripped and not emptiness-checked — whitespace is
    content here, and an empty payload is a poor artifact, not a bad response.

    The second trim is not redundant: the clamp re-appends a note and closing
    tags after the cut, so its output can land just above the raw ceiling.
    `_MAX_STORED_CHARS` is the ceiling that actually bounds what we store.
    """
    if not isinstance(value, str):
        return None
    return clamp_artifact_content(value)[:_MAX_STORED_CHARS]


def _parse_files(value: object) -> list[dict[str, str]] | None:
    """The artifact's file list, rebuilt entry by entry, or None if it is not a list.

    Filtered per entry rather than dropped whole — the opposite of the choice
    `_parse_notes` makes, and for a reason that does not apply here: a shorter
    file list is not a *claim*. "Here are three files" stays true when a fourth
    malformed one is discarded, whereas a shortened `critic_violations` asserts
    something about the work that is no longer true.

    `language` is deliberately not carried over. Nothing downstream needs it —
    the viewer renders `preview_html`, and `harden_artifact` already recognises
    an HTML file by its path suffix — so accepting it would only put another
    uncontrolled operator string on the display path.
    """
    if not isinstance(value, list):
        return None

    files: list[dict[str, str]] = []
    for entry in value:
        if len(files) >= MAX_FILES:
            break
        if not isinstance(entry, dict):
            continue  # a list of strings/ints is a shape mistake, not a file
        path = _clean_text(entry.get("path"), MAX_PATH_CHARS)
        content = _clean_html(entry.get("content"))
        if path is None or content is None:
            continue  # a file is a path AND bytes; half of one is neither
        files.append({"path": path, "content": content})
    return files


def _parse_artifact(value: object) -> dict[str, Any] | None:
    """The artifact, rebuilt from an allowlist and hardened, or None if empty.

    Refuses a non-dict: `artifact` is the thing the buyer is paying for, and an
    operator who returns a string or a list under that key has not produced one.
    Every field inside it, by contrast, is dropped when malformed — a title that
    arrived as an int does not invalidate the HTML beside it.

    `harden_artifact` runs LAST, over the rebuilt dict, and is the point of
    doing any of this in one place: an operator's `preview_html` goes into the
    viewer's `srcDoc` exactly as a local artifact's does, so it needs the same
    injected CSP. The iframe sandbox already blocks parent access; what the CSP
    adds is `connect-src 'none'` — no beaconing out of the frame the buyer just
    opened. Today only code_gen and code_critic call it, which is precisely why
    external HTML reaches the viewer unsealed (ADR 0004, D1).

    Order matters: clamp first, harden second. The CSP `<meta>` is injected at
    the top of the document, so hardening a payload that was later truncated
    would risk cutting the policy back off.
    """
    if not isinstance(value, dict):
        raise ExternalOutputError(
            "artifact_not_an_object",
            f"operator response 'artifact' was {type(value).__name__}, expected an object",
        )

    artifact: dict[str, Any] = {}
    title = _clean_text(value.get("title"), MAX_TITLE_CHARS)
    if title is not None:
        artifact["title"] = title
    files = _parse_files(value.get("files"))
    if files is not None:
        artifact["files"] = files
    preview_html = _clean_html(value.get("preview_html"))
    if preview_html is not None:
        artifact["preview_html"] = preview_html

    if not artifact:
        # Nothing survived the allowlist. Returning None rather than `{}` keeps
        # `output.get("artifact")` falsy for the two readers that branch on it
        # (the trace's artifact line, and synthetic_rating's +15), which is the
        # honest answer: no artifact was delivered.
        return None
    return harden_artifact(artifact)
