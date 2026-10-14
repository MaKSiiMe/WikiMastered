"""Module C : cotes de la collection, mise en vente aux heures de pointe, défausse protégée.

- Vente : doublons dont la cote moyenne (même rareté) dépasse SELL_MIN_VALUE, même C/PC.
  Fin d'enchère programmée dans LISTING_END_WINDOW (20:00-22:30, Paris).
- Arbitrage : les cartes gagnées la nuit sont remises en vente avec une fin vers 21:00.
- Défausse : doublons sans valeur, seulement si aucun palier de succès de taille de
  collection n'est proche (ACHIEVEMENT_MILESTONES), par lots de 50 [WR discard.js].
- Jamais touchés : favoris, shiny, exemplaires en échange ou déjà en vente.

Le worker publie les ventes programmées (même celles créées en CLI). Les scans
automatiques ne tournent que si AUTO_LIST / AUTO_DISCARD sont activés.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from src.config import RARITY_RANK, Settings
from src.core.client import UncertainMutation
from src.core.database import utcnow
from src.core.models import DiscardResult, OwnedCard
from src.core.pricing import Quote
from src.core.timeutils import now_local, random_in_window
from src.workers.base import Context, Worker

INVENTORY_PRICE_MAX_AGE = timedelta(hours=12)


# ---------------------------------------------------------------- logique pure
@dataclass(frozen=True)
class SaleCandidate:
    owned: OwnedCard
    average: float
    price: int


@dataclass(frozen=True)
class DiscardCandidate:
    owned: OwnedCard
    average: float | None


def keep_order(row: OwnedCard) -> tuple:
    """Les exemplaires à garder en premier : favori, shiny, plus haute rareté."""
    return (not row.starred, not row.is_shiny, -RARITY_RANK.get(row.rarity or "", -1), row.id)


def group_by_card(rows: list[OwnedCard]) -> dict[str, list[OwnedCard]]:
    groups: dict[str, list[OwnedCard]] = defaultdict(list)
    for row in rows:
        groups[row.catalog_id].append(row)
    for copies in groups.values():
        copies.sort(key=keep_order)
    return groups


def spare_copies(copies: list[OwnedCard], keep: int, excluded: set[str]) -> list[OwnedCard]:
    return [c for c in copies[keep:] if not c.starred and not c.is_shiny and c.id not in excluded]


def select_for_sale(groups: dict[str, list[OwnedCard]], quotes: dict[str, dict[str, Quote]],
                    excluded: set[str], s: Settings) -> list[SaleCandidate]:
    out: list[SaleCandidate] = []
    for card_id, copies in groups.items():
        for row in spare_copies(copies, s.sell_keep_copies, excluded):
            q = quotes.get(card_id, {}).get(row.rarity or "")
            if q and q.reliable(s.price_min_sales) and q.average >= s.sell_min_value:  # type: ignore[operator]
                out.append(SaleCandidate(row, q.average, max(1, round(q.average * s.sell_price_multiplier))))  # type: ignore[arg-type]
    out.sort(key=lambda c: c.average, reverse=True)
    return out


def select_for_discard(groups: dict[str, list[OwnedCard]], quotes: dict[str, dict[str, Quote]],
                       excluded: set[str], s: Settings) -> list[DiscardCandidate]:
    out: list[DiscardCandidate] = []
    keep = max(1, s.discard_keep_copies)  # on garde toujours au moins un exemplaire
    for card_id, copies in groups.items():
        for row in spare_copies(copies, keep, excluded):
            if row.rarity not in s.discard_rarities:
                continue
            q = quotes.get(card_id, {}).get(row.rarity or "")
            avg = q.average if q else None
            if (avg is None and s.discard_unknown_price_is_worthless) or (avg is not None and avg <= s.discard_max_value):
                out.append(DiscardCandidate(row, avg))
    return out


def achievement_guard(count: int, n_discard: int, milestones: list[int], margin: int,
                      affects_count: bool) -> tuple[int, str]:
    """Nombre d'exemplaires qu'on peut défausser sans compromettre un palier de collection."""
    if not milestones:
        return 0, "ACHIEVEMENT_MILESTONES non renseigné : défausse refusée par prudence"
    upcoming = min((m for m in milestones if m > count), default=None)
    if not affects_count:
        return n_discard, f"mode unique : {count} cartes distinctes, inchangé par la défausse de doublons"
    if upcoming is not None and upcoming - count <= margin:
        return 0, f"palier {upcoming} proche ({count}/{upcoming}, marge {margin}) : défausse bloquée"
    reached = max((m for m in milestones if m <= count), default=0)
    allowed = max(0, min(n_discard, count - reached))
    target = f"prochain palier {upcoming}" if upcoming else "tous les paliers atteints"
    return allowed, f"{count} exemplaires, {target} ; on ne repasse pas sous {reached}"


def plan_listing_time(now: datetime, start: time, end: time, durations: list[int], min_duration: int,
                      rng: random.Random | None = None) -> tuple[datetime, int, datetime]:
    """(heure de publication, durée en minutes, fin visée) pour finir dans la fenêtre [start, end]."""
    local_now = now
    for offset in range(4):
        target = random_in_window(local_now.date() + timedelta(days=offset), start, end, rng)
        if not durations:
            minutes = int((target - local_now).total_seconds() // 60)
            if minutes >= min_duration:
                return local_now, minutes, target
            continue
        feasible = [(target - timedelta(minutes=d), d) for d in durations if target - timedelta(minutes=d) >= local_now]
        if feasible:
            post_at, minutes = min(feasible)
            return post_at, minutes, target
    raise ValueError("aucune durée de vente ne permet de finir dans la fenêtre demandée")


# ---------------------------------------------------------------------- worker
class InventoryManager(Worker):
    name = "inventory"

    async def setup(self) -> None:
        self._next_scan = utcnow() + timedelta(minutes=2)

    async def step(self) -> float:
        await self.post_due_listings()
        if (self.settings.auto_list or self.settings.auto_discard) and utcnow() >= self._next_scan:
            self._next_scan = utcnow() + timedelta(minutes=self.settings.inventory_interval_minutes)
            await self.run_cycle(list_cards=self.settings.auto_list, discard=self.settings.auto_discard)
        return 30.0

    # ------------------------------------------------------------ analyse
    async def snapshot(self) -> tuple[dict[str, list[OwnedCard]], set[str], dict[str, dict[str, Quote]], int]:
        rows = await self.api.load_collection()
        groups = group_by_card(rows)
        excluded = await self.api.protected_owned_ids({r.id for r in rows})
        excluded |= await self.db.scheduled_owned_ids()
        to_price = [cid for cid, copies in groups.items() if spare_copies(copies, min(self.settings.sell_keep_copies,
                    max(1, self.settings.discard_keep_copies)), excluded)]
        self.log.info("collection : %d exemplaires, %d cartes, %d à coter", len(rows), len(groups), len(to_price))
        quotes: dict[str, dict[str, Quote]] = {}
        for i, card_id in enumerate(to_price, 1):
            quotes[card_id] = await self.ctx.prices.quotes(card_id, max_age=INVENTORY_PRICE_MAX_AGE)
            if i % 25 == 0:
                self.log.info("cotes : %d/%d", i, len(to_price))
        return groups, excluded, quotes, len(rows)

    async def run_cycle(self, *, list_cards: bool, discard: bool) -> dict:
        groups, excluded, quotes, total = await self.snapshot()
        report: dict = {}
        if list_cards:
            # Les reventes d'arbitrage passent en premier : les places de vente sont limitées.
            report["relisted"] = await self.schedule_arbitrage(groups, excluded)
            report["listed"] = await self.schedule_sales(select_for_sale(groups, quotes, excluded, self.settings))
        if discard:
            report["discard"] = await self.discard(groups, quotes, excluded, total)
        return report

    # ------------------------------------------------------------- ventes
    async def _schedule(self, owned: OwnedCard, price: int, reason: str, start: time, end: time) -> bool:
        s = self.settings
        post_at, minutes, target = plan_listing_time(now_local(), start, end, s.listing_duration_options,
                                                     s.listing_min_duration_minutes)
        listing_id = await self.db.schedule_listing(
            owned_card_id=owned.id, card_id=owned.catalog_id, title=owned.title, rarity=owned.rarity,
            base_amount=price, duration_minutes=minutes, post_at=post_at, target_end=target, reason=reason,
        )
        if listing_id:
            self.log.info("vente programmée : %s (%s) %d WB, publication %s, fin %s",
                          owned.title, owned.rarity, price, f"{post_at:%d/%m %H:%M}", f"{target:%d/%m %H:%M}")
        return bool(listing_id)

    async def free_slots(self) -> int:
        selling, maximum = await self.api.listing_slots()
        return max(0, maximum - selling - await self.db.pending_listings_count())

    async def schedule_sales(self, candidates: list[SaleCandidate]) -> int:
        s = self.settings
        count = 0
        slots = min(s.sell_max_listings_per_run, await self.free_slots())
        if candidates and not slots:
            self.log.info("aucune place de vente libre (%d candidat(s) en attente)", len(candidates))
        for c in candidates[:slots]:
            if await self._schedule(c.owned, c.price, f"cote {c.average:.0f}", s.listing_end_window_start,
                                    s.listing_end_window_end):
                count += 1
        if count:
            self.notifier.info("Ventes programmées", f"{count} carte(s) mises en vente avec fin entre "
                               f"{s.listing_end_window_start:%H:%M} et {s.listing_end_window_end:%H:%M}.")
        return count

    async def schedule_arbitrage(self, groups: dict[str, list[OwnedCard]], excluded: set[str]) -> int:
        s = self.settings
        count = 0
        center = datetime.combine(now_local().date(), s.arbitrage_relist_end_time)
        start, end = (center - timedelta(minutes=10)).time(), (center + timedelta(minutes=10)).time()
        slots = await self.free_slots()
        for bid in await self.db.won_unlisted_night_bids():
            if count >= slots:
                break
            copies = [c for c in groups.get(bid["card_id"], []) if not c.starred and c.id not in excluded
                      and c.rarity == bid["rarity"]]
            if not copies:
                continue
            quote = await self.ctx.prices.quote(bid["card_id"], bid["rarity"])
            price = max(round((quote.average or 0) * s.arbitrage_markup), bid["amount"] + 1)
            if await self._schedule(copies[-1], price, f"arbitrage (acheté {bid['amount']})", start, end):
                excluded.add(copies[-1].id)
                await self.db.update_bid(bid["id"], relisted=1)
                count += 1
        return count

    async def post_due_listings(self) -> None:
        due = await self.db.due_listings(utcnow())
        if not due:
            return
        selling, maximum = await self.api.listing_slots()
        for row in due:
            if selling >= maximum:
                await self._postpone(row)
                continue
            selling += 1
            minutes = row["duration_minutes"]
            if not self.settings.listing_duration_options and row["target_end"]:
                # Publication en retard (bot arrêté...) : on vise toujours la même heure de fin.
                left = (datetime.fromisoformat(row["target_end"]) - utcnow()).total_seconds() // 60
                minutes = int(max(self.settings.listing_min_duration_minutes, left))
            label = f"{row['title']} ({row['rarity']}) à {row['base_amount']} WB"
            try:
                result = await self.api.create_listing(row["owned_card_id"], row["base_amount"], minutes)
            except UncertainMutation as exc:
                await self.db.update_listing(row["id"], status="uncertain", error=str(exc))
                raise
            if result.dry_run:
                await self.db.update_listing(row["id"], status="simulated", posted_at=utcnow(), dry_run=1)
                self.notifier.info("Mise en vente simulée", label, durée=f"{minutes} min")
            elif result.ok:
                await self.db.update_listing(row["id"], status="posted", posted_at=utcnow(), duration_minutes=minutes)
                self.notifier.info("Carte mise en vente", label, durée=f"{minutes} min")
            else:
                payload = result.payload if isinstance(result.payload, dict) else {}
                error = payload.get("error") or payload.get("message") or f"HTTP {result.status}"
                await self.db.update_listing(row["id"], status="failed", error=str(error)[:300])
                self.notifier.warning("Mise en vente refusée", f"{label} : {error}")

    async def _postpone(self, row) -> None:
        """Plus de place de vente : on vise la même fenêtre de fin, le jour suivant."""
        s = self.settings
        post_at, minutes, target = plan_listing_time(now_local() + timedelta(days=1), s.listing_end_window_start,
                                                     s.listing_end_window_end, s.listing_duration_options,
                                                     s.listing_min_duration_minutes)
        await self.db.update_listing(row["id"], post_at=max(post_at, now_local()), duration_minutes=minutes,
                                     target_end=target)
        self.log.info("ventes pleines : %s reportée, fin visée %s", row["title"], f"{target:%d/%m %H:%M}")

    # ------------------------------------------------------------ défausse
    async def discard(self, groups: dict[str, list[OwnedCard]], quotes: dict[str, dict[str, Quote]],
                      excluded: set[str], total: int) -> dict:
        s = self.settings
        candidates = select_for_discard(groups, quotes, excluded, s)
        count = total if s.achievement_count_mode == "total" else len(groups)
        allowed, why = achievement_guard(count, len(candidates), s.achievement_milestones,
                                         s.achievement_margin, affects_count=s.achievement_count_mode == "total")
        self.log.info("défausse : %d candidat(s), %d autorisé(s) — %s", len(candidates), allowed, why)
        if not allowed:
            return {"candidates": len(candidates), "discarded": 0, "guard": why}
        # Les moins cotés partent en premier.
        chosen = sorted(candidates, key=lambda c: (c.average if c.average is not None else -1))[:allowed]
        discarded = 0
        for i in range(0, len(chosen), 50):
            batch = chosen[i:i + 50]
            result = await self.api.bulk_discard([c.owned.id for c in batch])
            rows = [(c.owned.id, c.owned.catalog_id, c.owned.title, c.owned.rarity) for c in batch]
            if result.dry_run:
                await self.db.record_discards(rows, dry_run=True)
                discarded += len(batch)
                continue
            if not result.ok:
                self.notifier.warning("Défausse refusée", str(result.payload)[:300])
                break
            parsed = DiscardResult.model_validate(result.payload)
            discarded += parsed.discarded_count
            if parsed.failed or parsed.discarded_count != len(batch):
                # Résultat partiel : on ne devine pas quels exemplaires sont partis [WR discard.js].
                self.notifier.warning("Défausse partielle", f"{parsed.discarded_count}/{len(batch)} : arrêt, "
                                      "relance un scan pour resynchroniser.")
                break
            await self.db.record_discards(rows, dry_run=False)
        gain = discarded  # 1 WikiBidou par carte défaussée
        self.notifier.info("Défausse terminée", f"{discarded} carte(s) défaussée(s), +{gain} WB. {why}")
        return {"candidates": len(candidates), "discarded": discarded, "guard": why}


# ------------------------------------------------------------------- CLI
async def cli(ctx: Context, action: str, apply: bool) -> None:
    worker = InventoryManager(ctx)
    await worker.setup()
    s = ctx.settings
    if action == "scan" or not apply:
        groups, excluded, quotes, total = await worker.snapshot()
        sales = select_for_sale(groups, quotes, excluded, s)
        discards = select_for_discard(groups, quotes, excluded, s)
        count = total if s.achievement_count_mode == "total" else len(groups)
        allowed, why = achievement_guard(count, len(discards), s.achievement_milestones, s.achievement_margin,
                                         affects_count=s.achievement_count_mode == "total")
        print(f"\n== À vendre (cote ≥ {s.sell_min_value} WB, {len(sales)} exemplaire(s)) ==")
        for c in sales[:50]:
            print(f"  {c.owned.rarity:>2} {c.owned.title[:50]:<50} cote {c.average:>7.1f} -> prix {c.price}")
        print(f"\n== À défausser ({len(discards)} candidat(s), {allowed} autorisé(s)) ==\n  {why}")
        for c in discards[:30]:
            avg = "—" if c.average is None else f"{c.average:.1f}"
            print(f"  {c.owned.rarity:>2} {c.owned.title[:50]:<50} cote {avg}")
        if action != "scan":
            print("\nAjoute --apply pour exécuter (toujours simulé tant que DRY_RUN=true).")
        return
    report = await worker.run_cycle(list_cards=action == "list", discard=action == "discard")
    if action == "list":
        print(f"{report.get('listed', 0)} vente(s) et {report.get('relisted', 0)} revente(s) d'arbitrage programmées ;"
              " elles seront publiées par le bot en marche (docker compose).")
    else:
        print(report.get("discard"))
