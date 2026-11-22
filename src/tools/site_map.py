"""Carte du site et changements depuis la carte précédente (à lancer après une mise à jour du site).

    pip install -r requirements-tools.txt && playwright install chromium
    python -m src.tools.site_map --import-cookie FICHIER   # une fois : session prise dans ton navigateur
    python -m src.tools.site_map                           # ensuite : ouvre les pages avec ce profil, sans fenêtre

La connexion se fait dans ton navigateur habituel : Cloudflare refuse la vérification de la page de connexion dans
un navigateur piloté (relevé le 22/11), et on ne cherche pas à le tromper. Dans une fenêtre privée, connecte-toi,
puis dans la console (F12) : le fichier téléchargé contient `document.cookie` (cookie de session Supabase).

    a=document.createElement('a');a.href=URL.createObjectURL(new Blob([document.cookie]));a.download='wm-session.txt';a.click()

Ferme ensuite la fenêtre privée sans te déconnecter (se déconnecter fermerait aussi cette session-là). Le cookie est
gardé dans le profil local (data/browser-profile), que le navigateur de l'outil tient à jour à chaque passage.

Lecture seule : les pages sont ouvertes comme par un joueur, sans aucun clic ; seules les requêtes que la page fait
d'elle-même partent. Images, médias et polices ne sont pas chargés. Les fichiers JS (publics) sont gardés pour lire le
contexte d'une nouveauté.

Relevé :
- pages visitées (liens de chaque page et chemins trouvés dans le JS, une ou deux par motif) ;
- appels faits par les pages : méthode, route (identifiants remplacés par {id}), paramètres, forme de la réponse
  (clés et types, jamais les valeurs) ;
- dans le JS : routes /api/, tables, fonctions et canaux Supabase, événements temps réel, constantes en snake_case
  (types de notification, codes d'erreur…).

Résultat dans data/site-map/ (non versionné) : <horodatage>.json (la carte), <horodatage>.md (ce qui a changé depuis
la carte précédente, avec un extrait du JS autour de chaque nouveauté) et js/<fichier> (fichiers JS).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

BASE_URL = "https://www.wiki-masters.com"
SEEDS = ["/", "/marketplace", "/collection", "/packs", "/trades", "/profile", "/achievements", "/friends", "/guilds",
         "/wishlist", "/leaderboard", "/settings", "/shop", "/pro", "/notifications", "/chat", "/rules", "/battle",
         "/dms", "/guild", "/iceberg", "/pulls"]
NEVER = ("/logout", "/signout", "/api/", "/_next/", "/auth/")  # pages jamais ouvertes
MAX_PAGES = 45
PER_PATTERN = 2          # pages ouvertes au plus par motif (/profile/{x} : deux profils suffisent)
PAGE_PAUSE = 3.0         # secondes entre deux pages
SETTLE_MS = 4000         # attente des requêtes de la page après son chargement
MAX_KEYS = 30            # au-delà, un objet est vu comme un dictionnaire (clés = identifiants, raretés…)

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
NUMBER = re.compile(r"(?<=/)\d+(?=/|$)")
TEMPLATE = re.compile(r"\$\{[^}]*\}")
API_STRING = re.compile(r"[`\"'](/api/[A-Za-z0-9_\-/.${}()?=&:+]*)")
PAGE_STRING = re.compile(r"[`\"'](/[a-z][a-z0-9\-]*(?:/[a-z0-9\-\[\]{}$.]+)*)/?[`\"'?]")
TABLE = re.compile(r"\.from\(\s*[\"'`]([A-Za-z0-9_]+)[\"'`]\s*\)")
RPC = re.compile(r"\.rpc\(\s*[\"'`]([A-Za-z0-9_]+)[\"'`]")
CHANNEL = re.compile(r"\.channel\(\s*([`\"'][^`\"']*[`\"'])")
BROADCAST = re.compile(r"[\"']broadcast[\"']\s*,\s*\{\s*event\s*:\s*[\"']([^\"']+)[\"']")
PG_CHANGES = re.compile(r"[\"']postgres_changes[\"']\s*,\s*\{[^}]*?table\s*:\s*[\"']([^\"']+)[\"']")
CONSTANT = re.compile(r"[\"']([a-z][a-z0-9]*(?:_[a-z0-9]+){2,})[\"']")
SETS = ("api_strings", "tables", "rpc", "channels", "broadcast", "postgres_changes", "page_strings", "constants")


def template_path(path: str) -> str:
    """/api/marketplace/cards/<uuid>/sales -> /api/marketplace/cards/{id}/sales ; ${x} et nombres pareils."""
    path = TEMPLATE.sub("{id}", UUID.sub("{id}", path))
    return NUMBER.sub("{n}", path)


def pattern(path: str) -> str:
    """Motif d'une page : identifiants remplacés, et au-delà du premier segment, chaque segment est variable."""
    parts = template_path(path).strip("/").split("/")
    return "/" + "/".join(parts[:1] + ["{x}" for _ in parts[1:]]) if parts != [""] else "/"


