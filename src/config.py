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

from src.core.text import tag_key


RARITIES: tuple[str, ...] = ("C", "PC", "R", "SR", "UR", "L")
RARITY_RANK: dict[str, int] = {r: i for i, r in enumerate(RARITIES)}

# Écritures du site regroupées par worker, pour LIVE_WRITES / SIMULATE (noms de routes acceptés aussi).
WRITE_GROUPS: dict[str, tuple[str, ...]] = {
    "sniper": ("place_bid",),            # mises des positions (achat pour revendre)
    "collection": ("collection_bid",),   # mises du mode collection (cartes gardées)
    "ventes": ("create_listing",), "inventory": ("create_listing", "bulk_discard"),
    "defausse": ("bulk_discard",), "tags": ("tag_card",), "themes": ("theme_tag",),
    "succes": ("achievements_check", "claim_achievement"),
    "souhaits": ("wishlist_write", "mark_notifications_read"),     # liste de souhaits du site, pour les cartes de collection manquantes
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
    # Écritures réelles malgré DRY_RUN=true (ex. ventes).
    live_writes: CsvList = Field(default_factory=list)
    # Écritures simulées malgré DRY_RUN=false (ex. sniper : tout est réel sauf les mises des positions).
    simulate: CsvList = Field(default_factory=list)
    log_level: str = "INFO"  # console (logs Portainer) ; le journal data/logs/journal.jsonl garde tout, DEBUG compris
    journal_keep_days: int = 7  # jours de journal gardés (un fichier par jour)
    stats_minutes: float = 15.0  # relevé des passages des workers et des requêtes au site (0 = aucun)
    timezone: str = "Europe/Paris"
    data_dir: Path = Path("data")
    # Sauvegarde quotidienne de la base (src/backup.py) dans un volume à part ; BACKUP_KEEP archives gardées.
    enable_backup: bool = True
    backup_dir: Path = Path("backups")
    backup_time: time = time(4, 30)
    backup_keep: int = 7
    # Ventes observées gardées N jours (0 = toutes, pour toujours) : chaque nuit à BACKUP_TIME, les ventes plus
    # anciennes sont effacées, sauf les shiny (rares : toutes gardées) et celles d'une carte vendue moins de
    # PRICE_RECENT_SALES fois sur la période (son estimation en dépend). Le 12/11 : 733 000 ventes en 30 jours,
    # 320 Mo sur une base de 372, sauvegarde de 25 min ; 96 % des achats s'appuient sur des ventes de moins de 3 jours.
    observed_keep_days: int = 0
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
    # son propre écart minimal entre deux de ses lectures : cotes=5,etiquettes=3 (ou en JSON). Un worker absent n'a
    # que le plafond commun ; le sniper n'y est pas, pour prendre les créneaux que les autres laissent. Vide ou 0 :
    # aucun écart propre (31/10 : la valeur 0 empêchait le bot de démarrer).
    http_worker_spacing_seconds: Annotated[dict[str, float], NoDecode] = Field(default_factory=dict)
    # Écritures (mises, ventes, étiquettes…) : leur propre file, hors du plafond des lectures, une toutes les N s.
    http_write_spacing_seconds: float = 1.0
    http_timeout_seconds: float = 20.0
    http_get_max_attempts: int = 4
    backoff_base_seconds: float = 30.0
    backoff_rate_limit_base_seconds: float = 60.0
    backoff_max_seconds: float = 900.0
    session_recheck_minutes: float = 15.0

    # --- Discord ----------------------------------------------------------------
    discord_webhook_url: str = ""
    discord_alert_webhook_url: str = ""
    # Salon d'activité (récap périodique, simulé) : vide = salon principal ;
    # « off » = logs seulement. Le salon principal garde alertes, achats, ventes, cartes gagnées, bilans.
    discord_activity_webhook_url: str = ""
    # Un salon par catégorie (un webhook chacun) : il reçoit tous ses messages, sauf les alertes critiques (salon
    # d'alertes). Une catégorie sans salon garde le salon principal ou d'activité. Le message unique de chaque position
    # (achetée, en vente, vendue…) va dans #ventes.
    discord_webhook_sniper: str = ""      # récap des enchères de 08:00, budget, mises à vérifier
    discord_webhook_ventes: str = ""      # positions, ventes, défausse, échanges
    discord_webhook_collection: str = ""  # cartes de collection, étiquettes, succès
    discord_webhook_bilan: str = ""       # bilan de 23:00, récap des workers toutes les 4 h
    discord_webhook_systeme: str = ""     # démarrage, session, réseau, sauvegardes
    discord_mention: str = ""
    # Commandes slash (/roi, /bilan, /snipe…) : bot Discord (portail développeur > Bot > token). Vide = pas de
    # commandes, les webhooks seuls. Seul DISCORD_OWNER_ID (ton identifiant utilisateur) peut s'en servir ;
    # DISCORD_GUILD_ID (ton serveur) les fait apparaître tout de suite au lieu d'attendre jusqu'à 1 h.
    discord_bot_token: str = ""
    discord_owner_id: int = 0
    discord_guild_id: int = 0
    daily_report_time: time = time(23, 0)
    recap_interval_hours: int = 4  # récap des workers toutes les N heures (0 = aucun)
    # Récap du sniper : chaque enchère misée sur les N dernières heures (gagnées, perdues, gain latent et manqué).
    sniper_report_time: time = time(8, 0)
    sniper_report_hours: int = 24  # 0 = pas de récap du sniper
    notify_each_bid: bool = False  # un message par mise du sniper (sinon : les logs, et le récap des enchères de 08:00)

    # --- Workers ----------------------------------------------------------------
    enable_sniper: bool = True
    enable_inventory: bool = True
    enable_achievements: bool = True
    # Garde : profil relu toutes les N min ; une sanction du site arrête tout le bot (data/HALT).
    enable_profile_guard: bool = True
    profile_check_minutes: float = 15.0
    packs_alert_at: int = 10                # message quand ce nombre de paquets attend d'être ouvert ; 0 = jamais
    # Échanges proposés relus toutes les N min : chacun est évalué une fois (src/trades.py), avec une contre-offre à
    # envoyer s'il est défavorable. Équitable : chaque côté vaut au moins (1 - TRADE_FAIR_MARGIN) fois l'autre.
    enable_trade_watch: bool = True
    # Cartes souhaitées (worker « souhaits ») : une carte que tu ajoutes toi-même à ta liste de souhaits du site est
    # misée par le sniper jusqu'à sa cote dans la rareté en vente, au plus WISH_MAX_PRICE (0 = désactivé), dans toute
    # rareté plus haute que celle que tu possèdes. Gagnée, elle est gardée et reçoit WISH_TAG (vide = aucune).
    wish_max_price: int = 0
    wish_tag: str = "Souhait"
    wish_check_minutes: float = 10.0
    trade_check_minutes: float = 10.0
    trade_fair_margin: float = 0.1
    achievements_time: time = time(22, 50)  # succès réclamés une fois par jour, avant le bilan de 23:00

    # --- Cotes ------------------------------------------------------------------
    price_cache_ttl_minutes: float = 180.0
    price_min_sales: int = 4     # ventes observées minimum avant d'acheter une carte
    # Un shiny en demande plus : son marché est maigre (Mark Wahlberg ✨, achetée 6119 le 24/10 sur 3 ventes de 3400,
    # 7797 et 8000, revendue 5000). Seules ses propres ventes comptent, la cote du site n'entre pas dans l'estimation.
    sniper_shiny_min_sales: int = 8
    price_recent_sales: int = 7  # estimation = médiane des N dernières ventes observées
    # L'estimation est plafonnée par la moyenne du site × cette marge. 1 : la moyenne elle-même. Elle suit les prix
    # avec retard : quand ils montent, 1,1 laisse le sniper acheter les cartes en hausse (voir pricing.combine).
    price_site_margin: float = Field(1.0, ge=1.0)
    # Achat d'une position : parmi ces N ventes, au moins tant de ventes disputées (plus d'une mise), et la plus
    # haute pas plus de X fois la plus basse (0 = sans limite). Le 18/10, l'éclipse XXIe était achetée 882 sur une
    # estimation de 1197 faite de 3 ventes au prix de départ et de 2 disputées (970 et 1774).
    sniper_min_contested_sales: int = 4
    sniper_max_price_spread: float = 1.5

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
    sniper_deal_ratio_max: float = 0.50
    # Position sautée si notre mise vaut moins de cette part de l'estimation (0 = jamais) : une enchère très sous sa
    # valeur partira bien plus haut, et la mise userait une place du quota anti-bot pour rien.
    sniper_min_bid_share: float = 0.25
    sniper_max_bid: int = 500        # plafond absolu d'une mise, /snipe excepté (0 = aucun, le budget par position s'applique)
    sniper_daily_budget: int = 0     # 0 = pas de budget quotidien
    sniper_min_balance_reserve: int = 50
    sniper_prefetch_seconds: float = 8.0
    sniper_allow_rebid: bool = False
    # Mise d'une position : la mise minimale + cette part de l'écart jusqu'à notre plafond ; surenchérie (si
    # SNIPER_ALLOW_REBID), la mise suivante va droit au plafond. Chaque mise use le quota de la vérification anti-bot
    # (~9-10 mises). 0 = minimum.
    sniper_bid_push: float = 0.0
    # Part des mises laissées au minimum malgré SNIPER_BID_PUSH (une enchère sur N, tirée de son identifiant) : un
    # groupe témoin, pour mesurer si pousser la mise rapporte plus qu'il ne coûte (`sniper-report --breakdown`,
    # ligne « mise au minimum »). 0 = aucune.
    sniper_bid_push_control_share: float = 0.0
    # Valeur minimale d'une mise, en WB : chance de gagner × (revente attendue − mise), d'après les mises réelles
    # des SNIPER_BID_VALUE_DAYS derniers jours (core/winmodel.py). Le quota de la vérification anti-bot va ainsi aux
    # mises qui rapportent le plus. `python -m src.main sniper-value` aide à choisir. 0 = pas de seuil.
    sniper_min_bid_value: float = 0.0
    sniper_bid_value_days: float = 14.0
    sniper_settle_grace_seconds: int = 30
    # Vérification anti-bot en attente : une seule mise réelle tentée toutes les N s, pour voir si elle est levée
    # (le 16/10, ~45 mises refusées en 1 h 30). 0 = chaque mise est tentée : la levée est vue tout de suite, utile
    # quand une extension passe la vérification dans le navigateur.
    sniper_antibot_retry_seconds: float = 300.0
    sniper_max_concurrent_bids: int = 0   # 0 = illimité (le site n'impose pas de limite aux mises)
    sniper_avoid_my_bids: bool = True     # ne jamais toucher aux cartes sur lesquelles tu mises toi-même

    # Arbitrage nocturne
    night_start: time = time(2, 0)
    night_end: time = time(7, 30)
    night_deal_ratio_max: float = 0.50
    night_window_seconds: int = 30
    night_budget_bonus: int = 0
    night_min_bid_value: float = 0.0  # valeur minimale d'une mise la nuit (0 = SNIPER_MIN_BID_VALUE)

    # --- Capital : positions achetées pour être revendues ----------------------------
    portfolio_slots: int = 5              # = places de vente simultanées du site
    # Plafonds de positions (0 = aucun).
    portfolio_max_positions: int = 5      # plafond absolu : positions détenues + enchères du bot
    portfolio_buffer: int = 0             # positions en attente d'une place de vente, au-delà des places libres
    portfolio_queue_profit_ratio: float = 1.0  # bénéfice exigé pour une position qui devra attendre
    portfolio_capital_ratio: float = 0.5  # part des WikiBidous engagée en positions
    portfolio_min_profit_ratio: float = 0.5  # bénéfice attendu ≥ 50 % du budget d'une position
    portfolio_min_profit: int = 100       # plancher absolu du bénéfice attendu
    sale_fee_pct: float = 0.0             # commission éventuelle du site sur une vente
    # Revente d'une position : c'est une enchère, les joueurs font monter le prix. Le prix de départ est le prix
    # d'achat + cette marge (commission comprise) ; jamais sous le prix d'achat, même remise en vente.
    position_list_margin: float = 0.20
    # Prix de départ relevé : prix d'achat + cette part du bénéfice attendu à l'achat. Les enchères montent peu
    # (relevé du 16/10 : prix final / prix de départ, médiane ×1,11 en UR, ×1,23 en L ; 41 % et 29 % sans
    # surenchère) et les 5 places de vente sont rares : un départ bas vendrait presque au prix d'achat.
    position_list_profit_share: float = 0.25
    # Revente publiée dès que la carte est arrivée (16/10 : 2 L sur 3 attendaient le lendemain 11 h, capital
    # immobilisé ; l'heure de fin change peu le prix, voir `python -m src.main hours`). False : fenêtre ci-dessous.
    position_post_now: bool = True
    position_post_start: time = time(11, 0)  # fenêtre de publication (Paris) si POSITION_POST_NOW=false
    position_post_end: time = time(12, 0)
    # Durée de la revente. Plus courte, les 5 places de vente tournent plus vite ; plus longue, la vente est plus vue.
    # Moins de POSITION_LIST_LONG_BELOW positions détenues (rien n'attend de place) : POSITION_LIST_LONG_MINUTES.
    position_list_duration_minutes: int = 360
    position_list_long_minutes: int = 720
    position_list_long_below: int = 0     # 0 = toujours POSITION_LIST_DURATION_MINUTES
    # Heures de fin (Paris) évitées par la revente de 12 h : 6 h si elle finit hors de ces heures (6 h n'est jamais
    # allongée : des positions attendent une place). Ventes de L du
    # 25/10 19:43 au 26/10 19:18 (prix ÷ médiane des 8 ventes précédentes de la carte) : ×0,91-0,97 et 23-33 % sous
    # ×0,8 pour une fin à 18-22 h, ×0,93-0,95 à 11 h et 13 h, contre ×1,04-1,09 à 00-01 h et 07-09 h. Vide = jamais.
    position_avoid_end_hours: IntList = Field(default_factory=lambda: [11, 12, 13, 18, 19, 20, 21, 22])
    # Échelle des remises en temps passé en vente sans acheteur : une étape toutes les N minutes (une vente plus longue
    # n'en compte qu'une). POSITION_MAX_RELISTS, POSITION_LOSS_AFTER et POSITION_GIVE_UP_AFTER comptent en étapes :
    # avec des reventes d'1 h, compter les ventes ferait renoncer en 3 h. Même prix entre deux étapes.
    # 0 = une étape par vente sans acheteur ; vide = 360.
    position_step_minutes: int = 360
    position_relist_discount: float = 0.95 # étape franchie : remise en vente à prix × cette valeur
    position_max_relists: int = 3         # puis au prix d'achat, tant qu'une place de vente est libre
    # Après POSITION_LOSS_AFTER étapes sans acheteur, la revente peut descendre jusqu'à POSITION_MAX_LOSS
    # sous le prix d'achat : une carte qui ne part pas bloque une des 5 places (~900 WB par revente qui tourne) et son
    # capital (Mark Wahlberg ✨, achetée 6119, invendue à 7037 puis à 6333 le 25/10). 0 = jamais sous le prix d'achat.
    position_max_loss: float = 0.0
    position_loss_after: int = 2
    # Après POSITION_GIVE_UP_AFTER étapes sans acheteur, le bot renonce : la carte reste dans la collection, la position
    # est close, et l'exemplaire reçoit l'étiquette POSITION_UNSOLD_TAG (à créer une fois sur le site ; "" = aucune),
    # pour la retrouver et la vendre autrement. Même étiquette quand une carte est gardée faute de place de vente.
    # Le bot ne brade jamais une carte. 0 = il ne renonce pas tant qu'une place de vente est libre.
    position_give_up_after: int = 0
    position_unsold_tag: str = "Invendue"
    # Position détenue depuis plus de N heures : alerte « Position qui traîne » avec un bouton (une fois). Au-delà de
    # l'échelle de remises (≈ 24-40 h), quelque chose bloque : pas de place de vente, exemplaire introuvable, favori.
    # 0 = jamais.
    position_stale_hours: float = 48.0
    # Positions chères (achetées ≥ POSITION_EXPENSIVE_FROM WB) : moins de remises, et le bot renonce plus tôt, le
    # capital et la place de vente ne restent pas bloqués 2 jours. 0 = mêmes règles pour toutes.
    position_expensive_from: int = 0
    position_expensive_max_relists: int = 1
    position_expensive_give_up_after: int = 2
    # Position achetée ≥ N WB, invendue alors que toutes les places de vente sont prises : elle attend une place au prix
    # plancher au lieu d'être gardée (11/11 : une L achetée 8 110, close après 4 essais en 13 h, capital immobilisé).
    # POSITION_GIVE_UP_AFTER s'applique toujours. 0 = jamais : la carte est gardée faute de place.
    position_wait_slot_from: int = 0

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
    # Valeur du compte (/valeur) : collection relue toutes les N heures au plus, si aucun autre worker ne l'a fait.
    enable_account_value: bool = True
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
    collection_sets: CsvList = Field(default_factory=list)    # « jours » + clés de config/collections.yaml
    collection_titles: CsvList = Field(default_factory=list)  # titres en plus, exacts (ex. Paris,Lyon)
    collection_max_price: int = 10                              # mise maximale pour une carte manquante
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
    collection_tag: str = "date"           # étiquette des jours et de COLLECTION_TITLES ("" = non)
    collection_tag_max_per_run: int = 60   # étiquettes posées par passage (requêtes espacées)

    # --- Étiquettes de thème (worker « etiquettes ») ---------------------------------------
    # Thème de chaque carte (Personne, Pays, Musique…) lu sur Wikidata selon config/etiquettes.yaml, posé sur
    # l'exemplaire gardé ; les autres exemplaires reçoivent THEME_DUPLICATE_TAG. Écritures : groupe « themes ».
    enable_theme_tags: bool = False
    theme_tags_hours: float = 6.0          # passage complet toutes les N h
    theme_tags_max_per_run: int = 200      # étiquettes posées ou retirées par passage (requêtes de fond)
    theme_lookups_per_run: int = 500       # cartes classées sur Wikidata par passage (le reste au suivant)
    theme_duplicate_tag: str = "Doublon"   # "" = pas d'étiquette de doublon
    theme_gif_tag: str = "GIF"             # sur l'exemplaire gardé d'une carte animée ; "" = pas d'étiquette GIF

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

    @field_validator("discard_rarities", "sniper_resale_rarities",
                     "sell_last_copy_rarities", "discard_last_copy_rarities")
    @classmethod
    def _check_rarities(cls, value: list[str]) -> list[str]:
        value = [v.upper() for v in value]
        unknown = [v for v in value if v not in RARITIES]
        if unknown:
            raise ValueError(f"raretés inconnues {unknown}, attendues : {RARITIES}")
        return value

    @field_validator("http_worker_spacing_seconds", mode="before")
    @classmethod
    def _worker_spacing(cls, value: Any) -> Any:
        """worker=écart séparés par des virgules, ou du JSON ; vide ou 0 : aucun."""
        if not isinstance(value, str | int | float):
            return value
        text = str(value).strip().strip("\"'")
        if text in ("", "0", "{}"):
            return {}
        if text.startswith("{"):
            return json.loads(text)
        pairs = [part.split("=", 1) for part in text.split(",") if part.strip()]
        if any(len(pair) != 2 for pair in pairs):
            raise ValueError(f"attendu worker=secondes séparés par des virgules (ex. cotes=5,etiquettes=3) : {text}")
        return {name.strip(): float(spacing) for name, spacing in pairs}

    @field_validator("position_step_minutes", mode="before")
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
        """PROTECTED_TAGS comparables aux étiquettes des cartes (OwnedCard.tag_names) : émojis et casse ignorés."""
        return {key for key in map(tag_key, self.protected_tags) if key}

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
