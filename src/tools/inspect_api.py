"""Inspection des vraies réponses JSON (lecture seule, aucune écriture envoyée).

    python -m src.main inspect [--out data/inspect]

Pour chaque route GET : enregistre la réponse brute, affiche sa structure (clés et
types) et la compare aux modèles de src/core/models.py (champs manquants, éléments
rejetés). Si quelque chose diverge, corrige config/endpoints.yaml ou models.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from src.core.client import ApiError
from src.core.models import (
    Auction, AuctionDetail, Balance, CollectionPage, CollectionStats, MarketPage, MyBids, OwnedCard,
    SaleRow, SalesHistory, SalesSummary, TradesList,
)
from src.workers.base import Context


def shape(value: Any, indent: str = "  ", depth: int = 0, max_depth: int = 4) -> list[str]:
    pad = indent * depth
    if isinstance(value, dict):
        lines = []
        for key, item in list(value.items())[:40]:
            kind = type(item).__name__
            preview = "" if isinstance(item, (dict, list)) else f" = {str(item)[:60]!r}"
            lines.append(f"{pad}{key}: {kind}{preview}")
            if isinstance(item, (dict, list)) and depth < max_depth:
                lines += shape(item, indent, depth + 1, max_depth)
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{pad}[] (vide)"]
        return [f"{pad}[{len(value)} éléments] premier :"] + shape(value[0], indent, depth + 1, max_depth)
    return [f"{pad}{type(value).__name__} = {str(value)[:60]!r}"]


def missing_fields(model: type[BaseModel], sample: Any) -> list[str]:
    if not isinstance(sample, dict):
        return []
    return [name for name in model.model_fields if name not in sample]


def check(model: type[BaseModel] | None, payload: Any, item_model: type[BaseModel] | None = None,
          items_key: str | None = None) -> list[str]:
    notes: list[str] = []
    if model is None:
        return notes
    try:
        model.model_validate(payload)
        notes.append(f"OK : conforme à {model.__name__}")
    except ValidationError as exc:
        notes.append(f"ÉCART : {model.__name__} -> {exc.error_count()} erreur(s)")
        notes += [f"    {'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:10]]
    missing = missing_fields(model, payload)
    if missing:
        notes.append(f"champs optionnels absents : {missing}")
    if item_model and items_key and isinstance(payload, dict) and isinstance(payload.get(items_key), list):
        items = payload[items_key]
        bad = 0
        for raw in items:
            try:
                item_model.model_validate(raw)
            except ValidationError:
                bad += 1
        notes.append(f"{items_key} : {len(items) - bad}/{len(items)} élément(s) lus comme {item_model.__name__}")
        if items:
            absent = missing_fields(item_model, items[0])
            if absent:
                notes.append(f"    champs absents du 1er élément : {absent}")
    return notes


async def inspect(ctx: Context, out_dir: str | None = None) -> None:
    out = Path(out_dir) if out_dir else ctx.settings.data_dir / "inspect"
    out.mkdir(parents=True, exist_ok=True)
    print(f"Joueur détecté : {ctx.client.user_id or 'inconnu (renseigne WM_USER_ID)'}\n")

    probes: list[tuple[str, dict[str, Any], type[BaseModel] | None, type[BaseModel] | None, str | None]] = [
        ("balance", {}, Balance, None, None),
        ("collection_stats", {}, CollectionStats, None, None),
        ("collection_page", {"page": 0}, CollectionPage, OwnedCard, "collection"),
        ("market_page", {}, MarketPage, Auction, "auctions"),
        ("active_trades", {}, TradesList, None, None),
        ("my_bids", {}, MyBids, None, None),
        ("my_listings", {}, None, None, None),
    ]
    payloads: dict[str, Any] = {}

    async def probe(name: str, params: dict[str, Any], model, item_model, items_key) -> None:
        print(f"=== {name} {params or ''}")
        if not ctx.client.endpoints.has(name):
            print("  (non déclaré dans endpoints.yaml)\n")
            return
        try:
            payload = await ctx.client.get(name, **params)
        except ApiError as exc:
            print(f"  ERREUR : {exc}\n")
            return
        payloads[name] = payload
        (out / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n".join(shape(payload)[:60]))
        for note in check(model, payload, item_model, items_key):
            print(f"  -> {note}")
        print()

    for probe_args in probes:
        await probe(*probe_args)

    auctions = (payloads.get("market_page") or {}).get("auctions") or []
    if auctions and isinstance(auctions[0], dict):
        first = auctions[0]
        await probe("auction_detail", {"auction_id": first.get("id")}, AuctionDetail, None, None)
        card_id = first.get("card_id") or (first.get("card") or {}).get("id")
        if card_id:
            await probe("sales_summary", {"card_id": card_id}, SalesSummary, None, None)
            await probe("sales_history", {"card_id": card_id}, SalesHistory, SaleRow, "recent")
    if ctx.client.endpoints.has("pack_status"):
        await probe("pack_status", {}, None, None, None)
    print(f"Réponses brutes enregistrées dans {out}/ (contiennent des données de ton compte).")