def shape(value: Any, depth: int = 0) -> Any:
    """Forme d'une réponse : clés et types, sans aucune valeur."""
    if depth > 8:
        return "…"
    if isinstance(value, dict):
        if len(value) > MAX_KEYS:
            first = next(iter(value.values()))
            return {f"<{len(value)} clés>": shape(first, depth + 1)}
        return {k: shape(v, depth + 1) for k, v in sorted(value.items())}
    if isinstance(value, list):
        merged: dict[str, Any] = {}
        for item in value[:20]:  # éléments de formes différentes (champs facultatifs) : union des clés
            s = shape(item, depth + 1)
            if not isinstance(s, dict):
                return [s]
            merged.update(s)
        return [dict(sorted(merged.items()))] if merged else []
    if value is None:
        return "null"
    return type(value).__name__


def extract(text: str) -> dict[str, set[str]]:
    """Ce qu'un fichier JS révèle du site."""
    pages = {p for p in PAGE_STRING.findall(text) if not p.startswith(NEVER) and "." not in p.split("/")[-1]
             and len(p) < 60}
    return {
        "api_strings": {template_path(s.rstrip("?&")) for s in API_STRING.findall(text)},
        "tables": set(TABLE.findall(text)),
        "rpc": set(RPC.findall(text)),
        "channels": {TEMPLATE.sub("{id}", c.strip("`\"'")) for c in CHANNEL.findall(text)},
        "broadcast": set(BROADCAST.findall(text)),
        "postgres_changes": set(PG_CHANGES.findall(text)),
        "page_strings": {template_path(p) for p in pages},
        "constants": set(CONSTANT.findall(text)),
    }


def analyse_js(files: dict[str, str]) -> dict[str, dict[str, list[str]]]:
    """{catégorie: {valeur: [fichiers où elle apparaît]}}."""
    found: dict[str, dict[str, set[str]]] = {key: {} for key in SETS}
    for name, text in files.items():
        for key, values in extract(text).items():
            for value in values:
                found[key].setdefault(value, set()).add(name)
    return {key: {v: sorted(f) for v, f in sorted(values.items())} for key, values in found.items()}


def context(text: str, needle: str, width: int = 160) -> str:
    i = text.find(needle)
    if i < 0:
        return ""
    return re.sub(r"\s+", " ", text[max(0, i - width):i + len(needle) + width])


