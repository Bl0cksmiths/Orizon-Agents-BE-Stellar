"""vision.ocr's images: finding them, and fetching them without becoming an SSRF proxy.

The fetch runs through the real resolve-check-pin transport with only DNS and
the far end faked: `getaddrinfo` answers from a table, and the HTTP server is
an `httpx.MockTransport` behind the pin, so every refusal here is the
production code path refusing.
"""

from __future__ import annotations

import asyncio
import base64
import socket
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from app.agents.workers import vision_input
from app.agents.workers.vision_input import ImageInputError, fetch_image, has_image_input, image_urls

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64
PUBLIC = "93.184.216.34"

DNS: dict[str, list[str]] = {
    "img.example.com": [PUBLIC],
    "cdn.example.org": [PUBLIC],
    "rebind.example.com": ["10.0.0.7"],
    "mixed.example.com": [PUBLIC, "169.254.169.254"],
    "cgnat.example.com": ["100.64.3.4"],
    "v6local.example.com": ["::1"],
}


@pytest.fixture(autouse=True)
def dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """The loop's resolver answers from DNS; an unknown name does not resolve."""

    async def getaddrinfo(self: Any, host: str, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        if host not in DNS:
            raise socket.gaierror(socket.EAI_NONAME, "unknown")
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in DNS[host]
        ]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", getaddrinfo)
    return DNS


class Server:
    """The far end: answers by path, records what it was asked."""

    def __init__(self, routes: dict[str, Callable[[httpx.Request], httpx.Response] | httpx.Response]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(request.url.path)
        if route is None:
            return httpx.Response(404)
        return route(request) if callable(route) else route

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _fetch(url: str, server: Server) -> Any:
    return asyncio.run(fetch_image(url, transport=server.transport))


def _refused(url: str, server: Server | None = None) -> ImageInputError:
    with pytest.raises(ImageInputError) as info:
        _fetch(url, server or Server({}))
    return info.value


# ── finding images ──────────────────────────────────────────────────────────


def test_image_urls_are_found_in_the_request_and_earlier_outputs() -> None:
    intent = "Read the menu at https://img.example.com/menu.PNG and the sign https://cdn.example.org/a/sign.jpg?w=800."
    context = {
        "research.pro": {"sources": ["see https://cdn.example.org/chart.webp", "https://img.example.com/menu.PNG"]},
        "kit": {"hero": "https://img.example.com/kit-only.png"},
    }
    assert image_urls(intent, context) == [
        "https://img.example.com/menu.PNG",
        "https://cdn.example.org/a/sign.jpg?w=800",
        "https://cdn.example.org/chart.webp",
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://img.example.com/a.png",
        "https://127.0.0.1/a.png",
        "https://169.254.169.254/latest/a.png",
        "https://10.1.2.3/a.png",
        "https://100.64.0.9/a.png",
        "https://[::1]/a.png",
        "https://2130706433/a.png",
        "https://localhost/a.png",
        "https://metadata.google.internal/a.png",
        "https://user:pw@img.example.com/a.png",
        "https://img.example.com:8443/a.png",
    ],
)
def test_a_link_the_fetch_would_refuse_is_not_image_input(url: str) -> None:
    assert image_urls(f"read {url} please") == []
    assert has_image_input(f"read {url} please") is False


def test_a_page_link_is_not_an_image() -> None:
    assert has_image_input("summarise https://img.example.com/menu.html and https://img.example.com/png") is False


def test_an_uploaded_image_is_image_input() -> None:
    upload = {"media_type": "image/png", "data": base64.b64encode(PNG).decode()}
    assert has_image_input("what does this say?", {"images": [upload]}) is True
    assert has_image_input("what does this say?", {"images": "nope"}) is False


def test_at_most_four_images_are_read() -> None:
    intent = " ".join(f"https://img.example.com/{i}.png" for i in range(6))
    assert [s.label for s in vision_input.image_sources(intent)] == ["img.example.com"] * vision_input.MAX_IMAGES


# ── fetching ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "media_type"), [(PNG, "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif"), (WEBP, "image/webp")]
)
def test_an_image_is_fetched_pinned_to_the_checked_address(body: bytes, media_type: str) -> None:
    server = Server({"/a.img": httpx.Response(200, content=body, headers={"content-type": "text/html"})})
    block = _fetch("https://img.example.com/a.img", server)
    assert block.media_type == media_type  # read from the bytes, not the header
    assert base64.b64decode(block.data) == body
    (request,) = server.requests
    assert request.url.host == PUBLIC  # dialled the address that was checked
    assert request.headers["host"] == "img.example.com"
    assert request.extensions["sni_hostname"] == "img.example.com"


