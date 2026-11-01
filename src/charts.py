"""Graphiques PNG pour Discord (/roi, /valeur), dessinés avec Pillow (≈ 20 Mo, sans matplotlib).

Deux formes : des barres par jour (bénéfice, vert au-dessus de zéro, rouge dessous) et une courbe (valeur du
compte). Fond sombre comme Discord ; chiffres sans espace insécable. Police : DejaVu Sans (src/assets, licence jointe),
car celle fournie avec Pillow n'a pas les lettres accentuées.
"""

from __future__ import annotations

import io
from datetime import date
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from src.core import colors

WIDTH, HEIGHT = 900, 420
LEFT, RIGHT, TOP, BOTTOM = 84, 24, 58, 46
BACKGROUND, GRID, TEXT, MUTED = "#2b2d31", "#3b3d44", "#dbdee1", "#949ba4"
GREEN, RED, BLUE = (f"#{c:06x}" for c in (colors.OK, colors.ALERT, colors.INFO))  # même code couleur que Discord
MAX_VALUE_LABELS = 16  # au-delà, les barres n'ont plus de chiffre au-dessus (illisible)
MAX_DATE_LABELS = 12


def number(value: float, sign: bool = False) -> str:
    return f"{value:{'+' if sign else ''},.0f}".replace(",", " ")


FONT = Path(__file__).parent / "assets" / "DejaVuSans.ttf"


@lru_cache(maxsize=8)
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(FONT), size)
    except OSError:  # police absente : celle de Pillow, sans accents mais lisible
        return ImageFont.load_default(size=size)


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def _nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    """Graduations rondes (1, 2, 5 × 10ⁿ) couvrant [low, high]."""
    span = max(high - low, 1.0)
    raw = span / max(count - 1, 1)
    magnitude = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 1
    step = next(m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw)
    first = (low // step) * step
    ticks, tick = [], first
    while tick <= high + step:
        ticks.append(tick)
        tick += step
    return ticks


def _canvas(title: str, subtitle: str = "") -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.text((LEFT, 16), title, font=_font(22), fill=TEXT)
    if subtitle:
        width, _ = _text_size(draw, subtitle, _font(16))
        draw.text((WIDTH - RIGHT - width, 20), subtitle, font=_font(16), fill=MUTED)
    return image, draw


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _axes(draw: ImageDraw.ImageDraw, low: float, high: float) -> tuple[float, float, callable]:
    """Grille horizontale et chiffres de l'axe ; renvoie (bas, haut, y(valeur)) en pixels."""
    ticks = _nice_ticks(low, high)
    bottom_value, top_value = ticks[0], ticks[-1]
    plot_top, plot_bottom = TOP, HEIGHT - BOTTOM

    def y(value: float) -> float:
        return plot_bottom - (value - bottom_value) / (top_value - bottom_value) * (plot_bottom - plot_top)

    font = _font(15)
    for tick in ticks:
        draw.line([(LEFT, y(tick)), (WIDTH - RIGHT, y(tick))], fill=GRID, width=1)
        label = number(tick)
        width, height = _text_size(draw, label, font)
        draw.text((LEFT - 8 - width, y(tick) - height / 2 - 2), label, font=font, fill=MUTED)
    return bottom_value, top_value, y


def _date_labels(draw: ImageDraw.ImageDraw, days: list[date], xs: list[float]) -> None:
    font = _font(15)
    every = max(1, -(-len(days) // MAX_DATE_LABELS))
    for i, (day, x) in enumerate(zip(days, xs, strict=True)):
        if i % every and i != len(days) - 1:
            continue
        label = f"{day:%d/%m}"
        width, _ = _text_size(draw, label, font)
        draw.text((x - width / 2, HEIGHT - BOTTOM + 10), label, font=font, fill=MUTED)


def profit_bars(days: list[date], profits: list[float], title: str, subtitle: str = "") -> bytes:
    """Barres du bénéfice par jour : vertes au-dessus de zéro, rouges dessous, chiffre au bout de chaque barre."""
    image, draw = _canvas(title, subtitle)
    _, _, y = _axes(draw, min(0.0, *profits), max(0.0, *profits))
    plot_width = WIDTH - LEFT - RIGHT
    slot = plot_width / len(days)
    bar = min(slot * 0.62, 70)
    xs = [LEFT + slot * (i + 0.5) for i in range(len(days))]
    zero = y(0)
    draw.line([(LEFT, zero), (WIDTH - RIGHT, zero)], fill=MUTED, width=2)
    font = _font(15)
    for x, profit in zip(xs, profits, strict=True):
        if not profit:  # jour sans revente : pas de barre
            continue
        top, bottom = sorted((y(profit), zero))
        draw.rounded_rectangle([x - bar / 2, top, x + bar / 2, max(bottom, top + 2)], radius=3,
                               fill=GREEN if profit >= 0 else RED)
        if len(days) <= MAX_VALUE_LABELS and profit:
            label = number(profit, True)
            width, height = _text_size(draw, label, font)
            at = top - height - 8 if profit > 0 else bottom + 4
            draw.text((x - width / 2, at), label, font=font, fill=TEXT)
    _date_labels(draw, days, xs)
    return _png(image)


def value_line(days: list[date], values: list[float], title: str, subtitle: str = "") -> bytes:
    """Courbe d'une valeur par jour (valeur du compte), avec la dernière valeur écrite au bout."""
    image, draw = _canvas(title, subtitle)
    margin = (max(values) - min(values)) * 0.08 or max(abs(values[0]) * 0.05, 1)
    _, _, y = _axes(draw, min(values) - margin, max(values) + margin)
    plot_width = WIDTH - LEFT - RIGHT - 60  # place pour la dernière valeur
    xs = [LEFT + 20 + plot_width * (i / max(len(days) - 1, 1)) for i in range(len(days))]
    points = [(x, y(v)) for x, v in zip(xs, values, strict=True)]
    if len(points) > 1:
        draw.line(points, fill=BLUE, width=4, joint="curve")
    for x, py in points:
        draw.ellipse([x - 5, py - 5, x + 5, py + 5], fill=BLUE, outline=BACKGROUND, width=2)
    label = number(values[-1])
    width, height = _text_size(draw, label, _font(17))
    draw.text((min(points[-1][0] - width / 2, WIDTH - RIGHT - width), points[-1][1] - height - 14), label,
              font=_font(17), fill=TEXT)
    _date_labels(draw, days, xs)
    return _png(image)
