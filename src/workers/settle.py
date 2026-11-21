"""Enchères finies que le site tarde à régler (« Finalisation… » sur la fiche) : finalisées par le bot, comme le lien de
la fiche (POST /api/marketplace/{id}/settle, relevé du 21/11). Côté achat (sniper) comme côté vente (inventaire)."""

from __future__ import annotations

from datetime import timedelta

from src.core.client import ApiError, server_message
from src.core.models import Auction

SETTLE_AFTER = timedelta(minutes=1)  # enchère finie depuis ce temps, encore « active » : finalisée par le bot
_asked: set[str] = set()  # enchères déjà finalisées par le bot (une demande chacune)


async def nudge_settle(worker, auction: Auction) -> bool:
    """Enchère finie depuis SETTLE_AFTER mais pas encore réglée par le site (« Finalisation… » sur la fiche, relevé du
    21/11) : le bot la finalise lui-même, comme le lien de la fiche, une seule fois par enchère (SETTLE_STUCK_AUCTIONS).
    La carte (ou les WikiBidous d'une vente) arrive plus tôt. True si la demande est partie."""
    if not worker.settings.settle_stuck_auctions or auction.id in _asked \
            or auction.seconds_left() > -SETTLE_AFTER.total_seconds():
        return False
    if len(_asked) > 5000:
        _asked.clear()
    _asked.add(auction.id)
    try:
        result = await worker.api.settle(auction.id)
    except ApiError as exc:
        worker.log.info("%s : finalisation refusée (%s)", auction.title, exc)
        return False
    if not result.dry_run:
        worker.log.info("%s : finie depuis %.0f s, pas encore réglée par le site : finalisée (%s)", auction.title,
                        -auction.seconds_left(), "faite" if result.ok else server_message(result.payload) or result.status)
    return not result.dry_run
