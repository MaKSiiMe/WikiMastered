"""Configuration chargée depuis l'environnement (.env) via pydantic-settings.

Les listes acceptent du JSON (`["UR","L"]`) ou du CSV (`UR,L`) ; les dictionnaires
du JSON (`{"SR": 15}`). Les heures s'écrivent `HH:MM` (heure de Paris).
"""

from __future__ import annotations

import json
import os
import re
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, get_args, get_origin

from dotenv import dotenv_values
from pydantic import Field, PrivateAttr, ValidationInfo, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (BaseSettings, DotEnvSettingsSource, NoDecode, PydanticBaseSettingsSource,
                               SettingsConfigDict)

from src.core.text import tag_key


RARITIES: tuple[str, ...] = ("C", "PC", "R", "SR", "UR", "L")
RARITY_RANK: dict[str, int] = {r: i for i, r in enumerate(RARITIES)}

# Écritures du site regroupées par worker, pour LIVE_WRITES / SIMULATE (noms de routes acceptés aussi).
WRITE_GROUPS: dict[str, tuple[str, ...]] = {
    "sniper": ("place_bid",),            # mises des positions (achat pour revendre)
    "collection": ("collection_bid",),   # mises du mode collection (cartes gardées)
    "resale": ("create_listing", "reprice_listing"), "inventory": ("create_listing", "reprice_listing", "bulk_discard"),
    "settle": ("settle_auction",),       # enchères finies que le site tarde à régler
    "showcases": ("showcase_gallery",),  # vitrines du profil (/profil vitrine)
    "discard": ("bulk_discard",), "tags": ("tag_card",), "themes": ("theme_tag",),
    "achievements": ("achievements_check", "claim_achievement"),
    "wishes": ("wishlist_write", "mark_notifications_read"),  # liste de souhaits du site, pour les cartes de collection manquantes
    "favorites": ("favorite_write",),  # favoris à monter : leur place dans ta liste de souhaits, favori déplacé
}

# Anciens noms des workers et des groupes d'écriture (renommés le 22/11/2026) -> nouveau nom. Encore acceptés dans
# LIVE_WRITES, SIMULATE et HTTP_WORKER_SPACING_SECONDS (avertissement au démarrage), et dans les pauses gardées en base.
RENAMED_WORKERS: dict[str, str] = {
    "garde": "guard", "echanges": "trades", "valeur": "account_value", "etiquettes": "themes", "succes": "achievements",
    "cotes": "value_scan", "souhaits": "wishes", "ventes": "resale", "reglements": "settle", "vitrines": "showcases",
    "defausse": "discard",
}

