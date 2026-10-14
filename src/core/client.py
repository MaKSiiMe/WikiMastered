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
_AUTH_COOKIE = re.compile(r"^sb-[a-z0-9]+-auth-token(?:\.(\d+))?$", re.I)
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


def user_id_from_cookie(header: str) -> str | None:
    """Lit user.id dans le cookie Supabase `sb-<ref>-auth-token[.N]` [KZ my-bids-logic.js]."""
    chunks: dict[int, str] = {}
    for name, value in parse_cookie_header(header).items():
        match = _AUTH_COOKIE.match(name)
        if match:
            chunks[int(match.group(1) or 0)] = value
    if not chunks:
        return None
    raw = unquote("".join(v for _, v in sorted(chunks.items())))
    try:
        data = _b64url_json(raw[7:]) if raw.startswith("base64-") else json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
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
        self._degraded_alerted = False
        self._last_rate_alert = 0.0
        # Limitations propres à une route (ex. 403 + Retry-After sur les cotes) : nom -> (fin, échecs)
        self._route_pause: dict[str, tuple[float, int]] = {}
        self._route_alerted: dict[str, float] = {}
        # Routes refusées alors que la session est valide (ex. réservées au PRO) : nom -> jusqu'à
        self._route_forbidden: dict[str, float] = {}
        self._session_ok = asyncio.Event()
        self._session_ok.set()
        self._probe_task: asyncio.Task[None] | None = None
        self._persisted_cookie = ""

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
        if now - self._route_alerted.get(name, 0.0) > 3600:
            self._route_alerted[name] = now
            self.notifier.warning(
                f"Route limitée : {name}",
                f"Le site refuse temporairement `{name}` (403). Seule cette route est en pause "
                f"({delay:.0f} s) ; les autres workers continuent.",
            )
        return delay

    def route_paused_for(self, name: str) -> float:
        until, _ = self._route_pause.get(name, (0.0, 0))
        return max(0.0, until - time.monotonic())

    def _on_success(self) -> None:
        if self._failures >= 3 and self._degraded_alerted:
            self.notifier.info("Serveurs WikiMasters de nouveau joignables")
        self._failures = 0
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
        self.notifier.critical(
            f"Session expirée (HTTP {status})",
            "Tous les workers sont en pause.\n"
            "Copie un nouveau cookie depuis ton navigateur dans `data/session_cookie.txt` "
            "(rechargé à chaud) ou dans WM_COOKIE puis redémarre le conteneur.",
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
            due = time.monotonic() - last_probe >= self.settings.session_recheck_minutes * 60
            if not (reloaded or due):
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
        if self._session_ok.is_set():
            self._consume_cookie_drop()

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
        while True:
            await self._session_ok.wait()
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
                self._backoff(f"{name}: {type(exc).__name__}", limited=False)
                continue
            payload = _json_or_none(resp)
            kind = self._classify(resp, payload)
            if kind == "ok":
                self._on_success()
                self._on_route_success(name)
                return payload
            if kind == "auth":
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
                self._on_auth_lost(name, 403)
                continue
            attempts += 1
            if kind == "client" or attempts >= self.settings.http_get_max_attempts:
                raise ApiError(resp.status_code, payload, name)
            self._backoff(f"{name}: HTTP {resp.status_code}", limited=kind == "rate",
                          retry_after=_retry_after_seconds(resp))

    async def mutate(self, name: str, **values: Any) -> MutationResult:
        req = self.endpoints.build(name, **values)
        if not req.is_mutation:
            raise ValueError(f"{name} n'est pas une écriture")
        await self._session_ok.wait()
        if self.dry_run:
            log.info("[DRY RUN] %s %s %s", req.method, req.url, json.dumps(req.json, ensure_ascii=False))
            return MutationResult(0, None, dry_run=True)
        try:
            resp = await self._send(req)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            self._backoff(f"{name}: {type(exc).__name__}", limited=False)
            raise ApiError(0, None, name, "requête non envoyée (connexion impossible)") from exc
        except httpx.HTTPError as exc:
            self._backoff(f"{name}: {type(exc).__name__}", limited=False)
            raise UncertainMutation(f"{name}: réponse perdue ({type(exc).__name__})") from exc

        payload = _json_or_none(resp)
        kind = self._classify(resp, payload)
        if kind != "ok" and isinstance(payload, dict) and _DOMAIN_KEYS & payload.keys():
            return MutationResult(resp.status_code, payload)
        if kind == "ok":
            self._on_success()
            return MutationResult(resp.status_code, payload)
        if kind == "auth":
            self._on_auth_lost(name, resp.status_code)
            raise SessionExpired(name)
        if kind == "forbidden":
            if await self._probe():
                return MutationResult(resp.status_code, payload)
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
            self._backoff(f"{name}: HTTP {resp.status_code}", limited=False,
                          retry_after=_retry_after_seconds(resp))
            raise UncertainMutation(f"{name}: HTTP {resp.status_code}, résultat à vérifier")
        return MutationResult(resp.status_code, payload)

    async def close(self) -> None:
        if self._probe_task:
            self._probe_task.cancel()
        await self._http.aclose()