def diff(old: dict | None, new: dict, js_text: dict[str, str]) -> str:
    """Ce qui a changé entre deux cartes, en Markdown."""
    lines = [f"# Carte du site du {new['taken_at']}", ""]
    if not old:
        lines += ["Première carte : rien à comparer.", ""]
        old = {}
    else:
        lines += [f"Comparée à celle du {old.get('taken_at')} (déploiement {old.get('dpl')} → {new.get('dpl')}).", ""]
    old_js, new_js = old.get("js", {}), new.get("js", {})
    changed = [n for n in new_js if n in old_js and old_js[n] != new_js[n]]
    lines.append(f"Fichiers JS : {len(new_js)} ({len(set(new_js) - set(old_js))} nouveaux, "
                 f"{len(set(old_js) - set(new_js))} disparus, {len(changed)} modifiés).")
    lines.append("")
    for key in SETS:
        before, after = set(old.get(key, {})), set(new.get(key, {}))
        added, removed = sorted(after - before), sorted(before - after)
        if not old or not (added or removed):
            continue
        lines.append(f"## {key} : +{len(added)} / -{len(removed)}")
        for value in added:
            files = new[key][value]
            lines.append(f"- **+ `{value}`** ({', '.join(files[:3])})")
            raw = value.split("{id}")[0] if key == "api_strings" else value
            snippet = context(js_text.get(files[0], ""), raw) if files else ""
            if snippet:
                lines.append(f"  - `{snippet[:400]}`")
        lines += [f"- − `{value}`" for value in removed]
        lines.append("")
    old_calls, new_calls = old.get("api_calls", {}), new.get("api_calls", {})
    added = sorted(set(new_calls) - set(old_calls))
    reshaped = sorted(k for k in set(new_calls) & set(old_calls)
                      if new_calls[k].get("shape") is not None and old_calls[k].get("shape") is not None
                      and new_calls[k]["shape"] != old_calls[k]["shape"])
    if old and (added or reshaped):
        lines.append("## Appels des pages")
        for k in added:
            lines.append(f"- **nouveau** `{k}` → {json.dumps(new_calls[k].get('shape'), ensure_ascii=False)[:600]}")
        for k in reshaped:
            lines.append(f"- **réponse changée** `{k}`")
            lines.append(f"  - avant : `{json.dumps(old_calls[k]['shape'], ensure_ascii=False)[:600]}`")
            lines.append(f"  - après : `{json.dumps(new_calls[k]['shape'], ensure_ascii=False)[:600]}`")
        lines.append("")
    gone = sorted(set(old_calls) - set(new_calls))
    if old and gone:
        lines.append("Appels plus vus cette fois (page non visitée ou route retirée) : "
                     + ", ".join(f"`{k}`" for k in gone))
        lines.append("")
    lines.append(f"Pages visitées : {', '.join(sorted(new.get('pages', {})))}")
    return "\n".join(lines) + "\n"


def cookies_from_header(header: str, host: str) -> list[dict]:
    """Cookies d'un en-tête « a=b; c=d » (`document.cookie`), pour le domaine du site. Sans cookie de session
    Supabase (sb-<ref>-auth-token[.N]) : erreur, le fichier n'est pas le bon."""
    cookies = []
    for part in header.strip().split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            cookies.append({"name": name, "value": value, "domain": host, "path": "/", "secure": True,
                            "sameSite": "Lax"})
    if not any(re.match(r"^sb-[a-z0-9]+-auth-token(\.\d+)?$", c["name"], re.I) for c in cookies):
        raise SystemExit("Pas de cookie de session (sb-…-auth-token) dans ce fichier : connecte-toi d'abord sur le site.")
    return cookies