# Anciens noms des réglages (renommés le 22/11/2026 : un domaine en tête de chaque nom) -> nouveau champ. Encore lus,
# variables d'environnement puis .env, avec un avertissement au démarrage ; un nouveau nom présent passe toujours avant.
RENAMED: dict[str, str] = {
    "JOURNAL_KEEP_DAYS": "log_journal_keep_days",
    "STATS_MINUTES": "log_stats_minutes",
    "ENABLE_BACKUP": "backup_enabled",
    "BACKUP_KEEP": "backup_keep_count",
    "SESSION_RECHECK_MINUTES": "session_check_minutes",
    "BACKOFF_BASE_SECONDS": "http_backoff_seconds",
    "BACKOFF_RATE_LIMIT_BASE_SECONDS": "http_backoff_rate_limit_seconds",
    "BACKOFF_MAX_SECONDS": "http_backoff_max_seconds",
    "ENABLE_REALTIME": "realtime_enabled",
    "DISCORD_WEBHOOK_URL": "discord_webhook_main",
    "DISCORD_ALERT_WEBHOOK_URL": "discord_webhook_alerts",
    "DISCORD_ACTIVITY_WEBHOOK_URL": "discord_webhook_activity",
    "DISCORD_WEBHOOK_VENTES": "discord_webhook_resale",
    "DISCORD_WEBHOOK_BILAN": "discord_webhook_report",
    "DISCORD_WEBHOOK_SYSTEME": "discord_webhook_system",
    "NOTIFY_EACH_BID": "discord_notify_each_bid",
    "DAILY_REPORT_TIME": "report_daily_time",
    "RECAP_INTERVAL_HOURS": "report_recap_hours",
    "SNIPER_REPORT_TIME": "report_sniper_time",
    "SNIPER_REPORT_HOURS": "report_sniper_hours",
    "ENABLE_PROFILE_GUARD": "guard_enabled",
    "PROFILE_CHECK_MINUTES": "guard_check_minutes",
    "PACKS_ALERT_AT": "guard_packs_alert_at",
    "ENABLE_ACHIEVEMENTS": "achievements_enabled",
    "ACHIEVEMENT_MILESTONES": "achievements_milestones",
    "ACHIEVEMENT_COUNT_MODE": "achievements_count_mode",
    "ACHIEVEMENT_MARGIN": "achievements_margin",
    "ENABLE_TRADE_WATCH": "trades_enabled",
    "TRADE_CHECK_MINUTES": "trades_check_minutes",
    "TRADE_FAIR_MARGIN": "trades_fair_margin_ratio",
    "ENABLE_ACCOUNT_VALUE": "account_value_enabled",
    "ENABLE_VALUE_SCAN": "value_scan_enabled",
    "PRICE_CACHE_TTL_MINUTES": "quote_cache_minutes",
    "PRICE_MIN_SALES": "quote_min_sales",
    "SNIPER_SHINY_MIN_SALES": "quote_shiny_min_sales",
    "PRICE_RECENT_SALES": "quote_recent_sales",
    "PRICE_SITE_MARGIN": "quote_site_margin_ratio",
    "PRICE_HISTORY_FALLBACK": "quote_history_fallback",
    "OBSERVED_KEEP_DAYS": "quote_observed_keep_days",
    "SALE_FEE_PCT": "market_fee_ratio",
    "ENABLE_SNIPER": "sniper_enabled",
    "SNIPER_DEAL_RATIO_MAX": "sniper_max_deal_ratio",
    "SNIPER_MIN_BID_SHARE": "sniper_min_bid_ratio",
    "SNIPER_MAX_PRICE_SPREAD": "sniper_max_spread_ratio",
    "SNIPER_MIN_BALANCE_RESERVE": "sniper_balance_reserve",
    "SNIPER_BID_PUSH": "sniper_bid_push_ratio",
    "SNIPER_BID_PUSH_CONTROL_SHARE": "sniper_bid_push_control_ratio",
    "SNIPER_SETTLE_GRACE_SECONDS": "sniper_settle_delay_seconds",
    "SETTLE_STUCK_AUCTIONS": "sniper_settle_stuck",
    "NIGHT_START": "sniper_night_start",
    "NIGHT_END": "sniper_night_end",
    "NIGHT_DEAL_RATIO_MAX": "sniper_night_max_deal_ratio",
    "NIGHT_WINDOW_SECONDS": "sniper_night_window_seconds",
    "NIGHT_BUDGET_BONUS": "sniper_night_budget_bonus",
    "NIGHT_MIN_BID_VALUE": "sniper_night_min_bid_value",
    "NIGHT_MAX_RISE": "sniper_night_max_rise",
    "PORTFOLIO_SLOTS": "resale_slots",
    "PORTFOLIO_MAX_POSITIONS": "resale_max_positions",
    "PORTFOLIO_BUFFER": "resale_queue_size",
    "PORTFOLIO_QUEUE_PROFIT_RATIO": "resale_queue_profit_ratio",
    "PORTFOLIO_CAPITAL_RATIO": "resale_capital_ratio",
    "PORTFOLIO_MIN_PROFIT_RATIO": "resale_min_profit_ratio",
    "PORTFOLIO_MIN_PROFIT": "resale_min_profit",
    "POSITION_LIST_MARGIN": "resale_start_margin_ratio",
    "POSITION_LIST_PROFIT_SHARE": "resale_start_profit_ratio",
    "POSITION_POST_NOW": "resale_post_now",
    "POSITION_POST_START": "resale_post_start",
    "POSITION_POST_END": "resale_post_end",
    "POSITION_LIST_DURATION_MINUTES": "resale_duration_minutes",
    "POSITION_LIST_LONG_MINUTES": "resale_long_duration_minutes",
    "POSITION_LIST_LONG_BELOW": "resale_long_below_positions",
    "POSITION_AVOID_END_HOURS": "resale_avoid_end_hours",
    "POSITION_STEP_MINUTES": "resale_step_minutes",
    "POSITION_RELIST_DISCOUNT": "resale_step_price_ratio",
    "POSITION_REPRICE": "resale_midway_reprice",
    "POSITION_MAX_RELISTS": "resale_max_steps",
    "POSITION_MAX_LOSS": "resale_max_loss_ratio",
    "POSITION_LOSS_AFTER": "resale_loss_after_steps",
    "POSITION_GIVE_UP_AFTER": "resale_give_up_after_steps",
    "POSITION_STALE_HOURS": "resale_stale_hours",
    "POSITION_EXPENSIVE_FROM": "resale_expensive_from",
    "POSITION_EXPENSIVE_MAX_RELISTS": "resale_expensive_max_steps",
    "POSITION_EXPENSIVE_GIVE_UP_AFTER": "resale_expensive_give_up_after_steps",
    "POSITION_WAIT_SLOT_FROM": "resale_wait_slot_from",
    "ENABLE_INVENTORY": "inventory_enabled",
    "AUTO_LIST": "duplicates_sell",
    "SELL_MIN_VALUE": "duplicates_min_value",
    "SELL_KEEP_COPIES": "duplicates_keep_copies",
    "SELL_LAST_COPY_RARITIES": "duplicates_last_copy_rarities",
    "SELL_PRICE_MULTIPLIER": "duplicates_price_ratio",
    "SELL_MAX_LISTINGS_PER_RUN": "duplicates_max_per_run",
    "SELL_ANYTIME": "duplicates_anytime",
    "SELL_CHECK_MINUTES": "duplicates_check_minutes",
    "SELL_PRICE_LOOKUPS_PER_RUN": "duplicates_quotes_per_run",
    "LISTING_END_WINDOW_START": "duplicates_end_window_start",
    "LISTING_END_WINDOW_END": "duplicates_end_window_end",
    "LISTING_PREFERRED_DURATION_MINUTES": "duplicates_duration_minutes",
    "LISTING_MIN_DURATION_MINUTES": "duplicates_min_duration_minutes",
    "LISTING_DURATION_OPTIONS": "market_duration_options",
    "AUTO_DISCARD": "discard_enabled",
    "DISCARD_UNKNOWN_PRICE_IS_WORTHLESS": "discard_unquoted",
    "COLLECTION_TAG_MAX_PER_RUN": "tag_collection_max_per_run",
    "ENABLE_THEME_TAGS": "themes_enabled",
    "THEME_TAGS_HOURS": "themes_interval_hours",
    "THEME_TAGS_MAX_PER_RUN": "themes_max_per_run",
    "THEME_LOOKUPS_PER_RUN": "themes_lookups_per_run",
    "PROTECTED_TAGS": "tag_protected",
    "COLLECTION_TAG": "tag_days",
    "SHOWCASE_TAG": "tag_showcase",
    "WISH_TAG": "tag_wish",
    "POSITION_UNSOLD_TAG": "tag_unsold",
    "MANUAL_RESALE_TAG": "tag_manual_resale",
    "THEME_DUPLICATE_TAG": "tag_duplicate",
    "THEME_GIF_TAG": "tag_gif",
}
_legacy_found: list[str] = []  # anciens noms lus par la dernière construction de Settings
_env_seen: list[str] = []  # noms donnés par l'environnement et le .env à la dernière construction de Settings

