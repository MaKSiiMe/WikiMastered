# Réglages

Tous les réglages du bot, rangés par domaine, avec leur valeur par défaut. Pour en changer un, ajoute sa ligne dans `.env` (ou dans les variables de la stack Portainer), puis redémarre le bot : `docker compose up -d` avec Compose, **Pull and redeploy** avec Portainer. Une ligne absente garde la valeur montrée ici.

Les valeurs par défaut sont volontairement prudentes et n'ont pas été optimisées : à toi de trouver les tiennes. Les plus utiles au départ sont résumées dans le [guide d'installation](INSTALLATION.md#5-choisir-les-réglages).

**Comment lire un nom** : le premier mot est le domaine (`SNIPER_`, `RESALE_`, `TAG_`…), `_ENABLED` allume un worker, et la fin donne l'unité : `_SECONDS`, `_MINUTES`, `_HOURS`, `_DAYS`, ou `_RATIO` pour une part ou un multiplicateur (0,25 ; 0,95). Un prix est toujours en WB.

**Formats** : listes en CSV (`UR,L`) ou JSON (`["UR","L"]`), dictionnaires en JSON, heures en `HH:MM` (heure de Paris). Dans `.env`, mets les valeurs qui contiennent des espaces ou des `;` entre apostrophes ; dans Portainer, jamais d'apostrophes.

**Noms changés le 22/11/2026** : 114 réglages ont pris leur domaine en tête. Les anciens noms restent lus pour l'instant, et le bot liste au démarrage ceux qu'il trouve chez toi. Table complète : [Anciens noms](#anciens-noms).

| Domaine | Ce qu'il règle |
|---|---|
| [Session](#session) | Connexion au site : cookie, clé publique |
| [Simulation](#simulation) | Ce qui est réel ou simulé |
| [Discord et rapports](#discord-et-rapports) | Salons, commandes, bilan et récaps |
| [Journal et sauvegardes](#journal-et-sauvegardes) | Logs, base sauvegardée chaque nuit |
| [Réseau](#réseau) | Rythme des requêtes au site |
| [Cotes](#cotes) | Estimation du prix d'une carte |
| [Sniper](#sniper) | Achats pour revendre : quelles enchères, quelle mise |
| [Revente](#revente) | Positions achetées : budget, prix de départ, remises |
| [Doublons](#doublons) | Vente automatique de tes doublons |
| [Défausse](#défausse) | Cartes sans valeur défaussées, cotes de toute la collection |
| [Collections](#collections) | Collections à compléter |
| [Souhaits](#souhaits) | Cartes de ta liste de souhaits achetées par le bot |
| [Thèmes](#thèmes) | Tags de thème lus sur Wikidata |
| [Tags](#tags) | Noms des tags posés ou lus par le bot |
| [Garde, succès, échanges, valeur](#garde-succès-échanges-valeur) | Les autres workers |
| [Avancé](#avancé) | Fuseau, dossiers |
| [Anciens noms](#anciens-noms) | Ancien nom → nouveau nom |

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
WM_AUTHORIZATION=                       # seulement si tes requêtes portent un en-tête Authorization
WM_EXTRA_HEADERS={}                     # en-têtes en plus, JSON (parse_curl --env le remplit)
# Clé publique du site. Vide : le bot la cherche au démarrage dans les scripts du site.
# Sans elle : pas de renouvellement de session, ni d'étiquettes, de succès ou de garde des sanctions.
# À la main, si ça échoue : DevTools > Réseau > requête vers *.supabase.co > en-tête `apikey`.
SUPABASE_ANON_KEY=
SUPABASE_URL=                           # vide = https://<ref>.supabase.co (ref lu dans le cookie)
SESSION_REFRESH_MARGIN_MINUTES=10       # renouvellement N minutes avant l'expiration du jeton
SESSION_CHECK_MINUTES=15                # après un 401/403, re-test de la session
```

## Simulation

`DRY_RUN=true` simule toutes les écritures (enchères, ventes, défausses, tags, succès). Ensuite, on passe au réel groupe par groupe : avec `DRY_RUN=true`, `LIVE_WRITES` liste ce qui est quand même réel ; avec `DRY_RUN=false`, `SIMULATE` liste ce qui reste simulé. Détail des étapes : [Passer en réel](INSTALLATION.md#6-passer-en-réel).

| Groupe | Écritures |
|---|---|
| `sniper` | Mises des positions (achat pour revendre) |
| `collection` | Mises des collections et des cartes demandées |
| `resale` | Mises en vente et baisses de prix |
| `discard` | Défausse |
| `inventory` | `resale` + `discard` |
| `tags` | Tags de collection, de vitrine et de souhait |
| `themes` | Tags de thème |
| `achievements` | Succès réclamés |
| `wishes` | Liste de souhaits du site |
| `settle` | Enchères finies que le site tarde à régler |
| `showcases` | Vitrines du profil |

Les anciens noms français (`ventes`, `defausse`, `succes`, `souhaits`…) restent acceptés, avec un avertissement au démarrage.

```sh
# Simule toutes les écritures (enchères, ventes, défausses, étiquettes, succès).
DRY_RUN=true
LIVE_WRITES=
SIMULATE=
```

**Exemples**
- `DRY_RUN=true`, `LIVE_WRITES=resale` : le bot vend vraiment tes doublons, mais ses enchères restent simulées.
- `DRY_RUN=false`, `SIMULATE=sniper` : tout est réel (ventes, défausse, étiquettes…) sauf les mises de revente. Pratique pour regarder quelques jours ce que le sniper aurait acheté avant de lui confier des WikiBidous.

## Discord et rapports

Salons et commandes : [Installation, Commandes Discord et salons](INSTALLATION.md#commandes-discord-et-salons). Chaque salon est un webhook (Paramètres du salon > Intégrations > Webhooks) ; une catégorie sans salon garde le principal ou l'activité.

```sh
# Webhook du salon principal (Paramètres du salon > Intégrations > Webhooks). Vide : messages dans les logs seulement.
DISCORD_WEBHOOK_MAIN=
# Salon séparé pour les alertes rouges (session, 429, vérification anti-bot) + mention.
DISCORD_WEBHOOK_ALERTS=
# Salon d'activité (récaps périodiques, simulé) ; vide = salon principal, off = logs
DISCORD_WEBHOOK_ACTIVITY=
DISCORD_WEBHOOK_SNIPER=                 # récap des enchères de 08:00, budget, mises à vérifier
DISCORD_WEBHOOK_RESALE=                 # un message par position, ventes, défausse, échanges
DISCORD_WEBHOOK_COLLECTION=             # cartes de collection, étiquettes, succès
DISCORD_WEBHOOK_REPORT=                 # bilan de 23:00, récap des workers toutes les 4 h
DISCORD_WEBHOOK_SYSTEM=                 # démarrage, session, réseau, sauvegardes
DISCORD_WEBHOOK_WIKINSIDER=             # achats et reventes de shiny à noter à la main sur Wikinsider, bouton « Noté » ; vide = rien
DISCORD_WIKINSIDER_KEPT=false           # true = aussi les achats gardés (collection, souhaits, cartes demandées)
DISCORD_MENTION=                        # ex. @here : mention des alertes rouges
DISCORD_NOTIFY_EACH_BID=false           # true = un message Discord par enchère (très bavard)
# Commandes slash (/compte roi, /compte bilan, /sniper demander…) : bot du portail développeur Discord (onglet Bot > Reset Token).
# Seul DISCORD_OWNER_ID (clic droit sur ton pseudo > Copier l'identifiant) peut s'en servir ; DISCORD_GUILD_ID
# (clic droit sur le serveur) les fait apparaître tout de suite. Vide = pas de commandes, les webhooks seuls.
DISCORD_BOT_TOKEN=
DISCORD_OWNER_ID=
DISCORD_GUILD_ID=
KUMA_PUSH_URL=                          # moniteur « Push » d'Uptime Kuma, appelé après les passages réussis
REPORT_DAILY_TIME=23:00
REPORT_RECAP_HOURS=4                    # récap des workers toutes les N heures (0 = aucun)
REPORT_SNIPER_TIME=08:00                # récap des enchères misées sur les REPORT_SNIPER_HOURS dernières heures
REPORT_SNIPER_HOURS=24                  # 0 = aucun
```

**Exemple** : avec seulement `DISCORD_WEBHOOK_MAIN` et `DISCORD_WEBHOOK_ALERTS`, tout arrive dans un salon, sauf les alertes rouges (vérification anti-bot à faire, session expirée), qui ont le leur. Si ce salon n'est réglé que pour les mentions, ton téléphone ne sonne que pour elles.

Si les logs disent `pas le droit d'écrire dans le salon du webhook, messages sans boutons`, le bot Discord (`DISCORD_BOT_TOKEN`) n'a pas le droit d'écrire dans ce salon. Les messages arrivent quand même par le webhook, mais sans boutons (« C'est fait », changer le prix, garder). Pour corriger, donne au bot les droits « Voir le salon » et « Envoyer des messages » dans ce salon.

## Journal et sauvegardes

La base est sauvegardée chaque nuit dans `BACKUP_DIR` (un volume à part). Restauration : [Installation, Sauvegardes](INSTALLATION.md#sauvegardes).

```sh
LOG_LEVEL=INFO                          # console ; le journal data/logs/journal.jsonl garde tout
LOG_JOURNAL_KEEP_DAYS=7                 # jours de journal gardés (un fichier par jour)
LOG_STATS_MINUTES=15                    # relevé des passages et des requêtes (0 = aucun)
BACKUP_ENABLED=true
BACKUP_DIR=backups
BACKUP_TIME=04:30
BACKUP_KEEP_COUNT=7
```

## Réseau

Toutes les lectures des workers passent par une file commune ; les écritures ont la leur.

```sh
HTTP_MIN_SPACING_SECONDS=1.5            # écart minimum entre deux lectures (tous workers confondus)
HTTP_JITTER_SECONDS=0.8                 # + aléa 0..x s
HTTP_PEAK_START=09:00                   # pic de ventes (Paris) : écart plus court entre deux requêtes
HTTP_PEAK_END=02:00
HTTP_PEAK_SPACING_SECONDS=0             # écart pendant le pic (0 = jamais de pic)
HTTP_PEAK_COOLDOWN_MINUTES=30           # site qui limite le bot (429) pendant le pic : écart normal pendant N min
HTTP_WORKER_SPACING_SECONDS=            # écart propre à un worker, en plus de la file commune ; ex. value_scan=5,themes=3
HTTP_WRITE_SPACING_SECONDS=1.0          # écritures (mises, ventes…) : leur propre file, une toutes les N s
HTTP_TIMEOUT_SECONDS=20
HTTP_GET_MAX_ATTEMPTS=4                 # essais d'une lecture (une écriture n'est jamais rejouée)
HTTP_BACKOFF_SECONDS=30                 # 5xx : 30 s, 60 s, 120 s... jusqu'à HTTP_BACKOFF_MAX_SECONDS
HTTP_BACKOFF_RATE_LIMIT_SECONDS=60      # 429 : 60 s, 120 s...
HTTP_BACKOFF_MAX_SECONDS=900
REALTIME_ENABLED=false                  # temps réel du site : mises sur tes enchères (relues aussitôt surenchéries) et notifications (carte souhaitée en vente lue sans attendre)
```

**Exemple** : avec 1,5 s d'écart et 0 à 0,8 s d'aléa, il y a environ 2 s entre deux lectures, soit une trentaine par minute pour tous les workers ensemble. Dans les logs, `Route sales_summary limitée par le site : pause 71 s` veut dire que le site a refusé une route. Le bot la met en pause et les autres continuent. Tant que ces pauses restent rares, il n'y a rien à changer.

## Cotes

Le prix d'une carte suit les tendances (actu, pop culture, mèmes) et la moyenne du site peut être très datée. La cote du bot est donc la médiane des ventes qu'il a lui-même observées (il voit toutes les fins d'enchères), plafonnée par la moyenne du site.

La moyenne du site vient de l'historique des prix de l'onglet « Marché » de la fiche : une requête donne toutes les raretés de la carte. C'est la moyenne de la semaine en cours quand la carte s'est vendue au moins 10 fois ; sinon, le bot remonte les semaines précédentes, jusqu'à 4. Si l'historique est refusé, il prend la médiane des 5 dernières ventes de la carte.

```sh
QUOTE_RECENT_SALES=7                    # nombre de ventes observées récentes prises en compte
QUOTE_SITE_MARGIN_RATIO=1.0             # plafond = moyenne du site × cette marge (1.1 quand les prix montent)
QUOTE_MIN_SALES=4                       # ventes observées minimum avant d'acheter une carte
QUOTE_SHINY_MIN_SALES=8                 # … pour un shiny, estimé sur ses seules ventes shiny
QUOTE_CACHE_MINUTES=180                 # cache de la moyenne du site
QUOTE_HISTORY_FALLBACK=true             # carte souhaitée ou de collection sans cote : cote tirée de l'historique des prix du site
QUOTE_OBSERVED_KEEP_DAYS=0              # ventes observées effacées après N jours, sauf shiny et cartes rarement vendues (0 = toutes gardées)
MARKET_FEE_RATIO=0.0                    # commission du site sur une vente, si elle existe
```

**Exemple** : les 5 dernières ventes observées d'une carte UR sont 700, 800, 820, 900 et 950 WB, dont 4 disputées (plus d'une mise).
- **Assez de ventes :** 5 ventes, il en faut 4 (`QUOTE_MIN_SALES`). C'est bon.
- **Assez de ventes disputées :** 4, il en faut 4 (`SNIPER_MIN_CONTESTED_SALES`). C'est bon.
- **Ventes assez proches :** la plus haute fait 950 / 700 = 1,36 fois la plus basse, la limite est 1,5 (`SNIPER_MAX_SPREAD_RATIO`). C'est bon. Avec une vente à 1200, on aurait 1200 / 700 = 1,7 et la carte serait écartée, parce que ses ventes sont trop dispersées.
- **Estimation :** la médiane vaut 820, mais la moyenne du site est de 780. Avec `QUOTE_SITE_MARGIN_RATIO=1.0`, l'estimation est plafonnée à 780 WB. Avec `1.1`, le plafond passerait à 858 et l'estimation resterait à 820.

Cette estimation est la « revente visée » des messages Discord. C'est ce que la carte rapporte en moyenne, pas son prix de mise en vente : voir plus bas.

**Exemple** : le bot note toutes les fins d'enchères du site. Au bout de quelques semaines, ça fait plusieurs centaines de Mo. Avec `QUOTE_OBSERVED_KEEP_DAYS=30`, une vente vieille de plus d'un mois est effacée. Les ventes de shiny et de cartes rarement vendues sont gardées, parce qu'elles sont les seules qui permettent d'estimer ces cartes.

## Sniper

Le sniper repère les enchères des raretés de `SNIPER_RESALE_RARITIES` et mise dans les dernières secondes, pour revendre. Rien n'est acheté tant que cette liste est vide. La nuit (`SNIPER_NIGHT_START` à `SNIPER_NIGHT_END`), les réglages `SNIPER_NIGHT_` remplacent ceux du jour.

```sh
SNIPER_ENABLED=true
SNIPER_RESALE_RARITIES=                 # raretés repérées et revendues, ex. SR (vide : aucune revente)
SNIPER_POLL_MIN_SECONDS=3.0             # pause entre deux passages du sniper (une requête chacun)
SNIPER_POLL_MAX_SECONDS=5.0
SNIPER_DISCOVERY_SECONDS=30             # nouvelles ventes de chaque rareté repérée, toutes les N s (pages suivantes lues jusqu'à la lecture d'avant)
SNIPER_OBSERVE_SECONDS=60               # ventes tout juste terminées (prix observés), toutes les N s
SNIPER_WINDOW_SECONDS=30                # miser s'il reste moins de X s
SNIPER_MIN_SECONDS_LEFT=15              # ... mais jamais dans les 10 dernières (prolongation de 60 s), marge de 5 s
SNIPER_MIN_CONTESTED_SALES=4            # parmi les QUOTE_MIN_SALES ventes, au moins N disputées (plus d'une mise) pour acheter
SNIPER_MAX_SPREAD_RATIO=1.5             # et la plus haute de ces ventes ≤ 1,5 × la plus basse (0 = sans limite)
SNIPER_MAX_DEAL_RATIO=0.5               # mise ≤ 50 % de l'estimation, en plus du bénéfice minimum
SNIPER_MIN_BID_RATIO=0.25               # mise < 25 % de l'estimation : position sautée (quota anti-bot gardé)
SNIPER_MIN_ESTIMATE=0                   # carte non shiny revendue seulement si estimée ≥ N WB (0 = toutes ; shiny toujours)
SNIPER_MAX_RISE=0                       # enchère sautée si sa mise minimale dépasse N × le prix de départ, ex. 1.5 (0 = jamais ; shiny toujours)
SNIPER_MAX_BID=500                      # plafond absolu par enchère (0 = aucun)
SNIPER_DAILY_BUDGET=0                   # budget quotidien (0 = aucun)
SNIPER_BALANCE_RESERVE=50               # solde jamais descendu sous ce seuil
SNIPER_BID_PUSH_RATIO=0                 # mise = minimum + cette part de l'écart jusqu'au plafond (0 : le minimum)
SNIPER_BID_PUSH_CONTROL_RATIO=0         # part des mises laissées au minimum malgré SNIPER_BID_PUSH_RATIO, pour mesurer ce qu'il rapporte
SNIPER_MIN_BID_VALUE=0                  # valeur minimale d'une mise, en WB : chance de gagner × (revente − mise),
#                                       # d'après tes mises passées ; garde le quota anti-bot pour les meilleures (0 = non)
SNIPER_BID_VALUE_DAYS=14                # … mises des N derniers jours (il en faut 200 ; sinon, pas de seuil)
SNIPER_ALLOW_REBID=false                # surenchérie : remiser jusqu'au plafond
SNIPER_ANTIBOT_RETRY_SECONDS=300        # vérification anti-bot en attente : une mise tentée toutes les N s (0 = chacune)
SNIPER_PREFETCH_SECONDS=8               # estimation anticipée d'une enchère toutes les N s
SNIPER_SETTLE_DELAY_SECONDS=30          # attente après la fin d'une enchère avant de lire son résultat
SNIPER_SETTLE_STUCK=true                # enchère finie depuis 1 min mais pas encore réglée par le site : finalisée par le bot
SNIPER_AVOID_MY_BIDS=true               # le bot ignore les cartes sur lesquelles tu mises (tes collections)
SNIPER_MAX_CONCURRENT_BIDS=0            # limite volontaire d'enchères en cours (0 = illimité, comme le site)
SNIPER_NIGHT_START=02:00
SNIPER_NIGHT_END=07:30
SNIPER_NIGHT_MAX_DEAL_RATIO=0.5
SNIPER_NIGHT_WINDOW_SECONDS=30
SNIPER_NIGHT_BUDGET_BONUS=0
SNIPER_NIGHT_MIN_BID_VALUE=0            # valeur minimale d'une mise la nuit (0 = SNIPER_MIN_BID_VALUE)
SNIPER_NIGHT_MAX_RISE=0                 # SNIPER_MAX_RISE la nuit, ex. 1.2 : une mise déjà faite au plus (0 = celui du jour)
```

## Revente

Chaque achat du sniper est une « position », revendue aux enchères sur une des 5 places de vente du site. Ces réglages fixent le budget d'une position, son prix de départ, et les remises tant que personne n'achète.

```sh
RESALE_SLOTS=5                          # = places de vente simultanées du site
RESALE_MAX_POSITIONS=5                  # plafond absolu : positions détenues + enchères du bot (0 = aucun)
RESALE_QUEUE_SIZE=0                     # positions en file d'attente au-delà des places de vente libres (0 = aucune limite)
RESALE_QUEUE_PROFIT_RATIO=1.0           # une position qui devra attendre exige ≥ 100 % du budget (~300 WB),
#                                       # sauf si moins de ventes en attente que de places libres
#                                       # rapportent plus qu'elle : elle passe devant les autres,
#                                       # au bénéfice minimum
RESALE_CAPITAL_RATIO=0.5                # budget d'une position = capital × 0,5 / 5 (~300 WB pour 3000)
RESALE_MIN_PROFIT_RATIO=0.5             # bénéfice attendu ≥ 50 % de ce budget (~150 WB)
RESALE_MIN_PROFIT=100                   # plancher absolu du bénéfice attendu
RESALE_START_PROFIT_RATIO=0.25          # prix de départ de la revente = achat + 25 % du bénéfice attendu
RESALE_START_MARGIN_RATIO=0.2           # ... et au moins achat + 20 %
RESALE_POST_NOW=true                    # revente publiée dès l'arrivée de la carte (false : fenêtre ci-dessous)
RESALE_POST_START=11:00
RESALE_POST_END=12:00
RESALE_DURATION_MINUTES=360             # durée de la revente d'une position (6 h)
RESALE_LONG_DURATION_MINUTES=720        # … 12 h tant que moins de RESALE_LONG_BELOW_POSITIONS positions sont détenues
RESALE_LONG_BELOW_POSITIONS=0           # 0 = toujours RESALE_DURATION_MINUTES
RESALE_AVOID_END_HOURS=11,12,13,18,19,20,21,22  # heures de fin évitées par la revente de 12 h : 6 h si elle finit hors de ces heures (6 h jamais allongée)
RESALE_STEP_MINUTES=360                 # une étape toutes les 6 h en vente sans acheteur (0 = une par vente)
RESALE_STEP_PRICE_RATIO=0.95            # étape franchie : remise en vente à -5 % (même prix entre deux étapes)
RESALE_MIDWAY_REPRICE=false             # aucune mise à mi-parcours : départ baissé une fois sur place, de ce même pourcentage
RESALE_MAX_STEPS=3                      # puis au prix d'achat tant qu'une place est libre
RESALE_MAX_LOSS_RATIO=0                 # après RESALE_LOSS_AFTER_STEPS étapes : jusqu'à cette part sous le prix d'achat (0 = non)
RESALE_LOSS_AFTER_STEPS=2
RESALE_GIVE_UP_AFTER_STEPS=0            # après N étapes : le bot renonce, la carte est gardée et étiquetée (0 = jamais)
RESALE_EXPENSIVE_FROM=0                 # position chère (achetée ≥ N WB, 0 = mêmes règles pour toutes) :
RESALE_EXPENSIVE_MAX_STEPS=1            # … moins de remises
RESALE_EXPENSIVE_GIVE_UP_AFTER_STEPS=2  # … le bot renonce après N étapes
RESALE_WAIT_SLOT_FROM=0                 # achetée ≥ N WB : attend une place de vente au lieu d'être gardée faute de place (0 = jamais)
RESALE_STALE_HOURS=48                   # alerte « Position qui traîne » après N heures (0 = jamais)
```

### Exemple : une position, de l'achat à la revente

Réglages par défaut, avec `SNIPER_RESALE_RARITIES=UR`, un solde de 3000 WB et aucune position. La carte est celle de l'exemple d'[Cotes](#cotes), estimée 780 WB.

**1. Le budget.**
- **Budget d'une position :** 3000 × 0,5 / 5 = **300 WB** (`RESALE_CAPITAL_RATIO`, `RESALE_SLOTS`).
- **Bénéfice minimum :** le plus grand de 300 × 0,5 = 150 (`RESALE_MIN_PROFIT_RATIO`) et de 100 (`RESALE_MIN_PROFIT`), soit **150 WB**.

Les positions déjà achetées comptent dans le capital : avec 2000 WB de solde et 1000 WB en cartes à revendre, le budget est le même.

**2. La mise maximale.** C'est la plus petite de ces limites :
- 780 × 0,5 = 390 (`SNIPER_MAX_DEAL_RATIO`) ;
- 780 − 150 = 630 (le bénéfice minimum doit rester) ;
- 300 (le budget d'une position) ;
- 500 (`SNIPER_MAX_BID`).

Le bot mise donc **300 WB au plus**.

**3. La mise.** Le bot attend qu'il reste entre 30 s (`SNIPER_WINDOW_SECONDS`) et 15 s (`SNIPER_MIN_SECONDS_LEFT`) avant la fin de l'enchère.
- **Mise minimale du site à 240 :** 240 ≤ 300, le bot mise 240 (`SNIPER_BID_PUSH_RATIO=0` : il mise toujours le minimum). Avec `SNIPER_BID_PUSH_RATIO=0.5`, il miserait 240 + 0,5 × (300 − 240) = 270. Ses chances de gagner sont meilleures, mais le bénéfice est plus petit.
- **Mise minimale à 150 :** 150 est sous 25 % de l'estimation (780 × 0,25 = 195, `SNIPER_MIN_BID_RATIO`). L'enchère partira sûrement bien plus haut, donc le bot ne gâche pas une mise du quota anti-bot dessus.
- **Mise minimale à 320 :** au-dessus de 300, le bot ne mise pas.

La nuit (`SNIPER_NIGHT_START` à `SNIPER_NIGHT_END`), `SNIPER_NIGHT_MAX_DEAL_RATIO` et `SNIPER_NIGHT_WINDOW_SECONDS` remplacent les réglages de jour.

**4. Le prix de mise en vente.** Le bot gagne à 240. La carte est remise en vente aux enchères, et le prix affiché est un **prix de départ** que les joueurs font monter. C'est le plus grand de :
- 240 × 1,2 = 288 (`RESALE_START_MARGIN_RATIO`) ;
- 240 + 25 % du bénéfice attendu = 240 + 0,25 × (780 − 240) = **375** (`RESALE_START_PROFIT_RATIO`).

La carte est mise en vente à **375 WB pour 6 h** (`RESALE_DURATION_MINUTES`). Le prix de départ est donc toujours entre le prix d'achat et la revente visée. Si la plupart de tes reventes partent à leur prix de départ, monte `RESALE_START_PROFIT_RATIO` : avec `0.5`, le départ passe à 510, avec moins d'acheteurs.

**5. Une vente concurrente.** Exemple réel : une carte achetée 1000 et estimée 2000 est prévue à 1250. Juste avant la publication, une autre vente de la même carte est à 1100. Le bot se met 1 WB en dessous, à 1099, pour être la moins chère. Il ne descend jamais sous le prix d'achat. Une vente concurrente sous 50 % de l'estimation (ici sous 1000) est ignorée : c'est un prix de départ, pas un vrai prix.

**6. Personne n'achète.** Sans acheteur, la carte avance d'une étape toutes les 6 h en vente (`RESALE_STEP_MINUTES`).
- **Étapes 1 à 3 :** la carte est remise en vente 5 % moins cher à chaque fois (`RESALE_STEP_PRICE_RATIO`, `RESALE_MAX_STEPS=3`) : 375 → 356 → 338 → 321.
- **Baisse à mi-parcours :** avec `RESALE_MIDWAY_REPRICE=true`, une vente à 375 sans aucune mise au bout de 3 h (sur 6 h) passe à 356 sur place, sans attendre la fin. Le site ne permet qu'une baisse par vente.
- **Ensuite :** elle est remise en vente au prix d'achat + 1 (241) tant qu'une place de vente est libre. Si les 5 places sont prises, le bot la garde dans ta collection.
- **Limite de perte :** `RESALE_MAX_LOSS_RATIO=0` interdit de vendre sous le prix d'achat. Avec `RESALE_MAX_LOSS_RATIO=0.1` et `RESALE_LOSS_AFTER_STEPS=2`, le prix pourrait descendre jusqu'à 217 (241 − 10 %) à partir de la 2e étape.
- **Abandon :** `RESALE_GIVE_UP_AFTER_STEPS=4` arrête après 4 étapes. La carte reste dans ta collection avec l'étiquette `Invendue`.

**7. Le bilan.** Dans le pire cas (réglages par défaut), la carte revient dans ta collection, achetée 240. Dans le meilleur, des joueurs surenchérissent au-dessus de 375.

## Doublons

Le worker `inventory` publie les reventes du sniper et, si tu l'actives, vend tes doublons. Ces réglages ne concernent que les cartes de ta collection ; celles achetées par le sniper suivent la [Revente](#revente).

```sh
INVENTORY_ENABLED=true                  # publie les ventes programmées ; scans auto via AUTO_*
INVENTORY_INTERVAL_MINUTES=360
DUPLICATES_SELL=false                   # mise en vente automatique (sinon : CLI inventory list)
DUPLICATES_MIN_VALUE=15
DUPLICATES_KEEP_COPIES=1                # exemplaires toujours gardés par carte
DUPLICATES_LAST_COPY_RARITIES=          # vendues même en dernier exemplaire (ex. L), hors cartes achetées par le bot
DUPLICATES_PRICE_RATIO=1.0              # prix de départ = cote × multiplicateur
DUPLICATES_MAX_PER_RUN=5                # le site limite à 5 ventes simultanées
DUPLICATES_ANYTIME=true                 # doublons vendus dès qu'une place se libère (sinon : fin 20:00-22:30)
DUPLICATES_CHECK_MINUTES=10             # vérification des places libres
DUPLICATES_QUOTES_PER_RUN=60            # cotes demandées au site par passage ; le reste : ventes observées
DUPLICATES_END_WINDOW_START=20:00
DUPLICATES_END_WINDOW_END=22:30
DUPLICATES_DURATION_MINUTES=360         # durée visée (la plus proche possible finissant dans la fenêtre)
DUPLICATES_MIN_DURATION_MINUTES=60
MARKET_DURATION_OPTIONS=10,30,60,180,360,720,1440  # durées du formulaire de vente du site
```

**Vente des doublons** (`DUPLICATES_SELL=true`) : tu as 3 exemplaires d'une carte cotée 83 WB.
- **Exemplaires vendus :** le bot en garde 1 (`DUPLICATES_KEEP_COPIES`) et vend les 2 autres, une vente par place libre.
- **Prix :** 83 ≥ 15 (`DUPLICATES_MIN_VALUE`), donc la carte est vendue au prix de départ 83 × 1,0 = 83 WB (`DUPLICATES_PRICE_RATIO`). Une carte cotée 10 WB ne serait pas vendue.
- **Horaire :** avec `DUPLICATES_ANYTIME=true`, la vente part dès qu'une place se libère. Avec `false`, elle est programmée pour finir entre 20:00 et 22:30.

## Défausse

La défausse rapporte 1 WB par exemplaire. Elle ne descend jamais sous un palier de succès « taille de collection » atteint : `ACHIEVEMENTS_MILESTONES` vide, aucune défausse par prudence ; `0`, aucun palier à protéger. Le worker `value_scan` cote toute la collection, derniers exemplaires compris, pour `DISCARD_LAST_COPY_RARITIES` (aucun message).

```sh
DISCARD_ENABLED=false
DISCARD_RARITIES=C,PC
DISCARD_MAX_VALUE=2                     # cote ≤ 2 WB = sans valeur marchande
DISCARD_KEEP_COPIES=1
DISCARD_UNQUOTED=false
# Raretés défaussées même en dernier exemplaire (la carte quitte ta collection) : seulement une carte déjà cotée par
# le passage de cotes (VALUE_SCAN_*), sans aucune vente connue au-dessus de DISCARD_MAX_VALUE. Vide = jamais.
DISCARD_LAST_COPY_RARITIES=             # ex. C,PC
DISCARD_MAX_PER_RUN=250                 # exemplaires défaussés par passage (INVENTORY_INTERVAL_MINUTES) ; 0 = sans limite
ACHIEVEMENTS_MILESTONES=                # ex. 100,250,500,1000,2500,5000
ACHIEVEMENTS_COUNT_MODE=total           # total = exemplaires, unique = cartes distinctes
ACHIEVEMENTS_MARGIN=50
VALUE_SCAN_ENABLED=true
VALUE_SCAN_HOURS=12                     # un passage toutes les N heures
VALUE_SCAN_LOOKUPS=0                    # cotes demandées au site par passage (0 = toutes)
VALUE_SCAN_LOOKUP_SECONDS=3             # au mieux une cote toutes les N s, en fond (ralentit si le site refuse)
VALUE_SCAN_MAX_AGE_DAYS=7               # cote plus récente : reprise du cache, pas redemandée
VALUE_SCAN_UNSOLD_DAYS=30               # carte sans aucune vente : recotée après N jours
VALUE_SCAN_FREEZE_BELOW=50              # cotée sous N WB : cote figée pour toujours (0 = jamais)
```

**Défausse** (`DISCARD_ENABLED=true`) : la défausse rapporte 1 WB par exemplaire. Avec `DISCARD_RARITIES=C,PC` et `DISCARD_MAX_VALUE=2`, chaque doublon C ou PC coté 2 WB ou moins est défaussé. Un exemplaire de chaque carte est toujours gardé (`DISCARD_KEEP_COPIES=1`), et les moins cotés partent en premier, 250 au plus par passage (`DISCARD_MAX_PER_RUN`).

**Paliers de succès**, avec `ACHIEVEMENTS_MILESTONES=1000,2500,5000` et `ACHIEVEMENTS_MARGIN=50` :
- **2480 exemplaires :** le palier 2500 est à 20 exemplaires, moins que la marge de 50. La défausse est bloquée pour ne pas t'en éloigner.
- **2700 exemplaires :** le palier 2500 est atteint, la défausse peut retirer 200 exemplaires au plus, pour ne pas repasser sous 2500.
- **`ACHIEVEMENTS_MILESTONES=0` :** aucun palier à protéger, la défausse n'a pas de limite de ce côté (log `on ne repasse pas sous 0`).
- **Vide (par défaut) :** aucune défausse.

## Collections

Collections disponibles : `days` (les 366 jours de l'année), `elements`, `countries`, `gifs` (articles à GIF animé), ou la tienne dans [`config/collections.yaml`](../config/collections.yaml). Les noms français (`jours`, `pays`) restent acceptés. Détail : [Fonctionnement](FONCTIONNEMENT.md#collections).

```sh
COLLECTION_SETS=                        # collections actives (vide : aucune) ; ex. ma_liste (config/collections.yaml)
COLLECTION_TITLES=                      # titres exacts en plus, séparés par des virgules
COLLECTION_MAX_PRICE=10                 # mise maximale pour une carte manquante
COLLECTION_SET_MAX_PRICE=               # plafond propre à une collection, à la place du précédent ; ex. elements=100
COLLECTION_SET_QUOTE_RATIO=             # collection misée jusqu'à sa cote × N, comme une revente ; ex. countries=0.8
COLLECTION_UPGRADE=false                # miser aussi sur une rareté plus haute que la meilleure possédée
COLLECTION_WISHLIST=false               # cartes manquantes en liste de souhaits du site : il annonce leurs ventes (SUPABASE_ANON_KEY requise)
COLLECTION_WISHLIST_SECONDS=120         # lecture des annonces du site toutes les N secondes
COLLECTION_BIDS_PER_CHECK=2             # mises de collection par vérification anti-bot (~9-10 mises) ; 0 = sans limite
COLLECTION_REFRESH_MINUTES=360          # relecture de la collection
COLLECTION_SEARCH_SECONDS=120           # recherche d'une carte de collection sur le marché toutes les N s
```

**Exemple**, avec `COLLECTION_SETS=days` et `COLLECTION_MAX_PRICE=50` : il te manque la carte « 14 juillet ». Une enchère sur cette carte, à n'importe quelle rareté, arrive à sa fin avec une mise minimale de 30 WB. Le bot mise 30 et garde la carte, sans la revendre. À 60 WB, il ne mise pas.

Avec `COLLECTION_UPGRADE=true`, s'il te reste un « 14 juillet » en C, le bot mise aussi sur un exemplaire de rareté plus haute (PC, R…), dans la même limite de 50 WB.

Un plafond par collection, avec `COLLECTION_SETS=days,elements,countries`, `COLLECTION_SET_MAX_PRICE=elements=100` et `COLLECTION_SET_QUOTE_RATIO=countries=0.8` :
- « Fer » en vente à 90 WB : misé, la limite des éléments est 100 WB. Un jour reste limité par `COLLECTION_MAX_PRICE`.
- « Canada » en L, cote 1 760 WB : misé jusqu'à 1 408 WB (0,8 × 1 760). Comme pour une revente, une autre vente du Canada en L à 1 500 WB ramène la cote à 1 500, donc le plafond à 1 200.
- Un pays sans cote connue dans la rareté en vente : pas de mise.
- Les deux réglages ensemble (`COLLECTION_SET_MAX_PRICE=countries=3000`) : la cote × 0,8, au plus 3 000 WB.

Une carte de collection manquante, ou à monter en rareté, n'est jamais achetée pour la revente, même si la revente permettrait de miser plus haut.

Le site laisse passer environ 9 ou 10 mises par vérification anti-bot. Avec `COLLECTION_BIDS_PER_CHECK=2`, la collection en prend 2 au plus, et les autres restent aux reventes.

## Souhaits

Le worker `wishes` achète les cartes que tu ajoutes toi-même à ta liste de souhaits du site, jusqu'à leur cote, dans toute rareté plus haute que celle que tu possèdes. Gagnée, la carte est gardée et reçoit `TAG_WISH`.

```sh
WISH_MAX_PRICE=0                        # cartes de ta liste de souhaits achetées jusqu'à leur cote, au plus N WB ; 0 = désactivé
WISH_UNQUOTED_MAX_PRICE=0               # carte souhaitée sans cote dans cette rareté : misée jusqu'à N WB quand même ; 0 = pas de mise
WISH_CHECK_MINUTES=10                   # lecture de ta liste de souhaits
```

**Exemples**
- `WISH_MAX_PRICE=2000` : une carte de ta liste de souhaits estimée 1500 WB est achetée jusqu'à 1500 WB. Une carte estimée 3000 l'est jusqu'à 2000 WB au plus.
- `WISH_UNQUOTED_MAX_PRICE=100` : une carte souhaitée jamais vue vendue dans cette rareté, en vente à 10 WB, est misée jusqu'à 100 WB. En vente à 600 WB, elle n'est pas misée et le bot te prévient.

## Thèmes

Règles des thèmes dans [`config/themes.yaml`](../config/themes.yaml). Détail : [Fonctionnement](FONCTIONNEMENT.md#étiquettes-de-thème).

```sh
THEMES_ENABLED=false                    # thèmes sur l'exemplaire gardé, « Doublon » sur les autres
THEMES_INTERVAL_HOURS=6                 # passage complet (30 min tant qu'il reste du travail)
THEMES_MAX_PER_RUN=200                  # étiquettes posées ou retirées par passage
THEMES_LOOKUPS_PER_RUN=500              # cartes lues sur Wikidata par passage
```

## Tags

Noms des tags du site que le bot pose ou lit. Chacun est à créer une fois sur le site (`/profil tags creer` le fait pour toi) ; les noms sont comparés sans émoji ni casse : « 📅 Jour » correspond à `Jour`.

```sh
TAG_PROTECTED=                          # étiquettes du site jamais vendues ni défaussées ; ex. date,GIF
TAG_DAYS=date                           # étiquette des jours et de COLLECTION_TITLES (vide = non)
TAG_COLLECTION_MAX_PER_RUN=60
TAG_SHOWCASE=                           # étiquette des cartes en vitrine et des exemplaires gardés des collections (vide = non)
TAG_WISH=Souhait                        # étiquette posée sur la carte obtenue (à créer une fois sur le site) ; vide = aucune
TAG_UNSOLD=Invendue                     # étiquette d'une carte que le bot renonce à vendre (à créer sur le site ; vide = aucune)
TAG_MANUAL_RESALE=revente               # tes achats portant cette étiquette : revendus par le bot (vide = non)
TAG_DUPLICATE=Doublon                   # vide = pas d'étiquette de doublon (ne pas la mettre dans TAG_PROTECTED)
TAG_GIF=GIF                             # sur l'exemplaire gardé d'une carte animée, retirée ailleurs ; vide = pas d'étiquette GIF
```

**Tags protégés** : avec `TAG_PROTECTED=GIF,date`, un exemplaire portant l'étiquette « GIF » ou « date » sur le site n'est jamais vendu ni défaussé, même en doublon.

## Garde, succès, échanges, valeur

La garde relit ton profil et arrête tout le bot à la première sanction du site. Les succès débloqués sont réclamés chaque soir. Les échanges qu'on te propose sont évalués, avec une contre-offre s'ils sont défavorables.

```sh
GUARD_ENABLED=true                      # une sanction du site arrête tout le bot (SUPABASE_ANON_KEY requise)
GUARD_CHECK_MINUTES=15
GUARD_PACKS_ALERT_AT=10                 # message quand N paquets attendent d'être ouverts (lu par la garde) ; 0 = jamais
ACHIEVEMENTS_ENABLED=true               # réclame les succès débloqués (SUPABASE_ANON_KEY requise)
ACHIEVEMENTS_TIME=22:50                 # une fois par jour, avant le bilan de 23:00
TRADES_ENABLED=true                     # échanges proposés évalués sur Discord, avec une contre-offre
TRADES_CHECK_MINUTES=10
TRADES_FAIR_MARGIN_RATIO=0.1            # équitable : chaque côté vaut au moins 90 % de l'autre
ACCOUNT_VALUE_ENABLED=true
ACCOUNT_VALUE_HOURS=6
```

**Exemple** : `TRADES_FAIR_MARGIN_RATIO=0.1` : on te propose 900 WB de cartes contre 1000 WB des tiennes. 900 fait 90 % de 1000, donc l'échange est jugé équitable. À 850 contre 1000, il est jugé défavorable, et le bot propose une contre-offre sur Discord.

## Avancé

```sh
TIMEZONE=Europe/Paris
DATA_DIR=data
ENDPOINTS_FILE=config/endpoints.yaml
```

## Anciens noms

Réglages renommés le 22/11/2026, triés par nouveau nom. L'ancien nom reste lu pour l'instant : un nouveau nom présent passe toujours avant, et le bot liste au démarrage les anciens noms trouvés chez toi.

| Nouveau nom | Ancien nom |
|---|---|
| `ACCOUNT_VALUE_ENABLED` | `ENABLE_ACCOUNT_VALUE` |
| `ACHIEVEMENTS_COUNT_MODE` | `ACHIEVEMENT_COUNT_MODE` |
| `ACHIEVEMENTS_ENABLED` | `ENABLE_ACHIEVEMENTS` |
| `ACHIEVEMENTS_MARGIN` | `ACHIEVEMENT_MARGIN` |
| `ACHIEVEMENTS_MILESTONES` | `ACHIEVEMENT_MILESTONES` |
| `BACKUP_ENABLED` | `ENABLE_BACKUP` |
| `BACKUP_KEEP_COUNT` | `BACKUP_KEEP` |
| `DISCARD_ENABLED` | `AUTO_DISCARD` |
| `DISCARD_UNQUOTED` | `DISCARD_UNKNOWN_PRICE_IS_WORTHLESS` |
| `DISCORD_NOTIFY_EACH_BID` | `NOTIFY_EACH_BID` |
| `DISCORD_WEBHOOK_ACTIVITY` | `DISCORD_ACTIVITY_WEBHOOK_URL` |
| `DISCORD_WEBHOOK_ALERTS` | `DISCORD_ALERT_WEBHOOK_URL` |
| `DISCORD_WEBHOOK_MAIN` | `DISCORD_WEBHOOK_URL` |
| `DISCORD_WEBHOOK_REPORT` | `DISCORD_WEBHOOK_BILAN` |
| `DISCORD_WEBHOOK_RESALE` | `DISCORD_WEBHOOK_VENTES` |
| `DISCORD_WEBHOOK_SYSTEM` | `DISCORD_WEBHOOK_SYSTEME` |
| `DUPLICATES_ANYTIME` | `SELL_ANYTIME` |
| `DUPLICATES_CHECK_MINUTES` | `SELL_CHECK_MINUTES` |
| `DUPLICATES_DURATION_MINUTES` | `LISTING_PREFERRED_DURATION_MINUTES` |
| `DUPLICATES_END_WINDOW_END` | `LISTING_END_WINDOW_END` |
| `DUPLICATES_END_WINDOW_START` | `LISTING_END_WINDOW_START` |
| `DUPLICATES_KEEP_COPIES` | `SELL_KEEP_COPIES` |
| `DUPLICATES_LAST_COPY_RARITIES` | `SELL_LAST_COPY_RARITIES` |
| `DUPLICATES_MAX_PER_RUN` | `SELL_MAX_LISTINGS_PER_RUN` |
| `DUPLICATES_MIN_DURATION_MINUTES` | `LISTING_MIN_DURATION_MINUTES` |
| `DUPLICATES_MIN_VALUE` | `SELL_MIN_VALUE` |
| `DUPLICATES_PRICE_RATIO` | `SELL_PRICE_MULTIPLIER` |
| `DUPLICATES_QUOTES_PER_RUN` | `SELL_PRICE_LOOKUPS_PER_RUN` |
| `DUPLICATES_SELL` | `AUTO_LIST` |
| `GUARD_CHECK_MINUTES` | `PROFILE_CHECK_MINUTES` |
| `GUARD_ENABLED` | `ENABLE_PROFILE_GUARD` |
| `GUARD_PACKS_ALERT_AT` | `PACKS_ALERT_AT` |
| `HTTP_BACKOFF_MAX_SECONDS` | `BACKOFF_MAX_SECONDS` |
| `HTTP_BACKOFF_RATE_LIMIT_SECONDS` | `BACKOFF_RATE_LIMIT_BASE_SECONDS` |
| `HTTP_BACKOFF_SECONDS` | `BACKOFF_BASE_SECONDS` |
| `INVENTORY_ENABLED` | `ENABLE_INVENTORY` |
| `LOG_JOURNAL_KEEP_DAYS` | `JOURNAL_KEEP_DAYS` |
| `LOG_STATS_MINUTES` | `STATS_MINUTES` |
| `MARKET_DURATION_OPTIONS` | `LISTING_DURATION_OPTIONS` |
| `MARKET_FEE_RATIO` | `SALE_FEE_PCT` |
| `QUOTE_CACHE_MINUTES` | `PRICE_CACHE_TTL_MINUTES` |
| `QUOTE_HISTORY_FALLBACK` | `PRICE_HISTORY_FALLBACK` |
| `QUOTE_MIN_SALES` | `PRICE_MIN_SALES` |
| `QUOTE_OBSERVED_KEEP_DAYS` | `OBSERVED_KEEP_DAYS` |
| `QUOTE_RECENT_SALES` | `PRICE_RECENT_SALES` |
| `QUOTE_SHINY_MIN_SALES` | `SNIPER_SHINY_MIN_SALES` |
| `QUOTE_SITE_MARGIN_RATIO` | `PRICE_SITE_MARGIN` |
| `REALTIME_ENABLED` | `ENABLE_REALTIME` |
| `REPORT_DAILY_TIME` | `DAILY_REPORT_TIME` |
| `REPORT_RECAP_HOURS` | `RECAP_INTERVAL_HOURS` |
| `REPORT_SNIPER_HOURS` | `SNIPER_REPORT_HOURS` |
| `REPORT_SNIPER_TIME` | `SNIPER_REPORT_TIME` |
| `RESALE_AVOID_END_HOURS` | `POSITION_AVOID_END_HOURS` |
| `RESALE_CAPITAL_RATIO` | `PORTFOLIO_CAPITAL_RATIO` |
| `RESALE_DURATION_MINUTES` | `POSITION_LIST_DURATION_MINUTES` |
| `RESALE_EXPENSIVE_FROM` | `POSITION_EXPENSIVE_FROM` |
| `RESALE_EXPENSIVE_GIVE_UP_AFTER_STEPS` | `POSITION_EXPENSIVE_GIVE_UP_AFTER` |
| `RESALE_EXPENSIVE_MAX_STEPS` | `POSITION_EXPENSIVE_MAX_RELISTS` |
| `RESALE_GIVE_UP_AFTER_STEPS` | `POSITION_GIVE_UP_AFTER` |
| `RESALE_LONG_BELOW_POSITIONS` | `POSITION_LIST_LONG_BELOW` |
| `RESALE_LONG_DURATION_MINUTES` | `POSITION_LIST_LONG_MINUTES` |
| `RESALE_LOSS_AFTER_STEPS` | `POSITION_LOSS_AFTER` |
| `RESALE_MAX_LOSS_RATIO` | `POSITION_MAX_LOSS` |
| `RESALE_MAX_POSITIONS` | `PORTFOLIO_MAX_POSITIONS` |
| `RESALE_MAX_STEPS` | `POSITION_MAX_RELISTS` |
| `RESALE_MIDWAY_REPRICE` | `POSITION_REPRICE` |
| `RESALE_MIN_PROFIT` | `PORTFOLIO_MIN_PROFIT` |
| `RESALE_MIN_PROFIT_RATIO` | `PORTFOLIO_MIN_PROFIT_RATIO` |
| `RESALE_POST_END` | `POSITION_POST_END` |
| `RESALE_POST_NOW` | `POSITION_POST_NOW` |
| `RESALE_POST_START` | `POSITION_POST_START` |
| `RESALE_QUEUE_PROFIT_RATIO` | `PORTFOLIO_QUEUE_PROFIT_RATIO` |
| `RESALE_QUEUE_SIZE` | `PORTFOLIO_BUFFER` |
| `RESALE_SLOTS` | `PORTFOLIO_SLOTS` |
| `RESALE_STALE_HOURS` | `POSITION_STALE_HOURS` |
| `RESALE_START_MARGIN_RATIO` | `POSITION_LIST_MARGIN` |
| `RESALE_START_PROFIT_RATIO` | `POSITION_LIST_PROFIT_SHARE` |
| `RESALE_STEP_MINUTES` | `POSITION_STEP_MINUTES` |
| `RESALE_STEP_PRICE_RATIO` | `POSITION_RELIST_DISCOUNT` |
| `RESALE_WAIT_SLOT_FROM` | `POSITION_WAIT_SLOT_FROM` |
| `SESSION_CHECK_MINUTES` | `SESSION_RECHECK_MINUTES` |
| `SNIPER_BALANCE_RESERVE` | `SNIPER_MIN_BALANCE_RESERVE` |
| `SNIPER_BID_PUSH_CONTROL_RATIO` | `SNIPER_BID_PUSH_CONTROL_SHARE` |
| `SNIPER_BID_PUSH_RATIO` | `SNIPER_BID_PUSH` |
| `SNIPER_ENABLED` | `ENABLE_SNIPER` |
| `SNIPER_MAX_DEAL_RATIO` | `SNIPER_DEAL_RATIO_MAX` |
| `SNIPER_MAX_SPREAD_RATIO` | `SNIPER_MAX_PRICE_SPREAD` |
| `SNIPER_MIN_BID_RATIO` | `SNIPER_MIN_BID_SHARE` |
| `SNIPER_NIGHT_BUDGET_BONUS` | `NIGHT_BUDGET_BONUS` |
| `SNIPER_NIGHT_END` | `NIGHT_END` |
| `SNIPER_NIGHT_MAX_DEAL_RATIO` | `NIGHT_DEAL_RATIO_MAX` |
| `SNIPER_NIGHT_MAX_RISE` | `NIGHT_MAX_RISE` |
| `SNIPER_NIGHT_MIN_BID_VALUE` | `NIGHT_MIN_BID_VALUE` |
| `SNIPER_NIGHT_START` | `NIGHT_START` |
| `SNIPER_NIGHT_WINDOW_SECONDS` | `NIGHT_WINDOW_SECONDS` |
| `SNIPER_SETTLE_DELAY_SECONDS` | `SNIPER_SETTLE_GRACE_SECONDS` |
| `SNIPER_SETTLE_STUCK` | `SETTLE_STUCK_AUCTIONS` |
| `TAG_COLLECTION_MAX_PER_RUN` | `COLLECTION_TAG_MAX_PER_RUN` |
| `TAG_DAYS` | `COLLECTION_TAG` |
| `TAG_DUPLICATE` | `THEME_DUPLICATE_TAG` |
| `TAG_GIF` | `THEME_GIF_TAG` |
| `TAG_MANUAL_RESALE` | `MANUAL_RESALE_TAG` |
| `TAG_PROTECTED` | `PROTECTED_TAGS` |
| `TAG_SHOWCASE` | `SHOWCASE_TAG` |
| `TAG_UNSOLD` | `POSITION_UNSOLD_TAG` |
| `TAG_WISH` | `WISH_TAG` |
| `THEMES_ENABLED` | `ENABLE_THEME_TAGS` |
| `THEMES_INTERVAL_HOURS` | `THEME_TAGS_HOURS` |
| `THEMES_LOOKUPS_PER_RUN` | `THEME_LOOKUPS_PER_RUN` |
| `THEMES_MAX_PER_RUN` | `THEME_TAGS_MAX_PER_RUN` |
| `TRADES_CHECK_MINUTES` | `TRADE_CHECK_MINUTES` |
| `TRADES_ENABLED` | `ENABLE_TRADE_WATCH` |
| `TRADES_FAIR_MARGIN_RATIO` | `TRADE_FAIR_MARGIN` |
| `VALUE_SCAN_ENABLED` | `ENABLE_VALUE_SCAN` |

Dans les valeurs de `LIVE_WRITES`, `SIMULATE` et `HTTP_WORKER_SPACING_SECONDS`, les anciens noms de workers et de groupes sont remplacés de la même façon : `garde` → `guard`, `echanges` → `trades`, `valeur` → `account_value`, `etiquettes` → `themes`, `succes` → `achievements`, `cotes` → `value_scan`, `souhaits` → `wishes`, `ventes` → `resale`, `reglements` → `settle`, `vitrines` → `showcases`, `defausse` → `discard`.
