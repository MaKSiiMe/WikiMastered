"""Configuration chargée depuis l'environnement (.env) via pydantic-settings.

Les listes acceptent du JSON (`["UR","L"]`) ou du CSV (`UR,L`) ; les dictionnaires
du JSON (`{"SR": 15}`). Les heures s'écrivent `HH:MM` (heure de Paris).
"""

from __future__ import annotations

import json
import re
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, get_args, get_origin

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


RARITIES: tuple[str, ...] = ("C", "PC", "R", "SR", "UR", "L")
RARITY_RANK: dict[str, int] = {r: i for i, r in enumerate(RARITIES)}

# Écritures du site regroupées par worker, pour LIVE_WRITES / SIMULATE (noms de routes acceptés aussi).
WRITE_GROUPS: dict[str, tuple[str, ...]] = {
    "sniper": ("place_bid",),            # mises des positions (achat pour revendre)
    "collection": ("collection_bid",),   # mises du mode collection (cartes gardées)
    "ventes": ("create_listing",), "inventory": ("create_listing", "bulk_discard"),
    "defausse": ("bulk_discard",), "tags": ("tag_card",),
    "succes": ("achievements_check", "claim_achievement"),
}

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

    # --- Général -------------------------------------------------------------
    dry_run: bool = True
    # Écritures réelles malgré DRY_RUN=true (ex. packs : ouvrir les paquets pour de vrai).
    live_writes: CsvList = Field(default_factory=list)
    # Écritures simulées malgré DRY_RUN=false (ex. sniper : tout est réel sauf les mises des positions).
    simulate: CsvList = Field(default_factory=list)
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
    # Renouvellement autonome de la session Supabase (API standard /auth/v1/token).
    # Clé publique « anon » : DevTools > Réseau > une requête vers *.supabase.co > en-tête `apikey`.
    supabase_anon_key: str = ""
    supabase_url: str = ""                  # vide = https://<ref>.supabase.co, ref lu dans le cookie
    session_refresh_margin_minutes: float = 10.0

    # --- Réseau / anti-ban -----------------------------------------------------
    http_min_spacing_seconds: float = 1.5
    http_jitter_seconds: float = 0.8
    # Pic de ventes (heures de Paris) : un écart plus court, pour relire plus de ventes à miser. Du 18 au 24/10, 1100 à
    # 1430 L finissaient par heure de 09 h à 02 h, deux fois moins de 03 h à 08 h. Dès que le site limite le bot (429,
    # ou une route refusée deux fois de suite), retour à HTTP_MIN_SPACING_SECONDS pendant HTTP_PEAK_COOLDOWN_MINUTES ;
    # une panne du site (500, délai dépassé) ne ralentit pas. HTTP_PEAK_SPACING_SECONDS=0 : jamais.
    http_peak_start: time = time(9, 0)
    http_peak_end: time = time(2, 0)
    http_peak_spacing_seconds: float = 1.0
    http_peak_cooldown_minutes: float = 30.0
    http_timeout_seconds: float = 20.0
    http_get_max_attempts: int = 4
    backoff_base_seconds: float = 30.0
    backoff_rate_limit_base_seconds: float = 60.0
    backoff_max_seconds: float = 900.0
    session_recheck_minutes: float = 15.0

    # --- Discord ----------------------------------------------------------------
    discord_webhook_url: str = ""
    discord_alert_webhook_url: str = ""
    # Salon d'activité (paquets, digests, récap périodique, tendances, simulé) : vide = salon principal ;
    # « off » = logs seulement. Le salon principal garde alertes, achats, ventes, cartes gagnées, bilans.
    discord_activity_webhook_url: str = ""
    discord_mention: str = ""
    daily_report_time: time = time(23, 0)
    recap_interval_hours: int = 4  # récap des workers toutes les N heures (0 = aucun)
    # Récap du sniper : chaque enchère misée sur les N dernières heures (gagnées, perdues, gain latent et manqué).
    sniper_report_time: time = time(8, 0)
    sniper_report_hours: int = 24  # 0 = pas de récap du sniper
    notify_each_bid: bool = False
    notify_digest_minutes: float = 60.0
    # Messages des achats et reventes simulés (sniper de test) ; false : seulement dans les logs, le récap et le bilan.
    notify_simulated: bool = True

    # --- Workers ----------------------------------------------------------------
    enable_sniper: bool = True
    enable_inventory: bool = True
    enable_achievements: bool = True
    # Garde : profil relu toutes les N min ; une sanction du site arrête tout le bot (data/HALT).
    enable_profile_guard: bool = True
    profile_check_minutes: float = 15.0
    achievements_time: time = time(22, 50)  # succès réclamés une fois par jour, avant le bilan de 23:00

    # --- Cotes ------------------------------------------------------------------
    price_cache_ttl_minutes: float = 180.0
    price_min_sales: int = 2     # ventes observées minimum avant d'acheter une carte
    price_recent_sales: int = 5  # estimation = médiane des N dernières ventes observées
    # Achat d'une position : parmi ces N ventes, au moins tant de ventes disputées (plus d'une mise), et la plus
    # haute pas plus de X fois la plus basse (0 = sans limite). Le 18/10, l'éclipse XXIe était achetée 882 sur une
    # estimation de 1197 faite de 3 ventes au prix de départ et de 2 disputées (970 et 1774).
    sniper_min_contested_sales: int = 3
    sniper_max_price_spread: float = 2.0

    # --- Module D : sniper ------------------------------------------------------
    sniper_poll_min_seconds: float = 2.5
    sniper_poll_max_seconds: float = 4.0
    # Le marché est trop gros pour lire les fins imminentes (environ 12 fins par seconde) : le sniper
    # repère les nouvelles ventes de ces raretés (tri « recent ») et ne revend que celles-ci.
    sniper_resale_rarities: CsvList = Field(default_factory=lambda: ["UR", "L"])
    sniper_discovery_seconds: float = 10.0  # nouvelles ventes de chaque rareté suivie, toutes les N s
    sniper_observe_seconds: float = 30.0    # ventes tout juste terminées (prix observés), toutes les N s
    # Fenêtre de mise : de 60 s à 15 s avant la fin. Large : le site lent (16/10) relisait trop peu de cibles dans
    # une fenêtre de 9 s (139 sur 159 « pas relues à temps »). Une mise en tête est relue avant la fin et rejouée.
    sniper_window_seconds: int = 60
    # Une mise dans les 10 dernières secondes prolonge l'enchère de 60 s : le bot n'y mise jamais (marge de 5 s).
    sniper_min_seconds_left: int = 15
    sniper_deal_ratio_max: float = 0.70
    # Position sautée si notre mise vaut moins de cette part de l'estimation (0 = jamais) : du 17 au 20/10,
    # 0 gagnée sur 33 réelles (5 sur 60 simulées), parties à ~2 fois notre mise ; une place du quota anti-bot perdue.
    sniper_min_bid_share: float = 0.40
    sniper_max_bid: int = 0          # 0 = pas de plafond absolu (le budget par position s'applique)
    sniper_daily_budget: int = 0     # 0 = pas de budget quotidien
    sniper_min_balance_reserve: int = 50
    sniper_prefetch_seconds: float = 8.0
    sniper_allow_rebid: bool = True
    # Mise d'une position : la mise minimale + cette part de l'écart jusqu'à notre plafond ; surenchérie, la
    # mise suivante va droit au plafond. Le 17/10 : 2 L réelles gagnées sur 29, les rivaux surenchérissent d'un
    # pas (+10 %) en 20-30 s, et chaque mise use le quota de la vérification anti-bot (~9-10 mises). 0 = minimum.
    sniper_bid_push: float = 0.5
    sniper_settle_grace_seconds: int = 30
    # Vérification anti-bot en attente : une seule mise réelle tentée toutes les N s, pour voir si elle est levée
    # (le 16/10, ~45 mises refusées en 1 h 30). 0 = chaque mise est tentée : la levée est vue tout de suite, utile
    # quand une extension passe la vérification dans le navigateur.
    sniper_antibot_retry_seconds: float = 300.0
    sniper_max_concurrent_bids: int = 0   # 0 = illimité (le site n'impose pas de limite aux mises)
    # Sniper de test : avec des mises réelles, simule en plus les achats que le sniper réel ne fait pas (au-dessus
    # de SNIPER_MAX_BID, plafond de positions atteint, raretés SNIPER_SHADOW_RARITIES), pour
    # continuer à mesurer la stratégie. Ni plafond de mise ni plafond de positions (le budget d'une position et le
    # solde simulé s'appliquent), et le sniper réel passe toujours en premier. Une rareté ajoutée ici coûte des
    # requêtes de repérage (moins de relectures pour le réel).
    sniper_shadow: bool = False
    sniper_shadow_rarities: CsvList = Field(default_factory=list)  # en plus de SNIPER_RESALE_RARITIES
    sniper_avoid_my_bids: bool = True     # ne jamais toucher aux cartes sur lesquelles tu mises toi-même

    # Arbitrage nocturne
    night_start: time = time(2, 0)
    night_end: time = time(7, 30)
    night_deal_ratio_max: float = 0.80
    night_window_seconds: int = 60
    night_budget_bonus: int = 0

    # --- Capital : positions achetées pour être revendues ----------------------------
    portfolio_slots: int = 5              # = places de vente simultanées du site
    portfolio_max_positions: int = 15     # plafond absolu : positions détenues + enchères du bot
    portfolio_buffer: int = 10            # positions en attente d'une place de vente, au-delà des places libres
    portfolio_queue_profit_ratio: float = 0.5  # bénéfice exigé pour une position qui devra attendre
    portfolio_capital_ratio: float = 0.9  # part des WikiBidous engagée en positions
    portfolio_min_profit_ratio: float = 0.25  # bénéfice attendu ≥ 25 % du budget d'une position
    portfolio_min_profit: int = 30        # plancher absolu du bénéfice attendu
    sale_fee_pct: float = 0.0             # commission éventuelle du site sur une vente
    # Revente d'une position : c'est une enchère, les joueurs font monter le prix. Le prix de départ est le prix
    # d'achat + cette marge (commission comprise) ; jamais sous le prix d'achat, même remise en vente.
    position_list_margin: float = 0.15
    # Prix de départ relevé : prix d'achat + cette part du bénéfice attendu à l'achat. Les enchères montent peu
    # (relevé du 16/10 : prix final / prix de départ, médiane ×1,11 en UR, ×1,23 en L ; 41 % et 29 % sans
    # surenchère) et les 5 places de vente sont rares : un départ bas vendrait presque au prix d'achat.
    position_list_profit_share: float = 0.5
    # Revente publiée dès que la carte est arrivée (16/10 : 2 L sur 3 attendaient le lendemain 11 h, capital
    # immobilisé ; l'heure de fin change peu le prix, voir `python -m src.main hours`). False : fenêtre ci-dessous.
    position_post_now: bool = True
    position_post_start: time = time(11, 0)  # fenêtre de publication (Paris) si POSITION_POST_NOW=false
    position_post_end: time = time(12, 0)
    # Durée de la revente : 6 h, pour faire tourner plus vite les 5 places de vente. Moins de
    # POSITION_LIST_LONG_BELOW positions détenues (rien n'attend de place) : POSITION_LIST_LONG_MINUTES, plus vue.
    position_list_duration_minutes: int = 360
    position_list_long_minutes: int = 720
    position_list_long_below: int = 5     # 0 = toujours POSITION_LIST_DURATION_MINUTES
    position_relist_discount: float = 0.9 # invendue : remise en vente à prix × 0,9
    position_max_relists: int = 2         # puis au prix d'achat, tant qu'une place de vente est libre
    # Après POSITION_LOSS_AFTER mises en vente sans acheteur, la revente peut descendre jusqu'à POSITION_MAX_LOSS
    # sous le prix d'achat : une carte qui ne part pas bloque une des 5 places (~900 WB par revente qui tourne) et son
    # capital (Mark Wahlberg ✨, achetée 6119, invendue à 7037 puis à 6333 le 25/10). 0 = jamais sous le prix d'achat.
    position_max_loss: float = 0.10
    position_loss_after: int = 2

    # --- Module C : inventaire --------------------------------------------------
    inventory_interval_minutes: float = 360.0
    auto_list: bool = False
    sell_min_value: int = 15
    sell_keep_copies: int = 1
    # Raretés vendues même en dernier exemplaire (une L sortie d'un paquet). Jamais une carte achetée par le bot.
    sell_last_copy_rarities: CsvList = Field(default_factory=list)
    sell_price_multiplier: float = 1.0
    sell_max_listings_per_run: int = 5
    sell_anytime: bool = True        # doublons vendus dès qu'une place se libère (6 h), pas seulement le soir
    sell_check_minutes: float = 10.0  # vérification des places libres
    sell_price_lookups_per_run: int = 60  # cotes demandées au site par passage (route limitée) ; le reste : ventes observées
    listing_end_window_start: time = time(20, 0)
    listing_end_window_end: time = time(22, 30)
    # Durées proposées par le formulaire de vente du site [KZ features/ranking.js].
    listing_duration_options: IntList = Field(default_factory=lambda: [10, 30, 60, 180, 360, 720, 1440])
    listing_preferred_duration_minutes: int = 360
    listing_min_duration_minutes: int = 60

    auto_discard: bool = False
    protected_tags: CsvList = Field(default_factory=list)  # étiquettes jamais vendues ni défaussées
    # Tes propres achats à revendre : un exemplaire portant cette étiquette (casse ignorée) devient une position du bot,
    # revendue avec les mêmes règles (prix de départ, remises à -10 %, plancher), au prix que tu l'as payé : ta vente
    # observée de la carte (le bot note l'acheteur des ventes des raretés qu'il suit), relue sur le site. Vide : jamais.
    manual_resale_tag: str = "revente"
    discard_rarities: CsvList = Field(default_factory=lambda: ["C", "PC"])
    discard_max_value: int = 2
    discard_keep_copies: int = 1
    discard_unknown_price_is_worthless: bool = False
    # Raretés défaussées même en dernier exemplaire (la carte quitte la collection), si le module I (VALUE_SCAN_*) l'a
    # cotée et qu'aucune vente connue ne dépasse DISCARD_MAX_VALUE (ou qu'elle n'en a aucune). Vide = jamais.
    discard_last_copy_rarities: CsvList = Field(default_factory=list)
    discard_max_per_run: int = 250  # exemplaires défaussés par passage au plus (0 = sans limite)
    achievement_milestones: IntList = Field(default_factory=list)
    achievement_count_mode: Literal["total", "unique"] = "total"
    achievement_margin: int = 50

    # --- Module I : cotes de toute la collection (défausse des derniers exemplaires) ------
    # Toutes les N heures, toute la collection est cotée, derniers exemplaires compris (la vente automatique ne cote
    # que des doublons) : la défausse de DISCARD_LAST_COPY_RARITIES s'en sert. Aucun message.
    enable_value_scan: bool = True
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
    collection_sets: CsvList = Field(default_factory=list)    # « jours » = les 366 jours de l'année
    collection_titles: CsvList = Field(default_factory=list)  # titres en plus, exacts (ex. Paris,Lyon)
    collection_max_price: int = 50                              # mise maximale pour une carte manquante
    collection_refresh_minutes: float = 360.0
    collection_search_seconds: float = 30.0  # le sniper cherche un jour manquant sur le marché toutes les N s
    # Mises réelles de collection par vérification anti-bot (le site en accepte ~9-10, puis la redemande) :
    # le reste est gardé pour les L. 0 = pas de limite.
    collection_bids_per_check: int = 2
    collection_tag: str = "date"           # étiquette ajoutée à un exemplaire de chaque carte possédée ("" = non)
    collection_tag_max_per_run: int = 60   # étiquettes posées par passage (requêtes espacées)

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
        """Une heure s'écrit aussi sans zéro initial : 3:00 vaut 03:00 (17/10 : une heure à 3:00 refusée)."""
        field = cls.model_fields.get(info.field_name or "")
        if field is not None and time in (field.annotation, *get_args(field.annotation)) and isinstance(value, str):
            value = value.strip()
            if re.fullmatch(r"\d:\d\d(:\d\d)?", value):
                return f"0{value}"
        return value

    @field_validator("discard_rarities", "sniper_resale_rarities", "sniper_shadow_rarities",
                     "sell_last_copy_rarities", "discard_last_copy_rarities")
    @classmethod
    def _check_rarities(cls, value: list[str]) -> list[str]:
        value = [v.upper() for v in value]
        unknown = [v for v in value if v not in RARITIES]
        if unknown:
            raise ValueError(f"raretés inconnues {unknown}, attendues : {RARITIES}")
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
        return label

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
