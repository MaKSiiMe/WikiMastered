"""Réglages fixes des tests : ils vérifient la logique du bot, sans dépendre des valeurs par défaut du dépôt.

Un test qui veut d'autres valeurs les passe à Settings(...), ce qui prime sur l'environnement. Les vrais défauts sont
vérifiés par tests/test_default_settings.py.
"""

from __future__ import annotations

import os

TEST_SETTINGS = {
    "SNIPER_RESALE_RARITIES": "UR,L",
    "SNIPER_MAX_DEAL_RATIO": "0.7",
    "SNIPER_NIGHT_MAX_DEAL_RATIO": "0.8",
    "SNIPER_MIN_BID_RATIO": "0.5",
    "SNIPER_MIN_CONTESTED_SALES": "3",
    "SNIPER_MAX_SPREAD_RATIO": "2.0",
    "QUOTE_RECENT_SALES": "5",
    "QUOTE_MIN_SALES": "2",
    "QUOTE_SHINY_MIN_SALES": "4",
    "SNIPER_MAX_BID": "0",
    "SNIPER_BID_PUSH_RATIO": "0.5",
    "SNIPER_ALLOW_REBID": "true",
    "SNIPER_WINDOW_SECONDS": "60",
    "SNIPER_NIGHT_WINDOW_SECONDS": "60",
    "SNIPER_POLL_MIN_SECONDS": "1.0",
    "SNIPER_POLL_MAX_SECONDS": "1.5",
    "SNIPER_DISCOVERY_SECONDS": "10.0",
    "SNIPER_OBSERVE_SECONDS": "30.0",
    "COLLECTION_SEARCH_SECONDS": "30.0",
    "COLLECTION_MAX_PRICE": "50",
    "COLLECTION_UPGRADE": "true",
    "HTTP_PEAK_SPACING_SECONDS": "1.0",
    "RESALE_CAPITAL_RATIO": "0.9",
    "RESALE_MIN_PROFIT_RATIO": "0.25",
    "RESALE_MIN_PROFIT": "30",
    "RESALE_QUEUE_PROFIT_RATIO": "0.5",
    "RESALE_MAX_POSITIONS": "0",
    "RESALE_DURATION_MINUTES": "60",
    "RESALE_START_PROFIT_RATIO": "0.5",
    "RESALE_START_MARGIN_RATIO": "0.15",
    "RESALE_STEP_PRICE_RATIO": "0.9",
    "RESALE_MAX_STEPS": "2",
    "RESALE_MAX_LOSS_RATIO": "0.1",
    "RESALE_GIVE_UP_AFTER_STEPS": "3",
    "RESALE_EXPENSIVE_FROM": "1000",
}

for name, value in TEST_SETTINGS.items():
    os.environ.setdefault(name, value)
