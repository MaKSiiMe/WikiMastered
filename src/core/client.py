"""Client HTTP WikiMasters : session, espacement, backoff et pause globale.

Règles (reprises des garde-fous de WikiRemastered/network.js) :
- une requête à la fois, espacées de HTTP_MIN_SPACING_SECONDS + jitter aléatoire ;
- 429, 5xx, pages HTML de passerelle : pause globale exponentielle, Retry-After respecté ;
- 401 (ou 403 confirmé par une sonde) : tous les workers se figent, alerte Discord,
  puis reprise automatique dès qu'un cookie valide est disponible ;
- une écriture (POST) n'est JAMAIS rejouée : un résultat incertain lève UncertainMutation ;
- DRY_RUN=true : aucune écriture n'est envoyée.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from src.config import Settings
from src.core.endpoints import BuiltRequest, Endpoints
from src.core.notifier import DiscordNotifier

log = logging.getLogger(__name__)

_RATE_HINT = re.compile(r"rate.?limit|too many requests|trop de requ[eê]tes", re.I)
_AUTH_COOKIE = re.compile(r"^sb-([a-z0-9]+)-auth-token(?:\.(\d+))?$", re.I)
SUPABASE_CHUNK_SIZE = 3180  # taille maximale d'un morceau de cookie [@supabase/ssr]
# Réponses métier qui ne sont ni une perte de session ni une limitation globale.
_DOMAIN_KEYS = {"human_verification_required", "rate_limited", "rate_limit_daily", "min"}


class ApiError(Exception):
    def __init__(self, status: int, payload: Any, endpoint: str, message: str = ""):
        self.status = status
        self.payload = payload
        self.endpoint = endpoint
        detail = message or server_message(payload) or f"HTTP {status}"
        super().__init__(f"{endpoint}: {detail}")


class SessionExpired(Exception):
    pass


class RateLimited(Exception):
    pass


class UncertainMutation(Exception):
    """L'écriture a peut-être abouti côté serveur : ne pas la rejouer, vérifier à la main."""


@dataclass
class MutationResult:
    status: int
    payload: Any
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not self.dry_run and 200 <= self.status < 300 and not (
            isinstance(self.payload, dict) and self.payload.get("error")
        )


