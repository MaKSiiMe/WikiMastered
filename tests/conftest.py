"""Réglages fixes des tests : ils vérifient la logique du bot, sans dépendre des valeurs par défaut du dépôt.

Un test qui veut d'autres valeurs les passe à Settings(...), ce qui prime sur l'environnement. Les vrais défauts sont
vérifiés par tests/test_default_settings.py.
"""

from __future__ import annotations

import os

TEST_SETTINGS = {
    "SNIPER_RESALE_RARITIES": "UR,L",
    "SNIPER_DEAL_RATIO_MAX": "0.7",
    "NIGHT_DEAL_RATIO_MAX": "0.8",
    "SNIPER_MIN_BID_SHARE": "0.5",
    "SNIPER_MIN_CONTESTED_SALES": "3",
    "SNIPER_MAX_PRICE_SPREAD": "2.0",
    "PRICE_RECENT_SALES": "5",
    "PRICE_MIN_SALES": "2",
    "SNIPER_SHINY_MIN_SALES": "4",
    "SNIPER_MAX_BID": "0",
    "SNIPER_BID_PUSH": "0.5",
    "SNIPER_ALLOW_REBID": "true",
    "SNIPER_WINDOW_SECONDS": "60",
    "NIGHT_WINDOW_SECONDS": "60",
    "SNIPER_POLL_MIN_SECONDS": "1.0",
    "SNIPER_POLL_MAX_SECONDS": "1.5",
    "SNIPER_DISCOVERY_SECONDS": "10.0",
    "SNIPER_OBSERVE_SECONDS": "30.0",
    "COLLECTION_SEARCH_SECONDS": "30.0",
    "COLLECTION_MAX_PRICE": "50",
    "COLLECTION_UPGRADE": "true",
    "HTTP_PEAK_SPACING_SECONDS": "1.0",
    "PORTFOLIO_CAPITAL_RATIO": "0.9",
    "PORTFOLIO_MIN_PROFIT_RATIO": "0.25",
    "PORTFOLIO_MIN_PROFIT": "30",
    "PORTFOLIO_QUEUE_PROFIT_RATIO": "0.5",
    "PORTFOLIO_MAX_POSITIONS": "0",
    "POSITION_LIST_DURATION_MINUTES": "60",
    "POSITION_LIST_PROFIT_SHARE": "0.5",
    "POSITION_LIST_MARGIN": "0.15",
    "POSITION_RELIST_DISCOUNT": "0.9",
    "POSITION_MAX_RELISTS": "2",
    "POSITION_MAX_LOSS": "0.1",
    "POSITION_GIVE_UP_AFTER": "3",
    "POSITION_EXPENSIVE_FROM": "1000",
}

for name, value in TEST_SETTINGS.items():
    os.environ.setdefault(name, value)
