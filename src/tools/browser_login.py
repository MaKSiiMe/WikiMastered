"""Utilitaire Playwright (optionnel, sur un poste avec écran, pas dans le conteneur).

    pip install -r requirements-tools.txt && playwright install chromium
    python -m src.tools.browser_login            # connexion manuelle -> data/session_cookie.txt
    python -m src.tools.browser_login --record   # + enregistre les appels /api/ dans data/inspect/capture/

Tu te connectes toi-même dans la fenêtre (et fais toi-même une éventuelle vérification
humaine). Le script récupère ensuite les cookies du domaine. Avec --record, chaque
requête /api/ du site (méthode, URL, corps, réponse JSON) est enregistrée : c'est le
moyen le plus sûr de découvrir une route manquante, à importer ensuite dans
config/endpoints.local.yaml.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

SENSITIVE = {"cookie", "authorization", "apikey"}


async def run(base_url: str, record: bool, out: Path, profile: Path) -> None:
    from playwright.async_api import async_playwright  # import local : dépendance optionnelle

    capture_dir = out / "capture"
    if record:
        capture_dir.mkdir(parents=True, exist_ok=True)
    counter = {"n": 0}

    async def on_response(response) -> None:
        url = response.url
        if "/api/" not in urlsplit(url).path and "/rest/v1/" not in urlsplit(url).path:
            return
        try:
            body = await response.json()
        except Exception:  # noqa: BLE001 - réponse non JSON
            body = None
        request = response.request
        counter["n"] += 1
        name = re.sub(r"[^a-zA-Z0-9]+", "_", urlsplit(url).path).strip("_")[:80]
        entry = {
            "method": request.method,
            "url": url,
            "status": response.status,
            "request_headers": {k: v for k, v in (await request.all_headers()).items() if k.lower() not in SENSITIVE},
            "post_data": request.post_data,
            "response": body,
        }
        path = capture_dir / f"{counter['n']:04d}_{request.method}_{name}.json"
        path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  capturé : {request.method} {url} -> {response.status}")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(str(profile), headless=False, locale="fr-FR")
        if record:
            context.on("response", lambda r: asyncio.ensure_future(on_response(r)))
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(base_url)
        prompt = "Connecte-toi dans la fenêtre, navigue si besoin, puis appuie sur Entrée ici… "
        await asyncio.get_running_loop().run_in_executor(None, input, prompt)

        host = urlsplit(base_url).hostname or ""
        domain = host.removeprefix("www.")
        cookies = [c for c in await context.cookies() if c["domain"].lstrip(".").endswith(domain)]
        header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        user_agent = await page.evaluate("navigator.userAgent")
        await context.close()

    if not header:
        print("Aucun cookie trouvé : es-tu bien connecté ?")
        return
    drop = out.parent / "session_cookie.txt"
    drop.write_text(header, encoding="utf-8")
    print(f"\nCookie écrit dans {drop} ({len(cookies)} cookies).")
    print(f"WM_USER_AGENT='{user_agent}'")
    print("Copie ce fichier dans data/ sur le serveur (le bot le charge à chaud), ou colle-le dans WM_COOKIE.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="https://www.wiki-masters.com")
    parser.add_argument("--record", action="store_true", help="enregistre les appels /api/")
    parser.add_argument("--out", default="data/inspect")
    parser.add_argument("--profile", default="data/browser-profile")
    args = parser.parse_args()
    Path(args.out).mkdir(parents=True, exist_ok=True)
    asyncio.run(run(args.base_url, args.record, Path(args.out), Path(args.profile)))


if __name__ == "__main__":
    main()