# Variables du système et de l'image Docker (python:3.12-slim, Dockerfile) : jamais signalées comme inutiles.
SYSTEM_NAMES = frozenset({"PATH", "HOME", "HOSTNAME", "LANG", "LANGUAGE", "TZ", "TERM", "SHLVL", "PWD", "OLDPWD",
                          "USER", "LOGNAME", "SHELL", "GPG_KEY", "_", "DEBIAN_FRONTEND", "NO_COLOR", "FORCE_COLOR",
                          "COLUMNS", "LINES", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"})
SYSTEM_PREFIXES = ("PYTHON", "PIP_", "LC_", "SSL_", "UV_", "VIRTUAL_ENV", "LD_", "XDG_", "PYTEST", "COV_")


class LegacyNames(PydanticBaseSettingsSource):
    """Source des anciens noms (RENAMED), placée après l'environnement et le .env : elle ne remplit que les réglages
    qu'aucun nouveau nom ne donne."""

    def __init__(self, settings_cls: type[BaseSettings], dotenv: PydanticBaseSettingsSource) -> None:
        super().__init__(settings_cls)
        files = dotenv.env_file if isinstance(dotenv, DotEnvSettingsSource) else None
        self._files = [files] if isinstance(files, str | Path) else list(files or [])

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for path in self._files:
            if Path(path).is_file():
                values |= {k.upper(): v for k, v in dotenv_values(path).items() if v is not None}
        values |= {k.upper(): v for k, v in os.environ.items()}
        found = sorted(old for old in RENAMED if old in values)
        _legacy_found[:] = found
        _env_seen[:] = sorted(values)
        return {RENAMED[old]: values[old] for old in found}


CsvList = Annotated[list[str], NoDecode]
IntList = Annotated[list[int], NoDecode]


def _split_list(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        if value.startswith("["):
            return json.loads(value)
        # Guillemets éventuels (« "jours" ») retirés : Portainer les garde tels quels.
        parts = [part.strip().strip("\"'").strip() for part in value.split(",")]
        return [part for part in parts if part]
    return value


def expand_writes(names: list[str]) -> set[str]:
    out: set[str] = set()
    for name in names:
        out.update(WRITE_GROUPS.get(name.casefold(), (name,)))
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    _legacy: list[str] = PrivateAttr(default_factory=list)
    _legacy_values: list[tuple[str, str]] = PrivateAttr(default_factory=list)
    _env_names: list[str] = PrivateAttr(default_factory=list)

    @classmethod
    def settings_customise_sources(cls, settings_cls: type[BaseSettings], init_settings: PydanticBaseSettingsSource,
                                   env_settings: PydanticBaseSettingsSource,
                                   dotenv_settings: PydanticBaseSettingsSource,
                                   file_secret_settings: PydanticBaseSettingsSource
                                   ) -> tuple[PydanticBaseSettingsSource, ...]:
        return init_settings, env_settings, dotenv_settings, LegacyNames(settings_cls, dotenv_settings),             file_secret_settings

    def model_post_init(self, context: Any, /) -> None:
        self._legacy = list(_legacy_found)
        _legacy_found.clear()
        self._env_names = list(_env_seen)
        _env_seen.clear()
        # Noms de workers et de groupes d'avant le 22/11 dans les valeurs : remplacés, et signalés au démarrage.
        self._legacy_values = []
        for field in ("live_writes", "simulate"):
            names = getattr(self, field)
            old = [n for n in names if n.casefold() in RENAMED_WORKERS]
            if old:
                setattr(self, field, [RENAMED_WORKERS.get(n.casefold(), n) for n in names])
                self._legacy_values += [(f"{field.upper()} : {n}", RENAMED_WORKERS[n.casefold()]) for n in old]
        spacing = self.http_worker_spacing_seconds
        if old := [n for n in spacing if n.casefold() in RENAMED_WORKERS]:
            self.http_worker_spacing_seconds = {RENAMED_WORKERS.get(n.casefold(), n): v for n, v in spacing.items()}
            self._legacy_values += [(f"HTTP_WORKER_SPACING_SECONDS : {n}", RENAMED_WORKERS[n.casefold()]) for n in old]

    @property
    def legacy_names(self) -> list[tuple[str, str]]:
        """(ancien nom, nouveau nom) des réglages lus sous leur ancien nom : à renommer dans Portainer ou le .env."""
        return [(old, RENAMED[old].upper()) for old in self._legacy] + self._legacy_values

    @property
    def unused_names(self) -> list[str]:
        """Variables de l'environnement ou du .env que le bot ne lit pas : réglage retiré, ou nom mal écrit
        (pydantic les ignore sans rien dire). Variables du système et de l'image exclues."""
        fields = {name.upper() for name in type(self).model_fields}
        return [n for n in self._env_names if n not in fields and n not in RENAMED and n not in SYSTEM_NAMES
                and not n.startswith(SYSTEM_PREFIXES) and n == n.upper()]

    @property
    def default_names(self) -> list[str]:
        """Réglages donnés par l'environnement ou le .env avec la valeur par défaut du dépôt : sans effet aujourd'hui,
        mais ils gardent ta valeur si le défaut change un jour."""
        fields = type(self).model_fields
        out = []
        for name in self._env_names:
            field = fields.get(name.lower())
            if field is not None and getattr(self, name.lower()) == field.get_default(call_default_factory=True):
                out.append(name)
        return out


    # --- Général -------------------------------------------------------------
    dry_run: bool = True
    # Écritures réelles malgré DRY_RUN=true (ex. ventes).
    live_writes: CsvList = Field(default_factory=list)
    # Écritures simulées malgré DRY_RUN=false (ex. sniper : tout est réel sauf les mises des positions).
    simulate: CsvList = Field(default_factory=list)
    log_level: str = "INFO"  # console (logs Portainer) ; le journal data/logs/journal.jsonl garde tout, DEBUG compris
    log_journal_keep_days: int = 7  # jours de journal gardés (un fichier par jour)
    log_stats_minutes: float = 15.0  # relevé des passages des workers et des requêtes au site (0 = aucun)
    timezone: str = "Europe/Paris"
    data_dir: Path = Path("data")
    # Sauvegarde quotidienne de la base (src/backup.py) dans un volume à part ; BACKUP_KEEP_COUNT archives gardées.
    backup_enabled: bool = True
    backup_dir: Path = Path("backups")
    backup_time: time = time(4, 30)
    backup_keep_count: int = 7
    # Ventes observées gardées N jours (0 = toutes, pour toujours) : chaque nuit à BACKUP_TIME, les ventes plus
    # anciennes sont effacées, sauf les shiny (rares : toutes gardées) et celles d'une carte vendue moins de
    # QUOTE_RECENT_SALES fois sur la période (son estimation en dépend). Le 12/11 : 733 000 ventes en 30 jours,
    # 320 Mo sur une base de 372, sauvegarde de 25 min ; 96 % des achats s'appuient sur des ventes de moins de 3 jours.
    quote_observed_keep_days: int = 0
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
    # Renouvellement autonome de la session Supabase (API standard /auth/v1/token).
    # Clé publique « anon » : vide, le bot la cherche au démarrage dans les scripts du site.
    # À la main : DevTools > Réseau > une requête vers *.supabase.co > en-tête `apikey`.
    supabase_anon_key: str = ""
    supabase_url: str = ""                  # vide = https://<ref>.supabase.co, ref lu dans le cookie
    session_refresh_margin_minutes: float = 10.0

    # --- Réseau / anti-ban -----------------------------------------------------
    http_min_spacing_seconds: float = 1.5
    http_jitter_seconds: float = 0.8
    # Pic de ventes (heures de Paris) : un écart plus court, pour relire plus de ventes à miser. Dès que le site limite le bot (429,
    # ou une route refusée deux fois de suite), retour à HTTP_MIN_SPACING_SECONDS pendant HTTP_PEAK_COOLDOWN_MINUTES ;
    # une panne du site (500, délai dépassé) ne ralentit pas. HTTP_PEAK_SPACING_SECONDS=0 : jamais.
    http_peak_start: time = time(9, 0)
    http_peak_end: time = time(2, 0)
    http_peak_spacing_seconds: float = 0.0
    http_peak_cooldown_minutes: float = 30.0
    # Les écarts ci-dessus plafonnent les lectures (GET) de tous les workers réunis. En plus, chaque worker peut avoir
    # son propre écart minimal entre deux de ses lectures : value_scan=5,themes=3 (ou en JSON). Un worker absent n'a
    # que le plafond commun ; le sniper n'y est pas, pour prendre les créneaux que les autres laissent. Vide ou 0 :
    # aucun écart propre (31/10 : la valeur 0 empêchait le bot de démarrer).
    http_worker_spacing_seconds: Annotated[dict[str, float], NoDecode] = Field(default_factory=dict)
    # Écritures (mises, ventes, étiquettes…) : leur propre file, hors du plafond des lectures, une toutes les N s.
    http_write_spacing_seconds: float = 1.0
    http_timeout_seconds: float = 20.0
    http_get_max_attempts: int = 4
    http_backoff_seconds: float = 30.0
    http_backoff_rate_limit_seconds: float = 60.0
    http_backoff_max_seconds: float = 900.0
    session_check_minutes: float = 15.0

    # --- Discord ----------------------------------------------------------------
    discord_webhook_main: str = ""
    discord_webhook_alerts: str = ""
    # Salon d'activité (récap périodique, simulé) : vide = salon principal ;
    # « off » = logs seulement. Le salon principal garde alertes, achats, ventes, cartes gagnées, bilans.
    discord_webhook_activity: str = ""
    # Un salon par catégorie (un webhook chacun) : il reçoit tous ses messages, sauf les alertes critiques (salon
    # d'alertes). Une catégorie sans salon garde le salon principal ou d'activité. Le message unique de chaque position
    # (achetée, en vente, vendue…) va dans #ventes.
    discord_webhook_sniper: str = ""      # récap des enchères de 08:00, budget, mises à vérifier
    discord_webhook_resale: str = ""      # positions, ventes, défausse, échanges
    discord_webhook_collection: str = ""  # cartes de collection, étiquettes, succès
    discord_webhook_report: str = ""       # bilan de 23:00, récap des workers toutes les 4 h
    discord_webhook_system: str = ""     # démarrage, session, réseau, sauvegardes
    # Salon #wikinsider : ce qu'il reste à noter à la main sur Wikinsider (achats et reventes réels de shiny),
    # un message par opération avec un bouton « Noté » qui l'efface. Vide = rien (jamais de repli sur un autre salon).
    discord_webhook_wikinsider: str = ""
    discord_wikinsider_kept: bool = False  # aussi les achats gardés (collection, souhaits, cartes demandées)
    discord_mention: str = ""
    # Moniteur « Push » d'Uptime Kuma : appelé au plus une fois par minute après un passage réussi d'un worker. Vide =
    # rien. Kuma prévient (sur Discord, par exemple) si le bot tourne mais ne fait plus rien.
    kuma_push_url: str = ""
    # Commandes slash (/compte roi, /compte bilan, /sniper demander…) : bot Discord (portail développeur > Bot > token). Vide = pas de
    # commandes, les webhooks seuls. Seul DISCORD_OWNER_ID (ton identifiant utilisateur) peut s'en servir ;
    # DISCORD_GUILD_ID (ton serveur) les fait apparaître tout de suite au lieu d'attendre jusqu'à 1 h.
    discord_bot_token: str = ""
    discord_owner_id: int = 0
    discord_guild_id: int = 0
    report_daily_time: time = time(23, 0)
    report_recap_hours: int = 4  # récap des workers toutes les N heures (0 = aucun)
    # Récap du sniper : chaque enchère misée sur les N dernières heures (gagnées, perdues, gain latent et manqué).
    report_sniper_time: time = time(8, 0)
    report_sniper_hours: int = 24  # 0 = pas de récap du sniper
    discord_notify_each_bid: bool = False  # un message par mise du sniper (sinon : les logs, et le récap des enchères de 08:00)

    # --- Workers ----------------------------------------------------------------
    sniper_enabled: bool = True
    inventory_enabled: bool = True
    achievements_enabled: bool = True
    # Garde : profil relu toutes les N min ; une sanction du site arrête tout le bot (data/HALT).
    guard_enabled: bool = True
    guard_check_minutes: float = 15.0
    guard_packs_alert_at: int = 10                # message quand ce nombre de paquets attend d'être ouvert ; 0 = jamais
    # Échanges proposés relus toutes les N min : chacun est évalué une fois (src/trades.py), avec une contre-offre à
    # envoyer s'il est défavorable. Équitable : chaque côté vaut au moins (1 - TRADES_FAIR_MARGIN_RATIO) fois l'autre.
    trades_enabled: bool = True
    # Cartes souhaitées (worker « wishes ») : une carte que tu ajoutes toi-même à ta liste de souhaits du site est
    # misée par le sniper jusqu'à sa cote dans la rareté en vente, au plus WISH_MAX_PRICE (0 = désactivé), dans toute
    # rareté plus haute que celle que tu possèdes. Gagnée, elle est gardée, reçoit TAG_WISH (vide = aucune) et sort
    # de ta liste de souhaits.
    wish_max_price: int = 0
    # Carte souhaitée sans cote dans la rareté en vente (jamais vue vendue) : misée jusqu'à N WB quand même, au plus
    # WISH_MAX_PRICE (le 20/11, 27 ventes ignorées faute de cote, dont la moitié entre 10 et 100 WB). 0 = pas de mise.
    wish_unquoted_max_price: int = 0
    # Carte gardée (souhaitée, collection misée à la cote) sans cote dans la rareté en vente : cote de secours tirée de
    # l'historique des prix du site (moyenne des 4 dernières semaines avec des ventes). Une requête par carte, à
    # l'avance, gardée 6 h.
    quote_history_fallback: bool = True
    tag_wish: str = "Souhait"
    wish_check_minutes: float = 10.0
    # Cartes de ta liste de souhaits chez tes amis (worker « friends ») : le catalogue du site est relu toutes les N h et
    # un message annonce chaque carte de ta liste qu'un ami possède dans une rareté qui t'avance, une fois. Lecture
    # seule : l'échange se propose sur le site. 0 = coupé (/collection amis reste disponible à la demande).
    friends_check_hours: float = 0.0
    # Favoris à monter (worker « favorites ») : chaque carte en favori est misée dans toute rareté au-dessus de ton
    # meilleur exemplaire, jusqu'à L, au plus à sa cote (jamais une vente shiny ; sans cote : pas de mise). Repérée sans
    # recherche par titre : le bot la met dans ta liste de souhaits (le site annonce ses ventes) et le sniper voit déjà
    # toutes les ventes de SNIPER_RESALE_RARITIES. Montée, le favori passe au nouvel exemplaire et l'ancien redevient
    # vendable. Écritures : groupe « favorites ».
    favorites_upgrade: bool = False
    # Favori posé par le bot sur l'exemplaire gardé de chaque carte des collections actives (COLLECTION_SETS) et sur
    # les cartes de tes vitrines, une fois par carte ; jamais retiré. Écritures : groupe « favorites ».
    favorites_auto: bool = False
    favorites_check_minutes: float = 60.0  # lecture de tes favoris
    trades_check_minutes: float = 10.0
    trades_fair_margin_ratio: float = 0.1
    achievements_time: time = time(22, 50)  # succès réclamés une fois par jour, avant le bilan de 23:00

    # --- Cotes ------------------------------------------------------------------
    quote_cache_minutes: float = 180.0
    # Prix partagés avec d'autres joueurs (serveur de l'extension WikiMastered, core/shared_prices.py) : historiques
    # des prix lus par les autres participants depuis moins de QUOTE_CACHE_MINUTES, et dépôt des nôtres. Vide = coupé.
    shared_prices_url: str = ""
    shared_prices_token: str = ""
    quote_min_sales: int = 4     # ventes observées minimum avant d'acheter une carte
    # Un shiny en demande plus : son marché est maigre (Mark Wahlberg ✨, achetée 6119 le 24/10 sur 3 ventes de 3400,
    # 7797 et 8000, revendue 5000). Seules ses propres ventes comptent, la cote du site n'entre pas dans l'estimation.
    quote_shiny_min_sales: int = 8
    quote_recent_sales: int = 7  # estimation = médiane des N dernières ventes observées
    # L'estimation est plafonnée par la moyenne du site × cette marge. 1 : la moyenne elle-même. Elle suit les prix
    # avec retard : quand ils montent, 1,1 laisse le sniper acheter les cartes en hausse (voir pricing.combine).
    quote_site_margin_ratio: float = Field(1.0, ge=1.0)
    # Achat d'une position : parmi ces N ventes, au moins tant de ventes disputées (plus d'une mise), et la plus
    # haute pas plus de X fois la plus basse (0 = sans limite). Le 18/10, l'éclipse XXIe était achetée 882 sur une
    # estimation de 1197 faite de 3 ventes au prix de départ et de 2 disputées (970 et 1774). Les shiny échappent à la
    # dispersion (leurs ventes dispersées se revendaient aussi bien).
    sniper_min_contested_sales: int = 4
    sniper_max_spread_ratio: float = 1.5

    # --- Module D : sniper ------------------------------------------------------
    # Pause entre deux passages (une requête chacun) quand aucune cible n'attend. Plus courte : repérage plus rapide,
    # mais plus de requêtes au site.
    sniper_poll_min_seconds: float = 3.0
    sniper_poll_max_seconds: float = 5.0
    # Le marché est trop gros pour lire les fins imminentes (environ 12 fins par seconde) : le sniper
    # repère les nouvelles ventes de ces raretés (tri « recent ») et ne revend que celles-ci. Vide : aucun achat pour
    # revendre, et aucune vente observée.
    sniper_resale_rarities: CsvList = Field(default_factory=list)
    sniper_discovery_seconds: float = 30.0  # nouvelles ventes de chaque rareté suivie, toutes les N s
    sniper_observe_seconds: float = 60.0    # ventes tout juste terminées (prix observés), toutes les N s
    # Fenêtre de mise : le bot mise s'il reste moins de N s (et au moins SNIPER_MIN_SECONDS_LEFT). Trop courte, un site
    # lent ne relit pas toutes les cibles à temps. Une mise en tête est relue avant la fin.
    sniper_window_seconds: int = 30
    # Une mise dans les 10 dernières secondes prolonge l'enchère de 60 s : le bot n'y mise jamais (marge de 5 s).
    sniper_min_seconds_left: int = 15
    sniper_max_deal_ratio: float = 0.50
    # Position sautée si notre mise vaut moins de cette part de l'estimation (0 = jamais) : une enchère très sous sa
    # valeur partira bien plus haut, et la mise userait une place du quota anti-bot pour rien.
    sniper_min_bid_ratio: float = 0.25
    # Revente d'une carte non shiny seulement si son estimation atteint ce prix (0 = toutes) : moins de mises, donc
    # moins de vérifications anti-bot, gardées pour les cartes chères. Les shiny sont toujours revendues ; collection,
    # cartes demandées et souhaits ne sont pas concernés. Du 01/11 au 16/11 : shiny et L ≥ 2000 = 16 % des mises
    # tentées, 37 % du bénéfice.
    sniper_min_estimate: int = 0
    # Revente d'une carte non shiny sautée si sa mise minimale dépasse N × son prix de départ (0 = jamais) : une
    # enchère déjà très disputée part le plus souvent au-dessus de notre plafond. Chaque mise minimale vaut 1,1 × la
    # précédente : 1,1 = une mise déjà faite, 1,21 = deux, 1,46 = quatre. Collection, /sniper demander et souhaits non concernés.
    sniper_max_rise: float = 0.0
    sniper_max_bid: int = 500        # plafond absolu d'une mise, /sniper demander excepté (0 = aucun, le budget par position s'applique)
    sniper_daily_budget: int = 0     # 0 = pas de budget quotidien
    sniper_balance_reserve: int = 50
    sniper_prefetch_seconds: float = 8.0
    sniper_allow_rebid: bool = False
    # Mise d'une position : la mise minimale + cette part de l'écart jusqu'à notre plafond ; surenchérie (si
    # SNIPER_ALLOW_REBID), la mise suivante va droit au plafond. Chaque mise use le quota de la vérification anti-bot
    # (~9-10 mises). 0 = minimum.
    sniper_bid_push_ratio: float = 0.0
    # Part des mises laissées au minimum malgré SNIPER_BID_PUSH_RATIO (une enchère sur N, tirée de son identifiant) : un
    # groupe témoin, pour mesurer si pousser la mise rapporte plus qu'il ne coûte (`sniper-report --breakdown`,
    # ligne « mise au minimum »). 0 = aucune.
    sniper_bid_push_control_ratio: float = 0.0
    # Valeur minimale d'une mise, en WB : chance de gagner × (revente attendue − mise), d'après les mises réelles
    # des SNIPER_BID_VALUE_DAYS derniers jours (core/winmodel.py). Le quota de la vérification anti-bot va ainsi aux
    # mises qui rapportent le plus. `python -m src.main sniper-value` aide à choisir. 0 = pas de seuil.
    sniper_min_bid_value: float = 0.0
    sniper_bid_value_days: float = 14.0
    sniper_settle_delay_seconds: int = 30
    # Vérification anti-bot en attente : une seule mise réelle tentée toutes les N s, pour voir si elle est levée
    # (le 16/10, ~45 mises refusées en 1 h 30). 0 = chaque mise est tentée : la levée est vue tout de suite, utile
    # quand une extension passe la vérification dans le navigateur.
    sniper_antibot_retry_seconds: float = 300.0
    sniper_max_concurrent_bids: int = 0   # 0 = illimité (le site n'impose pas de limite aux mises)
    sniper_avoid_my_bids: bool = True     # ne jamais toucher aux cartes sur lesquelles tu mises toi-même

    # Réglages de nuit, de SNIPER_NIGHT_START à SNIPER_NIGHT_END. Même heure aux deux (le défaut) : pas de nuit, les
    # réglages du jour valent toute la journée. Chaque réglage de nuit à 0 reprend celui du jour.
    sniper_night_start: time = time(0, 0)
    sniper_night_end: time = time(0, 0)
    sniper_night_max_deal_ratio: float = 0.0  # SNIPER_MAX_DEAL_RATIO la nuit (0 = celui du jour)
    sniper_night_window_seconds: int = 0      # SNIPER_WINDOW_SECONDS la nuit (0 = celui du jour)
    sniper_night_budget_bonus: int = 0
    sniper_night_min_bid_value: float = 0.0  # valeur minimale d'une mise la nuit (0 = SNIPER_MIN_BID_VALUE)
    sniper_night_max_rise: float = 0.0  # SNIPER_MAX_RISE la nuit (0 = celui du jour)

    # --- Capital : positions achetées pour être revendues ----------------------------
    resale_slots: int = 5              # = places de vente simultanées du site
    # Plafonds de positions (0 = aucun).
    resale_max_positions: int = 5      # plafond absolu : positions détenues + enchères du bot
    resale_queue_size: int = 0             # positions en attente d'une place de vente, au-delà des places libres
    resale_queue_profit_ratio: float = 1.0  # bénéfice exigé pour une position qui devra attendre
    resale_capital_ratio: float = 0.5  # part des WikiBidous engagée en positions
    resale_min_profit_ratio: float = 0.5  # bénéfice attendu ≥ 50 % du budget d'une position
    resale_min_profit: int = 100       # plancher absolu du bénéfice attendu
    market_fee_ratio: float = 0.0             # commission éventuelle du site sur une vente
    # Revente d'une position : c'est une enchère, les joueurs font monter le prix. Le prix de départ est le prix
    # d'achat + cette marge (commission comprise) ; jamais sous le prix d'achat, même remise en vente.
    resale_start_margin_ratio: float = 0.20
    # Prix de départ relevé : prix d'achat + cette part du bénéfice attendu à l'achat. Les enchères montent peu
    # (relevé du 16/10 : prix final / prix de départ, médiane ×1,11 en UR, ×1,23 en L ; 41 % et 29 % sans
    # surenchère) et les 5 places de vente sont rares : un départ bas vendrait presque au prix d'achat.
    resale_start_profit_ratio: float = 0.25
    # Revente publiée dès que la carte est arrivée (16/10 : 2 L sur 3 attendaient le lendemain 11 h, capital
    # immobilisé ; l'heure de fin change peu le prix, voir `python -m src.main hours`). False : fenêtre ci-dessous.
    resale_post_now: bool = True
    resale_post_start: time = time(11, 0)  # fenêtre de publication (Paris) si RESALE_POST_NOW=false
    resale_post_end: time = time(12, 0)
    # Durée de la revente. Plus courte, les 5 places de vente tournent plus vite ; plus longue, la vente est plus vue.
    # Moins de RESALE_LONG_BELOW_POSITIONS positions détenues (rien n'attend de place) : RESALE_LONG_DURATION_MINUTES.
    resale_duration_minutes: int = 360
    resale_long_duration_minutes: int = 720
    resale_long_below_positions: int = 0     # 0 = toujours RESALE_DURATION_MINUTES
    # Heures de fin (Paris) évitées par la revente de 12 h : 6 h si elle finit hors de ces heures (6 h n'est jamais
    # allongée : des positions attendent une place). Ventes de L du
    # 25/10 19:43 au 26/10 19:18 (prix ÷ médiane des 8 ventes précédentes de la carte) : ×0,91-0,97 et 23-33 % sous
    # ×0,8 pour une fin à 18-22 h, ×0,93-0,95 à 11 h et 13 h, contre ×1,04-1,09 à 00-01 h et 07-09 h. Vide = jamais.
    resale_avoid_end_hours: IntList = Field(default_factory=lambda: [11, 12, 13, 18, 19, 20, 21, 22])
    # Échelle des remises en temps passé en vente sans acheteur : une étape toutes les N minutes (une vente plus longue
    # n'en compte qu'une). RESALE_MAX_STEPS, RESALE_LOSS_AFTER_STEPS et RESALE_GIVE_UP_AFTER_STEPS comptent en étapes :
    # avec des reventes d'1 h, compter les ventes ferait renoncer en 3 h. Même prix entre deux étapes.
    # 0 = une étape par vente sans acheteur ; vide = 360.
    resale_step_minutes: int = 360
    resale_step_price_ratio: float = 0.95 # étape franchie : remise en vente à prix × cette valeur
    # Revente sans aucune mise à mi-parcours : mise de départ baissée une fois de RESALE_STEP_PRICE_RATIO sur place
    # (jamais sous le plancher), au lieu d'attendre la fin pour la remettre en vente. Le site ne le permet qu'une fois
    # par vente, sans mise, après la moitié de la durée (relevé du 21/11).
    resale_midway_reprice: bool = False
    # Enchère finie depuis plus d'une minute mais pas encore réglée par le site (« Finalisation… ») : le bot la
    # finalise lui-même, comme le lien de la fiche, une fois (sa mise en tête, ou sa vente). Écritures : « settle ».
    sniper_settle_stuck: bool = True
    # Temps réel du site (src/core/realtime.py) : le bot écoute les mises sur les enchères où il est engagé (relecture
    # aussitôt surenchéri) et tes notifications (carte souhaitée ou de collection en vente : lue sans attendre).
    realtime_enabled: bool = False
    resale_max_steps: int = 3         # puis au prix d'achat, tant qu'une place de vente est libre
    # Après RESALE_LOSS_AFTER_STEPS étapes sans acheteur, la revente peut descendre jusqu'à RESALE_MAX_LOSS_RATIO
    # sous le prix d'achat : une carte qui ne part pas bloque une des 5 places (~900 WB par revente qui tourne) et son
    # capital (Mark Wahlberg ✨, achetée 6119, invendue à 7037 puis à 6333 le 25/10). 0 = jamais sous le prix d'achat.
    resale_max_loss_ratio: float = 0.0
    resale_loss_after_steps: int = 2
    # Après RESALE_GIVE_UP_AFTER_STEPS étapes sans acheteur, le bot renonce : la carte reste dans la collection, la position
    # est close, et l'exemplaire reçoit l'étiquette TAG_UNSOLD (à créer une fois sur le site ; "" = aucune),
    # pour la retrouver et la vendre autrement. Même étiquette quand une carte est gardée faute de place de vente.
    # Le bot ne brade jamais une carte. 0 = il ne renonce pas tant qu'une place de vente est libre.
    resale_give_up_after_steps: int = 0
    tag_unsold: str = "Invendue"
    # Position détenue depuis plus de N heures : alerte « Position qui traîne » avec un bouton (une fois). Au-delà de
    # l'échelle de remises (≈ 24-40 h), quelque chose bloque : pas de place de vente, exemplaire introuvable, favori.
    # 0 = jamais.
    resale_stale_hours: float = 48.0
    # Positions chères (achetées ≥ RESALE_EXPENSIVE_FROM WB) : moins de remises, et le bot renonce plus tôt, le
    # capital et la place de vente ne restent pas bloqués 2 jours. 0 = mêmes règles pour toutes.
    resale_expensive_from: int = 0
    resale_expensive_max_steps: int = 1
    resale_expensive_give_up_after_steps: int = 2
    # Position achetée ≥ N WB, invendue alors que toutes les places de vente sont prises : elle attend une place au prix
    # plancher au lieu d'être gardée (11/11 : une L achetée 8 110, close après 4 essais en 13 h, capital immobilisé).
    # RESALE_GIVE_UP_AFTER_STEPS s'applique toujours. 0 = jamais : la carte est gardée faute de place.
    resale_wait_slot_from: int = 0

    # --- Module C : inventaire --------------------------------------------------
    inventory_interval_minutes: float = 360.0
    duplicates_sell: bool = False
    duplicates_min_value: int = 15
    duplicates_keep_copies: int = 1
    # Raretés vendues même en dernier exemplaire (une L sortie d'un paquet). Jamais une carte achetée par le bot.
    duplicates_last_copy_rarities: CsvList = Field(default_factory=list)
    duplicates_price_ratio: float = 1.0
    duplicates_max_per_run: int = 5
    duplicates_anytime: bool = True        # doublons vendus dès qu'une place se libère (6 h), pas seulement le soir
    duplicates_check_minutes: float = 10.0  # vérification des places libres
    duplicates_quotes_per_run: int = 60  # cotes demandées au site par passage (route limitée) ; le reste : ventes observées
    duplicates_end_window_start: time = time(20, 0)
    duplicates_end_window_end: time = time(22, 30)
    # Durées proposées par le formulaire de vente du site [KZ features/ranking.js].
    market_duration_options: IntList = Field(default_factory=lambda: [10, 30, 60, 180, 360, 720, 1440])
    duplicates_duration_minutes: int = 360
    duplicates_min_duration_minutes: int = 60

    discard_enabled: bool = False
    tag_protected: CsvList = Field(default_factory=list)  # étiquettes jamais vendues ni défaussées
    # Tes propres achats à revendre : un exemplaire portant cette étiquette (casse ignorée) devient une position du bot,
    # revendue avec les mêmes règles (prix de départ, remises à -10 %, plancher), au prix que tu l'as payé : ta vente
    # observée de la carte (le bot note l'acheteur des ventes des raretés qu'il suit), relue sur le site. Vide : jamais.
    tag_manual_resale: str = "revente"
    discard_rarities: CsvList = Field(default_factory=lambda: ["C", "PC"])
    discard_max_value: int = 2
    discard_keep_copies: int = 1
    discard_unquoted: bool = False
    # Raretés défaussées même en dernier exemplaire (la carte quitte la collection), si le module I (VALUE_SCAN_*) l'a
    # cotée et qu'aucune vente connue ne dépasse DISCARD_MAX_VALUE (ou qu'elle n'en a aucune). Vide = jamais.
    discard_last_copy_rarities: CsvList = Field(default_factory=list)
    discard_max_per_run: int = 250  # exemplaires défaussés par passage au plus (0 = sans limite)
    achievements_milestones: IntList = Field(default_factory=list)
    achievements_count_mode: Literal["total", "unique"] = "total"
    achievements_margin: int = 50

    # --- Module I : cotes de toute la collection (défausse des derniers exemplaires) ------
    # Toutes les N heures, toute la collection est cotée, derniers exemplaires compris (la vente automatique ne cote
    # que des doublons) : la défausse de DISCARD_LAST_COPY_RARITIES s'en sert. Aucun message.
    value_scan_enabled: bool = True
    # Valeur du compte (/compte valeur) : collection relue toutes les N heures au plus, si aucun autre worker ne l'a fait.
    account_value_enabled: bool = True
    account_value_hours: float = 6.0
    value_scan_hours: float = 12.0
    # Cotes demandées au site par passage (0 = toutes), au plus une toutes les N s, et seulement quand aucune autre
    # requête n'attend. La route des cotes est limitée (18/10 : refusée 3 fois en 4 min au seul sniper) : refusée,
    # le rythme ralentit de moitié, puis réaccélère. Une cote de moins de VALUE_SCAN_MAX_AGE_DAYS jours est gardée ;
    # une carte sans aucune vente est recotée après VALUE_SCAN_UNSOLD_DAYS jours.
    value_scan_lookups: int = 0
    value_scan_lookup_seconds: float = 3.0
    value_scan_max_age_days: float = 7.0
    value_scan_unsold_days: float = 30.0
    # Une carte cotée sous N WB (toutes ses raretés possédées) garde cette cote pour toujours : jamais redemandée,
    # ni par l'analyse ni par la vente des doublons (0 = jamais figée).
    value_scan_freeze_below: int = 50

    # --- Module G : collection (cartes gardées, jamais revendues) ---------------------
    collection_sets: CsvList = Field(default_factory=list)    # « days » + clés de config/collections.yaml
    collection_titles: CsvList = Field(default_factory=list)  # titres en plus, exacts (ex. Paris,Lyon)
    collection_max_price: int = 10                              # mise maximale pour une carte manquante
    # Plafond propre à une collection, à la place de COLLECTION_MAX_PRICE : elements=100,countries=3000 (clés de
    # COLLECTION_SETS ; vide : aucune).
    collection_set_max_price: Annotated[dict[str, float], NoDecode] = Field(default_factory=dict)
    # Collection misée comme une revente : jusqu'à sa cote dans la rareté en vente × ce ratio (countries=0.8), au plus son
    # plafond de COLLECTION_SET_MAX_PRICE s'il en a un. Sans cote connue : pas de mise. Vide : aucune.
    collection_set_quote_ratio: Annotated[dict[str, float], NoDecode] = Field(default_factory=dict)
    # Carte possédée : le sniper mise aussi sur un exemplaire de rareté plus haute que la meilleure possédée,
    # jusqu'à COLLECTION_MAX_PRICE (le but : chaque carte dans sa plus haute rareté).
    collection_upgrade: bool = False
    # Met les cartes manquantes en liste de souhaits : le site annonce leurs ventes, plus de recherche par titre.
    collection_wishlist: bool = False
    collection_wishlist_seconds: float = 120.0  # lecture des annonces du site toutes les N secondes
    collection_refresh_minutes: float = 360.0
    collection_search_seconds: float = 120.0  # le sniper cherche une carte de collection sur le marché toutes les N s
    # Mises réelles de collection par vérification anti-bot (le site en accepte ~9-10, puis la redemande) :
    # le reste est gardé pour les L. 0 = pas de limite.
    collection_bids_per_check: int = 2
    tag_days: str = "date"           # étiquette des jours et de COLLECTION_TITLES ("" = non)
    tag_collection_max_per_run: int = 60   # étiquettes posées par passage (requêtes espacées)
    # Étiquette posée sur les cartes de tes vitrines du profil, retirée quand la carte en sort (les collections ont leur
    # propre étiquette). Le bot la gère seul : ne la pose pas à la main. "" = non.
    tag_showcase: str = ""

    # --- Étiquettes de thème (worker « themes ») ---------------------------------------
    # Thème de chaque carte (Personne, Pays, Musique…) lu sur Wikidata selon config/themes.yaml, posé sur
    # l'exemplaire gardé ; les autres exemplaires reçoivent TAG_DUPLICATE. Écritures : groupe « themes ».
    themes_enabled: bool = False
    themes_interval_hours: float = 6.0          # passage complet toutes les N h
    themes_max_per_run: int = 200      # étiquettes posées ou retirées par passage (requêtes de fond)
    themes_lookups_per_run: int = 500       # cartes classées sur Wikidata par passage (le reste au suivant)
    tag_duplicate: str = "Doublon"   # "" = pas d'étiquette de doublon
    tag_gif: str = "GIF"             # sur l'exemplaire gardé d'une carte animée ; "" = pas d'étiquette GIF

    @field_validator("*", mode="before")
    @classmethod
    def _parse_lists(cls, value: Any, info: ValidationInfo) -> Any:
        """Toute variable de type liste accepte du JSON (["a","b"]) ou du CSV (a,b), même un seul élément (date)."""
        field = cls.model_fields.get(info.field_name or "")
        if field is not None and get_origin(field.annotation) is list:
            return _split_list(value)
        return value

    @field_validator("*", mode="before")
    @classmethod
    def _pad_hours(cls, value: Any, info: ValidationInfo) -> Any:
        """Une heure s'écrit aussi sans zéro initial : 3:00 vaut 03:00 (17/10 : une heure à 3:00 refusée), ou sans
        minutes : 3 vaut 03:00 (25/11 : SNIPER_NIGHT_END=0 empêchait le bot de démarrer)."""
        field = cls.model_fields.get(info.field_name or "")
        if field is not None and time in (field.annotation, *get_args(field.annotation)) and isinstance(value, str):
            value = value.strip()
            if re.fullmatch(r"\d{1,2}", value):
                return f"{int(value):02d}:00"
            if re.fullmatch(r"\d:\d\d(:\d\d)?", value):
                return f"0{value}"
        return value

    @field_validator("discard_rarities", "sniper_resale_rarities",
                     "duplicates_last_copy_rarities", "discard_last_copy_rarities")
    @classmethod
    def _check_rarities(cls, value: list[str]) -> list[str]:
        value = [v.upper() for v in value]
        unknown = [v for v in value if v not in RARITIES]
        if unknown:
            raise ValueError(f"raretés inconnues {unknown}, attendues : {RARITIES}")
        return value

    @field_validator("http_worker_spacing_seconds", "collection_set_max_price", "collection_set_quote_ratio",
                     mode="before")
    @classmethod
    def _name_values(cls, value: Any, info: ValidationInfo) -> Any:
        """nom=valeur (ou nom:valeur) séparés par des virgules, ou du JSON ; vide ou 0 : aucun."""
        if not isinstance(value, str | int | float):
            return value
        text = str(value).strip().strip("\"'")
        if text in ("", "0", "{}"):
            return {}
        if text.startswith("{"):
            return json.loads(text)
        pairs = [re.split(r"[=:]", part, maxsplit=1) for part in text.split(",") if part.strip()]
        if any(len(pair) != 2 for pair in pairs):
            example = {"http_worker_spacing_seconds": "value_scan=5,themes=3",
                       "collection_set_quote_ratio": "countries=0.8"}.get(info.field_name or "", "elements=100")
            raise ValueError(f"{(info.field_name or '').upper()} : attendu nom=valeur séparés par des virgules "
                             f"(ex. {example}) : {text}")
        return {name.strip(): float(number) for name, number in pairs}

    @field_validator("resale_step_minutes", mode="before")
    @classmethod
    def _step_minutes(cls, value: Any) -> Any:
        """Vide : la valeur par défaut (Portainer garde les variables vidées)."""
        return 360 if isinstance(value, str) and not value.strip() else value

    @field_validator("discord_owner_id", "discord_guild_id", mode="before")
    @classmethod
    def _discord_id(cls, value: Any) -> Any:
        """Identifiant Discord : un nombre (clic droit > Copier l'identifiant). Autre chose (un pseudo…) vaut -1 et
        coupe les commandes au lieu d'empêcher le bot de démarrer (26/10 : pseudo dans DISCORD_OWNER_ID, bot
        redémarré en boucle)."""
        if isinstance(value, str):
            value = value.strip()
            return int(value) if value.isdigit() else (0 if not value else -1)
        return value

    def is_live(self, endpoint: str) -> bool:
        """Cette écriture part-elle vraiment au site ? DRY_RUN, sauf LIVE_WRITES ; réel, sauf SIMULATE."""
        if self.dry_run:
            live = endpoint in expand_writes(self.live_writes)
        else:
            live = endpoint not in expand_writes(self.simulate)
        return live

    @property
    def mode_label(self) -> str:
        if self.dry_run:
            return f"DRY RUN (réel : {', '.join(self.live_writes)})" if self.live_writes else "DRY RUN (aucune écriture)"
        label = f"RÉEL (simulé : {', '.join(self.simulate)})" if self.simulate else "RÉEL"
        if 0 < self.http_peak_spacing_seconds < self.http_min_spacing_seconds:
            label += (f", une requête toutes les {self.http_peak_spacing_seconds:g} s de {self.http_peak_start:%H:%M} "
                      f"à {self.http_peak_end:%H:%M} ({self.http_min_spacing_seconds:g} s sinon)")
        if self.http_worker_spacing_seconds:
            label += ", par worker : " + ", ".join(f"{name} {spacing:g} s"
                                                   for name, spacing in self.http_worker_spacing_seconds.items())
        return label

    @property
    def protected_tag_keys(self) -> set[str]:
        """TAG_PROTECTED comparables aux étiquettes des cartes (OwnedCard.tag_names) : émojis et casse ignorés."""
        return {key for key in map(tag_key, self.tag_protected) if key}

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
