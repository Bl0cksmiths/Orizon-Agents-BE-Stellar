"""The images a vision.ocr step reads: where they come from, and fetching them safely.

Two sources, in this order:

  * uploaded images — `context["images"]`, a list of `{"media_type", "data"}`
    with base64 data, the shape a future upload path hands the run loop;
  * image URLs — https links ending in an image extension (.png, .jpg, .jpeg,
    .gif, .webp), found in the request or in an earlier step's output.

`has_image_input` is the planner's question: is there anything here for
vision.ocr to read? It is pure (no DNS, no fetch) and judges a URL by the same
pure endpoint policy the fetch starts with, so a request carrying only a
refused link (http, a private address, localhost) is not routed to the agent.

Fetching an image the buyer named is an outbound request from inside our
network, so it is held to the operator-endpoint rules (`endpoint_policy`):

  * https only, port 443 only, no credentials in the URL;
  * the host must be public — the pure check refuses private, loopback,
    link-local (the metadata service), reserved and multicast literals and
    the loopback / metadata names, and every connect resolves the name and
    dials only the address it checked (`_PinnedPublicTransport`), so a name
    that resolves, or is rebound, into a private range is refused — and so is
    any address that is not globally routable;
  * redirects are followed by hand, at most `MAX_REDIRECTS`, and every hop is
    judged exactly like the first — a redirect cannot launder a private target;
  * one wall-clock deadline per image (`FETCH_TIMEOUT_SECONDS`), the body is
    read in chunks and abandoned past `MAX_IMAGE_BYTES`, and the format is
    read from the bytes, never from a Content-Type header.

Every refusal is an `ImageInputError` naming a rule and the HOST, never the
URL: a link can carry a credential in its query string.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import re
import socket
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ...llm.claude import ImageBlock
from ...services.endpoint_policy import EndpointPolicyError, resolve_checked_addresses, validate_endpoint_url

# The context key a future upload path fills with `{"media_type", "data"}` dicts.
UPLOADED_IMAGES_KEY = "images"

MAX_IMAGES = 4
# Claude takes at most 5 MB per image; the base64 of this many bytes stays under it.
MAX_IMAGE_BYTES = 3_750_000
FETCH_TIMEOUT_SECONDS = 15.0
CONNECT_TIMEOUT_SECONDS = 5.0
MAX_REDIRECTS = 3
MAX_URL_CHARS = 2_048

_REDIRECTS = frozenset({301, 302, 303, 307, 308})

# An https link whose path ends in an image extension, optionally with a query.
_IMAGE_URL_RE = re.compile(
    r"https://[^\s<>\"'`()\[\]{}|\\^]+?\.(?:png|jpe?g|gif|webp)(?:\?[^\s<>\"'`()\[\]{}|\\^]*?)?"
    r"(?=$|[\s<>\"'`()\[\]{}|,;!.])",
    re.IGNORECASE,
)

# What a refusal can be. Lowercase tokens, safe for a trace.
IMAGE_RULES = frozenset(
    {
        "image_refused",  # the URL or an address it resolves to is not one we will fetch
        "image_unreachable",  # DNS, connect, TLS or an HTTP status other than 200
        "image_timeout",  # the fetch outlasted its deadline
        "image_too_large",  # past MAX_IMAGE_BYTES
        "image_unsupported",  # not a PNG, JPEG, GIF or WebP
    }
)


class ImageInputError(ValueError):
    """One image that could not be used, naming the rule and the host (never the URL)."""

    def __init__(self, rule: str, source: str, message: str) -> None:
        if rule not in IMAGE_RULES:
            raise ValueError(f"unknown image rule {rule!r}")
        super().__init__(f"{source}: {message}")
        self.rule = rule
        self.source = source


@dataclass(frozen=True)
class ImageSource:
    """One image a step can read: an upload (`data` set) or a URL to fetch."""

    label: str  # "upload 1" or the URL's host — what output and logs may name
    url: str | None = None
    upload: Mapping[str, Any] | None = None


def sniff_media_type(data: bytes) -> str | None:
    """The image format of `data` from its magic bytes, or None for anything else."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _strings(value: Any, depth: int = 0) -> Iterator[str]:
    """Every string inside `value` (dicts, lists), bounded in depth."""
    if depth > 6:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item, depth + 1)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item, depth + 1)


def _allowed_url(url: str) -> bool:
    """Whether `url` passes the pure fetch rules (no I/O)."""
    try:
        _check_url(url)
    except ImageInputError:
        return False
    return True


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "unknown host").rstrip(".")
    except ValueError:
        return "unknown host"


def _check_url(url: str) -> None:
    """The pure rules for one URL: length, https, port 443, no credentials, public host."""
    host = _host(url)
    if len(url) > MAX_URL_CHARS:
        raise ImageInputError("image_refused", host, "URL too long")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise ImageInputError("image_refused", host, "URL could not be parsed") from e
    if parts.username is not None or parts.password is not None:
        raise ImageInputError("image_refused", host, "credentials in the URL")
    if port not in (None, 443):
        raise ImageInputError("image_refused", host, f"port {port} is not 443")
    try:
        validate_endpoint_url(url)
    except EndpointPolicyError as e:
        raise ImageInputError("image_refused", host, f"refused by endpoint policy ({e.rule})") from e
    literal = _ip_literal(host)
    if literal is not None and not literal.is_global:
        raise ImageInputError("image_refused", host, "not a globally routable address")


def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address `host` spells in any form the resolver honours, or None for a name."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ipaddress.AddressValueError):
        return None


def _uploads(context: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    raw = (context or {}).get(UPLOADED_IMAGES_KEY)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping) and isinstance(item.get("data"), str)]


