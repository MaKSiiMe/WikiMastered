"""Importe une requête copiée depuis DevTools (« Copier comme cURL (bash) »).

    python -m src.tools.parse_curl requete.curl                 # affiche l'analyse
    python -m src.tools.parse_curl --name place_bid --write < requete.curl
    python -m src.tools.parse_curl --env --hot < requete.curl

--name/--write  ajoute l'endpoint à config/endpoints.local.yaml (prioritaire sur endpoints.yaml)
--env           met à jour WM_COOKIE / WM_USER_AGENT / WM_EXTRA_HEADERS dans .env
--hot           écrit le cookie dans data/session_cookie.txt (pris en compte à chaud par le bot)
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import yaml

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
# En-têtes gérés automatiquement par le client (ou propres à une requête).
SKIP_HEADERS = {
    "cookie", "user-agent", "content-length", "host", "accept-encoding", "connection", "referer",
    "origin", "accept", "accept-language", "content-type", "priority", "if-none-match",
}


@dataclass
class ParsedCurl:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    cookie: str = ""
    body: Any = None

    @property
    def user_agent(self) -> str:
        return self.headers.get("user-agent", "")

    @property
    def extra_headers(self) -> dict[str, str]:
        return {k: v for k, v in self.headers.items() if k not in SKIP_HEADERS and k != "authorization"}

    @property
    def authorization(self) -> str:
        return self.headers.get("authorization", "")


def parse_curl(text: str) -> ParsedCurl:
    text = re.sub(r"\\\r?\n", " ", text.strip())
    tokens = shlex.split(text)
    if not tokens or tokens[0] != "curl":
        raise ValueError("ce n'est pas une commande cURL")
    method, url, data = None, None, None
    headers: dict[str, str] = {}
    cookie = ""
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok in ("-X", "--request"):
            method, i = nxt.upper(), i + 2
        elif tok in ("-H", "--header"):
            name, _, value = nxt.partition(":")
            headers[name.strip().lower()] = value.strip()
            i += 2
        elif tok in ("-b", "--cookie"):
            cookie, i = nxt, i + 2
        elif tok in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii"):
            data, i = nxt, i + 2
        elif tok == "--url":
            url, i = nxt, i + 2
        elif tok.startswith("-"):
            i += 1  # --compressed, -s, -i...
        else:
            url = url or tok
            i += 1
    if not url:
        raise ValueError("URL introuvable")
    cookie = cookie or headers.pop("cookie", "")
    headers.pop("cookie", None)
    body: Any = None
    if data is not None:
        try:
            body = json.loads(data)
        except ValueError:
            body = data
    return ParsedCurl(method or ("POST" if data is not None else "GET"), url, headers, cookie, body)


def to_endpoint(parsed: ParsedCurl, id_names: list[str] | None = None) -> dict[str, Any]:
    """Transforme la requête en entrée endpoints.yaml ; les UUID deviennent des placeholders.

    `id_names` nomme les placeholders dans l'ordre (ex. ["auction_id"]) : ils doivent
    correspondre aux noms qu'utilise le code (voir config/endpoints.yaml).
    """
    parts = urlsplit(parsed.url)
    names = list(id_names or [])
    counter = {"n": 0}

    def placeholder(_: re.Match[str]) -> str:
        counter["n"] += 1
        if counter["n"] <= len(names):
            return "{" + names[counter["n"] - 1] + "}"
        return "{id}" if counter["n"] == 1 else f"{{id{counter['n']}}}"

    path = UUID.sub(placeholder, parts.path)
    spec: dict[str, Any] = {"method": parsed.method, "path": path}
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    if params:
        spec["params"] = params
    if parsed.body is not None:
        spec["json"] = parsed.body
    return spec


def expected_placeholders(base_file: Path, name: str) -> list[str]:
    """Placeholders du chemin d'origine d'un endpoint connu (ex. place_bid -> ["auction_id"])."""
    try:
        base = yaml.safe_load(base_file.read_text(encoding="utf-8")) or {}
    except OSError:
        return []
    path = str(((base.get("endpoints") or {}).get(name) or {}).get("path", ""))
    return re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", path)


def _env_quote(value: str) -> str:
    return "'" + value.replace("'", "") + "'"


def update_env(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for key, value in values.items():
        line = f"{key}={value}"
        for idx, existing in enumerate(lines):
            if existing.split("=", 1)[0].strip() == key:
                lines[idx] = line
                break
        else:
            lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", nargs="?", help="fichier contenant la commande cURL (sinon stdin)")
    parser.add_argument("--name", help="nom de l'endpoint (ex. place_bid)")
    parser.add_argument("--write", action="store_true", help="écrit dans config/endpoints.local.yaml")
    parser.add_argument("--env", action="store_true", help="met à jour .env")
    parser.add_argument("--hot", action="store_true", help="écrit data/session_cookie.txt")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--local-endpoints", default="config/endpoints.local.yaml")
    parser.add_argument("--base-endpoints", default="config/endpoints.yaml")
    args = parser.parse_args(argv)

    text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    parsed = parse_curl(text)
    id_names = expected_placeholders(Path(args.base_endpoints), args.name) if args.name else []
    spec = to_endpoint(parsed, id_names)

    print(f"# {parsed.method} {parsed.url}")
    print(yaml.safe_dump({args.name or "nouvel_endpoint": spec}, allow_unicode=True, sort_keys=False))
    print(f"# Cookie : {len(parsed.cookie)} caractères ; User-Agent : {parsed.user_agent[:60]}…")
    if parsed.extra_headers:
        print(f"# En-têtes supplémentaires : {list(parsed.extra_headers)}")

    if args.write:
        if not args.name:
            parser.error("--write nécessite --name")
        local = Path(args.local_endpoints)
        data = yaml.safe_load(local.read_text(encoding="utf-8")) if local.exists() else None
        data = data or {"endpoints": {}}
        data.setdefault("endpoints", {})[args.name] = spec
        local.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        print(f"-> endpoint « {args.name} » écrit dans {local}")

    if args.env:
        values = {}
        if parsed.cookie:
            values["WM_COOKIE"] = _env_quote(parsed.cookie)
        if parsed.user_agent:
            values["WM_USER_AGENT"] = _env_quote(parsed.user_agent)
        if parsed.extra_headers:
            values["WM_EXTRA_HEADERS"] = _env_quote(json.dumps(parsed.extra_headers))
        if parsed.authorization:
            values["WM_AUTHORIZATION"] = _env_quote(parsed.authorization)
        update_env(Path(args.env_file), values)
        print(f"-> {', '.join(values)} mis à jour dans {args.env_file}")

    if args.hot:
        if not parsed.cookie:
            parser.error("aucun cookie dans cette requête")
        drop = Path("data/session_cookie.txt")
        drop.parent.mkdir(parents=True, exist_ok=True)
        drop.write_text(parsed.cookie, encoding="utf-8")
        print(f"-> cookie écrit dans {drop} (chargé par le bot sous 30 s)")


if __name__ == "__main__":
    main()
