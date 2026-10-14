"""Module C : cotes de la collection, mise en vente aux heures de pointe, défausse protégée.

- Vente : doublons dont la cote moyenne (même rareté) dépasse SELL_MIN_VALUE, même C/PC.
  Fin d'enchère programmée dans LISTING_END_WINDOW (20:00-22:30, Paris).
- Positions : chaque carte achetée par le sniper est remise en vente en priorité (avant
  les doublons), au prix estimé ; invendue, elle est remise à prix réduit, puis gardée.
  Le résultat (bénéfice ou perte) est enregistré. En DRY_RUN, la revente est simulée.
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
from datetime import datetime, time, timedelta, timezone

from src.config import RARITY_RANK, Settings
from src.core.client import ApiError, RateLimited, UncertainMutation
from src.core.database import utcnow
from src.core.models import DiscardResult, MyBids, OwnedCard
from src.core.pricing import Quote
from src.core.timeutils import now_local, random_in_window
from src.workers.base import Context, Worker

INVENTORY_PRICE_MAX_AGE = timedelta(hours=12)
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


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
            if q and q.average is not None and q.average >= s.sell_min_value:
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


def parse_selling(mine: MyBids) -> list:
    from src.core.models import Auction, parse_items
    return parse_items(Auction, mine.selling, label="vente")


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
        await self.manage_positions()
        await self.post_due_listings()
        await self.settle_listings()
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
        protected = {t.casefold() for t in self.settings.protected_tags}
        excluded |= {r.id for r in rows if r.tag_names & protected}
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
            report["listed"] = await self.schedule_sales(select_for_sale(groups, quotes, excluded, self.settings))
        if discard:
            report["discard"] = await self.discard(groups, quotes, excluded, total)
        return report

    # ------------------------------------------------------------ places
    async def slot_usage(self) -> tuple[int, int]:
        """(ventes en cours, maximum). En DRY_RUN, les ventes simulées occupent aussi une place."""
        selling, maximum = await self.api.listing_slots()
        if self.settings.dry_run:
            selling += await self.db.simulated_active_listings(utcnow())
        return selling, maximum

    async def free_slots(self) -> int:
        """Places libres pour des doublons : les positions à revendre sont servies d'abord."""
        selling, maximum = await self.slot_usage()
        reserved = await self.db.pending_listings_count() + len(await self.db.positions_to_list())
        return max(0, maximum - selling - reserved)

    def _window(self, at_peak: bool) -> tuple[time, time]:
        s = self.settings
        if at_peak:
            return s.listing_end_window_start, s.listing_end_window_end
        soon = now_local() + timedelta(minutes=s.listing_min_duration_minutes + 1)
        return soon.time(), (soon + timedelta(minutes=30)).time()

    async def _schedule(self, *, owned_id: str, card_id: str, title: str, rarity: str | None, price: int,
                        reason: str, at_peak: bool = True, bid_id: int | None = None, relist_count: int = 0) -> bool:
        s = self.settings
        start, end = self._window(at_peak)
        post_at, minutes, target = plan_listing_time(now_local(), start, end, s.listing_duration_options,
                                                     s.listing_min_duration_minutes)
        listing_id = await self.db.schedule_listing(
            owned_card_id=owned_id, card_id=card_id, title=title, rarity=rarity, base_amount=price,
            duration_minutes=minutes, post_at=post_at, target_end=target, reason=reason, bid_id=bid_id,
            relist_count=relist_count,
        )
        if listing_id:
            self.log.info("vente programmée : %s (%s) %d WB, publication %s, fin %s",
                          title, rarity, price, f"{post_at:%d/%m %H:%M}", f"{target:%d/%m %H:%M}")
        return bool(listing_id)

    # ------------------------------------------------------- ventes de doublons
    async def schedule_sales(self, candidates: list[SaleCandidate]) -> int:
        s = self.settings
        count = 0
        slots = min(s.sell_max_listings_per_run, await self.free_slots())
        if candidates and not slots:
            self.log.info("aucune place de vente libre (%d candidat(s) en attente)", len(candidates))
        for c in candidates[:slots]:
            if await self._schedule(owned_id=c.owned.id, card_id=c.owned.catalog_id, title=c.owned.title,
                                    rarity=c.owned.rarity, price=c.price, reason=f"cote {c.average:.0f}"):
                count += 1
        if count:
            self.notifier.info("Ventes programmées", f"{count} doublon(s) mis en vente avec fin entre "
                               f"{s.listing_end_window_start:%H:%M} et {s.listing_end_window_end:%H:%M}.")
        return count

    # --------------------------------------------------------------- positions
    async def manage_positions(self) -> None:
        """Programme la revente des cartes achetées par le sniper (2 par passage au plus)."""
        s = self.settings
        for row in (await self.db.positions_to_list())[:2]:
            owned_id = f"sim-{row['id']}" if row["dry_run"] else await self._find_copy(row)
            if not owned_id:
                continue
            try:
                quote = await self.ctx.prices.quote(row["card_id"], row["rarity"], wait=False)
                estimate = quote.average
            except (RateLimited, ApiError):
                estimate = None
            estimate = estimate or row["reference_price"] or row["amount"]
            price = max(round(estimate * s.position_list_ratio), row["amount"] + 1)
            await self._schedule(owned_id=owned_id, card_id=row["card_id"], title=row["title"], rarity=row["rarity"],
                                 price=price, reason=f"position (achetée {row['amount']})",
                                 at_peak=s.position_list_at_peak, bid_id=row["id"])

    async def _find_copy(self, row) -> str | None:
        """Exemplaire acheté : le plus récent de cette carte dans la collection."""
        page = await self.api.collection_page(0, sort="rarity", q=row["title"])
        taken = await self.db.scheduled_owned_ids()
        copies = [r for r in page.rows() if r.catalog_id == row["card_id"] and r.id not in taken]
        if not copies:
            self.log.warning("position %s : exemplaire introuvable dans la collection (réessai)", row["title"])
            return None
        copies.sort(key=lambda r: r.obtained_at or _EPOCH, reverse=True)
        return copies[0].id

    # --------------------------------------------------------- publication
    async def post_due_listings(self) -> None:
        due = await self.db.due_listings(utcnow())
        if not due:
            return
        selling, maximum = await self.slot_usage()
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
            if row["dry_run"] or row["owned_card_id"].startswith("sim-"):
                result = None
            else:
                try:
                    result = await self.api.create_listing(row["owned_card_id"], row["base_amount"], minutes)
                except UncertainMutation as exc:
                    await self.db.update_listing(row["id"], status="uncertain", error=str(exc))
                    raise
            target_end = utcnow() + timedelta(minutes=minutes)
            if result is None or result.dry_run:
                await self.db.update_listing(row["id"], status="simulated", posted_at=utcnow(), dry_run=1,
                                             duration_minutes=minutes, target_end=target_end)
                self.notifier.info("Mise en vente simulée", label, durée=f"{minutes} min")
            elif result.ok:
                auction_id = await self._listing_auction_id(result.payload, row["card_id"])
                await self.db.update_listing(row["id"], status="posted", posted_at=utcnow(), duration_minutes=minutes,
                                             target_end=target_end, auction_id=auction_id)
                self.notifier.info("Carte mise en vente", label, durée=f"{minutes} min")
            else:
                payload = result.payload if isinstance(result.payload, dict) else {}
                error = payload.get("error") or payload.get("message") or f"HTTP {result.status}"
                await self.db.update_listing(row["id"], status="failed", error=str(error)[:300])
                self.notifier.warning("Mise en vente refusée", f"{label} : {error}")

    async def _listing_auction_id(self, payload, card_id: str) -> str | None:
        """Identifiant de l'enchère créée : dans la réponse, sinon dans mes ventes en cours."""
        if isinstance(payload, dict):
            for candidate in (payload.get("auction"), payload.get("listing"), payload):
                if isinstance(candidate, dict) and isinstance(candidate.get("id"), str):
                    return candidate["id"]
        try:
            mine = MyBids.model_validate(await self.ctx.client.get("my_bids"))
        except ApiError:
            return None
        selling = [a for a in parse_selling(mine) if a.catalog_id == card_id]
        return max(selling, key=lambda a: a.end_at).id if selling else None

    async def _postpone(self, row) -> None:
        """Plus de place de vente : on vise la même fenêtre de fin, le jour suivant."""
        s = self.settings
        start, end = self._window(row["bid_id"] is None or s.position_list_at_peak)
        post_at, minutes, target = plan_listing_time(now_local() + timedelta(days=1), start, end,
                                                     s.listing_duration_options, s.listing_min_duration_minutes)
        await self.db.update_listing(row["id"], post_at=max(post_at, now_local()), duration_minutes=minutes,
                                     target_end=target)
        self.log.info("ventes pleines : %s reportée, fin visée %s", row["title"], f"{target:%d/%m %H:%M}")

    # ------------------------------------------------------------- résultats
    async def settle_listings(self) -> None:
        for row in await self.db.listings_to_settle(utcnow() - timedelta(minutes=2)):
            bid = await self.db.get_bid(row["bid_id"]) if row["bid_id"] else None
            label = f"{row['title']} ({row['rarity']})"
            if row["status"] == "simulated":
                # Hypothèse optimiste : vendue à son prix de départ.
                await self.db.update_listing(row["id"], status="sim_sold", sale_price=row["base_amount"],
                                             settled_at=utcnow())
                if bid:
                    await self.db.close_position(bid["id"], row["base_amount"])
                    self.notifier.info("Revente simulée", f"{label} : achetée {bid['amount']}, vendue "
                                       f"{row['base_amount']} WB ({row['base_amount'] - bid['amount']:+d})")
                continue
            if not row["auction_id"]:
                await self.db.update_listing(row["id"], status="ended", settled_at=utcnow())
                if bid:
                    await self.db.close_position(bid["id"], None)
                    self.notifier.warning("Revente à vérifier", f"{label} : enchère introuvable, vérifie sur le site.")
                continue
            auction = await self.api.auction(row["auction_id"])
            if auction.status == "active":
                await self.db.update_listing(row["id"], target_end=auction.end_at)  # prolongée
                continue
            if auction.status == "settled_sold":
                price = auction.final_price or auction.current_bid or row["base_amount"]
                await self.db.update_listing(row["id"], status="sold", sale_price=price, settled_at=utcnow())
                if bid:
                    await self.db.close_position(bid["id"], price)
                    self.notifier.success("Revente réussie", f"{label} : achetée {bid['amount']}, vendue {price} WB "
                                          f"({price - bid['amount']:+d})")
                continue
            await self.db.update_listing(row["id"], status="unsold", settled_at=utcnow())
            if not bid:
                continue
            if row["relist_count"] < self.settings.position_max_relists:
                price = max(1, round(row["base_amount"] * self.settings.position_relist_discount))
                await self._schedule(owned_id=row["owned_card_id"], card_id=row["card_id"], title=row["title"],
                                     rarity=row["rarity"], price=price, reason=f"position (achetée {bid['amount']})",
                                     at_peak=self.settings.position_list_at_peak, bid_id=bid["id"],
                                     relist_count=row["relist_count"] + 1)
                self.log.info("%s invendue, remise en vente à %d WB", label, price)
            else:
                await self.db.close_position(bid["id"], None)
                self.notifier.warning("Position invendue", f"{label} : invendue après "
                                      f"{row['relist_count'] + 1} essai(s), carte gardée.")

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
        print(f"{report.get('listed', 0)} vente(s) programmée(s) ; elles seront publiées par le bot en marche.")
    else:
        print(report.get("discard"))