@pytest.mark.parametrize(
    "host", ["rebind.example.com", "mixed.example.com", "cgnat.example.com", "v6local.example.com"]
)
def test_a_name_that_resolves_to_a_non_public_address_is_refused_before_any_request(host: str) -> None:
    server = Server({"/a.png": httpx.Response(200, content=PNG)})
    err = _refused(f"https://{host}/a.png", server)
    assert err.rule == "image_refused"
    assert server.requests == []


def test_an_unresolvable_name_is_refused() -> None:
    assert _refused("https://nowhere.example.net/a.png").rule == "image_refused"


@pytest.mark.parametrize(
    "location",
    [
        "https://169.254.169.254/latest/meta-data/",
        "https://rebind.example.com/a.png",
        "http://img.example.com/a.png",
        "https://localhost/a.png",
        "https://img.example.com:22/a.png",
    ],
)
def test_a_redirect_to_somewhere_refused_is_refused(location: str) -> None:
    server = Server({"/a.png": httpx.Response(302, headers={"location": location})})
    err = _refused("https://img.example.com/a.png", server)
    assert err.rule == "image_refused"
    assert len(server.requests) == 1


def test_a_redirect_to_a_public_image_is_followed_and_rechecked() -> None:
    server = Server(
        {
            "/old.png": httpx.Response(301, headers={"location": "https://cdn.example.org/new.png"}),
            "/new.png": httpx.Response(200, content=PNG),
        }
    )
    assert _fetch("https://img.example.com/old.png", server).media_type == "image/png"
    assert [r.extensions["sni_hostname"] for r in server.requests] == ["img.example.com", "cdn.example.org"]


def test_a_redirect_loop_stops() -> None:
    server = Server({"/a.png": httpx.Response(302, headers={"location": "/a.png"})})
    assert _refused("https://img.example.com/a.png", server).rule == "image_unreachable"
    assert len(server.requests) == vision_input.MAX_REDIRECTS + 1


@pytest.mark.parametrize(
    ("response", "rule"),
    [
        (httpx.Response(404), "image_unreachable"),
        (httpx.Response(200, content=b"<svg xmlns='http://www.w3.org/2000/svg'/>"), "image_unsupported"),
        (
            httpx.Response(200, content=b"<html>not an image</html>", headers={"content-type": "image/png"}),
            "image_unsupported",
        ),
        (httpx.Response(200, content=PNG, headers={"content-length": "99999999"}), "image_too_large"),
    ],
    ids=["404", "svg", "lying-content-type", "declared-too-large"],
)
def test_a_response_that_is_not_a_usable_image_is_refused(response: httpx.Response, rule: str) -> None:
    assert _refused("https://img.example.com/a.png", Server({"/a.png": response})).rule == rule


def test_a_body_past_the_cap_is_abandoned_while_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision_input, "MAX_IMAGE_BYTES", 1_000)

    sent: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        yield PNG
        for i in range(100):
            sent.append(i)
            yield b"\x00" * 500

    def chunks(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    assert _refused("https://img.example.com/a.png", Server({"/a.png": chunks})).rule == "image_too_large"
    assert len(sent) < 5  # stopped reading at the cap, not after the whole body


def test_a_slow_image_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision_input, "FETCH_TIMEOUT_SECONDS", 0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, content=PNG)

    transport = httpx.MockTransport(slow)
    with pytest.raises(ImageInputError) as info:
        asyncio.run(fetch_image("https://img.example.com/a.png", transport=transport))
    assert info.value.rule == "image_timeout"


def test_a_refusal_names_the_host_never_the_url() -> None:
    err = _refused("https://rebind.example.com/a.png?token=SECRET-TOKEN")
    assert "SECRET-TOKEN" not in str(err)
    assert err.source == "rebind.example.com"


# ── uploads ─────────────────────────────────────────────────────────────────


def test_an_upload_is_decoded_and_typed_from_its_bytes() -> None:
    block = vision_input.decode_upload({"media_type": "image/gif", "data": base64.b64encode(JPEG).decode()}, "upload 1")
    assert block.media_type == "image/jpeg"


@pytest.mark.parametrize(
    ("data", "rule"),
    [("not base64!!", "image_unsupported"), (base64.b64encode(b"%PDF-1.7").decode(), "image_unsupported")],
)
def test_an_unusable_upload_is_refused(data: str, rule: str) -> None:
    with pytest.raises(ImageInputError) as info:
        vision_input.decode_upload({"data": data}, "upload 1")
    assert info.value.rule == rule


def test_an_oversized_upload_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision_input, "MAX_IMAGE_BYTES", 10)
    with pytest.raises(ImageInputError) as info:
        vision_input.decode_upload({"data": base64.b64encode(PNG).decode()}, "upload 1")
    assert info.value.rule == "image_too_large"
