"""The sidebar dashboard's direct guest-link address follows Supervisor.

Without a Guest URL, links built from the HA sidebar point at the add-on's
published Network port on the HA host. That used to be hardcoded as
http://homeassistant.local:5880, which is wrong as soon as the admin remaps the
port, disables it, or has renamed the host. Supervisor is mocked here at the
httpx boundary — everything above it (parsing, caching, fallback, rendering) is
the real code.
"""
from unittest.mock import patch

import httpx
import pytest

import app.ingress
from app.config import settings

INGRESS_PREFIX = "/api/hassio_ingress/abc123"


@pytest.fixture(autouse=True)
def _reset_guest_link_cache():
    """The lookup is cached at module level; it must not leak between tests."""
    app.ingress._guest_link_cache = None
    app.ingress._guest_link_cache_expires = 0.0
    yield
    app.ingress._guest_link_cache = None
    app.ingress._guest_link_cache_expires = 0.0


class FakeSupervisor:
    """Stands in for httpx.AsyncClient against http://supervisor.

    `routes` maps a path to a JSON payload, an HTTP status code, or an
    exception to raise. Every request is recorded so caching can be asserted.
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []
        self.headers_seen: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.headers_seen.append(kwargs.get("headers") or {})
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, path):
        self.calls.append(path)
        answer = self.routes[path]
        if isinstance(answer, Exception):
            raise answer
        request = httpx.Request("GET", f"http://supervisor{path}")
        if isinstance(answer, int):
            return httpx.Response(answer, request=request)
        return httpx.Response(200, json=answer, request=request)


def _self_info(network):
    return {"result": "ok", "data": {"slug": "homepass", "network": network}}


def _info(hostname):
    return {"result": "ok", "data": {"hostname": hostname, "arch": "amd64"}}


def _supervisor(**routes):
    fake = FakeSupervisor({
        "/addons/self/info": routes.get("self_info", _self_info({"5880/tcp": 5880})),
        "/info": routes.get("info", _info("homeassistant")),
    })
    return fake


# ---------------------------------------------------------------------------
# get_guest_link_target
# ---------------------------------------------------------------------------

async def test_standalone_mode_never_asks_supervisor():
    fake = _supervisor()
    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", None), \
         patch("app.ingress.httpx.AsyncClient", fake):
        target = await app.ingress.get_guest_link_target()
    assert target.base_url == "http://homeassistant.local:5880"
    assert fake.calls == []


async def test_follows_a_remapped_network_port_and_host_name():
    fake = _supervisor(self_info=_self_info({"5880/tcp": 8443}), info=_info("Cabin"))
    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", "sv-token"), \
         patch("app.ingress.httpx.AsyncClient", fake):
        target = await app.ingress.get_guest_link_target()
    assert target.base_url == "http://cabin.local:8443"
    assert target.published is True
    assert fake.headers_seen[0]["Authorization"] == "Bearer sv-token"


async def test_result_is_cached_within_the_ttl():
    fake = _supervisor(self_info=_self_info({"5880/tcp": 8080}))
    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", "sv-token"), \
         patch("app.ingress.httpx.AsyncClient", fake):
        first = await app.ingress.get_guest_link_target()
        second = await app.ingress.get_guest_link_target()
    assert first == second
    assert fake.calls == ["/addons/self/info", "/info"]


async def test_cache_expires_and_is_refreshed():
    fake = _supervisor(self_info=_self_info({"5880/tcp": 8080}))
    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", "sv-token"), \
         patch("app.ingress.httpx.AsyncClient", fake):
        await app.ingress.get_guest_link_target()
        app.ingress._guest_link_cache_expires = 0.0
        fake.routes["/addons/self/info"] = _self_info({"5880/tcp": 9090})
        target = await app.ingress.get_guest_link_target()
    assert target.port == 9090


async def test_a_disabled_port_is_reported_not_papered_over():
    """Supervisor maps a disabled port to null. That is an answer, not an
    error, and falling back to 5880 for it would hand out a dead link."""
    fake = _supervisor(self_info=_self_info({"5880/tcp": None}))
    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", "sv-token"), \
         patch("app.ingress.httpx.AsyncClient", fake):
        target = await app.ingress.get_guest_link_target()
    assert target.published is False
    assert target.port == 5880


@pytest.mark.parametrize("answer", [
    httpx.ConnectError("refused"),
    403,
    500,
    {"result": "ok", "data": {"network": None}},
    {"result": "ok", "data": {"network": {"8080/tcp": 8080}}},
    {"result": "ok", "data": {"network": {"5880/tcp": "5880; rm"}}},
    {"result": "ok", "data": {"network": {"5880/tcp": 70000}}},
    {"result": "ok", "data": {"network": {"5880/tcp": True}}},
    {"unexpected": "shape"},
])
async def test_port_lookup_failure_falls_back_to_the_default(answer):
    fake = _supervisor(self_info=answer)
    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", "sv-token"), \
         patch("app.ingress.httpx.AsyncClient", fake):
        target = await app.ingress.get_guest_link_target()
    assert target.port == 5880
    assert target.published is True


@pytest.mark.parametrize("hostname", [
    None, "", "has space", "evil.example.com", "a" * 64, "-leading", "quote'd", "<script>",
])
async def test_an_unusable_host_name_falls_back_to_homeassistant_local(hostname):
    """The host name ends up in a link a guest opens, so anything that is not
    plainly one DNS label is refused rather than escaped."""
    fake = _supervisor(self_info=_self_info({"5880/tcp": 8080}), info=_info(hostname))
    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", "sv-token"), \
         patch("app.ingress.httpx.AsyncClient", fake):
        target = await app.ingress.get_guest_link_target()
    # The good half of the answer survives the bad half.
    assert target.base_url == "http://homeassistant.local:8080"


async def test_host_lookup_failure_keeps_the_port():
    fake = _supervisor(self_info=_self_info({"5880/tcp": 8080}), info=httpx.ReadTimeout("slow"))
    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", "sv-token"), \
         patch("app.ingress.httpx.AsyncClient", fake):
        target = await app.ingress.get_guest_link_target()
    assert target.base_url == "http://homeassistant.local:8080"


async def test_failure_keeps_the_last_good_answer_and_retries_sooner():
    fake = _supervisor(self_info=_self_info({"5880/tcp": 8080}), info=_info("cabin"))
    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", "sv-token"), \
         patch("app.ingress.httpx.AsyncClient", fake):
        await app.ingress.get_guest_link_target()
        app.ingress._guest_link_cache_expires = 0.0
        fake.routes["/addons/self/info"] = httpx.ConnectError("refused")
        fake.routes["/info"] = 500
        with patch("app.ingress.time.monotonic", return_value=1000.0):
            target = await app.ingress.get_guest_link_target()
    assert target.base_url == "http://cabin.local:8080"
    assert app.ingress._guest_link_cache_expires == 1000.0 + app.ingress.GUEST_LINK_FAILURE_TTL


# ---------------------------------------------------------------------------
# Dashboard rendering
# ---------------------------------------------------------------------------

async def test_sidebar_dashboard_builds_links_on_the_mapped_port(client, test_db):
    fake = _supervisor(self_info=_self_info({"5880/tcp": 8443}), info=_info("cabin"))
    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", "sv-token"), \
         patch("app.ingress.httpx.AsyncClient", fake), \
         patch.object(settings, "guest_url", ""):
        resp = await client.get("/admin/dashboard", headers={"X-Ingress-Path": INGRESS_PREFIX})
    assert resp.status_code == 200
    assert 'const DIRECT_GUEST_BASE = "http://cabin.local:8443";' in resp.text
    assert "guest-port-warning" not in resp.text


async def test_sidebar_dashboard_warns_when_the_port_is_disabled(client, test_db):
    fake = _supervisor(self_info=_self_info({"5880/tcp": None}))
    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", "sv-token"), \
         patch("app.ingress.httpx.AsyncClient", fake), \
         patch.object(settings, "guest_url", ""):
        resp = await client.get("/admin/dashboard", headers={"X-Ingress-Path": INGRESS_PREFIX})
    assert 'id="guest-port-warning"' in resp.text


async def test_guest_url_wins_and_skips_the_lookup(client, test_db):
    fake = _supervisor()
    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", "sv-token"), \
         patch("app.ingress.httpx.AsyncClient", fake), \
         patch.object(settings, "guest_url", "https://guest.example.com"):
        resp = await client.get("/admin/dashboard", headers={"X-Ingress-Path": INGRESS_PREFIX})
    assert fake.calls == []
    assert 'const GUEST_URL = "https://guest.example.com";' in resp.text
    assert 'const DIRECT_GUEST_BASE = "";' in resp.text


async def test_direct_port_dashboard_does_not_ask_supervisor(client, admin_session):
    """Off the sidebar the admin is already on the guest origin."""
    fake = _supervisor()
    with patch.object(app.ingress, "_SUPERVISOR_TOKEN", "sv-token"), \
         patch("app.ingress.httpx.AsyncClient", fake), \
         patch.object(settings, "guest_url", ""):
        resp = await client.get("/admin/dashboard", cookies=admin_session)
    assert resp.status_code == 200
    assert fake.calls == []
