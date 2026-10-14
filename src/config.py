"""Configuration chargée depuis l'environnement (.env) via pydantic-settings.

Les listes acceptent du JSON (`["UR","L"]`) ou du CSV (`UR,L`) ; les dictionnaires
du JSON (`{"SR": 15}`). Les heures s'écrivent `HH:MM` (heure de Paris).
"""

from __future__ import annotations

import json
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

RARITIES: tuple[str, ...] = ("C", "PC", "R", "SR", "UR", "L")
RARITY_RANK: dict[str, int] = {r: i for i, r in enumerate(RARITIES)}

CsvList = Annotated[list[str], NoDecode]
IntList = Annotated[list[int], NoDecode]


def _split_list(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        if value.startswith("["):
            return json.loads(value)
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Général -------------------------------------------------------------
    dry_run: bool = True
    log_level: str = "INFO"
    timezone: str = "Europe/Paris"
    data_dir: Path = Path("data")
    endpoints_file: Path = Path("config/endpoints.yaml")

    # --- Session WikiMasters ---------------------------------------------------
    wm_base_url: str = "https://www.wiki-masters.com"
    wm_cookie: str = ""
    wm_authorization: str = ""
    wm_user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Safari/537.36"
    )
    wm_user_id: str = ""
    wm_extra_headers: dict[str, str] = Field(default_factory=dict)

    # --- Réseau / anti-ban -----------------------------------------------------
    http_min_spacing_seconds: float = 1.5
    http_jitter_seconds: float = 0.8
    http_timeout_seconds: float = 20.0
    http_get_max_attempts: int = 4
    backoff_base_seconds: float = 30.0
    backoff_rate_limit_base_seconds: float = 60.0
    backoff_max_seconds: float = 900.0
    session_recheck_minutes: float = 15.0

    # --- Discord ----------------------------------------------------------------
    discord_webhook_url: str = ""
    discord_alert_webhook_url: str = ""
    discord_mention: str = ""
    daily_report_time: time = time(23, 0)
    notify_each_bid: bool = False
    notify_digest_minutes: float = 60.0

    # --- Workers ----------------------------------------------------------------
    enable_pack_opener: bool = True
    enable_sniper: bool = True
    enable_inventory: bool = True
    enable_wiki_trends: bool = True

    # --- Module B : paquets -----------------------------------------------------
    pack_regen_minutes: float = 10.0
    pack_max_stock: int = 10
    pack_open_min_stock: int = 1
    pack_delay_min_seconds: float = 15.0
    pack_delay_max_seconds: float = 60.0
    pack_between_opens_min_seconds: float = 2.0
    pack_between_opens_max_seconds: float = 6.0
    pack_notify_rarities: CsvList = Field(default_factory=lambda: ["UR", "L"])
    pack_notify_shiny: bool = True
    pack_verification_recheck_minutes: float = 30.0

    # --- Cotes ------------------------------------------------------------------
    price_cache_ttl_minutes: float = 180.0
    price_min_sales: int = 2     # ventes observées minimum avant d'acheter une carte
    price_recent_sales: int = 5  # estimation = médiane des N dernières ventes observées

    # --- Module D : sniper ------------------------------------------------------
    sniper_poll_min_seconds: float = 2.5
    sniper_poll_max_seconds: float = 4.0
    sniper_window_seconds: int = 20
    # Achat « collection » par rareté : désactivé (le prix suit les tendances, pas la rareté).
    sniper_rarity_caps: dict[str, int] = Field(default_factory=dict)
    sniper_shiny_cap: int = 0
    sniper_deal_ratio_max: float = 0.70
    sniper_max_bid: int = 0          # 0 = pas de plafond absolu (le budget par position s'applique)
    sniper_daily_budget: int = 0     # 0 = pas de budget quotidien
    sniper_min_balance_reserve: int = 50
    sniper_prefetch_seconds: float = 8.0
    sniper_allow_rebid: bool = True
    sniper_settle_grace_seconds: int = 30
    sniper_max_concurrent_bids: int = 0   # 0 = illimité (le site n'impose pas de limite aux mises)

    # Arbitrage nocturne
    night_start: time = time(2, 0)
    night_end: time = time(7, 30)
    night_deal_ratio_max: float = 0.80
    night_caps_multiplier: float = 1.3
    night_window_seconds: int = 45
    night_budget_bonus: int = 0

    # --- Capital : positions achetées pour être revendues ----------------------------
    portfolio_slots: int = 5              # = places de vente simultanées du site
    portfolio_max_positions: int = 6      # positions détenues + enchères du bot en cours
    portfolio_capital_ratio: float = 0.9  # part des WikiBidous engagée en positions
    portfolio_min_profit_ratio: float = 0.25  # bénéfice attendu ≥ 25 % du budget d'une position
    portfolio_min_profit: int = 30        # plancher absolu du bénéfice attendu
    sale_fee_pct: float = 0.0             # commission éventuelle du site sur une vente
    position_list_ratio: float = 1.0      # prix de départ = estimation × ratio
    position_list_at_peak: bool = True    # fin de vente dans LISTING_END_WINDOW
    position_relist_discount: float = 0.9 # invendue : remise en vente à prix × 0,9
    position_max_relists: int = 2

    # --- Module C : inventaire --------------------------------------------------
    inventory_interval_minutes: float = 360.0
    auto_list: bool = False
    sell_min_value: int = 15
    sell_keep_copies: int = 1
    sell_price_multiplier: float = 1.0
    sell_max_listings_per_run: int = 5
    listing_end_window_start: time = time(20, 0)
    listing_end_window_end: time = time(22, 30)
    listing_duration_options: IntList = Field(default_factory=list)
    listing_min_duration_minutes: int = 60

    auto_discard: bool = False
    discard_rarities: CsvList = Field(default_factory=lambda: ["C", "PC"])
    discard_max_value: int = 2
    discard_keep_copies: int = 1
    discard_unknown_price_is_worthless: bool = False
    achievement_milestones: IntList = Field(default_factory=list)
    achievement_count_mode: Literal["total", "unique"] = "total"
    achievement_margin: int = 50

    # --- Module E : tendances Wikipédia -----------------------------------------
    trends_interval_minutes: float = 60.0
    trends_top_n: int = 100
    trends_spike_ratio: float = 3.0
    trends_min_daily_views: int = 2000
    trends_rarity_thresholds: dict[str, int] = Field(default_factory=lambda: {"L": 20000})
    trends_search_limit: int = 15
    trends_max_price: int = 30
    trends_auto_buy: bool = False
    trends_auto_buy_max: int = 20
    wikimedia_user_agent: str = "WikiMasteredBot/1.0 (self-hosted; configure WIKIMEDIA_USER_AGENT)"

    @field_validator(
        "pack_notify_rarities", "discard_rarities", "listing_duration_options",
        "achievement_milestones", mode="before",
    )
    @classmethod
    def _parse_lists(cls, value: Any) -> Any:
        return _split_list(value)

    @field_validator("pack_notify_rarities", "discard_rarities")
    @classmethod
    def _check_rarities(cls, value: list[str]) -> list[str]:
        value = [v.upper() for v in value]
        unknown = [v for v in value if v not in RARITIES]
        if unknown:
            raise ValueError(f"raretés inconnues {unknown}, attendues : {RARITIES}")
        return value

    @field_validator("sniper_rarity_caps", "trends_rarity_thresholds")
    @classmethod
    def _check_rarity_keys(cls, value: dict[str, int]) -> dict[str, int]:
        value = {k.upper(): v for k, v in value.items()}
        unknown = [k for k in value if k not in RARITIES]
        if unknown:
            raise ValueError(f"raretés inconnues {unknown}, attendues : {RARITIES}")
        return value

    @property
    def session_file(self) -> Path:
        """Cookies tenus à jour par le bot (Set-Cookie du serveur)."""
        return self.data_dir / "session.json"

    @property
    def cookie_drop_file(self) -> Path:
        """Déposer un nouveau cookie ici le charge à chaud, sans redémarrer."""
        return self.data_dir / "session_cookie.txt"


@lru_cache
def get_settings() -> Settings:
    return Settings()
