"""Récap du sniper sur une période (la nuit, par défaut les 24 dernières heures) : toutes les enchères misées.

Pour chaque enchère, sa dernière mise et son issue :
- gagnée : prix d'achat, estimation à l'achat, plus-value latente (estimation, commission déduite, − prix
  d'achat) tant que la carte n'est pas revendue, sinon le gain réalisé ; et où en est sa revente ;
- perdue : prix final payé par le gagnant et gain non acquis (estimation − prix final, si positif). « Dans nos
  limites » : le gagnant a payé moins que notre plafond, la vente était gagnable ;
- en cours, refusée (vérification anti-bot…) ou incertaine.
Trois blocs : positions réelles, sniper de test (simulé), collection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.core import timeutils

COLLECTION_PREFIX = "collection"
WON = ("won", "sim_won")
LOST = ("lost", "sim_lost", "outbid")
PENDING = ("placed", "simulated")


@dataclass
class Block:
    """Une famille de mises : réelles, simulées (sniper de test) ou collection."""
    lines_won: list[tuple[float, str]] = field(default_factory=list)
    lines_lost: list[tuple[float, str]] = field(default_factory=list)
    lines_other: list[str] = field(default_factory=list)
    bids: int = 0
    won: int = 0
    lost: int = 0
    pending: int = 0
    refused: int = 0
    spent: int = 0       # prix d'achat des cartes gagnées
    latent: float = 0.0         # plus-value latente des cartes gagnées pas encore revendues
    realized: int = 0           # gain des cartes gagnées déjà revendues
    missed: float = 0.0         # gain non acquis sur les enchères perdues
    missed_within: float = 0.0  # dont celles que le gagnant a payées moins que notre plafond

    def summary(self) -> str:
        parts = [f"{self.bids} enchère(s) : {self.won} gagnée(s) pour {self.spent} WB, {self.lost} perdue(s)"]
        if self.pending:
            parts.append(f"{self.pending} en cours")
        if self.refused:
            parts.append(f"{self.refused} refusée(s)")
        money = []
        if self.latent:
            money.append(f"plus-value latente {self.latent:+.0f} WB")
        if self.realized:
            money.append(f"gain réalisé {self.realized:+d} WB")
        if self.missed:
            money.append(f"gain non acquis ~{self.missed:.0f} WB (dont ~{self.missed_within:.0f} dans nos limites)")
        return ", ".join(parts) + (" · " + " · ".join(money) if money else "")


def _when(value: str | None) -> str:
    return f"{datetime.fromisoformat(value).astimezone(timeutils.PARIS):%d/%m %H:%M}" if value else "?"


def build(rows, fee: float = 0.0) -> dict[str, Block]:
    blocks = {"real": Block(), "sim": Block(), "collection": Block()}
    for r in rows:
        collection = str(r["reason"] or "").startswith(COLLECTION_PREFIX)
        block = blocks["collection" if collection else ("sim" if r["dry_run"] else "real")]
        block.bids += 1
        label = f"**{r['title']}** ({r['rarity']})"
        ref = r["reference_price"]
        net = ref * (1 - fee) if ref else None
        tries = f", {r['n_bids']} mises" if r["n_bids"] > 1 else ""
        status = r["status"]
        if status in WON:
            block.won += 1
            block.spent += r["amount"]
            if collection:
                block.lines_won.append((0, f"{label} : {r['amount']} WB"))
            elif r["closed"] and r["sale_price"] is not None:
                gain = r["sale_price"] - r["amount"]
                block.realized += gain
                block.lines_won.append((gain, f"{label} : achetée {r['amount']}, revendue {r['sale_price']} ({gain:+d})"))
            else:
                latent = (net - r["amount"]) if net is not None else 0.0
                block.latent += latent
                resale = ""
                if r["listing_status"] in ("scheduled",):
                    resale = f" · en vente le {_when(r['list_post_at'])} à {r['list_price']}"
                elif r["listing_status"] in ("posted", "simulated"):
                    resale = f" · en vente à {r['list_price']}"
                elif r["closed"]:
                    resale = " · gardée (invendue)"
                estimate = f", estimée ~{ref:.0f} → latente {latent:+.0f}" if ref else ""
                block.lines_won.append((latent, f"{label} : achetée {r['amount']}{estimate}{resale}"))
        elif status in LOST:
            block.lost += 1
            final = r["final"]
            if final is None:
                block.lines_lost.append((0, f"{label} : misé {r['amount']}{tries}, prix final inconnu"))
                continue
            within = r["ceiling"] is not None and final <= r["ceiling"]
            missed = max(0.0, net - final) if net is not None and not collection else 0.0
            block.missed += missed
            block.missed_within += missed if within else 0.0
            limit = (" · dans nos limites" if within else f" · au-dessus de notre plafond ({r['ceiling']})") \
                if r["ceiling"] is not None else ""
            gain = f", estimée ~{ref:.0f} → manqué ~{missed:.0f}" if ref and not collection else ""
            block.lines_lost.append((missed, f"{label} : misé {r['amount']}{tries}, partie à {final}{gain}{limit}"))
        elif status in PENDING:
            block.pending += 1
            block.lines_other.append(f"⏳ {label} : misé {r['amount']}, fin {_when(r['end_at'])}")
        elif status == "rejected":
            block.refused += 1
            block.lines_other.append(f"⛔ {label} : {r['amount']} WB refusée ({(r['note'] or '?')[:60]})")
        else:  # incertaine : relue sur le site au prochain passage
            block.lines_other.append(f"❔ {label} : {r['amount']} WB, résultat à vérifier")
    return blocks


def _lines(block: Block, limit: int | None) -> list[str]:
    won = [f"✅ {text}" for _, text in sorted(block.lines_won, key=lambda x: -x[0])]
    lost = [f"❌ {text}" for _, text in sorted(block.lines_lost, key=lambda x: -x[0])]
    lines = won + lost + block.lines_other
    if limit is not None and len(lines) > limit:
        lines = lines[:limit] + [f"… et {len(lines) - limit} autre(s) (commande sniper-report pour tout voir)"]
    return lines


def render(blocks: dict[str, Block], since: datetime, until: datetime, *,
           limit: int | None = 8) -> tuple[str, str, list[tuple[str, str]]]:
    """(titre, résumé, blocs) ; `limit` lignes au plus par bloc (Discord), None pour tout (console)."""
    title = f"Sniper : récap du {since.astimezone(timeutils.PARIS):%d/%m %H:%M} au " \
            f"{until.astimezone(timeutils.PARIS):%d/%m %H:%M}"
    names = {"real": "Positions réelles", "sim": "Sniper de test (simulé)", "collection": "Collection"}
    summary, sections = [], []
    for key, name in names.items():
        block = blocks[key]
        if not block.bids:
            continue
        summary.append(f"**{name}** : {block.summary()}")
        sections.append((name, "\n".join(_lines(block, limit))))
    return title, "\n".join(summary) or "Aucune enchère misée sur la période.", sections


async def sniper_report(db, since: datetime, until: datetime, fee: float = 0.0, *,
                        limit: int | None = 8) -> tuple[str, str, list[tuple[str, str]]]:
    return render(build(await db.bids_since(since), fee), since, until, limit=limit)
