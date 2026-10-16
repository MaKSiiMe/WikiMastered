"""Module C : cotes de la collection, mise en vente aux heures de pointe, défausse protégée.

- Vente : doublons dont la cote moyenne (même rareté) dépasse SELL_MIN_VALUE, même C/PC.
  Fin d'enchère programmée dans LISTING_END_WINDOW (20:00-22:30, Paris).
- Positions : chaque carte achetée par le sniper est remise en vente en priorité (avant
  les doublons). C'est une enchère : le prix de départ est le prix d'achat + la moitié du bénéfice attendu
  (POSITION_LIST_PROFIT_SHARE), au moins + POSITION_LIST_MARGIN ; les joueurs le font monter. Invendue, elle repart à −10 % sans passer sous le prix d'achat, puis est gardée.
  Le résultat (bénéfice ou perte) est enregistré. En DRY_RUN, la revente est simulée.
- Défausse : doublons sans valeur, seulement si aucun palier de succès de taille de
  collection n'est proche (ACHIEVEMENT_MILESTONES), par lots de 50 [WR discard.js].
- Jamais touchés : favoris, shiny, exemplaires en échange ou déjà en vente.

Le worker publie les ventes programmées (même celles créées en CLI). Les scans
automatiques ne tournent que si AUTO_LIST / AUTO_DISCARD sont activés.
"""

from __future__ import annotations

import csv
import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from src.config import RARITY_RANK, Settings
from src.core.client import ApiError, RateLimited, UncertainMutation
from src.core.database import utcnow
from src.core.models import DiscardResult, MyBids, OwnedCard
from src.core.notifier import Topic
from src.core.pricing import ANY, Quote
from src.core.timeutils import local_dt, now_local, random_in_window
from src.workers.base import Context, Worker

INVENTORY_PRICE_MAX_AGE = timedelta(days=3)  # la cote d'un doublon bouge peu
# Refus du site : « Cette carte est en favori : retirez-la des favoris pour la mettre en vente » (relevé du 16/10).
FAVORITE_HINT = "favori"
POSITION_MAX_LIST_FAILURES = 3  # mises en vente refusées avant de garder la carte
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)
SIM_MARKET_WINDOW = timedelta(days=3)  # ventes observées qui décident d'une revente simulée


# ---------------------------------------------------------------- logique pure
def list_price(cost: int, margin: float, fee: float = 0.0, estimate: float | None = None, share: float = 0.0) -> int:
    """Prix de départ d'une revente, commission du site payée : le prix d'achat + `share` du bénéfice attendu
    (`estimate` à l'achat), au moins le prix d'achat + `margin`, et toujours au moins +1 WB."""
    keep = max(0.01, 1 - fee)
    price = max(cost + 1, math.ceil(cost * (1 + margin) / keep))
    if estimate and share > 0:
        price = max(price, math.ceil((cost + share * (estimate * keep - cost)) / keep))
    return price


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
            card_quotes = quotes.get(card_id, {})
            q = card_quotes.get(row.rarity or "") or card_quotes.get(ANY)
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
            if row.rarity not in s.discard_rarities or row.card.is_gif:
                continue  # GIF : se vendraient mieux (16/10), jamais défaussés tant que ce n'est pas mesuré
            q = quotes.get(card_id, {}).get(row.rarity or "")
            avg = q.average if q else None
            if (avg is None and s.discard_unknown_price_is_worthless) or (avg is not None and avg <= s.discard_max_value):
                out.append(DiscardCandidate(row, avg))
    return out


