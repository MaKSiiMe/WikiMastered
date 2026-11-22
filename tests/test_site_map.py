"""Carte du site (src/tools/site_map.py) : extraction du JS, forme des réponses, différences entre deux cartes."""

from __future__ import annotations

from src.tools.site_map import analyse_js, build_map, diff, pattern, shape, template_path

JS_OLD = ('fetch(`/api/marketplace/cards/${e}/sales?scope=summary`);s.from("wishlist_items").select();'
          's.rpc("get_my_profile");s.channel(`auction:${id}`).on("broadcast",{event:"BID"},f);'
          'x="/marketplace";y="marketplace_wishlist_listed"')
JS_NEW = JS_OLD + ';fetch(`/api/marketplace/cards/${e}/price-history`);n="marketplace_price_alert";z="/iceberg"'


def test_ids_become_placeholders():
    assert template_path("/api/x/0a8dc0b9-51c3-4baf-a533-8d46c0cce29d/sales") == "/api/x/{id}/sales"
    assert template_path("/api/chat/${t}/12") == "/api/chat/{id}/{n}"
    assert pattern("/profile/lpancho") == pattern("/profile/zidane") == "/profile/{x}"
    assert pattern("/") == "/"


def test_js_reveals_routes_tables_rpc_channels_events_and_constants():
    found = analyse_js({"a.js": JS_OLD})
    assert "/api/marketplace/cards/{id}/sales?scope=summary" in found["api_strings"]  # paramètres gardés
    assert found["tables"] == {"wishlist_items": ["a.js"]} and list(found["rpc"]) == ["get_my_profile"]
    assert list(found["channels"]) == ["auction:{id}"] and list(found["broadcast"]) == ["BID"]
    assert "/marketplace" in found["page_strings"] and "marketplace_wishlist_listed" in found["constants"]


def test_shape_keeps_keys_and_types_never_values():
    s = shape({"recent": [{"id": "x", "final_price": 2310, "rarity": "L"}, {"id": "y", "settled_at": None}],
               "by_rarity": {str(i): {"avg": 1.5} for i in range(40)}})
    assert s["recent"] == [{"final_price": "int", "id": "str", "rarity": "str", "settled_at": "null"}]
    assert s["by_rarity"] == {"<40 clés>": {"avg": "float"}}
    assert "2310" not in str(s)


def test_diff_lists_what_is_new_with_its_context_and_changed_responses():
    old = build_map("2026-11-20_1200", {"/": {}}, {"GET /api/marketplace/cards/{id}/sales": {
        "shape": {"summary": {"L": {"average": "int"}}}}}, {"a.js": JS_OLD})
    new = build_map("2026-11-22_1200", {"/": {}}, {"GET /api/marketplace/cards/{id}/sales": {
        "shape": {"recent": [{"final_price": "int"}]}}}, {"a.js": JS_NEW})
    report = diff(old, new, {"a.js": JS_NEW})
    assert "**+ `/api/marketplace/cards/{id}/price-history`**" in report and "fetch(`/api/marketplace" in report
    assert "**+ `marketplace_price_alert`**" in report and "**+ `/iceberg`**" in report
    assert "**réponse changée** `GET /api/marketplace/cards/{id}/sales`" in report
    assert "modifiés" in report and "Première carte" in diff(None, new, {})
