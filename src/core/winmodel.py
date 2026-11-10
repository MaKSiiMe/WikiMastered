"""Valeur d'une mise : ce qu'elle rapporte en moyenne, d'après les mises réelles déjà réglées.

La vérification anti-bot du site ne laisse passer qu'un petit nombre de mises : chacune est une place à ne pas
gâcher. Or le bénéfice attendu (estimation − mise) dit ce que rapporte une enchère gagnée, pas la chance de la gagner :
une mise très sous l'estimation promet beaucoup et ne gagne presque jamais, les autres joueurs la font monter.

Valeur = chance de gagner × (revente attendue − mise), où :
- la chance de gagner est le taux de victoire des mises passées de même rapport mise / estimation (par tranches de
  5 %, shiny à part), ramené vers le taux moyen quand la tranche compte peu de mises ;
- la revente attendue est l'estimation × le rapport médian prix de vente / estimation des positions revendues.

Tout vient de la base du bot : rien n'est réglé à la main, et le modèle suit le marché. Avec trop peu de mises
réglées (`MIN_SAMPLES`), il n'y a pas de modèle et aucun filtre ne s'applique.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

BUCKET = 0.05         # largeur d'une tranche du rapport mise / estimation
MAX_BUCKET = 30       # au-delà de 150 % : même tranche
PRIOR = 20            # poids du taux moyen face aux mises d'une tranche
MIN_SAMPLES = 200     # mises réglées au moins pour bâtir le modèle
MIN_RESALES = 10      # reventes au moins pour mesurer prix de vente / estimation (sinon 1)
RESALE_BOUNDS = (0.5, 1.2)
CONTROL_MARK = "témoin"  # fin du motif d'une mise laissée au minimum pour mesurer SNIPER_BID_PUSH : hors modèle
ACCEPTED = ("won", "lost", "outbid")


def bucket(amount: float, reference: float) -> int:
    return max(0, min(MAX_BUCKET, int(amount / reference / BUCKET)))


@dataclass(frozen=True)
class WinModel:
    rates: dict[tuple[bool, int], float]  # (shiny, tranche) -> chance de gagner
    base: dict[bool, float]               # shiny -> taux moyen, pour une tranche jamais vue
    resale: dict[bool, float]             # shiny -> prix de vente / estimation
    samples: int

    def win_chance(self, amount: float, reference: float, shiny: bool) -> float:
        return self.rates.get((shiny, bucket(amount, reference)), self.base[shiny])

    def value(self, amount: float, reference: float, shiny: bool, fee: float = 0.0) -> float:
        """Gain moyen attendu de cette mise, en WB (négatif : la revente attendue ne couvre pas la mise)."""
        return self.win_chance(amount, reference, shiny) * (reference * self.resale[shiny] * (1 - fee) - amount)

    def describe(self) -> str:
        return (f"{self.samples} mises réglées, {self.base[False]:.0%} gagnées ({self.base[True]:.0%} en shiny), "
                f"revente à {self.resale[False]:.0%} de l'estimation")


def fit(rows: Iterable, min_samples: int = MIN_SAMPLES) -> WinModel | None:
    """Modèle d'après les mises de positions réelles acceptées par le site (`Database.bid_outcomes`) ; None s'il y
    en a moins de `min_samples`."""
    rows = [r for r in rows if r["status"] in ACCEPTED and (r["reference_price"] or 0) > 0
            and not (r["reason"] or "").endswith(CONTROL_MARK)]
    if len(rows) < min_samples:
        return None
    bids: Counter = Counter()
    wins: Counter = Counter()
    sold: dict[bool, list[float]] = {False: [], True: []}
    for r in rows:
        shiny, won = bool(r["is_shiny"]), r["status"] == "won"
        for key in (shiny, (shiny, bucket(r["amount"], r["reference_price"]))):
            bids[key] += 1
            wins[key] += won
        if won and r["sale_price"]:
            sold[shiny].append(r["sale_price"] / r["reference_price"])
    overall = (wins[False] + wins[True]) / len(rows)
    base = {shiny: (wins[shiny] + PRIOR * overall) / (bids[shiny] + PRIOR) for shiny in (False, True)}
    rates = {key: (wins[key] + PRIOR * base[key[0]]) / (n + PRIOR) for key, n in bids.items() if isinstance(key, tuple)}
    every = sold[False] + sold[True]
    low, high = RESALE_BOUNDS

    def ratio(prices: list[float]) -> float:
        prices = prices if len(prices) >= MIN_RESALES else every
        return max(low, min(high, statistics.median(prices))) if len(prices) >= MIN_RESALES else 1.0

    return WinModel(rates, base, {shiny: ratio(sold[shiny]) for shiny in (False, True)}, len(rows))


THRESHOLDS = (0, 10, 20, 30, 40, 50, 60, 80, 100, 150, 200)


def thresholds_text(rows: Iterable, days: float, fee: float = 0.0) -> str:
    """Ce qu'aurait donné chaque seuil SNIPER_MIN_BID_VALUE sur les occasions de la période : mises acceptées, et
    mises refusées par la vérification anti-bot (une par enchère), qui seraient passées sans le blocage."""
    rows = list(rows)
    model = fit(rows)
    if model is None:
        return f"Pas assez de mises réelles réglées sur {days:g} j (il en faut {MIN_SAMPLES}) : aucun modèle."
    chances: dict[str, tuple[float, bool]] = {}  # enchère -> (valeur, acceptée) ; une mise acceptée prime
    for r in rows:
        if (r["reference_price"] or 0) <= 0 or (r["reason"] or "").endswith(CONTROL_MARK):
            continue
        accepted = r["status"] in ACCEPTED
        if accepted or r["auction_id"] not in chances:
            chances[r["auction_id"]] = (model.value(r["amount"], r["reference_price"], bool(r["is_shiny"]), fee),
                                        accepted)
    quota = sum(1 for _, accepted in chances.values() if accepted)
    lines = [f"Valeur d'une mise sur {days:g} j : {model.describe()}.",
             f"{len(chances)} occasions ({len(chances) / days:.0f}/j), dont {quota} mises acceptées par le site "
             f"({quota / days:.0f}/j) ; les autres ont été refusées par la vérification anti-bot.",
             "Pour chaque seuil : occasions gardées par jour, gain attendu par jour si toutes passaient, et avec le "
             "même nombre de mises acceptées qu'aujourd'hui.", "",
             f"{'seuil':>6} {'occasions/j':>12} {'gain/j, toutes':>15} {'gain/j, même quota':>19} {'par mise':>9}"]
    for threshold in THRESHOLDS:
        kept = [value for value, _ in chances.values() if value >= threshold]
        if not kept:
            break
        total = sum(kept)
        capped = total * min(1.0, quota / len(kept))
        lines.append(f"{threshold:>6} {len(kept) / days:>12.0f} {total / days:>15.0f} {capped / days:>19.0f} "
                     f"{total / len(kept):>9.0f}")
    lines += ["", "Le meilleur seuil est celui qui donne le plus grand gain à quota égal. Les seuils sous le "
                  "SNIPER_MIN_BID_VALUE en place sont sous-estimés : leurs occasions ne sont plus misées."]
    return "\n".join(lines)