def valuation(groups: dict[str, list[OwnedCard]], quotes: dict[str, dict[str, Quote]], excluded: set[str],
              s: Settings) -> list[dict]:
    """Une ligne par carte et rareté : exemplaires, cote, et ce que deviennent les doublons (mêmes règles que la
    vente et la défausse automatiques). Les exemplaires gardés (1er exemplaire, favoris, shiny, étiquettes
    protégées, en échange ou déjà en vente) ne sont ni vendus ni défaussés."""
    sale = {c.owned.id: c for c in select_for_sale(groups, quotes, excluded, s)}
    discard = {c.owned.id for c in select_for_discard(groups, quotes, excluded, s)}
    keep = min(s.sell_keep_copies, max(1, s.discard_keep_copies))
    rows: dict[tuple[str, str], dict] = {}
    for card_id, copies in groups.items():
        spare = {c.id for c in spare_copies(copies, keep, excluded)}
        for copy in copies:
            rarity = copy.rarity or "?"
            q = quotes.get(card_id, {}).get(rarity) or quotes.get(card_id, {}).get(ANY)
            row = rows.setdefault((card_id, rarity), {
                "carte": copy.title, "rareté": rarity, "gif": "oui" if copy.card.is_gif else "",
                "cote": round(q.average) if q and q.average is not None else None,
                "exemplaires": 0, "à vendre": 0, "prix de vente": 0, "à défausser": 0, "doublons gardés": 0,
                "sans cote": 0})
            row["exemplaires"] += 1
            if copy.id in sale:
                row["à vendre"] += 1
                row["prix de vente"] += sale[copy.id].price
            elif copy.id in discard:
                row["à défausser"] += 1
            elif copy.id in spare:
                row["sans cote" if row["cote"] is None else "doublons gardés"] += 1
    return sorted(rows.values(), key=lambda r: (r["cote"] is None, -(r["cote"] or 0), r["carte"]))


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


def plan_position_time(now: datetime, start: time, end: time, minutes: int, durations: list[int],
                       rng: random.Random | None = None) -> tuple[datetime, int, datetime]:
    """(publication, durée, fin) d'une revente : publiée entre `start` et `end` (aujourd'hui si ce n'est pas
    passé, sinon demain), pour la durée du site la plus proche de `minutes`."""
    if durations:
        minutes = min(durations, key=lambda d: abs(d - minutes))
    post_at = random_in_window(now.date(), start, end, rng)
    if post_at < now:
        if local_dt(now.date(), end) > now:
            post_at = now  # déjà dans la fenêtre : publiée tout de suite
        else:
            post_at = random_in_window(now.date() + timedelta(days=1), start, end, rng)
    return post_at, minutes, post_at + timedelta(minutes=minutes)


def plan_listing_time(now: datetime, start: time, end: time, durations: list[int], min_duration: int,
                      rng: random.Random | None = None, preferred: int = 360) -> tuple[datetime, int, datetime]:
    """(heure de publication, durée en minutes, fin visée) pour finir dans la fenêtre [start, end].

    Parmi les durées du site, on prend la plus proche de `preferred` : assez longue pour
    être vue, sans bloquer une des 5 places de vente toute une journée."""
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
            post_at, minutes = min(feasible, key=lambda f: (abs(f[1] - preferred), f[0]))
            return post_at, minutes, target
    raise ValueError("aucune durée de vente ne permet de finir dans la fenêtre demandée")