def image_urls(intent: str, context: Mapping[str, Any] | None = None) -> list[str]:
    """The image URLs in the request and earlier outputs that pass the pure fetch
    rules, de-duplicated, request first. `context["kit"]` is never read."""
    texts = [intent, *_strings({k: v for k, v in (context or {}).items() if k not in ("kit", UPLOADED_IMAGES_KEY)})]
    found: list[str] = []
    for text in texts:
        for match in _IMAGE_URL_RE.finditer(text):
            url = match.group(0)
            if url not in found and _allowed_url(url):
                found.append(url)
    return found


def image_sources(intent: str, context: Mapping[str, Any] | None = None) -> list[ImageSource]:
    """What a vision step will read: uploads first, then URLs, at most `MAX_IMAGES`."""
    sources = [ImageSource(label=f"upload {i}", upload=item) for i, item in enumerate(_uploads(context), start=1)]
    sources.extend(ImageSource(label=_host(url), url=url) for url in image_urls(intent, context))
    return sources[:MAX_IMAGES]


def has_image_input(intent: str, context: Mapping[str, Any] | None = None) -> bool:
    """True when vision.ocr has something to read: an uploaded image, or an https
    image URL that passes the fetch rules. Pure — the planner can call it per plan."""
    return bool(image_sources(intent, context))


def decode_upload(item: Mapping[str, Any], label: str) -> ImageBlock:
    """An uploaded image as an `ImageBlock`, checked like a fetched one."""
    try:
        data = base64.b64decode(str(item.get("data", "")), validate=True)
    except (binascii.Error, ValueError) as e:
        raise ImageInputError("image_unsupported", label, "upload is not valid base64") from e
    return _block(data, label)


def _block(data: bytes, label: str) -> ImageBlock:
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageInputError("image_too_large", label, f"image is over {MAX_IMAGE_BYTES:,} bytes")
    media_type = sniff_media_type(data)
    if media_type is None:
        raise ImageInputError("image_unsupported", label, "not a PNG, JPEG, GIF or WebP image")
    return ImageBlock(media_type=media_type, data=base64.b64encode(data).decode("ascii"))


class _PinnedPublicTransport(httpx.AsyncBaseTransport):
    """Resolve the host, refuse it unless every address is public, then dial the
    address that was checked — the same resolve-check-pin as an operator
    dispatch (`external_http._PinnedAddressTransport`, whose notes cover DNS
    rebinding and why SNI keeps the certificate check on the name), stricter by
    one rule: an address must also be globally routable, which refuses the
    shared 100.64.0.0/10 range the dispatch predicate lets through."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.raw_host.decode("ascii").rstrip(".")
        addresses = await resolve_checked_addresses(host)
        for address in addresses:
            if not ipaddress.ip_address(address).is_global:
                raise EndpointPolicyError("non_public_address", f"host {host!r} resolves to non-global {address}")
        request.extensions = {**request.extensions, "sni_hostname": host}
        request.url = request.url.copy_with(host=addresses[0])
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


async def fetch_image(url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> ImageBlock:
    """Fetch one image URL under the rules above. `transport` is the INNER
    transport (tests pass a mock); the resolve-check-pin wrapper always applies."""
    host = _host(url)
    try:
        return await asyncio.wait_for(_fetch(url, transport), timeout=FETCH_TIMEOUT_SECONDS)
    except TimeoutError as e:
        raise ImageInputError("image_timeout", host, f"no image within {FETCH_TIMEOUT_SECONDS:.0f} s") from e


async def _fetch(url: str, transport: httpx.AsyncBaseTransport | None) -> ImageBlock:
    async with httpx.AsyncClient(
        transport=_PinnedPublicTransport(transport or httpx.AsyncHTTPTransport()),
        timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
        # Never followed by the client: each hop is judged below before it is dialled.
        follow_redirects=False,
        headers={"Accept": "image/png,image/jpeg,image/gif,image/webp", "User-Agent": "orizon-vision/1"},
    ) as client:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            host = _host(current)
            _check_url(current)
            try:
                async with client.stream("GET", current) as response:
                    if response.status_code in _REDIRECTS and "location" in response.headers:
                        current = urljoin(current, response.headers["location"])
                        continue
                    if response.status_code != 200:
                        raise ImageInputError("image_unreachable", host, f"HTTP {response.status_code}")
                    return _block(await _read_capped(response, host), host)
            except EndpointPolicyError as e:
                # The pinned transport's per-connect address check.
                raise ImageInputError("image_refused", host, f"refused by endpoint policy ({e.rule})") from e
            except httpx.HTTPError as e:
                raise ImageInputError("image_unreachable", host, f"fetch failed: {type(e).__name__}") from e
        raise ImageInputError("image_unreachable", _host(current), f"more than {MAX_REDIRECTS} redirects")


async def _read_capped(response: httpx.Response, host: str) -> bytes:
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
        raise ImageInputError("image_too_large", host, f"image is over {MAX_IMAGE_BYTES:,} bytes")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > MAX_IMAGE_BYTES:
            raise ImageInputError("image_too_large", host, f"image is over {MAX_IMAGE_BYTES:,} bytes")
    return bytes(body)


async def load_image(source: ImageSource, *, transport: httpx.AsyncBaseTransport | None = None) -> ImageBlock:
    """The `ImageBlock` for one source: an upload decoded, or a URL fetched."""
    if source.upload is not None:
        return decode_upload(source.upload, source.label)
    if source.url is None:  # pragma: no cover — ImageSource is built with one or the other
        raise ImageInputError("image_unsupported", source.label, "no image")
    return await fetch_image(source.url, transport=transport)
