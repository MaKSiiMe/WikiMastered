# Réglages

Tous les réglages du bot, avec leur valeur par défaut. Il suffit d'en ajouter une ligne dans `.env` (ou dans les variables de la stack Portainer) pour la changer, puis de redémarrer le bot : `docker compose up -d` avec Compose, **Pull and redeploy** avec Portainer. Une ligne absente garde la valeur montrée ici.

Les valeurs par défaut sont volontairement prudentes et n'ont pas été optimisées : à toi de trouver les tiennes. Les plus utiles au départ sont résumées dans le [guide d'installation](INSTALLATION.md#5-choisir-les-réglages).

Formats : listes en CSV (`UR,L`) ou JSON (`["UR","L"]`), dictionnaires en JSON, heures en `HH:MM` (heure de Paris). Dans `.env`, mets les valeurs qui contiennent des espaces ou des `;` entre apostrophes ; dans Portainer, jamais d'apostrophes.

- [Session](#session)
- [Simulation](#simulation)
- [Discord](#discord)
- [Sauvegardes](#sauvegardes)
- [Réseau](#réseau)
- [Workers](#workers)
- [Estimation des prix](#estimation-des-prix)
- [Sniper : achats et reventes](#sniper--achats-et-reventes)
- [Inventaire : ventes et défausse](#inventaire--ventes-et-défausse)
- [Collections](#collections)
- [Étiquettes de thème](#étiquettes-de-thème)
- [Avancé](#avancé)

## Session

Comment obtenir le cookie : [Installation, étape 1](INSTALLATION.md#1-récupérer-la-session-du-bot).

```sh
# En-tête Cookie complet d'une requête /api/ de wiki-masters.com, pris dans une fenêtre privée. Doit contenir
# sb-<ref>-auth-token (éventuellement découpé en .0, .1...). Entre apostrophes dans .env.
WM_COOKIE=
# User-Agent du navigateur qui a produit le cookie (même requête).
WM_USER_AGENT='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36'
# Ton identifiant joueur (UUID). Déduit du cookie si vide ; `python -m src.main check` l'affiche.
# Sert à ne jamais enchérir sur tes propres ventes.
WM_USER_ID=
WM_BASE_URL=https://www.wiki-masters.com
WM_AUTHORIZATION=                     # seulement si tes requêtes portent un en-tête Authorization
WM_EXTRA_HEADERS={}                   # en-têtes en plus, JSON (parse_curl --env le remplit)

# Clé publique du site. Vide : le bot la cherche au démarrage dans les scripts du site.
# Sans elle : pas de renouvellement de session, ni d'étiquettes, de succès ou de garde des sanctions.
# À la main, si ça échoue : DevTools > Réseau > requête vers *.supabase.co > en-tête `apikey`.
SUPABASE_ANON_KEY=
SUPABASE_URL=                         # vide = https://<ref>.supabase.co (ref lu dans le cookie)
SESSION_REFRESH_MARGIN_MINUTES=10     # renouvellement N minutes avant l'expiration du jeton
```

## Simulation

Détail des étapes : [Passer en réel](INSTALLATION.md#6-passer-en-réel).

```sh
# Simule toutes les écritures (enchères, ventes, défausses, étiquettes, succès).
DRY_RUN=true
# Réel / simulé worker par worker. Groupes : sniper (mises des positions), collection (mises du
# mode collection), ventes, defausse, tags, themes, succes, inventory (= ventes + defausse).
# Avec DRY_RUN=true : LIVE_WRITES liste ce qui est quand même réel (ex. ventes).
# Avec DRY_RUN=false : SIMULATE liste ce qui reste simulé (ex. sniper : tout le reste est réel).
LIVE_WRITES=
SIMULATE=
```

**Exemples**
- `DRY_RUN=true`, `LIVE_WRITES=ventes` : le bot vend vraiment tes doublons, mais ses enchères restent simulées.
- `DRY_RUN=false`, `SIMULATE=sniper` : tout est réel (ventes, défausse, étiquettes…) sauf les mises de revente. Pratique pour regarder quelques jours ce que le sniper aurait acheté avant de lui confier des WikiBidous.

## Discord

Salons et commandes : [Installation, Commandes Discord et salons](INSTALLATION.md#commandes-discord-et-salons).

```sh
# Webhook du salon principal (Paramètres du salon > Intégrations > Webhooks). Vide : messages dans les logs seulement.
DISCORD_WEBHOOK_URL=
# Salon séparé pour les alertes rouges (session, 429, vérification anti-bot) + mention.
DISCORD_ALERT_WEBHOOK_URL=
# Salon d'activité (récaps périodiques, simulé) ; vide = salon principal, off = logs
DISCORD_ACTIVITY_WEBHOOK_URL=
# Un salon par catégorie ; une catégorie sans salon garde le principal ou l'activité.
DISCORD_WEBHOOK_SNIPER=               # récap des enchères de 08:00, budget, mises à vérifier
DISCORD_WEBHOOK_VENTES=               # un message par position, ventes, défausse, échanges
DISCORD_WEBHOOK_COLLECTION=           # cartes de collection, étiquettes, succès
DISCORD_WEBHOOK_BILAN=                # bilan de 23:00, récap des workers toutes les 4 h
DISCORD_WEBHOOK_SYSTEME=              # démarrage, session, réseau, sauvegardes
DISCORD_MENTION=                      # ex. @here : mention des alertes rouges
KUMA_PUSH_URL=                        # moniteur « Push » d'Uptime Kuma, appelé après les passages réussis
# Commandes slash (/roi, /bilan, /snipe…) : bot du portail développeur Discord (onglet Bot > Reset Token).
# Seul DISCORD_OWNER_ID (clic droit sur ton pseudo > Copier l'identifiant) peut s'en servir ; DISCORD_GUILD_ID
# (clic droit sur le serveur) les fait apparaître tout de suite. Vide = pas de commandes, les webhooks seuls.
DISCORD_BOT_TOKEN=
DISCORD_OWNER_ID=
DISCORD_GUILD_ID=
```

**Exemple** : avec seulement `DISCORD_WEBHOOK_URL` et `DISCORD_ALERT_WEBHOOK_URL`, tout arrive dans un salon, sauf les alertes rouges (vérification anti-bot à faire, session expirée), qui ont le leur. Si ce salon n'est réglé que pour les mentions, ton téléphone ne sonne que pour elles.

Si les logs disent `pas le droit d'écrire dans le salon du webhook, messages sans boutons`, le bot Discord (`DISCORD_BOT_TOKEN`) n'a pas le droit d'écrire dans ce salon. Les messages arrivent quand même par le webhook, mais sans boutons (« C'est fait », changer le prix, garder). Pour corriger, donne au bot les droits « Voir le salon » et « Envoyer des messages » dans ce salon.

## Sauvegardes

Restauration : [Installation, Sauvegardes](INSTALLATION.md#sauvegardes).

```sh
# Sauvegarde nocturne de la base, dans BACKUP_DIR (un volume à part) ; BACKUP_KEEP archives gardées.
ENABLE_BACKUP=true
BACKUP_DIR=backups
BACKUP_TIME=04:30
BACKUP_KEEP=7
OBSERVED_KEEP_DAYS=0                  # ventes observées effacées après N jours, sauf shiny et cartes rarement vendues (0 = toutes gardées)
```

**Exemple** : le bot note toutes les fins d'enchères du site. Au bout de quelques semaines, ça fait plusieurs centaines de Mo. Avec `OBSERVED_KEEP_DAYS=30`, une vente vieille de plus d'un mois est effacée. Les ventes de shiny et de cartes rarement vendues sont gardées, parce qu'elles sont les seules qui permettent d'estimer ces cartes.

## Réseau

```sh
HTTP_MIN_SPACING_SECONDS=1.5          # écart minimum entre deux lectures (tous workers confondus)
HTTP_JITTER_SECONDS=0.8               # + aléa 0..x s
HTTP_PEAK_START=09:00                 # pic de ventes (Paris) : écart plus court entre deux requêtes
HTTP_PEAK_END=02:00
HTTP_PEAK_SPACING_SECONDS=0           # écart pendant le pic (0 = jamais de pic)
HTTP_PEAK_COOLDOWN_MINUTES=30         # site qui limite le bot (429) pendant le pic : écart normal pendant N min
HTTP_WORKER_SPACING_SECONDS=          # écart propre à un worker, en plus de la file commune ; ex. cotes=5,etiquettes=3
HTTP_WRITE_SPACING_SECONDS=1.0        # écritures (mises, ventes…) : leur propre file, une toutes les N s
HTTP_TIMEOUT_SECONDS=20
HTTP_GET_MAX_ATTEMPTS=4               # essais d'une lecture (une écriture n'est jamais rejouée)
BACKOFF_BASE_SECONDS=30               # 5xx : 30 s, 60 s, 120 s... jusqu'à BACKOFF_MAX_SECONDS
BACKOFF_RATE_LIMIT_BASE_SECONDS=60    # 429 : 60 s, 120 s...
BACKOFF_MAX_SECONDS=900
SESSION_RECHECK_MINUTES=15            # après un 401/403, re-test de la session
```

**Exemple** : avec 1,5 s d'écart et 0 à 0,8 s d'aléa, il y a environ 2 s entre deux lectures, soit une trentaine par minute pour tous les workers ensemble. Dans les logs, `Route sales_summary limitée par le site : pause 71 s` veut dire que le site a refusé une route. Le bot la met en pause et les autres continuent. Tant que ces pauses restent rares, il n'y a rien à changer.

## Workers

```sh
ENABLE_SNIPER=true
ENABLE_INVENTORY=true                 # publie les ventes programmées ; scans auto via AUTO_*
ENABLE_ACHIEVEMENTS=true              # réclame les succès débloqués (SUPABASE_ANON_KEY requise)
ENABLE_PROFILE_GUARD=true             # une sanction du site arrête tout le bot (SUPABASE_ANON_KEY requise)
PROFILE_CHECK_MINUTES=15
PACKS_ALERT_AT=10                     # message quand N paquets attendent d'être ouverts (lu par la garde) ; 0 = jamais
ENABLE_TRADE_WATCH=true               # échanges proposés évalués sur Discord, avec une contre-offre
TRADE_CHECK_MINUTES=10
TRADE_FAIR_MARGIN=0.1                 # équitable : chaque côté vaut au moins 90 % de l'autre
WISH_MAX_PRICE=0                      # cartes de ta liste de souhaits achetées jusqu'à leur cote, au plus N WB ; 0 = désactivé
WISH_UNQUOTED_MAX_PRICE=0             # carte souhaitée sans cote dans cette rareté : misée jusqu'à N WB quand même ; 0 = pas de mise
WISH_TAG=Souhait                      # étiquette posée sur la carte obtenue (à créer une fois sur le site) ; vide = aucune
WISH_CHECK_MINUTES=10                 # lecture de ta liste de souhaits
ACHIEVEMENTS_TIME=22:50               # une fois par jour, avant le bilan de 23:00
DAILY_REPORT_TIME=23:00
RECAP_INTERVAL_HOURS=4                # récap des workers toutes les N heures (0 = aucun)
SNIPER_REPORT_TIME=08:00              # récap des enchères misées sur les SNIPER_REPORT_HOURS dernières heures
SNIPER_REPORT_HOURS=24                # 0 = aucun
NOTIFY_EACH_BID=false                 # true = un message Discord par enchère (très bavard)
LOG_LEVEL=INFO                        # console ; le journal data/logs/journal.jsonl garde tout
JOURNAL_KEEP_DAYS=7                   # jours de journal gardés (un fichier par jour)
STATS_MINUTES=15                      # relevé des passages et des requêtes (0 = aucun)
```

**Exemples**
- `TRADE_FAIR_MARGIN=0.1` : on te propose 900 WB de cartes contre 1000 WB des tiennes. 900 fait 90 % de 1000, donc l'échange est jugé équitable. À 850 contre 1000, il est jugé défavorable, et le bot propose une contre-offre sur Discord.
- `WISH_MAX_PRICE=2000` : une carte de ta liste de souhaits estimée 1500 WB est achetée jusqu'à 1500 WB. Une carte estimée 3000 l'est jusqu'à 2000 WB au plus.
- `WISH_UNQUOTED_MAX_PRICE=100` : une carte souhaitée jamais vue vendue dans cette rareté, en vente à 10 WB, est misée jusqu'à 100 WB. En vente à 600 WB, elle n'est pas misée et le bot te prévient.

## Estimation des prix

```sh
# Le prix suit les tendances (actu, pop culture, mèmes) et la moyenne du site peut être
# très datée. Estimation = médiane des ventes que le bot a lui-même observées (il voit
# toutes les fins d'enchères), plafonnée par la moyenne du site.
PRICE_RECENT_SALES=7                  # nombre de ventes observées récentes prises en compte
PRICE_SITE_MARGIN=1.0                 # plafond = moyenne du site × cette marge (1.1 quand les prix montent)
PRICE_MIN_SALES=4                     # ventes observées minimum avant d'acheter une carte
SNIPER_SHINY_MIN_SALES=8              # … pour un shiny, estimé sur ses seules ventes shiny
SNIPER_MIN_CONTESTED_SALES=4          # parmi elles, ventes disputées (plus d'une mise) minimum pour acheter
SNIPER_MAX_PRICE_SPREAD=1.5           # et la plus haute ≤ 1,5 × la plus basse (0 = sans limite)
PRICE_CACHE_TTL_MINUTES=180            # cache de la moyenne du site (route limitée)
```

**Exemple** : les 5 dernières ventes observées d'une carte UR sont 700, 800, 820, 900 et 950 WB, dont 4 disputées (plus d'une mise).
- **Assez de ventes :** 5 ventes, il en faut 4 (`PRICE_MIN_SALES`). C'est bon.
- **Assez de ventes disputées :** 4, il en faut 4 (`SNIPER_MIN_CONTESTED_SALES`). C'est bon.
- **Ventes assez proches :** la plus haute fait 950 / 700 = 1,36 fois la plus basse, la limite est 1,5 (`SNIPER_MAX_PRICE_SPREAD`). C'est bon. Avec une vente à 1200, on aurait 1200 / 700 = 1,7 et la carte serait écartée, parce que ses ventes sont trop dispersées.
- **Estimation :** la médiane vaut 820, mais la moyenne du site est de 780. Avec `PRICE_SITE_MARGIN=1.0`, l'estimation est plafonnée à 780 WB. Avec `1.1`, le plafond passerait à 858 et l'estimation resterait à 820.

Cette estimation est la « revente visée » des messages Discord. C'est ce que la carte rapporte en moyenne, pas son prix de mise en vente : voir plus bas.

## Sniper : achats et reventes

```sh
# Le bot n'achète que pour revendre : chaque achat est une « position » qui occupera une des
# 5 places de vente. Rien n'est acheté tant que SNIPER_RESALE_RARITIES est vide.
# Ces valeurs sont volontairement prudentes et n'ont pas été optimisées : à toi de les ajuster.
PORTFOLIO_SLOTS=5                     # = places de vente simultanées du site
PORTFOLIO_MAX_POSITIONS=5             # plafond absolu : positions détenues + enchères du bot (0 = aucun)
PORTFOLIO_BUFFER=0                    # positions en file d'attente au-delà des places de vente libres (0 = aucune limite)
PORTFOLIO_QUEUE_PROFIT_RATIO=1.0      # une position qui devra attendre exige ≥ 100 % du budget (~300 WB),
#                                       # sauf si moins de ventes en attente que de places libres
#                                       # rapportent plus qu'elle : elle passe devant les autres,
#                                       # au bénéfice minimum
PORTFOLIO_CAPITAL_RATIO=0.5           # budget d'une position = capital × 0,5 / 5 (~300 WB pour 3000)
PORTFOLIO_MIN_PROFIT_RATIO=0.5        # bénéfice attendu ≥ 50 % de ce budget (~150 WB)
PORTFOLIO_MIN_PROFIT=100              # plancher absolu du bénéfice attendu
SALE_FEE_PCT=0.0                      # commission du site sur une vente, si elle existe
POSITION_LIST_PROFIT_SHARE=0.25       # prix de départ de la revente = achat + 25 % du bénéfice attendu
POSITION_LIST_MARGIN=0.2              # ... et au moins achat + 20 %
POSITION_POST_NOW=true                # revente publiée dès l'arrivée de la carte (false : fenêtre ci-dessous)
POSITION_POST_START=11:00
POSITION_POST_END=12:00
POSITION_LIST_DURATION_MINUTES=360    # durée de la revente d'une position (6 h)
POSITION_LIST_LONG_MINUTES=720        # … 12 h tant que moins de POSITION_LIST_LONG_BELOW positions sont détenues
POSITION_LIST_LONG_BELOW=0            # 0 = toujours POSITION_LIST_DURATION_MINUTES
POSITION_AVOID_END_HOURS=11,12,13,18,19,20,21,22  # heures de fin évitées par la revente de 12 h : 6 h si elle finit hors de ces heures (6 h jamais allongée)
POSITION_STEP_MINUTES=360             # une étape toutes les 6 h en vente sans acheteur (0 = une par vente)
POSITION_RELIST_DISCOUNT=0.95         # étape franchie : remise en vente à -5 % (même prix entre deux étapes)
POSITION_MAX_RELISTS=3                # puis au prix d'achat tant qu'une place est libre
POSITION_MAX_LOSS=0                   # après POSITION_LOSS_AFTER étapes : jusqu'à cette part sous le prix d'achat (0 = non)
POSITION_LOSS_AFTER=2
POSITION_GIVE_UP_AFTER=0              # après N étapes : le bot renonce, la carte est gardée et étiquetée (0 = jamais)
POSITION_UNSOLD_TAG=Invendue          # étiquette d'une carte que le bot renonce à vendre (à créer sur le site ; vide = aucune)
POSITION_EXPENSIVE_FROM=0             # position chère (achetée ≥ N WB, 0 = mêmes règles pour toutes) :
POSITION_EXPENSIVE_MAX_RELISTS=1      # … moins de remises
POSITION_EXPENSIVE_GIVE_UP_AFTER=2    # … le bot renonce après N étapes
POSITION_WAIT_SLOT_FROM=0             # achetée ≥ N WB : attend une place de vente au lieu d'être gardée faute de place (0 = jamais)
POSITION_STALE_HOURS=48               # alerte « Position qui traîne » après N heures (0 = jamais)

SNIPER_POLL_MIN_SECONDS=3.0            # pause entre deux passages du sniper (une requête chacun)
SNIPER_POLL_MAX_SECONDS=5.0
SNIPER_RESALE_RARITIES=                # raretés repérées et revendues, ex. SR (vide : aucune revente)
SNIPER_DISCOVERY_SECONDS=30           # nouvelles ventes de chaque rareté repérée, toutes les N s (pages suivantes lues jusqu'à la lecture d'avant)
SNIPER_OBSERVE_SECONDS=60             # ventes tout juste terminées (prix observés), toutes les N s
COLLECTION_SEARCH_SECONDS=120         # recherche d'une carte de collection sur le marché toutes les N s
SNIPER_WINDOW_SECONDS=30              # miser s'il reste moins de X s
SNIPER_MIN_SECONDS_LEFT=15            # ... mais jamais dans les 10 dernières (prolongation de 60 s), marge de 5 s
SNIPER_DEAL_RATIO_MAX=0.5             # mise ≤ 50 % de l'estimation, en plus du bénéfice minimum
SNIPER_MIN_BID_SHARE=0.25             # mise < 25 % de l'estimation : position sautée (quota anti-bot gardé)
SNIPER_MIN_ESTIMATE=0                 # carte non shiny revendue seulement si estimée ≥ N WB (0 = toutes ; shiny toujours)
SNIPER_MAX_RISE=0                     # enchère sautée si sa mise minimale dépasse N × le prix de départ, ex. 1.5 (0 = jamais ; shiny toujours)
SNIPER_ANTIBOT_RETRY_SECONDS=300     # vérification anti-bot en attente : une mise tentée toutes les N s (0 = chacune)
SNIPER_PREFETCH_SECONDS=8             # estimation anticipée d'une enchère toutes les N s
SNIPER_MIN_BALANCE_RESERVE=50         # solde jamais descendu sous ce seuil
SNIPER_MAX_BID=500                    # plafond absolu par enchère (0 = aucun)
SNIPER_DAILY_BUDGET=0                 # budget quotidien (0 = aucun)
SNIPER_ALLOW_REBID=false              # surenchérie : remiser jusqu'au plafond
SNIPER_SETTLE_GRACE_SECONDS=30        # attente après la fin d'une enchère avant de lire son résultat
SNIPER_BID_PUSH=0                     # mise = minimum + cette part de l'écart jusqu'au plafond (0 : le minimum)
SNIPER_BID_PUSH_CONTROL_SHARE=0       # part des mises laissées au minimum malgré SNIPER_BID_PUSH, pour mesurer ce qu'il rapporte
SNIPER_MIN_BID_VALUE=0                # valeur minimale d'une mise, en WB : chance de gagner × (revente − mise),
#                                       # d'après tes mises passées ; garde le quota anti-bot pour les meilleures (0 = non)
SNIPER_BID_VALUE_DAYS=14              # … mises des N derniers jours (il en faut 200 ; sinon, pas de seuil)
SNIPER_AVOID_MY_BIDS=true             # le bot ignore les cartes sur lesquelles tu mises (tes collections)
SNIPER_MAX_CONCURRENT_BIDS=0          # limite volontaire d'enchères en cours (0 = illimité, comme le site)

# Arbitrage nocturne (heure de Paris)
NIGHT_START=02:00
NIGHT_END=07:30
NIGHT_DEAL_RATIO_MAX=0.5
NIGHT_WINDOW_SECONDS=30
NIGHT_BUDGET_BONUS=0
NIGHT_MIN_BID_VALUE=0                 # valeur minimale d'une mise la nuit (0 = SNIPER_MIN_BID_VALUE)
NIGHT_MAX_RISE=0                      # SNIPER_MAX_RISE la nuit, ex. 1.2 : une mise déjà faite au plus (0 = celui du jour)
```

### Exemple : une position, de l'achat à la revente

Réglages par défaut, avec `SNIPER_RESALE_RARITIES=UR`, un solde de 3000 WB et aucune position. La carte est celle de l'exemple d'[Estimation des prix](#estimation-des-prix), estimée 780 WB.

**1. Le budget.**
- **Budget d'une position :** 3000 × 0,5 / 5 = **300 WB** (`PORTFOLIO_CAPITAL_RATIO`, `PORTFOLIO_SLOTS`).
- **Bénéfice minimum :** le plus grand de 300 × 0,5 = 150 (`PORTFOLIO_MIN_PROFIT_RATIO`) et de 100 (`PORTFOLIO_MIN_PROFIT`), soit **150 WB**.

Les positions déjà achetées comptent dans le capital : avec 2000 WB de solde et 1000 WB en cartes à revendre, le budget est le même.

**2. La mise maximale.** C'est la plus petite de ces limites :
- 780 × 0,5 = 390 (`SNIPER_DEAL_RATIO_MAX`) ;
- 780 − 150 = 630 (le bénéfice minimum doit rester) ;
- 300 (le budget d'une position) ;
- 500 (`SNIPER_MAX_BID`).

Le bot mise donc **300 WB au plus**.

**3. La mise.** Le bot attend qu'il reste entre 30 s (`SNIPER_WINDOW_SECONDS`) et 15 s (`SNIPER_MIN_SECONDS_LEFT`) avant la fin de l'enchère.
- **Mise minimale du site à 240 :** 240 ≤ 300, le bot mise 240 (`SNIPER_BID_PUSH=0` : il mise toujours le minimum). Avec `SNIPER_BID_PUSH=0.5`, il miserait 240 + 0,5 × (300 − 240) = 270. Ses chances de gagner sont meilleures, mais le bénéfice est plus petit.
- **Mise minimale à 150 :** 150 est sous 25 % de l'estimation (780 × 0,25 = 195, `SNIPER_MIN_BID_SHARE`). L'enchère partira sûrement bien plus haut, donc le bot ne gâche pas une mise du quota anti-bot dessus.
- **Mise minimale à 320 :** au-dessus de 300, le bot ne mise pas.

La nuit (`NIGHT_START` à `NIGHT_END`), `NIGHT_DEAL_RATIO_MAX` et `NIGHT_WINDOW_SECONDS` remplacent les réglages de jour.

**4. Le prix de mise en vente.** Le bot gagne à 240. La carte est remise en vente aux enchères, et le prix affiché est un **prix de départ** que les joueurs font monter. C'est le plus grand de :
- 240 × 1,2 = 288 (`POSITION_LIST_MARGIN`) ;
- 240 + 25 % du bénéfice attendu = 240 + 0,25 × (780 − 240) = **375** (`POSITION_LIST_PROFIT_SHARE`).

La carte est mise en vente à **375 WB pour 6 h** (`POSITION_LIST_DURATION_MINUTES`). Le prix de départ est donc toujours entre le prix d'achat et la revente visée. Si la plupart de tes reventes partent à leur prix de départ, monte `POSITION_LIST_PROFIT_SHARE` : avec `0.5`, le départ passe à 510, avec moins d'acheteurs.

**5. Une vente concurrente.** Exemple réel : une carte achetée 1000 et estimée 2000 est prévue à 1250. Juste avant la publication, une autre vente de la même carte est à 1100. Le bot se met 1 WB en dessous, à 1099, pour être la moins chère. Il ne descend jamais sous le prix d'achat. Une vente concurrente sous 50 % de l'estimation (ici sous 1000) est ignorée : c'est un prix de départ, pas un vrai prix.

**6. Personne n'achète.** Sans acheteur, la carte avance d'une étape toutes les 6 h en vente (`POSITION_STEP_MINUTES`).
- **Étapes 1 à 3 :** la carte est remise en vente 5 % moins cher à chaque fois (`POSITION_RELIST_DISCOUNT`, `POSITION_MAX_RELISTS=3`) : 375 → 356 → 338 → 321.
- **Ensuite :** elle est remise en vente au prix d'achat + 1 (241) tant qu'une place de vente est libre. Si les 5 places sont prises, le bot la garde dans ta collection.
- **Limite de perte :** `POSITION_MAX_LOSS=0` interdit de vendre sous le prix d'achat. Avec `POSITION_MAX_LOSS=0.1` et `POSITION_LOSS_AFTER=2`, le prix pourrait descendre jusqu'à 217 (241 − 10 %) à partir de la 2e étape.
- **Abandon :** `POSITION_GIVE_UP_AFTER=4` arrête après 4 étapes. La carte reste dans ta collection avec l'étiquette `Invendue`.

**7. Le bilan.** Dans le pire cas (réglages par défaut), la carte revient dans ta collection, achetée 240. Dans le meilleur, des joueurs surenchérissent au-dessus de 375.

## Inventaire : ventes et défausse

```sh
INVENTORY_INTERVAL_MINUTES=360
AUTO_LIST=false                       # mise en vente automatique (sinon : CLI inventory list)
SELL_MIN_VALUE=15
SELL_KEEP_COPIES=1                    # exemplaires toujours gardés par carte
SELL_LAST_COPY_RARITIES=              # vendues même en dernier exemplaire (ex. L), hors cartes achetées par le bot
SELL_PRICE_MULTIPLIER=1.0             # prix de départ = cote × multiplicateur
SELL_MAX_LISTINGS_PER_RUN=5             # le site limite à 5 ventes simultanées
SELL_ANYTIME=true                     # doublons vendus dès qu'une place se libère (sinon : fin 20:00-22:30)
SELL_CHECK_MINUTES=10                 # vérification des places libres
SELL_PRICE_LOOKUPS_PER_RUN=60         # cotes demandées au site par passage ; le reste : ventes observées
LISTING_END_WINDOW_START=20:00
LISTING_END_WINDOW_END=22:30
LISTING_DURATION_OPTIONS=10,30,60,180,360,720,1440   # durées du formulaire de vente du site
LISTING_PREFERRED_DURATION_MINUTES=360  # durée visée (la plus proche possible finissant dans la fenêtre)
LISTING_MIN_DURATION_MINUTES=60

PROTECTED_TAGS=                       # étiquettes du site jamais vendues ni défaussées ; ex. date,GIF
MANUAL_RESALE_TAG=revente             # tes achats portant cette étiquette : revendus par le bot (vide = non)

AUTO_DISCARD=false
DISCARD_RARITIES=C,PC
DISCARD_MAX_VALUE=2                   # cote ≤ 2 WB = sans valeur marchande
DISCARD_KEEP_COPIES=1
DISCARD_UNKNOWN_PRICE_IS_WORTHLESS=false
# Raretés défaussées même en dernier exemplaire (la carte quitte ta collection) : seulement une carte déjà cotée par
# le passage de cotes (VALUE_SCAN_*), sans aucune vente connue au-dessus de DISCARD_MAX_VALUE. Vide = jamais.
DISCARD_LAST_COPY_RARITIES=           # ex. C,PC
DISCARD_MAX_PER_RUN=250               # exemplaires défaussés par passage (INVENTORY_INTERVAL_MINUTES) ; 0 = sans limite
# Paliers de succès « taille de collection » : la défausse ne repasse jamais sous un palier atteint.
# Vide : aucune défausse, par prudence. 0 : aucun palier à protéger.
ACHIEVEMENT_MILESTONES=               # ex. 100,250,500,1000,2500,5000
ACHIEVEMENT_COUNT_MODE=total          # total = exemplaires, unique = cartes distinctes
ACHIEVEMENT_MARGIN=50

# Cotes de toute la collection, derniers exemplaires compris, pour DISCARD_LAST_COPY_RARITIES (aucun message).
ENABLE_VALUE_SCAN=true
VALUE_SCAN_HOURS=12                   # un passage toutes les N heures
VALUE_SCAN_LOOKUPS=0                  # cotes demandées au site par passage (0 = toutes)
VALUE_SCAN_LOOKUP_SECONDS=3           # au mieux une cote toutes les N s, en fond (ralentit si le site refuse)
VALUE_SCAN_MAX_AGE_DAYS=7             # cote plus récente : reprise du cache, pas redemandée
VALUE_SCAN_UNSOLD_DAYS=30             # carte sans aucune vente : recotée après N jours
VALUE_SCAN_FREEZE_BELOW=50            # cotée sous N WB : cote figée pour toujours (0 = jamais)

# Valeur du compte (/valeur) : collection relue toutes les N heures au plus, si aucun autre worker ne l'a fait.
ENABLE_ACCOUNT_VALUE=true
ACCOUNT_VALUE_HOURS=6
```

Cette partie ne concerne que les cartes de ta collection : les doublons qu'on vend, et les cartes sans valeur qu'on défausse. Les cartes achetées par le sniper suivent les règles de la section précédente.

**Vente des doublons** (`AUTO_LIST=true`) : tu as 3 exemplaires d'une carte cotée 83 WB.
- **Exemplaires vendus :** le bot en garde 1 (`SELL_KEEP_COPIES`) et vend les 2 autres, une vente par place libre.
- **Prix :** 83 ≥ 15 (`SELL_MIN_VALUE`), donc la carte est vendue au prix de départ 83 × 1,0 = 83 WB (`SELL_PRICE_MULTIPLIER`). Une carte cotée 10 WB ne serait pas vendue.
- **Horaire :** avec `SELL_ANYTIME=true`, la vente part dès qu'une place se libère. Avec `false`, elle est programmée pour finir entre 20:00 et 22:30.

**Défausse** (`AUTO_DISCARD=true`) : la défausse rapporte 1 WB par exemplaire. Avec `DISCARD_RARITIES=C,PC` et `DISCARD_MAX_VALUE=2`, chaque doublon C ou PC coté 2 WB ou moins est défaussé. Un exemplaire de chaque carte est toujours gardé (`DISCARD_KEEP_COPIES=1`), et les moins cotés partent en premier, 250 au plus par passage (`DISCARD_MAX_PER_RUN`).

**Paliers de succès**, avec `ACHIEVEMENT_MILESTONES=1000,2500,5000` et `ACHIEVEMENT_MARGIN=50` :
- **2480 exemplaires :** le palier 2500 est à 20 exemplaires, moins que la marge de 50. La défausse est bloquée pour ne pas t'en éloigner.
- **2700 exemplaires :** le palier 2500 est atteint, la défausse peut retirer 200 exemplaires au plus, pour ne pas repasser sous 2500.
- **`ACHIEVEMENT_MILESTONES=0` :** aucun palier à protéger, la défausse n'a pas de limite de ce côté (log `on ne repasse pas sous 0`).
- **Vide (par défaut) :** aucune défausse.

**Étiquettes protégées** : avec `PROTECTED_TAGS=GIF,date`, un exemplaire portant l'étiquette « GIF » ou « date » sur le site n'est jamais vendu ni défaussé, même en doublon.

## Collections

Collections disponibles : `jours` (les 366 jours de l'année), `gifs` (articles à GIF animé), ou la tienne dans [`config/collections.yaml`](../config/collections.yaml). Détail : [Fonctionnement](FONCTIONNEMENT.md#collections).

```sh
COLLECTION_SETS=                      # collections actives (vide : aucune) ; ex. ma_liste (config/collections.yaml)
COLLECTION_TITLES=                    # titres exacts en plus, séparés par des virgules
COLLECTION_MAX_PRICE=10               # mise maximale pour une carte manquante
COLLECTION_SET_MAX_PRICE=             # plafond propre à une collection, à la place du précédent ; ex. elements=100
COLLECTION_SET_QUOTE_RATIO=           # collection misée jusqu'à sa cote × N, comme une revente ; ex. pays=0.8
COLLECTION_UPGRADE=false              # miser aussi sur une rareté plus haute que la meilleure possédée
COLLECTION_WISHLIST=false             # cartes manquantes en liste de souhaits du site : il annonce leurs ventes (SUPABASE_ANON_KEY requise)
COLLECTION_WISHLIST_SECONDS=120       # lecture des annonces du site toutes les N secondes
COLLECTION_BIDS_PER_CHECK=2           # mises de collection par vérification anti-bot (~9-10 mises) ; 0 = sans limite
COLLECTION_REFRESH_MINUTES=360        # relecture de la collection
COLLECTION_TAG=date                   # étiquette des jours et de COLLECTION_TITLES (vide = non)
COLLECTION_TAG_MAX_PER_RUN=60
```

**Exemple**, avec `COLLECTION_SETS=jours` et `COLLECTION_MAX_PRICE=50` : il te manque la carte « 14 juillet ». Une enchère sur cette carte, à n'importe quelle rareté, arrive à sa fin avec une mise minimale de 30 WB. Le bot mise 30 et garde la carte, sans la revendre. À 60 WB, il ne mise pas.

Avec `COLLECTION_UPGRADE=true`, s'il te reste un « 14 juillet » en C, le bot mise aussi sur un exemplaire de rareté plus haute (PC, R…), dans la même limite de 50 WB.

Un plafond par collection, avec `COLLECTION_SETS=jours,elements,pays`, `COLLECTION_SET_MAX_PRICE=elements=100` et `COLLECTION_SET_QUOTE_RATIO=pays=0.8` :
- « Fer » en vente à 90 WB : misé, la limite des éléments est 100 WB. Un jour reste limité par `COLLECTION_MAX_PRICE`.
- « Canada » en L, cote 1 760 WB : misé jusqu'à 1 408 WB (0,8 × 1 760). Comme pour une revente, une autre vente du Canada en L à 1 500 WB ramène la cote à 1 500, donc le plafond à 1 200.
- Un pays sans cote connue dans la rareté en vente : pas de mise.
- Les deux réglages ensemble (`COLLECTION_SET_MAX_PRICE=pays=3000`) : la cote × 0,8, au plus 3 000 WB.

Une carte de collection manquante, ou à monter en rareté, n'est jamais achetée pour la revente, même si la revente permettrait de miser plus haut.

Le site laisse passer environ 9 ou 10 mises par vérification anti-bot. Avec `COLLECTION_BIDS_PER_CHECK=2`, la collection en prend 2 au plus, et les autres restent aux reventes.

## Étiquettes de thème

Règles des thèmes dans [`config/etiquettes.yaml`](../config/etiquettes.yaml). Détail : [Fonctionnement](FONCTIONNEMENT.md#étiquettes-de-thème).

```sh
ENABLE_THEME_TAGS=false               # thèmes sur l'exemplaire gardé, « Doublon » sur les autres
THEME_TAGS_HOURS=6                    # passage complet (30 min tant qu'il reste du travail)
THEME_TAGS_MAX_PER_RUN=200            # étiquettes posées ou retirées par passage
THEME_LOOKUPS_PER_RUN=500             # cartes lues sur Wikidata par passage
THEME_DUPLICATE_TAG=Doublon           # vide = pas d'étiquette de doublon (ne pas la mettre dans PROTECTED_TAGS)
THEME_GIF_TAG=GIF                     # sur l'exemplaire gardé d'une carte animée, retirée ailleurs ; vide = pas d'étiquette GIF
```

## Avancé

```sh
TIMEZONE=Europe/Paris
DATA_DIR=data
ENDPOINTS_FILE=config/endpoints.yaml
```