# ---------------------------------------------------------------------- worker
class InventoryManager(Worker):
    name = "inventory"
    topic = Topic.SALES

    async def setup(self) -> None:
        self._next_scan = utcnow() + timedelta(minutes=2)
        self._next_sale_check = utcnow() + timedelta(minutes=2)
        # Doublons à vendre, du mieux coté au moins coté : relus au plus toutes les INVENTORY_INTERVAL_MINUTES
        # (lire la collection coûte ~175 requêtes), consommés à chaque place libérée.
        self._sale_queue: list[SaleCandidate] = []
        self._queue_at: datetime | None = None
        self._starred_warned: set[int] = set()  # positions dont l'exemplaire est en favori (prévenu une fois)
        reopened = await self.db.reopen_favorite_positions()
        if reopened:
            self.log.info("%d revente(s) abandonnée(s) pour une carte en favori : reprise(s)", reopened)
        if self.settings.position_post_now:
            # Programmées avec l'ancienne règle (lendemain 11 h-12 h) : publiées dès maintenant. Une seule fois, au
            # démarrage : une revente reportée faute de place de vente ne doit pas être avancée en boucle.
            advanced = await self.db.advance_position_listings(utcnow())
            if advanced:
                self.log.info("%d revente(s) programmée(s) avancée(s) : publiées maintenant", advanced)

    async def step(self) -> float:
        await self.reconcile_listings()
        await self.manage_positions()
        await self.post_due_listings()
        await self.settle_listings()
        s = self.settings
        now = utcnow()
        if s.auto_list and now >= self._next_sale_check:
            self._next_sale_check = now + timedelta(minutes=s.sell_check_minutes)
            await self.fill_free_slots()
        if s.auto_discard and now >= self._next_scan:
            self._next_scan = now + timedelta(minutes=s.inventory_interval_minutes)
            await self.run_cycle(list_cards=False, discard=True)
        return 30.0

    async def fill_free_slots(self) -> int:
        """Remplit les places de vente libres avec les doublons les mieux cotés."""
        s = self.settings
        free = min(s.sell_max_listings_per_run, await self.free_slots())
        if free <= 0:
            return 0
        stale = self._queue_at is None or utcnow() - self._queue_at > timedelta(minutes=s.inventory_interval_minutes)
        if not self._sale_queue and stale:
            groups, excluded, quotes, _ = await self.snapshot()
            self._sale_queue, self._queue_at = select_for_sale(groups, quotes, excluded, s), utcnow()
            self.log.info("%d doublon(s) à vendre (cote ≥ %d WB)", len(self._sale_queue), s.sell_min_value)
        batch, self._sale_queue = self._sale_queue[:free], self._sale_queue[free:]
        return await self.schedule_sales(batch, slots=free)

    # ------------------------------------------------------------ analyse
    async def snapshot(self, lookups: int | None = None
                       ) -> tuple[dict[str, list[OwnedCard]], set[str], dict[str, dict[str, Quote]], int]:
        rows = await self.api.load_collection()
        groups = group_by_card(rows)
        excluded = await self.api.protected_owned_ids({r.id for r in rows})
        excluded |= await self.db.scheduled_owned_ids()
        protected = {t.casefold() for t in self.settings.protected_tags}
        excluded |= {r.id for r in rows if r.tag_names & protected}
        to_price = [cid for cid, copies in groups.items() if spare_copies(copies, min(self.settings.sell_keep_copies,
                    max(1, self.settings.discard_keep_copies)), excluded)]
        self.log.info("collection : %d exemplaires, %d cartes, %d à coter", len(rows), len(groups), len(to_price))
        # La route des cotes est limitée par le site : au plus SELL_PRICE_LOOKUPS_PER_RUN nouvelles cotes
        # par passage (cartes aux plus nombreux doublons d'abord). Les autres : cache ou ventes observées.
        # Le cache (3 jours) se remplit ainsi au fil des passages.
        to_price.sort(key=lambda cid: len(groups[cid]), reverse=True)
        quotes: dict[str, dict[str, Quote]] = {}
        budget = self.settings.sell_price_lookups_per_run if lookups is None else lookups
        looked_up = 0
        for card_id in to_price:
            fresh = await self.ctx.prices.is_fresh(card_id, INVENTORY_PRICE_MAX_AGE)
            if fresh or looked_up < budget:
                looked_up += not fresh
                try:
                    quotes[card_id] = await self.ctx.prices.quotes(card_id, max_age=INVENTORY_PRICE_MAX_AGE)
                    continue
                except (RateLimited, ApiError) as exc:
                    self.log.debug("cote indisponible pour %s (%s)", card_id, exc)
            quotes[card_id] = await self.ctx.prices.observed_quote(card_id)
        known = sum(1 for q in quotes.values() if q)
        self.log.info("cotes : %d demandée(s) au site, %d carte(s) cotée(s) sur %d", looked_up, known, len(to_price))
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
    @property
    def listing_live(self) -> bool:
        return self.settings.is_live("create_listing")

    async def slot_usage(self) -> tuple[int, int]:
        """(ventes en cours, maximum). Si les ventes sont simulées, les ventes simulées occupent aussi une place ;
        si elles sont réelles (sniper seul simulé), les reventes simulées n'occupent pas tes vraies places."""
        selling, maximum = await self.api.listing_slots()
        if selling is None:  # compteur absent : on compte les ventes publiées par le bot
            selling = await self.db.posted_listings_count()
        if not self.listing_live:
            selling += await self.db.simulated_active_listings(utcnow())
        return selling, maximum

    async def free_slots(self) -> int:
        """Places libres pour des doublons : les positions à revendre sont servies d'abord."""
        selling, maximum = await self.slot_usage()
        real_only = self.listing_live
        positions = [r for r in await self.db.positions_to_list() if not (real_only and r["dry_run"])]
        reserved = await self.db.pending_listings_count(real_only) + len(positions)
        return max(0, maximum - selling - reserved)

    def _plan(self, now: datetime, *, position: bool, at_peak: bool) -> tuple[datetime, int, datetime]:
        """Revente d'une position : publiée vers 11 h-12 h pour 12 h ; doublon : fin dans la fenêtre choisie."""
        s = self.settings
        if position and s.position_post_now:
            minutes = min(s.listing_duration_options or [s.position_list_duration_minutes],
                          key=lambda d: abs(d - s.position_list_duration_minutes))
            return now, minutes, now + timedelta(minutes=minutes)
        if position:
            return plan_position_time(now, s.position_post_start, s.position_post_end,
                                      s.position_list_duration_minutes, s.listing_duration_options)
        start, end = self._window(at_peak)
        return plan_listing_time(now, start, end, s.listing_duration_options, s.listing_min_duration_minutes,
                                 preferred=s.listing_preferred_duration_minutes)

    def _window(self, at_peak: bool) -> tuple[time, time]:
        s = self.settings
        soon = now_local() + timedelta(minutes=max(s.listing_min_duration_minutes,
                                                   s.listing_preferred_duration_minutes) + 1)
        # Une vente qui finirait en pleine nuit (peu d'acheteurs) vise plutôt l'heure de pointe.
        if at_peak or soon.time() < time(9, 0):
            return s.listing_end_window_start, s.listing_end_window_end
        return soon.time(), (soon + timedelta(minutes=30)).time()

    async def _schedule(self, *, owned_id: str, card_id: str, title: str, rarity: str | None, price: int,
                        reason: str, at_peak: bool = True, bid_id: int | None = None, relist_count: int = 0) -> bool:
        post_at, minutes, target = self._plan(now_local(), position=bid_id is not None, at_peak=at_peak)
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
    async def schedule_sales(self, candidates: list[SaleCandidate], slots: int | None = None) -> int:
        """Programme la vente de doublons (pas de message Discord : le bilan du soir les compte)."""
        s = self.settings
        count = 0
        if slots is None:
            slots = min(s.sell_max_listings_per_run, await self.free_slots())
        if candidates and not slots:
            self.log.info("aucune place de vente libre (%d candidat(s) en attente)", len(candidates))
        for c in candidates[:slots]:
            if await self._schedule(owned_id=c.owned.id, card_id=c.owned.catalog_id, title=c.owned.title,
                                    rarity=c.owned.rarity, price=c.price, reason=f"cote {c.average:.0f}",
                                    at_peak=not s.sell_anytime):
                count += 1
        if count:
            self.log.info("%d doublon(s) mis en vente", count)
        return count

    # --------------------------------------------------------------- positions
    async def manage_positions(self) -> None:
        """Programme la revente des cartes achetées par le sniper (2 par passage au plus)."""
        s = self.settings
        for row in (await self.db.positions_to_list())[:2]:
            failures = await self.db.failed_listings_count(row["id"])
            if failures >= POSITION_MAX_LIST_FAILURES:
                await self.db.close_position(row["id"], None)
                self.notifier.warning("Revente abandonnée", f"{row['title']} ({row['rarity']}) : mise en vente "
                                      f"refusée {failures} fois, carte gardée. Voir les erreurs dans les logs.")
                continue
            owned_id = f"sim-{row['id']}" if row["dry_run"] else await self._find_copy(row)
            if not owned_id:
                continue
            price = list_price(row["amount"], s.position_list_margin, s.sale_fee_pct,
                               estimate=row["reference_price"], share=s.position_list_profit_share)
            await self._schedule(owned_id=owned_id, card_id=row["card_id"], title=row["title"], rarity=row["rarity"],
                                 price=price, reason=f"position (achetée {row['amount']})", bid_id=row["id"])

    async def _find_copy(self, row) -> str | None:
        """Exemplaire acheté : le plus récent de cette carte dans la collection, jamais un favori (le site refuse
        de le vendre : le 16/10, Place publique refusée 3 fois « en favori », puis revente abandonnée)."""
        page = await self.api.collection_page(0, sort="rarity", q=row["title"])
        taken = await self.db.scheduled_owned_ids()
        copies = [r for r in page.rows() if r.catalog_id == row["card_id"] and r.id not in taken]
        if not copies:
            self.log.warning("position %s : exemplaire introuvable dans la collection (réessai)", row["title"])
            return None
        if all(c.starred for c in copies):
            if row["id"] not in self._starred_warned:
                self._starred_warned.add(row["id"])
                self.notifier.warning("Revente en attente : carte en favori",
                                      f"{row['title']} ({row['rarity']}) est en favori, le site refuse de la vendre. "
                                      "Retire l'étoile pour que le bot la revende (il réessaie toutes les 10 min).")
            self.log.debug("position %s : seul exemplaire en favori (réessai)", row["title"])
            return None
        copies = [c for c in copies if not c.starred]
        copies.sort(key=lambda r: r.obtained_at or _EPOCH, reverse=True)
        return copies[0].id

    # --------------------------------------------------------- publication
    async def post_due_listings(self) -> None:
        due = await self.db.due_listings(utcnow())
        if not due:
            return
        selling, maximum = await self.slot_usage()
        for row in due:
            simulated = bool(row["dry_run"]) or row["owned_card_id"].startswith("sim-")
            if not (simulated and self.listing_live):  # une revente simulée n'occupe pas une vraie place
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
            if simulated:
                result = None
            else:
                try:
                    result = await self.api.create_listing(row["owned_card_id"], row["base_amount"], minutes)
                except UncertainMutation as exc:
                    await self.db.update_listing(row["id"], status="uncertain", error=str(exc), posted_at=utcnow(),
                                                 duration_minutes=minutes)
                    raise
            target_end = utcnow() + timedelta(minutes=minutes)
            if result is None or result.dry_run:
                await self.db.update_listing(row["id"], status="simulated", posted_at=utcnow(), dry_run=1,
                                             duration_minutes=minutes, target_end=target_end)
                if row["bid_id"] and self.settings.notify_simulated:  # positions : message ; doublons : bilan
                    self.notifier.info("Mise en vente simulée", label, durée=f"{minutes} min")
            elif result.ok:
                auction_id = await self._listing_auction_id(result.payload, row["card_id"])
                await self.db.update_listing(row["id"], status="posted", posted_at=utcnow(), duration_minutes=minutes,
                                             target_end=target_end, auction_id=auction_id)
                if row["bid_id"]:
                    self.notifier.info("Carte mise en vente", label, durée=f"{minutes} min", important=True)
                else:
                    self.log.info("doublon mis en vente : %s (%d min)", label, minutes)
            else:
                payload = result.payload if isinstance(result.payload, dict) else {}
                error = payload.get("error") or payload.get("message") or f"HTTP {result.status}"
                # Carte en favori : pas un échec (ne mène pas à l'abandon) ; l'exemplaire est recherché de nouveau.
                status = "favorite" if FAVORITE_HINT in str(error).lower() else "failed"
                await self.db.update_listing(row["id"], status=status, error=str(error)[:300])
                self.notifier.warning("Mise en vente refusée", f"{label} : {error}")

    async def _listing_auction_id(self, payload, card_id: str) -> str | None:
        """Identifiant de l'enchère créée : dans la réponse, sinon dans mes ventes en cours."""
        if isinstance(payload, dict):
            # Réponse réelle (relevé du 14/10/2026) : HTTP 201 {"auction_id": "<uuid>"}.
            if isinstance(payload.get("auction_id"), str):
                return payload["auction_id"]
            for candidate in (payload.get("auction"), payload.get("listing"), payload):
                if isinstance(candidate, dict) and isinstance(candidate.get("id"), str):
                    return candidate["id"]
        try:
            mine = MyBids.model_validate(await self.ctx.client.get("my_bids"))
        except ApiError:
            return None
        selling = [a for a in parse_selling(mine) if a.catalog_id == card_id]
        return max(selling, key=lambda a: a.end_at).id if selling else None

    async def reconcile_listings(self) -> None:
        """Mises en vente au résultat incertain : cherchées dans mes ventes en cours, jamais rejouées."""
        rows = await self.db.uncertain_listings(utcnow() - timedelta(minutes=1))
        if not rows:
            return
        selling = parse_selling(MyBids.model_validate(await self.ctx.client.get("my_bids")))
        known = await self.db.listed_auction_ids()
        for row in rows:
            label = f"{row['title']} ({row['rarity']}) à {row['base_amount']} WB"
            match = [a for a in selling if a.catalog_id == row["card_id"] and a.base_amount == row["base_amount"]
                     and a.id not in known]
            if match:
                auction = max(match, key=lambda a: a.end_at)
                known.add(auction.id)
                await self.db.update_listing(row["id"], status="posted", auction_id=auction.id,
                                             target_end=auction.end_at, error=None)
                self.notifier.info("Mise en vente vérifiée", f"{label} : bien en vente.")
            else:
                await self.db.update_listing(row["id"], status="failed", error="non créée (vérifiée dans mes ventes)")
                again = " Elle sera reprogrammée." if row["bid_id"] else ""
                self.notifier.info("Mise en vente vérifiée", f"{label} : pas créée.{again}")

    async def _postpone(self, row) -> None:
        """Plus de place de vente. Revente d'une position publiée tout de suite (POSITION_POST_NOW) : nouvel essai
        dans SELL_CHECK_MINUTES, dès qu'une place se libère (le 16/10, Place publique était reportée de 24 h).
        Sinon : même horaire, le jour suivant."""
        s = self.settings
        if row["bid_id"] is not None and s.position_post_now:
            retry = utcnow() + timedelta(minutes=s.sell_check_minutes)
            await self.db.update_listing(row["id"], post_at=retry,
                                         target_end=retry + timedelta(minutes=row["duration_minutes"] or 0))
            self.log.debug("ventes pleines : %s attend une place (nouvel essai à %s)", row["title"],
                           f"{retry.astimezone(now_local().tzinfo):%H:%M}")
            return
        post_at, minutes, target = self._plan(now_local() + timedelta(days=1), position=row["bid_id"] is not None,
                                              at_peak=True)
        await self.db.update_listing(row["id"], post_at=max(post_at, now_local()), duration_minutes=minutes,
                                     target_end=target)
        self.log.info("ventes pleines : %s reportée, fin visée %s", row["title"], f"{target:%d/%m %H:%M}")

    # ------------------------------------------------------------- résultats
    async def settle_listings(self) -> None:
        for row in await self.db.listings_to_settle(utcnow() - timedelta(minutes=2)):
            bid = await self.db.get_bid(row["bid_id"]) if row["bid_id"] else None
            label = f"{row['title']} ({row['rarity']})"
            if row["status"] == "simulated":
                price = await self._simulated_sale(row, bid)
                if price is None:
                    await self.db.update_listing(row["id"], status="sim_unsold", settled_at=utcnow())
                    await self._unsold(row, bid, label)
                    continue
                await self.db.update_listing(row["id"], status="sim_sold", sale_price=price, settled_at=utcnow())
                if bid:
                    await self.db.close_position(bid["id"], price)
                    self.log.info("revente simulée : %s, achetée %d, vendue %d WB", label, bid["amount"], price)
                if bid and self.settings.notify_simulated:
                    self.notifier.info("Revente simulée", f"{label} : achetée {bid['amount']}, départ "
                                       f"{row['base_amount']}, vendue {price} WB ({price - bid['amount']:+d})")
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
                                          f"({price - bid['amount']:+d})", important=True)
                continue
            await self.db.update_listing(row["id"], status="unsold", settled_at=utcnow())
            await self._unsold(row, bid, label)

    async def _simulated_sale(self, row, bid) -> int | None:
        """Prix de vente simulé : la médiane des ventes observées ces derniers jours, si elle atteint le prix
        de départ (les joueurs font monter l'enchère jusqu'au prix du marché).

        Personne ne mise sous le prix de départ : si les ventes observées de ces derniers jours
        sont plus basses (tendance retombée) ou absentes, la vente simulée reste invendue (None)."""
        prices = await self.db.observed_prices_since(row["card_id"], utcnow() - SIM_MARKET_WINDOW,
                                                     self.settings.price_recent_sales, rarity=row["rarity"],
                                                     shiny=bool(bid["is_shiny"]) if bid else None)
        market = round(statistics.median(prices)) if prices else 0
        return market if prices and market >= row["base_amount"] else None

    async def _unsold(self, row, bid, label: str) -> None:
        """Invendue : remise en vente à prix réduit, puis carte gardée (réel comme simulé)."""
        if not bid:
            return
        if row["relist_count"] < self.settings.position_max_relists:
            # Jamais sous le prix d'achat (commission comprise) : une revente à perte n'a pas d'intérêt.
            floor = list_price(bid["amount"], 0.0, self.settings.sale_fee_pct)
            price = max(floor, round(row["base_amount"] * self.settings.position_relist_discount))
            self.log.info("%s invendue, remise en vente à %d WB", label, price)  # avant « vente programmée »
            if not bid["dry_run"]:
                self.notifier.info("Revente sans acheteur", f"{label} : remise en vente à {price} WB "
                                   f"(achetée {bid['amount']}).", important=True)
            await self._schedule(owned_id=row["owned_card_id"], card_id=row["card_id"], title=row["title"],
                                 rarity=row["rarity"], price=price, reason=f"position (achetée {bid['amount']})",
                                 bid_id=bid["id"], relist_count=row["relist_count"] + 1)
        else:
            await self.db.close_position(bid["id"], None)
            if not bid["dry_run"] or self.settings.notify_simulated:
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
async def report(ctx: Context, worker: InventoryManager, lookups: int) -> None:
    """Toute la collection cotée : résumé à l'écran, détail par carte dans data/inventaire-<date>.csv."""
    s = ctx.settings
    groups, excluded, quotes, total = await worker.snapshot(lookups=lookups)
    rows = valuation(groups, quotes, excluded, s)
    path = s.data_dir / f"inventaire-{now_local():%Y%m%d-%H%M}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["carte"])
        writer.writeheader()
        writer.writerows(rows)
    count = lambda key: sum(r[key] for r in rows)  # noqa: E731
    sell, wb, discard = count("à vendre"), count("prix de vente"), count("à défausser")
    kept, unknown = count("doublons gardés"), count("sans cote")
    allowed, why = achievement_guard(total if s.achievement_count_mode == "total" else len(groups), discard,
                                     s.achievement_milestones, s.achievement_margin,
                                     affects_count=s.achievement_count_mode == "total")
    print(f"\n{total} exemplaires, {len(groups)} cartes différentes")
    print(f"  À vendre      : {sell} doublon(s) coté(s) ≥ {s.sell_min_value} WB, ~{wb} WB au total "
          f"(5 ventes à la fois : les mieux cotés d'abord)")
    print(f"  À défausser   : {discard} doublon(s) {'/'.join(s.discard_rarities)} coté(s) ≤ {s.discard_max_value} WB, "
          f"+{discard} WB ; {allowed} permis aujourd'hui ({why})")
    print(f"  Gardés        : {kept} doublon(s) entre les deux seuils ou d'une rareté jamais défaussée")
    print(f"  Sans cote     : {unknown} doublon(s) ; relance avec --lookups plus grand (cotes gardées 3 jours)")
    by_rarity: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        by_rarity[r["rareté"]][0] += r["à vendre"]
        by_rarity[r["rareté"]][1] += r["prix de vente"]
        by_rarity[r["rareté"]][2] += r["à défausser"]
    print("\nPar rareté (à vendre / WB / à défausser) :")
    for rarity, (n, value, d) in sorted(by_rarity.items(), key=lambda kv: -RARITY_RANK.get(kv[0], -1)):
        print(f"  {rarity:>2} : {n} / {value} WB / {d}")
    print("\nLes 25 ventes les plus intéressantes :")
    for r in sorted(rows, key=lambda r: -r["prix de vente"])[:25]:
        if r["à vendre"]:
            print(f"  {r['rareté']:>2} {r['carte'][:45]:<45} cote {r['cote']:>5} × {r['à vendre']} = {r['prix de vente']} WB")
    print(f"\nDétail par carte : {path}")


async def cli(ctx: Context, action: str, apply: bool, lookups: int | None = None) -> None:
    worker = InventoryManager(ctx)
    await worker.setup()
    s = ctx.settings
    if action == "report":
        await report(ctx, worker, s.sell_price_lookups_per_run if lookups is None else lookups)
        return
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
    outcome = await worker.run_cycle(list_cards=action == "list", discard=action == "discard")
    if action == "list":
        print(f"{outcome.get('listed', 0)} vente(s) programmée(s) ; elles seront publiées par le bot en marche.")
    else:
        print(outcome.get("discard"))
