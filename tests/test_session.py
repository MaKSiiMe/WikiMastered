"""Session autonome : renouvellement Supabase avant expiration ou sur 401, jamais de rejeu d'écriture."""

from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx
import pytest

from src.core.api import WikiMastersAPI
from src.core.client import AuthSession, SessionExpired, auth_session_from_cookie, user_id_from_cookie
from tests.test_integration import ME, make_client

REF = "cyrxjeppjqsxxjayfrur"
TOKEN_URL = f"https://{REF}.supabase.co/auth/v1/token"


def cookie_for(access: str, refresh: str, expires_in: float, pad: int = 0) -> str:
    session = AuthSession(REF, {"access_token": access, "refresh_token": refresh, "token_type": "bearer",
                                "expires_at": int(time.time() + expires_in), "user": {"id": ME, "pad": "x" * pad}})
    return "; ".join(f"{k}={v}" for k, v in session.cookies().items())


def test_auth_cookie_round_trip_and_chunks():
    big = cookie_for("a1", "r1", 3600, pad=5000)  # plus de 3180 caractères : découpé en .0, .1…
    assert f"sb-{REF}-auth-token.0=" in big and f"sb-{REF}-auth-token.1=" in big
    session = auth_session_from_cookie(big)
    assert session.ref == REF and session.refresh_token == "r1" and session.expires_at > time.time()
    assert user_id_from_cookie(big) == ME


class FakeSupabase:
    """Faux Supabase + faux site : le site n'accepte que le jeton d'accès courant."""

    def __init__(self, valid_access: str, *, refuse: bool = False):
        self.valid = valid_access
        self.refuse = refuse
        self.refreshes: list[str] = []
        self.writes = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(TOKEN_URL):
            assert request.url.params["grant_type"] == "refresh_token"
            assert request.headers["apikey"] == "anon-key"
            assert "sb-" not in request.headers.get("cookie", "")  # les cookies du site ne partent pas chez Supabase
            token = json.loads(request.content)["refresh_token"]
            self.refreshes.append(token)
            if self.refuse:
                return httpx.Response(400, json={"error": "invalid_grant",
                                                 "error_description": "Invalid Refresh Token: Already Used"})
            n = len(self.refreshes)
            self.valid = f"a{n + 1}"
            return httpx.Response(200, json={"access_token": self.valid, "refresh_token": f"r{n + 1}",
                                             "token_type": "bearer", "expires_in": 3600, "user": {"id": ME}})
        session = auth_session_from_cookie(request.headers.get("cookie", ""))
        if session is None or session.data.get("access_token") != self.valid:
            return httpx.Response(401, json={"error": "unauthorized"})
        if request.method == "POST":
            self.writes += 1
            return httpx.Response(200, json={"current_bid": 5, "bidder_balance": 1})
        return httpx.Response(200, json={"balance": 42})


@pytest.mark.asyncio
async def test_session_refreshed_before_expiry(tmp_path):
    server = FakeSupabase("a1")
    client, notifier = make_client(tmp_path, server, wm_cookie=cookie_for("a1", "r1", 120),
                                   supabase_anon_key="anon-key")
    assert await WikiMastersAPI(client).balance() == 42
    assert server.refreshes == ["r1"]  # expirait dans 2 min (< marge de 10 min)
    assert await WikiMastersAPI(client).balance() == 42
    assert server.refreshes == ["r1"]  # nouveau jeton valable 1 h : pas de second renouvellement
    saved = json.loads((tmp_path / "session.json").read_text())["cookie"]
    assert auth_session_from_cookie(saved).refresh_token == "r2"  # survit à un redémarrage
    assert "actif" in client.session_status()
    assert not notifier.sent


@pytest.mark.asyncio
async def test_401_refreshes_once_and_never_replays_a_write(tmp_path):
    server = FakeSupabase("autre")  # le site a déjà invalidé notre jeton d'accès
    client, notifier = make_client(tmp_path, server, wm_cookie=cookie_for("a1", "r1", 3600),
                                   supabase_anon_key="anon-key", dry_run=False)
    with pytest.raises(SessionExpired):
        await WikiMastersAPI(client).place_bid("abc", 5)
    assert server.writes == 0 and server.refreshes == ["r1"] and client.session_ok
    assert await WikiMastersAPI(client).balance() == 42  # nouveau jeton : tout repart
    assert not notifier.sent