async def crawl(base_url: str, profile: Path, headed: bool, js_dir: Path,
                cookies: list[dict] | None = None) -> tuple[dict, dict, dict[str, str]]:
    from playwright.async_api import async_playwright  # dépendance optionnelle

    pages: dict[str, dict] = {}
    calls: dict[str, dict] = {}
    js: dict[str, str] = {}
    host = urlsplit(base_url).hostname or ""
    pending: list[tuple[str, Any]] = []

    async def on_response(response) -> None:
        url = urlsplit(response.url)
        if url.path.startswith("/_next/static/") and url.path.endswith(".js"):
            name = url.path.rsplit("/", 1)[-1]
            if name not in js:
                try:
                    js[name] = (await response.body()).decode("utf-8", "replace")
                except Exception:  # noqa: BLE001 - réponse déjà libérée
                    return
            return
        api = url.path.startswith("/api/") or "/rest/v1/" in url.path or "/functions/v1/" in url.path
        if not api:
            return
        method = response.request.method
        key = f"{method} {url.hostname if url.hostname != host else ''}{template_path(url.path)}"
        entry = calls.setdefault(key, {"params": [], "status": [], "shape": None})
        params = sorted({k for k, _ in parse_qsl(url.query, keep_blank_values=True)})
        entry["params"] = sorted(set(entry["params"]) | set(params))
        entry["status"] = sorted(set(entry["status"]) | {response.status})
        if entry["shape"] is None and response.ok:
            try:
                entry["shape"] = shape(await response.json())
            except Exception:  # noqa: BLE001 - pas du JSON
                pass

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(str(profile), headless=not headed, locale="fr-FR")
        if cookies:
            await context.add_cookies(cookies)
        await context.route("**/*", lambda route: route.abort() if route.request.resource_type in
                            ("image", "media", "font") else route.continue_())
        context.on("response", lambda r: pending.append(("r", asyncio.ensure_future(on_response(r)))))
        page = context.pages[0] if context.pages else await context.new_page()
        queue = list(SEEDS)
        seen_patterns: dict[str, int] = {}
        looked_in_js = False
        while len(pages) < MAX_PAGES:
            if not queue and not looked_in_js:
                looked_in_js = True  # pages trouvées seulement dans le JS, une fois les liens épuisés
                queue = sorted(analyse_js(js)["page_strings"])
            if not queue:
                break
            path = queue.pop(0)
            if path in pages or path.startswith(NEVER) or "{" in path or "[" in path:
                continue
            motif = pattern(path)
            if seen_patterns.get(motif, 0) >= PER_PATTERN:
                continue
            seen_patterns[motif] = seen_patterns.get(motif, 0) + 1
            try:
                response = await page.goto(base_url + path, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(SETTLE_MS)
            except Exception as exc:  # noqa: BLE001 - page en erreur : on passe à la suivante
                pages[path] = {"status": None, "error": str(exc)[:200]}
                continue
            final = urlsplit(page.url).path
            pages[path] = {"status": response.status if response else None, "final": final}
            print(f"  {path} -> {pages[path]['status']} {final if final != path else ''}")
            if final == "/login" and path != "/login":
                if path == "/marketplace":
                    await context.close()
                    raise SystemExit("Session absente ou expirée : reprends-la de ton navigateur avec --import-cookie.")
                continue
            links = await page.eval_on_selector_all("a[href^='/']", "els => els.map(e => e.getAttribute('href'))")
            queue += [link.split("?")[0].split("#")[0] for link in links if link]
            await asyncio.sleep(PAGE_PAUSE)
        await asyncio.gather(*(f for _, f in pending), return_exceptions=True)
        await context.close()

    js_dir.mkdir(parents=True, exist_ok=True)
    for name, text in js.items():
        (js_dir / name).write_text(text, encoding="utf-8")
    return pages, calls, js


def latest(out: Path, before: str) -> dict | None:
    maps = sorted(p for p in out.glob("*.json") if p.stem < before)
    return json.loads(maps[-1].read_text(encoding="utf-8")) if maps else None


def build_map(taken_at: str, pages: dict, calls: dict, js: dict[str, str]) -> dict:
    dpl = next((m.group(0) for text in js.values() if (m := re.search(r"dpl_[A-Za-z0-9]+", text))), None)
    return {"taken_at": taken_at, "dpl": dpl, "pages": pages, "api_calls": dict(sorted(calls.items())),
            "js": {n: hashlib.sha256(t.encode()).hexdigest()[:16] for n, t in sorted(js.items())},
            **analyse_js(js)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--profile", default="data/browser-profile")
    parser.add_argument("--out", default="data/site-map")
    parser.add_argument("--headed", action="store_true", help="montre la fenêtre du navigateur")
    parser.add_argument("--import-cookie", type=Path, help="fichier contenant document.cookie, pris dans ton navigateur")
    args = parser.parse_args()
    cookies = None
    if args.import_cookie:
        cookies = cookies_from_header(args.import_cookie.read_text(encoding="utf-8"),
                                      urlsplit(args.base_url).hostname or "")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    taken_at = datetime.now().strftime("%Y-%m-%d_%H%M")
    pages, calls, js = asyncio.run(crawl(args.base_url, Path(args.profile), args.headed, out / "js", cookies))
    new = build_map(taken_at, pages, calls, js)
    report = diff(latest(out, taken_at), new, js)
    (out / f"{taken_at}.json").write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / f"{taken_at}.md").write_text(report, encoding="utf-8")
    print(f"\n{len(pages)} pages, {len(calls)} appels, {len(js)} fichiers JS, "
          f"{len(new['api_strings'])} routes /api/ dans le JS.")
    print(f"Carte : {out / (taken_at + '.json')}\nChangements : {out / (taken_at + '.md')}")


if __name__ == "__main__":
    main()
