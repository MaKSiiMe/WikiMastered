"""Une seule fois, avant le changement des valeurs par défaut du dépôt : fige les réglages d'une instance déjà en route.

    python -m src.tools.figer_reglages

Affiche, sous la forme VARIABLE=valeur, les réglages de stratégie qui suivent encore le défaut du code sur cette
instance : ceux déjà renseignés dans l'environnement ne sont pas listés. Une fois ces lignes ajoutées aux variables de
la stack, les nouveaux défauts ne changent rien pour elle. Ne lit ni ne montre aucun secret. Lecture seule.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import time

from src.config import Settings

FIELDS = (
    "SNIPER_RESALE_RARITIES", "SNIPER_DEAL_RATIO_MAX", "NIGHT_DEAL_RATIO_MAX", "SNIPER_MIN_BID_SHARE",
    "SNIPER_MIN_CONTESTED_SALES", "SNIPER_MAX_PRICE_SPREAD", "PRICE_RECENT_SALES", "PRICE_MIN_SALES",
    "SNIPER_SHINY_MIN_SALES", "SNIPER_MAX_BID", "SNIPER_BID_PUSH", "SNIPER_ALLOW_REBID", "SNIPER_WINDOW_SECONDS",
    "NIGHT_WINDOW_SECONDS", "SNIPER_POLL_MIN_SECONDS", "SNIPER_POLL_MAX_SECONDS", "SNIPER_DISCOVERY_SECONDS",
    "SNIPER_OBSERVE_SECONDS", "COLLECTION_SEARCH_SECONDS", "COLLECTION_MAX_PRICE", "COLLECTION_UPGRADE",
    "HTTP_PEAK_SPACING_SECONDS", "PORTFOLIO_CAPITAL_RATIO", "PORTFOLIO_MIN_PROFIT_RATIO", "PORTFOLIO_MIN_PROFIT",
    "PORTFOLIO_QUEUE_PROFIT_RATIO", "PORTFOLIO_MAX_POSITIONS", "POSITION_LIST_DURATION_MINUTES",
    "POSITION_LIST_PROFIT_SHARE", "POSITION_LIST_MARGIN", "POSITION_RELIST_DISCOUNT", "POSITION_MAX_RELISTS",
    "POSITION_MAX_LOSS", "POSITION_FIRE_SALE_AFTER", "POSITION_EXPENSIVE_FROM",
)


def render(value: object) -> str:
    """Comme l'attend la variable d'environnement : listes en CSV, booléens en minuscules, heures en HH:MM."""
    if isinstance(value, list):
        return ",".join(map(str, value))
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, time):
        return f"{value:%H:%M}"
    return str(value)


def lines(env: Mapping[str, str]) -> list[str]:
    """Une ligne par réglage absent de `env`, avec le défaut actuel du code."""
    return [f"{name}={render(Settings.model_fields[name.lower()].get_default(call_default_factory=True))}"
            for name in FIELDS if name not in env]


def main() -> None:
    missing = lines(os.environ)
    if not missing:
        print("Rien à ajouter : tous ces réglages sont déjà renseignés.")
        return
    print(f"# {len(missing)} ligne(s) à ajouter aux variables de la stack (Advanced mode, puis Save settings) :")
    print("\n".join(missing))


if __name__ == "__main__":
    main()
