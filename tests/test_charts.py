"""Graphiques PNG (Pillow) : barres du bénéfice, courbe de la valeur du compte."""

from __future__ import annotations

import io
from datetime import date

from PIL import Image

from src import charts

DAYS = [date(2026, 10, d) for d in range(21, 28)]


def colors(png: bytes) -> set[tuple[int, int, int]]:
    image = Image.open(io.BytesIO(png)).convert("RGB")
    return {px for px in image.getdata()}


def test_profit_bars_draw_green_above_and_red_below_zero():
    png = charts.profit_bars(DAYS, [1059, 8969, 5011, 9528, 3060, 9631, -1119], "Bénéfice", "+37 258 WB")
    image = Image.open(io.BytesIO(png))
    assert image.format == "PNG" and image.size == (charts.WIDTH, charts.HEIGHT)
    found = colors(png)
    assert (0x22, 0xC5, 0x5E) in found and (0xEF, 0x44, 0x44) in found  # vert et rouge
    only_gains = colors(charts.profit_bars(DAYS, [100, 200, 300, 400, 500, 600, 700], "t"))
    assert (0xEF, 0x44, 0x44) not in only_gains


def test_charts_handle_edge_cases():
    charts.profit_bars(DAYS[:2], [0, 0], "rien du tout")  # aucune vente : pas de division par zéro
    sixty = [date.fromordinal(date(2026, 9, 1).toordinal() + i) for i in range(60)]
    charts.profit_bars(sixty, [i * 10 - 100 for i in range(60)], "60 jours")
    charts.value_line(DAYS[:2], [5000, 5000], "plat")  # valeur constante
    png = charts.value_line(DAYS, [101200, 104900, 108300, 112000, 116400, 119307, 123138], "Valeur du compte")
    assert (0x3B, 0x82, 0xF6) in colors(png)  # la courbe


def test_font_with_accents_is_shipped():
    assert charts.FONT.exists() and charts.FONT.stat().st_size > 100_000
    font = charts._font(20)
    assert font.getlength("é") > 0 and font.getmask("é").getbbox() != font.getmask("�").getbbox()
    assert charts.number(-1119, True) == "-1 119" and charts.number(123138) == "123 138"