@pytest.mark.asyncio
async def test_refused_refresh_pauses_with_clear_alert(tmp_path, monkeypatch):
    server = FakeSupabase("autre", refuse=True)
    client, notifier = make_client(tmp_path, server, wm_cookie=cookie_for("a1", "r1", 3600),
                                   supabase_anon_key="anon-key")
    real_sleep = asyncio.sleep
    monkeypatch.setattr("src.core.client.asyncio.sleep", lambda s: real_sleep(0.01))
    task = asyncio.create_task(WikiMastersAPI(client).balance())
    await real_sleep(0.1)
    assert not client.session_ok and not task.done()
    titles = [t for _, t in notifier.sent]
    assert "Session : renouvellement refusé" in titles and "Session expirée (HTTP 401)" in titles
    assert server.refreshes == ["r1"]  # bloqué ensuite : pas d'insistance
    task.cancel()
    await client.close()


@pytest.mark.asyncio
async def test_without_anon_key_nothing_changes(tmp_path):
    server = FakeSupabase("a1")
    client, _ = make_client(tmp_path, server, wm_cookie=cookie_for("a1", "r1", 60))
    assert await WikiMastersAPI(client).balance() == 42
    assert server.refreshes == [] and "inactif" in client.session_status()


@pytest.mark.asyncio
async def test_bot_adopts_session_renewed_by_a_console_command(tmp_path):
    """`check` lancé dans la console renouvelle la session : le bot reprend la nouvelle au lieu de
    présenter un refresh_token déjà consommé."""
    server = FakeSupabase("a1")
    cookie = cookie_for("a1", "r1", 120)
    bot, _ = make_client(tmp_path, server, wm_cookie=cookie, supabase_anon_key="anon-key")
    console, _ = make_client(tmp_path, server, wm_cookie=cookie, supabase_anon_key="anon-key")
    assert await WikiMastersAPI(console).balance() == 42
    assert await WikiMastersAPI(bot).balance() == 42
    assert server.refreshes == ["r1"]  # un seul renouvellement
    assert bot.auth_session().refresh_token == "r2"


def jwt_for(**claims) -> str:
    encode = lambda data: base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    return f"{encode({'alg': 'HS256'})}.{encode(claims)}.signaturesignature"


ANON = jwt_for(role="anon", ref=REF)
PAGE = '<script src="/_next/static/chunks/a.js?dpl=1"></script><script src="/_next/static/chunks/b.js"></script>'


def site(seen: list[str], scripts: dict[str, str], page: str = PAGE, status: int = 200):
    def handler(request):
        assert "cookie" not in request.headers  # page publique : jamais la session
        seen.append(str(request.url))
        if request.url.path == "/login":
            return httpx.Response(status, text=page)
        return httpx.Response(200, text=scripts.get(request.url.path, ""))
    return handler


def public_client(tmp_path, handler, **settings):
    client, _ = make_client(tmp_path, handler, wm_cookie=cookie_for("a1", "r1", 3600), **settings)
    client._public_http = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


@pytest.mark.asyncio
async def test_public_key_is_found_in_the_site_scripts_and_kept(tmp_path):
    other = jwt_for(role="anon", ref="autre") + " " + jwt_for(role="service_role", ref=REF)
    seen: list[str] = []
    scripts = {"/_next/static/chunks/a.js": other, "/_next/static/chunks/b.js": f'x="{ANON}"'}
    client = public_client(tmp_path, site(seen, scripts))
    await client.discover_anon_key()
    assert client.settings.supabase_anon_key == ANON
    assert (tmp_path / "supabase_anon_key").read_text() == ANON
    assert all(url.startswith(client.endpoints.base_url) for url in seen) and len(seen) == 3
    await client.close()

    # Site indisponible au redémarrage : la clé trouvée reste en place.
    client = public_client(tmp_path, site([], {}, status=503))
    assert client.settings.supabase_anon_key == ANON
    await client.discover_anon_key()
    assert client.settings.supabase_anon_key == ANON
    await client.close()


@pytest.mark.asyncio
async def test_public_key_set_by_hand_means_no_request(tmp_path):
    seen: list[str] = []
    client = public_client(tmp_path, site(seen, {}), supabase_anon_key="a-la-main")
    await client.discover_anon_key()
    assert seen == [] and client.settings.supabase_anon_key == "a-la-main"
    await client.close()


@pytest.mark.asyncio
async def test_public_key_not_found_leaves_the_bot_without_it(tmp_path):
    client = public_client(tmp_path, site([], {}))
    await client.discover_anon_key()
    assert client.settings.supabase_anon_key == "" and not client.can_refresh
    await client.close()
