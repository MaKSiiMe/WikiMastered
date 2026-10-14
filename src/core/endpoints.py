"""Registre d'endpoints chargé depuis config/endpoints.yaml.

Aucune route n'est codée en dur dans les workers : ils demandent un endpoint par son
nom et ses paramètres, ce qui permet de corriger une route sans toucher au code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class EndpointError(KeyError):
    pass


@dataclass(frozen=True)
class Endpoint:
    name: str
    method: str
    path: str
    params: dict[str, Any] = field(default_factory=dict)
    json: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)

    @property
    def is_mutation(self) -> bool:
        return self.method not in ("GET", "HEAD")


@dataclass(frozen=True)
class BuiltRequest:
    name: str
    method: str
    url: str
    params: dict[str, str]
    json: Any
    headers: dict[str, str]
    is_mutation: bool


def _render_str(template: str, values: dict[str, Any], *, encode: bool) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise EndpointError(f"paramètre manquant : {key}")
        text = str(values[key])
        return quote(text, safe="") if encode else text

    return _PLACEHOLDER.sub(repl, template)


def render_value(template: Any, values: dict[str, Any]) -> Any:
    """Remplace les placeholders ; `"{x}"` seul conserve le type de x (int, list...)."""
    if isinstance(template, str):
        whole = _PLACEHOLDER.fullmatch(template)
        if whole:
            key = whole.group(1)
            if key not in values:
                raise EndpointError(f"paramètre manquant : {key}")
            return values[key]
        return _render_str(template, values, encode=False)
    if isinstance(template, dict):
        return {k: render_value(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [render_value(v, values) for v in template]
    return template


class Endpoints:
    def __init__(self, base_url: str, endpoints: dict[str, Endpoint]):
        self.base_url = base_url.rstrip("/")
        self._endpoints = endpoints

    @classmethod
    def load(cls, path: Path, base_url_override: str | None = None) -> Endpoints:
        """Charge `endpoints.yaml`, puis `endpoints.local.yaml` (s'il existe) qui le surcharge."""
        path = Path(path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        specs = dict(raw.get("endpoints") or {})
        local = path.with_name(f"{path.stem}.local{path.suffix}")
        if local.exists():
            local_raw = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
            specs.update(local_raw.get("endpoints") or {})
            raw["base_url"] = local_raw.get("base_url") or raw.get("base_url")
        endpoints: dict[str, Endpoint] = {}
        for name, spec in specs.items():
            if not isinstance(spec, dict) or "path" not in spec:
                raise ValueError(f"endpoint {name!r} invalide dans {path}")
            endpoints[name] = Endpoint(
                name=name,
                method=str(spec.get("method", "GET")).upper(),
                path=str(spec["path"]),
                params=dict(spec.get("params") or {}),
                json=spec.get("json"),
                headers={k: str(v) for k, v in (spec.get("headers") or {}).items()},
                defaults=dict(spec.get("defaults") or {}),
            )
        base_url = base_url_override or raw.get("base_url") or ""
        return cls(base_url, endpoints)

    def has(self, name: str) -> bool:
        return name in self._endpoints

    def get(self, name: str) -> Endpoint:
        try:
            return self._endpoints[name]
        except KeyError:
            raise EndpointError(f"endpoint inconnu : {name} (voir config/endpoints.yaml)") from None

    def names(self) -> list[str]:
        return list(self._endpoints)

    def build(self, name: str, **values: Any) -> BuiltRequest:
        ep = self.get(name)
        merged = {**ep.defaults, **values}
        path = _render_str(ep.path, merged, encode=True)
        url = path if path.startswith(("http://", "https://")) else f"{self.base_url}{path}"
        params: dict[str, str] = {}
        for key, template in ep.params.items():
            rendered = render_value(template, merged)
            if rendered is None or rendered == "":
                continue
            params[key] = str(rendered)
        body = render_value(ep.json, merged) if ep.json is not None else None
        headers = {k: _render_str(v, merged, encode=False) for k, v in ep.headers.items()}
        return BuiltRequest(name, ep.method, url, params, body, headers, ep.is_mutation)
