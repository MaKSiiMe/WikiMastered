"""Statistiques de fonctionnement : durée des passages de chaque worker, requêtes au site par route.

Comptées en continu et relevées toutes les STATS_MINUTES (src/main.py, stats_loop) : une ligne lisible en console,
le détail dans le journal (événement « stats »), le dernier relevé dans /etat.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

_KEPT = 2000  # durées gardées par série pour les centiles (au-delà, une sur deux est oubliée)


@dataclass
class Timings:
    """Durées (s) d'une série : nombre, moyenne, médiane, 95e centile, maximum."""

    values: list[float] = field(default_factory=list)
    count: int = 0
    total: float = 0.0
    longest: float = 0.0

    def add(self, seconds: float) -> None:
        self.count += 1
        self.total += seconds
        self.longest = max(self.longest, seconds)
        self.values.append(seconds)
        if len(self.values) > _KEPT:
            del self.values[::2]

    def percentile(self, share: float) -> float:
        if not self.values:
            return 0.0
        ranked = sorted(self.values)
        return ranked[min(len(ranked) - 1, max(0, math.ceil(share * len(ranked)) - 1))]

    def summary(self) -> dict[str, Any]:
        if not self.count:
            return {"n": 0}
        return {"n": self.count, "moy": round(self.total / self.count, 3), "p50": round(self.percentile(0.5), 3),
                "p95": round(self.percentile(0.95), 3), "max": round(self.longest, 3)}


@dataclass
class PassStats:
    """Passages d'un worker depuis le dernier relevé."""

    timings: Timings = field(default_factory=Timings)
    errors: int = 0

    def take(self) -> dict[str, Any]:
        out = {**self.timings.summary(), "erreurs": self.errors}
        self.timings, self.errors = Timings(), 0
        return out


@dataclass
class RouteStats:
    latency: Timings = field(default_factory=Timings)  # envoi -> réponse
    statuses: Counter[str] = field(default_factory=Counter)  # "200", "429", "ReadTimeout"…


class NetStats:
    """Requêtes au site depuis le dernier relevé : par route (latence, codes de réponse) et par file (attente
    d'un créneau, par worker ; « écritures » pour les mises, ventes…)."""

    def __init__(self) -> None:
        self.routes: defaultdict[str, RouteStats] = defaultdict(RouteStats)
        self.waits: defaultdict[str, Timings] = defaultdict(Timings)

    def record(self, route: str, lane: str, wait: float, latency: float, status: str) -> None:
        stats = self.routes[route]
        stats.latency.add(latency)
        stats.statuses[status] += 1
        self.waits[lane].add(wait)

    def take(self) -> dict[str, Any]:
        routes, waits = self.routes, self.waits
        self.routes, self.waits = defaultdict(RouteStats), defaultdict(Timings)
        return {
            "routes": {name: {**r.latency.summary(), "codes": dict(r.statuses)}
                       for name, r in sorted(routes.items(), key=lambda kv: -kv[1].latency.count)},
            "attente": {lane: t.summary() for lane, t in sorted(waits.items(), key=lambda kv: -kv[1].count)},
        }


def is_failure(status: str) -> bool:
    return not status.isdigit() or int(status) >= 400


def _ms(seconds: float) -> str:
    return f"{seconds * 1000:.0f} ms"


def _s(seconds: float) -> str:
    return f"{seconds:.1f} s" if seconds < 100 else f"{seconds:.0f} s"


def workers_line(workers: dict[str, dict[str, Any]]) -> str:
    """« sniper 612 passages, moy 1.3 s, p95 4.0 s, max 8.2 s · garde 1 passage… » ; erreurs à la fin."""
    parts, errors = [], []
    for name, w in workers.items():
        if w.get("pause"):
            parts.append(f"{name} en pause")
            continue
        if not w["n"]:
            parts.append(f"{name} aucun passage")
        elif w["n"] < 5:
            parts.append(f"{name} {w['n']} passage(s), {_s(w['moy'])}" + (f" (max {_s(w['max'])})" if w["n"] > 1 else ""))
        else:
            parts.append(f"{name} {w['n']} passages, moy {_s(w['moy'])}, p95 {_s(w['p95'])}, max {_s(w['max'])}")
        if w.get("erreurs"):
            errors.append(f"{name} {w['erreurs']}")
    return " · ".join(parts) + " — erreurs : " + (", ".join(errors) if errors else "aucune")


def network_lines(net: dict[str, Any], seconds: float, top: int = 6) -> list[str]:
    """Total, latence et attente d'un créneau par file, puis les routes les plus appelées et celles en échec."""
    routes: dict[str, dict[str, Any]] = net["routes"]
    if not routes:
        return ["aucune requête"]
    total = sum(r["n"] for r in routes.values())
    failed: Counter[str] = Counter()
    for name, r in routes.items():
        for status, n in r["codes"].items():
            if is_failure(status):
                failed[f"{name} {status}"] += n
    waits = [f"{lane} moy {_s(w['moy'])} p95 {_s(w['p95'])}" for lane, w in net["attente"].items() if w["n"]]
    busiest = [f"{name} {r['n']} ({_ms(r['p50'])}, p95 {_ms(r['p95'])})" for name, r in list(routes.items())[:top]]
    return [
        f"{total} requêtes ({total / max(seconds, 1):.2f}/s) — échecs : "
        + (", ".join(f"{k} ×{n}" for k, n in failed.most_common()) if failed else "aucun"),
        "attente d'un créneau : " + " · ".join(waits),
        "routes : " + " · ".join(busiest),
    ]