def server_message(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("error", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip() and "<" not in value:
                return value.strip()[:300]
    return ""


def parse_cookie_header(header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            cookies[name.strip()] = value.strip()
    return cookies


def _b64url_json(data: str) -> Any:
    data += "=" * (-len(data) % 4)
    return json.loads(base64.urlsafe_b64decode(data).decode("utf-8"))


def _auth_cookie(header: str) -> tuple[str, Any] | None:
    """(ref du projet Supabase, contenu décodé) du cookie `sb-<ref>-auth-token[.N]` [KZ my-bids-logic.js]."""
    chunks: dict[int, str] = {}
    ref = ""
    for name, value in parse_cookie_header(header).items():
        match = _AUTH_COOKIE.match(name)
        if match:
            ref = match.group(1)
            chunks[int(match.group(2) or 0)] = value
    if not chunks:
        return None
    raw = unquote("".join(v for _, v in sorted(chunks.items())))
    try:
        return ref, _b64url_json(raw[7:]) if raw.startswith("base64-") else json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


@dataclass
class AuthSession:
    """Session Supabase stockée dans le cookie : access_token, refresh_token, expires_at, user…"""
    ref: str
    data: dict[str, Any]

    @property
    def refresh_token(self) -> str | None:
        token = self.data.get("refresh_token")
        return token if isinstance(token, str) and token else None

    @property
    def expires_at(self) -> float | None:
        exp = self.data.get("expires_at")
        if isinstance(exp, (int, float)):
            return float(exp)
        try:  # sinon, `exp` du jeton d'accès (JWT)
            return float(_b64url_json(str(self.data.get("access_token", "")).split(".")[1])["exp"])
        except (IndexError, KeyError, TypeError, ValueError):
            return None

    def cookies(self) -> dict[str, str]:
        """Cookies au format du site : `base64-` + JSON en base64url, découpé en `.0`, `.1`… si trop long."""
        text = json.dumps(self.data, separators=(",", ":"), ensure_ascii=False)
        value = "base64-" + base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
        name = f"sb-{self.ref}-auth-token"
        if len(value) <= SUPABASE_CHUNK_SIZE:
            return {name: value}
        size = SUPABASE_CHUNK_SIZE
        return {f"{name}.{i}": value[i * size:(i + 1) * size] for i in range((len(value) + size - 1) // size)}


def auth_session_from_cookie(header: str) -> AuthSession | None:
    parsed = _auth_cookie(header)
    if parsed is None or not isinstance(parsed[1], dict):
        return None
    return AuthSession(parsed[0], parsed[1])


def user_id_from_cookie(header: str) -> str | None:
    """Lit user.id dans le cookie Supabase `sb-<ref>-auth-token[.N]` [KZ my-bids-logic.js]."""
    parsed = _auth_cookie(header)
    if parsed is None:
        return None
    data = parsed[1]
    if isinstance(data, dict):
        user = data.get("user")
        if isinstance(user, dict) and isinstance(user.get("id"), str):
            return user["id"].lower()
        token = data.get("access_token")
    elif isinstance(data, list) and data and isinstance(data[0], str):
        token = data[0]
    else:
        return None
    try:
        sub = _b64url_json(str(token).split(".")[1]).get("sub")
        return sub.lower() if isinstance(sub, str) else None
    except (IndexError, ValueError, AttributeError):
        return None


def _retry_after_seconds(resp: httpx.Response) -> float:
    header = resp.headers.get("Retry-After")
    if not header:
        return 0.0
    try:
        return max(0.0, float(header))
    except ValueError:
        pass
    try:
        return max(0.0, (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def _json_or_none(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class WikiMastersClient:
    def __init__(self, settings: Settings, endpoints: Endpoints, notifier: DiscordNotifier):
        self.settings = settings
        self.endpoints = endpoints
        self.notifier = notifier
        self.dry_run = settings.dry_run
        self._host = urlparse(endpoints.base_url).hostname or ""

        headers = {
            "User-Agent": settings.wm_user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Referer": f"{endpoints.base_url}/",
            "Origin": endpoints.base_url,
            **settings.wm_extra_headers,
        }
        if settings.wm_authorization:
            headers["Authorization"] = settings.wm_authorization
        self._http = httpx.AsyncClient(
            http2=True,
            headers=headers,
            timeout=httpx.Timeout(settings.http_timeout_seconds),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=60),
            follow_redirects=False,
        )

        self._slot_lock = asyncio.Lock()
        self._next_slot = 0.0
        self._pause_until = 0.0
        self._failures = 0
        self._server_error_route: str | None = None  # route d'un 500 récent, sans succès depuis
        self._degraded_alerted = False
        # time.monotonic() compte depuis le démarrage de la machine : « jamais » = -inf, pas 0.
        self._last_rate_alert = float("-inf")
        # Limitations propres à une route (ex. 403 + Retry-After sur les cotes) : nom -> (fin, échecs)
        self._route_pause: dict[str, tuple[float, int]] = {}
        self._route_alerted: dict[str, float] = {}
        # Routes refusées alors que la session est valide (ex. réservées au PRO) : nom -> jusqu'à
        self._route_forbidden: dict[str, float] = {}
        self._session_ok = asyncio.Event()
        self._session_ok.set()
        self._probe_task: asyncio.Task[None] | None = None
        self._persisted_cookie = ""
        self._refresh_lock = asyncio.Lock()
        self._refresh_blocked = False
        self._last_refresh = float("-inf")
        self._auth_cache: tuple[str, AuthSession | None] = ("", None)

        cookie = self._initial_cookie()
        self._set_cookies(cookie)
        self.user_id = (settings.wm_user_id or user_id_from_cookie(cookie) or "").lower() or None
        if not cookie:
            log.warning("Aucun cookie WikiMasters configuré (WM_COOKIE) : les routes privées échoueront.")
        if not self.user_id:
            log.warning("Identifiant joueur inconnu : renseigne WM_USER_ID (sert à exclure tes propres ventes).")

    # ------------------------------------------------------------------ cookies
    def _initial_cookie(self) -> str:
        env_cookie = self.settings.wm_cookie.strip()
        try:
            saved = json.loads(self.settings.session_file.read_text(encoding="utf-8"))
            if saved.get("env_hash") == _sha(env_cookie) and saved.get("cookie"):
                log.info("Session rechargée depuis %s", self.settings.session_file)
                return str(saved["cookie"])
        except (OSError, ValueError):
            pass
        return env_cookie

    def _set_cookies(self, header: str) -> None:
        self._http.cookies.clear()
        for name, value in parse_cookie_header(header).items():
            self._http.cookies.set(name, value, domain=self._host, path="/")
        self._persisted_cookie = header
        self._refresh_blocked = False  # nouveau cookie : on peut de nouveau tenter un renouvellement

    def cookie_header(self) -> str:
        values: dict[str, str] = {}
        for cookie in self._http.cookies.jar:
            if cookie.value is not None:
                values[cookie.name] = cookie.value
        return "; ".join(f"{k}={v}" for k, v in values.items())

    def _persist_cookies(self) -> None:
        """Conserve les cookies rafraîchis par le serveur pour survivre aux redémarrages."""
        current = self.cookie_header()
        if not current or current == self._persisted_cookie:
            return
        self._persisted_cookie = current
        new_uid = user_id_from_cookie(current)
        if new_uid and self.user_id and new_uid != self.user_id:
            log.error("Le cookie appartient à un autre compte (%s) : ignoré", new_uid)
            return
        path = self.settings.session_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "env_hash": _sha(self.settings.wm_cookie.strip()),
                "cookie": current,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }), encoding="utf-8")
            tmp.replace(path)
            log.debug("Cookies de session mis à jour")
        except OSError:
            log.exception("Impossible d'enregistrer la session")

    def _consume_cookie_drop(self) -> bool:
        """Charge data/session_cookie.txt s'il existe (remplacement à chaud du cookie)."""
        drop = self.settings.cookie_drop_file
        try:
            header = drop.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        if header.lower().startswith("cookie:"):
            header = header[7:].strip()
        drop.replace(drop.with_suffix(".txt.used"))
        if not header:
            return False
        self._set_cookies(header)
        uid = user_id_from_cookie(header)
        if uid and not self.settings.wm_user_id:
            self.user_id = uid
        self._persisted_cookie = ""
        self._persist_cookies()
        log.info("Nouveau cookie chargé depuis %s", drop)
        return True

    # ------------------------------------------------------- session Supabase
    def auth_session(self) -> AuthSession | None:
        header = self.cookie_header()
        if header != self._auth_cache[0]:
            self._auth_cache = (header, auth_session_from_cookie(header))
        return self._auth_cache[1]

    @property
    def can_refresh(self) -> bool:
        return bool(self.settings.supabase_anon_key) and not self._refresh_blocked

    def session_status(self) -> str:
        session = self.auth_session()
        expires = session.expires_at if session else None
        when = (datetime.fromtimestamp(expires, timezone.utc).astimezone().strftime("%d/%m %H:%M")
                if expires else "inconnue")
        if not self.settings.supabase_anon_key:
            auto = "inactif (SUPABASE_ANON_KEY vide)"
        elif session is None or not session.refresh_token:
            auto = "impossible (pas de refresh_token dans le cookie)"
        else:
            auto = "bloqué (jeton refusé)" if self._refresh_blocked else "actif"
        return f"jeton d'accès valable jusqu'au {when}, renouvellement automatique {auto}"

    def adopt_saved_session(self) -> bool:
        """Reprend la session de data/session.json si elle est plus récente que la nôtre.

        Une commande lancée dans la console (check, inventory…) est un autre processus : si elle
        renouvelle la session, notre refresh_token est consommé et seul ce fichier a le nouveau."""
        try:
            saved = json.loads(self.settings.session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        cookie = saved.get("cookie")
        if (saved.get("env_hash") != _sha(self.settings.wm_cookie.strip()) or not cookie
                or cookie == self._persisted_cookie):
            return False
        theirs, ours = auth_session_from_cookie(cookie), self.auth_session()
        if theirs is None or theirs.expires_at is None:
            return False
        if ours is not None and ours.expires_at is not None and theirs.expires_at <= ours.expires_at:
            return False
        uid = user_id_from_cookie(cookie)
        if uid and self.user_id and uid != self.user_id:
            return False
        self._set_cookies(cookie)
        log.info("Session plus récente reprise depuis %s", self.settings.session_file)
        return True

    async def _maybe_refresh(self) -> None:
        """Renouvelle la session avant l'expiration du jeton d'accès (comme le fait le navigateur)."""
        if not self.can_refresh:
            return
        session = self.auth_session()
        expires = session.expires_at if session else None
        if expires is not None and expires - time.time() <= self.settings.session_refresh_margin_minutes * 60:
            await self.refresh_session()

    def _apply_auth(self, session: AuthSession) -> None:
        prefix = f"sb-{session.ref}-auth-token"
        for cookie in list(self._http.cookies.jar):
            if cookie.name == prefix or cookie.name.startswith(prefix + "."):
                self._http.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)
        for name, value in session.cookies().items():
            self._http.cookies.set(name, value, domain=self._host, path="/")

    async def refresh_session(self) -> bool:
        """Échange le refresh_token contre une nouvelle session (API Supabase /auth/v1/token).

        Le refresh_token est à usage unique : si ton navigateur utilise la même session, l'un des
        deux finit par présenter un jeton déjà consommé et Supabase le refuse."""
        async with self._refresh_lock:
            if time.monotonic() - self._last_refresh < 30:
                return True  # une autre tâche vient de le faire
            if self.adopt_saved_session():
                session = self.auth_session()
                margin = self.settings.session_refresh_margin_minutes * 60
                if session and session.expires_at and session.expires_at - time.time() > margin:
                    return True
            session = self.auth_session()
            if not self.can_refresh or session is None or not session.refresh_token:
                return False
            key = self.settings.supabase_anon_key
            base = (self.settings.supabase_url or f"https://{session.ref}.supabase.co").rstrip("/")
            try:
                resp = await self._http.post(
                    f"{base}/auth/v1/token", params={"grant_type": "refresh_token"},
                    json={"refresh_token": session.refresh_token},
                    headers={"apikey": key, "Authorization": f"Bearer {key}"},
                )
            except httpx.HTTPError as exc:
                log.warning("Renouvellement de session impossible pour l'instant (%s)", type(exc).__name__)
                return False
            payload = _json_or_none(resp)
            if (resp.status_code != 200 or not isinstance(payload, dict)
                    or not payload.get("access_token") or not payload.get("refresh_token")):
                detail = ""
                if isinstance(payload, dict):
                    detail = str(payload.get("error_description") or payload.get("msg")
                                 or payload.get("error") or "")[:200]
                log.warning("Renouvellement de session refusé : HTTP %s %s", resp.status_code, detail)
                if 400 <= resp.status_code < 500:
                    self._refresh_blocked = True
                    self.notifier.critical(
                        "Session : renouvellement refusé",
                        f"Supabase refuse le jeton de renouvellement (HTTP {resp.status_code} {detail}).\n"
                        "Cause la plus fréquente : ton navigateur et le bot partagent la même session. "
                        "Donne au bot sa propre session (docs/PORTAINER.md, « Session autonome »), "
                        "puis dépose le nouveau cookie.",
                    )
                return False
            data = dict(payload)
            if not isinstance(data.get("expires_at"), (int, float)) and isinstance(data.get("expires_in"), (int, float)):
                data["expires_at"] = int(time.time() + data["expires_in"])
            if "user" not in data and "user" in session.data:
                data["user"] = session.data["user"]
            self._apply_auth(AuthSession(session.ref, data))
            self._last_refresh = time.monotonic()
            self._persist_cookies()
            log.info("Session renouvelée (%s)", self.session_status())
            return True

    async def supabase(self, method: str, table: str, *, params: dict[str, str] | None = None,
                       json_body: Any = None, prefer: str = "") -> tuple[int, Any]:
        """Requête à l'API REST Supabase du site (tables `tags`, `user_card_tags`), comme son éditeur
        d'étiquettes [WP tags.js]. Nécessite SUPABASE_ANON_KEY et le jeton d'accès de la session."""
        await self._maybe_refresh()
        session = self.auth_session()
        key = self.settings.supabase_anon_key
        if not key or session is None or not session.data.get("access_token"):
            raise ApiError(0, None, f"supabase:{table}", "SUPABASE_ANON_KEY ou session Supabase manquante")
        await self._acquire_slot()
        base = (self.settings.supabase_url or f"https://{session.ref}.supabase.co").rstrip("/")
        headers = {"apikey": key, "Authorization": f"Bearer {session.data['access_token']}",
                   "Accept": "application/json"}
        if prefer:
            headers["Prefer"] = prefer
        try:
            resp = await self._http.request(method, f"{base}/rest/v1/{table}", params=params, json=json_body,
                                            headers=headers)
        except httpx.HTTPError as exc:
            raise ApiError(0, None, f"supabase:{table}", f"réseau : {type(exc).__name__}") from exc
        return resp.status_code, _json_or_none(resp)

    # ------------------------------------------------------------ espacement
    async def _acquire_slot(self) -> None:
        async with self._slot_lock:
            while True:
                wait = max(self._next_slot, self._pause_until) - time.monotonic()
                if wait <= 0:
                    break
                await asyncio.sleep(wait)
            self._next_slot = (
                time.monotonic()
                + self.settings.http_min_spacing_seconds
                + random.uniform(0, self.settings.http_jitter_seconds)
            )

    async def _send(self, req: BuiltRequest) -> httpx.Response:
        await self._acquire_slot()
        resp = await self._http.request(
            req.method, req.url,
            params=req.params or None,
            json=req.json,
            headers=req.headers or None,
        )
        return resp

    def _backoff(self, reason: str, *, limited: bool, retry_after: float = 0.0) -> float:
        self._failures = min(self._failures + 1, 10)
        base = self.settings.backoff_rate_limit_base_seconds if limited else self.settings.backoff_base_seconds
        delay = min(self.settings.backoff_max_seconds, base * 2 ** (self._failures - 1))
        delay += random.uniform(0, delay * 0.2)
        delay = max(delay, retry_after)
        self._pause_until = max(self._pause_until, time.monotonic() + delay)
        log.warning("Pause réseau %.0f s (%s, échec #%d)", delay, reason, self._failures)
        now = time.monotonic()
        if limited and now - self._last_rate_alert > 900:
            self._last_rate_alert = now
            self.notifier.critical(
                "Limitation de requêtes (429)",
                f"WikiMasters limite le bot : tous les workers sont en pause {delay:.0f} s.\n"
                "Si ça se répète, augmente `HTTP_MIN_SPACING_SECONDS` et `SNIPER_POLL_MIN_SECONDS`.",
                cause=reason,
            )
        elif not limited and self._failures >= 3 and not self._degraded_alerted:
            self._degraded_alerted = True
            self.notifier.warning("Serveurs WikiMasters instables", f"{reason} — backoff {delay:.0f} s")
        return delay

    def _route_backoff(self, name: str, retry_after: float) -> float:
        """Pause limitée à une route : les autres workers continuent."""
        _, failures = self._route_pause.get(name, (0.0, 0))
        failures = min(failures + 1, 10)
        delay = min(self.settings.backoff_max_seconds,
                    self.settings.backoff_rate_limit_base_seconds * 2 ** (failures - 1))
        delay = max(delay + random.uniform(0, delay * 0.2), retry_after)
        self._route_pause[name] = (time.monotonic() + delay, failures)
        log.warning("Route %s limitée par le site : pause %.0f s (échec #%d)", name, delay, failures)
        now = time.monotonic()
        if now - self._route_alerted.get(name, float("-inf")) > 3600:
            self._route_alerted[name] = now
            self.notifier.warning(
                f"Route limitée : {name}",
                f"Le site refuse temporairement `{name}` (403). Seule cette route est en pause "
                f"({delay:.0f} s) ; les autres workers continuent.",
            )
        return delay

    def _route_server_error(self, name: str, cause: str) -> None:
        """Erreur serveur isolée : seule cette route attend, les autres workers (sniper) continuent."""
        self._server_error_route = name
        _, failures = self._route_pause.get(name, (0.0, 0))
        failures = min(failures + 1, 10)
        delay = min(self.settings.backoff_max_seconds, self.settings.backoff_base_seconds * 2 ** (failures - 1))
        delay += random.uniform(0, delay * 0.2)
        self._route_pause[name] = (time.monotonic() + delay, failures)
        log.warning("Route %s en pause %.0f s (%s, échec #%d) ; les autres routes continuent",
                    name, delay, cause, failures)

    def _fail_route_or_all(self, name: str, cause: str, retry_after: float = 0.0) -> None:
        """Panne d'écriture : isolée (une seule route, sans autre panne en cours), seule cette route attend
        (ex. ouverture de paquets en panne : le sniper continue) ; sinon, pause générale."""
        if self._failures == 0 and self._server_error_route in (None, name):
            self._route_server_error(name, cause)
        else:
            self._backoff(f"{name}: {cause}", limited=False, retry_after=retry_after)

    def route_paused_for(self, name: str) -> float:
        until, _ = self._route_pause.get(name, (0.0, 0))
        return max(0.0, until - time.monotonic())

    def _on_success(self) -> None:
        if self._failures >= 3 and self._degraded_alerted:
            self.notifier.info("Serveurs WikiMasters de nouveau joignables")
        self._failures = 0
        self._server_error_route = None
        self._degraded_alerted = False
        self._persist_cookies()

    def _on_route_success(self, name: str) -> None:
        self._route_pause.pop(name, None)

    @staticmethod
    def _classify(resp: httpx.Response, payload: Any) -> str:
        status = resp.status_code
        text_hint = server_message(payload) or ("" if payload is not None else resp.text[:300])
        if 300 <= status < 400 or status == 401:
            return "auth"
        if status == 429:
            return "rate"
        if status == 403 and (resp.headers.get("Retry-After") or _RATE_HINT.search(text_hint)):
            return "route_rate"
        if status == 403:
            return "forbidden"
        if status >= 500:
            return "server"
        if 200 <= status < 300:
            # Une page HTML (Cloudflare, maintenance) au lieu de JSON = panne de passerelle.
            return "ok" if payload is not None else "server"
        return "client"

    # ----------------------------------------------------------- session
    def _on_auth_lost(self, endpoint: str, status: int) -> None:
        if not self._session_ok.is_set():
            return
        self._session_ok.clear()
        hint = ("" if self.settings.supabase_anon_key else
                "\n\nPour que ça ne se reproduise plus : ajoute SUPABASE_ANON_KEY dans la stack, avec un cookie "
                "pris dans une fenêtre privée (docs/PORTAINER.md, « Session autonome »). Le bot renouvellera "
                "alors sa session lui-même.")
        self.notifier.critical(
            f"Session expirée (HTTP {status})",
            "Tous les workers sont en pause.\n"
            "Copie un nouveau cookie depuis ton navigateur dans `data/session_cookie.txt` "
            "(rechargé à chaud) ou dans WM_COOKIE puis redémarre le conteneur." + hint,
            endpoint=endpoint,
        )
        if self._probe_task is None or self._probe_task.done():
            self._probe_task = asyncio.create_task(self._probe_loop(), name="session-probe")

    async def _probe(self) -> bool:
        try:
            resp = await self._send(self.endpoints.build("balance"))
        except httpx.HTTPError:
            return False
        payload = _json_or_none(resp)
        return resp.status_code == 200 and isinstance(payload, dict) and "balance" in payload

    async def _probe_loop(self) -> None:
        last_probe = time.monotonic()
        while not self._session_ok.is_set():
            await asyncio.sleep(30)
            reloaded = self._consume_cookie_drop()
            refreshed = not reloaded and (self.adopt_saved_session()
                                          or (self.can_refresh and await self.refresh_session()))
            due = time.monotonic() - last_probe >= self.settings.session_recheck_minutes * 60
            if not (reloaded or refreshed or due):
                continue
            last_probe = time.monotonic()
            if await self._probe():
                self._session_ok.set()
                self._persist_cookies()
                self.notifier.success("Session rétablie", "Les workers reprennent.")

    async def wait_session(self) -> None:
        await self._session_ok.wait()

    @property
    def session_ok(self) -> bool:
        return self._session_ok.is_set()

    def check_cookie_drop(self) -> None:
        """Appelé périodiquement : un cookie déposé est pris en compte même sans panne."""
        if self._session_ok.is_set() and not self._consume_cookie_drop():
            self.adopt_saved_session()

    # ------------------------------------------------------------- appels
    async def get(self, name: str, *, wait: bool = True, **values: Any) -> Any:
        """GET avec reprises. `wait=False` : lève RateLimited au lieu d'attendre la fin
        d'une limitation propre à cette route (utile au sniper, qui ne peut pas attendre)."""
        req = self.endpoints.build(name, **values)
        if req.is_mutation:
            raise ValueError(f"{name} est une écriture : utiliser mutate()")
        if self._route_forbidden.get(name, 0.0) > time.monotonic():
            raise ApiError(403, None, name, "route refusée par le site (mémorisé)")
        attempts = 0
        refreshed = False
        while True:
            await self._session_ok.wait()
            await self._maybe_refresh()
            paused = self.route_paused_for(name)
            if paused > 0:
                if not wait:
                    raise RateLimited(name)
                await asyncio.sleep(paused)
            try:
                resp = await self._send(req)
            except httpx.HTTPError as exc:
                attempts += 1
                if attempts >= self.settings.http_get_max_attempts:
                    raise ApiError(0, None, name, f"réseau : {exc!r}") from exc
                if isinstance(exc, httpx.ReadTimeout) and self._failures == 0 \
                        and self._server_error_route in (None, name):
                    # Une route lente (souvent la cote d'une carte) ne fige pas tout le bot, comme un 500 isolé.
                    self._route_server_error(name, type(exc).__name__)
                    continue
                self._backoff(f"{name}: {type(exc).__name__}", limited=False)
                continue
            payload = _json_or_none(resp)
            kind = self._classify(resp, payload)
            if kind == "ok":
                self._on_success()
                self._on_route_success(name)
                return payload
            if kind == "auth":
                if not refreshed and await self.refresh_session():
                    refreshed = True
                    continue
                self._on_auth_lost(name, resp.status_code)
                continue
            if kind == "route_rate":
                self._route_backoff(name, _retry_after_seconds(resp))
                continue
            if kind == "forbidden":
                # 403 peut être un droit manquant (route PRO) : on ne fige tout que si la session est morte.
                if name != "balance" and await self._probe():
                    self._route_forbidden[name] = time.monotonic() + 6 * 3600
                    log.info("route %s refusée (session valide) : ignorée pendant 6 h", name)
                    raise ApiError(403, payload, name, "accès refusé à cette route (session valide)")
                if not refreshed and await self.refresh_session():
                    refreshed = True
                    continue
                self._on_auth_lost(name, 403)
                continue
            attempts += 1
            if kind == "client" or attempts >= self.settings.http_get_max_attempts:
                raise ApiError(resp.status_code, payload, name)
            if kind == "server" and self._failures == 0 and self._server_error_route in (None, name):
                # Un 500 sur une seule route (souvent la cote d'une carte) ne fige pas tout le bot ;
                # des 500 sur deux routes, sans succès entre les deux, déclenchent la pause générale.
                self._route_server_error(name, f"HTTP {resp.status_code}")
                continue
            self._backoff(f"{name}: HTTP {resp.status_code}", limited=kind == "rate",
                          retry_after=_retry_after_seconds(resp))

    def _capture_write(self, name: str, status: int, payload: Any) -> None:
        """Garde les 3 premières vraies réponses de chaque écriture (data/inspect/writes/) : leur
        format n'est pas encore vérifié. La première est résumée sur Discord."""
        folder = self.settings.data_dir / "inspect" / "writes"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            n = len(list(folder.glob(f"{name}-*.json")))
            if n >= 3:
                return
            (folder / f"{name}-{n + 1}.json").write_text(
                json.dumps({"status": status, "payload": payload, "at": datetime.now(timezone.utc).isoformat()},
                           ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except OSError:
            log.exception("Impossible d'enregistrer la réponse de %s", name)
            return
        if n == 0:
            keys = ", ".join(sorted(payload)) if isinstance(payload, dict) else type(payload).__name__
            self.notifier.info(f"Première réponse réelle : {name}",
                               f"HTTP {status}, clés : {keys or '—'}\n{server_message(payload)}\n"
                               f"Détail : data/inspect/writes/{name}-1.json")

    async def mutate(self, name: str, *, live: bool | None = None, **values: Any) -> MutationResult:
        """Écriture ; `live` force réel/simulé (sinon DRY_RUN, LIVE_WRITES et SIMULATE décident)."""
        req = self.endpoints.build(name, **values)
        if not req.is_mutation:
            raise ValueError(f"{name} n'est pas une écriture")
        await self._session_ok.wait()
        await self._maybe_refresh()
        if not (self.settings.is_live(name) if live is None else live):
            log.info("[DRY RUN] %s %s %s", req.method, req.url, json.dumps(req.json, ensure_ascii=False))
            return MutationResult(0, None, dry_run=True)
        if self.route_paused_for(name) > 0:
            raise RateLimited(name)  # route en pause (erreur ou limite) : rien n'est envoyé
        try:
            resp = await self._send(req)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            self._backoff(f"{name}: {type(exc).__name__}", limited=False)
            raise ApiError(0, None, name, "requête non envoyée (connexion impossible)") from exc
        except httpx.HTTPError as exc:
            self._fail_route_or_all(name, type(exc).__name__)
            raise UncertainMutation(f"{name}: réponse perdue ({type(exc).__name__})") from exc

        payload = _json_or_none(resp)
        kind = self._classify(resp, payload)
        if kind in ("ok", "client", "forbidden"):  # vraie réponse du site, pas une panne ni un 401/429
            self._capture_write(name, resp.status_code, payload if payload is not None else resp.text[:2000])
        if kind != "ok" and isinstance(payload, dict) and _DOMAIN_KEYS & payload.keys():
            return MutationResult(resp.status_code, payload)
        if kind == "ok":
            self._on_success()
            return MutationResult(resp.status_code, payload)
        # Refusée faute de session : rien n'a été écrit. Après renouvellement, le worker
        # refera sa décision au passage suivant ; l'écriture n'est pas rejouée ici.
        if kind == "auth":
            if not await self.refresh_session():
                self._on_auth_lost(name, resp.status_code)
            raise SessionExpired(name)
        if kind == "forbidden":
            if await self._probe():
                return MutationResult(resp.status_code, payload)
            if not await self.refresh_session():
                self._on_auth_lost(name, 403)
            raise SessionExpired(name)
        if kind == "route_rate":
            self._route_backoff(name, _retry_after_seconds(resp))
            raise RateLimited(name)
        if kind == "rate":
            self._backoff(f"{name}: HTTP {resp.status_code}", limited=True,
                          retry_after=_retry_after_seconds(resp))
            raise RateLimited(name)
        if kind == "server":
            self._fail_route_or_all(name, f"HTTP {resp.status_code}", _retry_after_seconds(resp))
            raise UncertainMutation(f"{name}: HTTP {resp.status_code}, résultat à vérifier")
        return MutationResult(resp.status_code, payload)

    async def close(self) -> None:
        if self._probe_task:
            self._probe_task.cancel()
        await self._http.aclose()
