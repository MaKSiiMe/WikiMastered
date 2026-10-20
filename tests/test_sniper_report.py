"""Récap de nuit du sniper : toutes les enchères misées, gain latent, gain réalisé, gain non acquis."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.core.database import Database
from src.sniper_report import breakdown, build, render


@pytest.mark.asyncio
async def test_night_report_counts_every_bid_and_its_money(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    now = datetime.now(UTC)

    async def bid(aid, title, amount, status, *, ref=None, ceiling=None, dry_run=False, reason="revente",
                  **fields):
        bid_id = await db.record_bid(auction_id=aid, card_id=f"c-{aid}", title=title, rarity="L", is_shiny=False,
                                     amount=amount, reference_price=ref, reason=reason, night=True, dry_run=dry_run,
                                     status=status, end_at=now, ceiling=ceiling)
        if fields:
            await db.update_bid(bid_id, **fields)
        return bid_id

    held = await bid("a1", "Romelu Lukaku", 481, "won", ref=881, ceiling=520)
    await db.schedule_listing(owned_card_id="o1", card_id="c-a1", title="Romelu Lukaku", rarity="L", base_amount=681,
                              duration_minutes=720, post_at=now + timedelta(hours=3),
                              target_end=now + timedelta(hours=15), reason="position", bid_id=held)
    await bid("a2", "Le Parrain", 150, "won", ref=550, closed=1, sale_price=400)
    await bid("a3", "Miley Cyrus", 280, "outbid", ref=900, ceiling=500)  # surenchérie, puis rejouée
    await bid("a3", "Miley Cyrus", 310, "lost", ref=900, ceiling=500, final_price=450)
    await bid("a4", "Cent Ans", 400, "outbid", ref=700, ceiling=500)
    await db.db.execute("INSERT INTO observed_sales (auction_id, card_id, title, rarity, price, ended_at) "
                        "VALUES ('a4', 'c-a4', 't', 'L', 650, ?)", (now.isoformat(),))
    await db.db.commit()
    await bid("a5", "Squid Game", 300, "rejected", ref=800, note="Vérification anti-bot requise.")
    await bid("s1", "Mème", 200, "sim_won", ref=600, dry_run=True)
    await bid("k1", "14 octobre", 10, "won", reason="collection : carte manquante ≤ 20")
    await bid("k2", "15 octobre", 12, "outbid", reason="collection : carte manquante ≤ 20", final_price=25)

    blocks = build(await db.bids_since(now - timedelta(hours=1)))
    real, sim, col = blocks["real"], blocks["sim"], blocks["collection"]
    assert (real.bids, real.won, real.lost, real.refused) == (5, 2, 2, 1)
    assert real.spent == 631 and real.latent == 400 and real.realized == 250
    assert real.missed == 450 + 50 and real.missed_within == 450  # Cent Ans : partie au-dessus de notre plafond
    assert (sim.won, sim.latent) == (1, 400) and (col.won, col.lost, col.spent) == (1, 1, 10)

    title, summary, sections = render(blocks, now - timedelta(hours=1), now, limit=None)
    text = dict(sections)["Positions réelles"]
    assert "✅ **Romelu Lukaku** (L) : achetée 481, estimée ~881 → latente +400 · en vente le" in text
    assert "à 681" in text and "✅ **Le Parrain** (L) : achetée 150, revendue 400 (+250)" in text
    assert "❌ **Miley Cyrus** (L) : misé 310, 2 mises, partie à 450, estimée ~900 → manqué ~450 · dans nos limites" in text
    assert "partie à 650, estimée ~700 → manqué ~50 · au-dessus de notre plafond (500)" in text
    assert "⛔ **Squid Game** (L) : 300 WB refusée (Vérification anti-bot requise.)" in text
    assert "gain non acquis ~500 WB (dont ~450 dans nos limites)" in summary
    # Chiffres pour régler la stratégie (17/10) : lisibles dans le récap, sans la liste complète.
    assert ("↳ 6 mise(s) pour 5 enchère(s) · perdues : 1/2 dans nos limites, prix final médian = notre mise ×1.54"
            " · refus : anti-bot 1, mise trop basse 0, autres 0 · revendues : 1, prix / estimation médian ×0.73"
            ) in summary
    assert title.startswith("Sniper : récap du ")
    await db.close()


@pytest.mark.asyncio
async def test_real_bid_lost_at_our_own_price_is_flagged_not_counted(tmp_path):
    """17/10 : Shakira « perdue » 479 → 479. Incohérent pour une mise réelle : signalé, hors gain non acquis."""
    db = Database(tmp_path / "t.db")
    await db.connect()
    now = datetime.now(UTC)
    await db.record_bid(auction_id="a1", card_id="c1", title="Shakira", rarity="L", is_shiny=False, amount=479,
                        reference_price=1947, reason="revente", night=False, dry_run=False, status="lost",
                        end_at=now, ceiling=500)
    await db.update_bid((await db.last_bid("a1"))["id"], final_price=479)
    blocks = build(await db.bids_since(now - timedelta(hours=1)))
    real = blocks["real"]
    assert real.lost == 1 and real.missed == 0 and real.lost_within == 0 and not real.lost_steps
    _, _, sections = render(blocks, now - timedelta(hours=1), now, limit=None)
    assert "❌ **Shakira** (L) : misé 479, partie à 479 · ⚠ prix final ≤ notre mise : à vérifier" \
        in dict(sections)["Positions réelles"]
    await db.close()


@pytest.mark.asyncio
async def test_breakdown_splits_win_rate_by_contest_and_bid_share(tmp_path):
    """20/10 : 16 L gagnées sur 153 misées. Quelles enchères sont gagnables (quota anti-bot à garder) ?"""
    db = Database(tmp_path / "t.db")
    await db.connect()
    now = datetime.now(UTC)

    async def bid(aid, amount, status, ref, *, reason="revente", dry_run=False, **fields):
        extra = {k: fields.pop(k) for k in ("ceiling", "min_bid", "start_price", "seconds_left") if k in fields}
        bid_id = await db.record_bid(auction_id=aid, card_id=f"c-{aid}", title=aid, rarity="L", is_shiny=False,
                                     amount=amount, reference_price=ref, reason=reason, night=False,
                                     dry_run=dry_run, status=status, end_at=now, **extra)
        if fields:
            await db.update_bid(bid_id, **fields)

    # Personne n'avait misé, revendue 1100 : +500.
    await bid("a", 600, "won", 1000, min_bid=500, start_price=500, seconds_left=45, closed=1, sale_price=1100)
    await bid("b", 950, "lost", 2000, min_bid=900, start_price=400, seconds_left=20, final_price=1900)
    # Mise d'avant la colonne min_bid : minimum recalculé (654 = 550 + 0,5 × (758 − 550)), départ lu dans la vente
    # observée (20/10 : l'enchère terminée n'est plus dans auctions_seen, tout tombait en « inconnu »).
    await bid("c", 654, "lost", 1100, ceiling=758, final_price=1300)
    await db.db.execute("INSERT INTO observed_sales (auction_id, card_id, title, rarity, price, ended_at, "
                        "base_amount) VALUES ('c', 'c-c', 'c', 'L', 1300, ?, 500)", (now.isoformat(),))
    await db.db.commit()
    await bid("s", 350, "sim_won", 800, dry_run=True, min_bid=300, start_price=300)
    await bid("k", 10, "won", None, reason="collection : carte manquante ≤ 20", min_bid=10, start_price=10)
    await bid("r", 400, "rejected", 900, min_bid=380, start_price=380, note="Vérification anti-bot requise.")

    text = breakdown(await db.bids_since(now - timedelta(hours=1)))
    assert "  personne n'avait misé : réelles 1/1 (100 %, +500 WB/enchère) · simulées 1/1 (100 %, +450 WB/enchère)" \
        in text
    assert "  déjà misée, minimum ≤ ×1,5 le départ : réelles 0/1 (0 %, +0 WB/enchère) · simulées 0" in text
    assert "  déjà misée, minimum ×1,5 à ×3 : réelles 0/1 (0 %, +0 WB/enchère) · simulées 0" in text
    assert "  55-70 % : réelles 1/2 (50 %, +250 WB/enchère) · simulées 0" in text
    assert "  40-55 % : réelles 0/1 (0 %, +0 WB/enchère) · simulées 1/1 (100 %, +450 WB/enchère)" in text
    assert "  inconnu : réelles 0/1 (0 %, +0 WB/enchère) · simulées 1/1 (100 %, +450 WB/enchère)" in text
    assert text.index("40-55 %") < text.index("55-70 %") and "collection" not in text
    await db.close()
