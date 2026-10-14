# WikiMastered

Bot headless (Docker) d'automatisation et d'analyse de marché pour [WikiMasters](https://www.wiki-masters.com).
Il parle directement aux routes JSON du site, sans navigateur.

| Module | Fichier | Rôle |
|---|---|---|
| A | `src/core/client.py` | Session, espacement + jitter, backoff 429/5xx, pause globale sur 401/403 |
| B | `src/workers/pack_opener.py` | Ouvre les paquets gratuits, alerte Discord sur UR / L / Shiny |
| C | `src/workers/inventory_manager.py` | Cotes de la collection, ventes aux heures de pointe, défausse protégée |
| D | `src/workers/auction_sniper.py` | Sniper orienté capital : achète gros pour revendre, arbitrage nocturne |
| E | `src/workers/wiki_trends.py` | Pics de vues Wikipédia (API Wikimedia) croisés avec le marché |
| F | `src/workers/google_trends.py` | Recherches Google en hausse en France (flux RSS), croisées avec le marché |
| G | `src/workers/collection.py` | Compléter une collection (jours de l'année…) : mises sur les cartes manquantes |

> **À lire avant de lancer**
> - Automatiser un compte peut être contraire aux conditions d'utilisation de WikiMasters et mener à un bannissement. Le jitter et l'espacement limitent la charge sur le site ; ils ne garantissent rien.
> - `DRY_RUN=true` par défaut : aucune enchère, vente, défausse ni ouverture n'est envoyée. Les actions sont seulement journalisées et annoncées sur Discord avec le préfixe `[DRY RUN]`. On peut passer en réel worker par worker : `DRY_RUN=false` avec `SIMULATE=sniper` rend tout réel sauf les mises des positions, et `DRY_RUN=true` avec `LIVE_WRITES=packs` rend seulement les paquets réels.
> - Quand le site demande une **vérification humaine** pour les paquets, le bot s'arrête et te prévient. Il ne tente jamais de la contourner : fais-la toi-même dans ton navigateur.

## D'où viennent les routes

Aucune route n'est inventée. Toutes viennent du code des extensions communautaires (relevé le 14/10/2026) et sont décrites dans [config/endpoints.yaml](config/endpoints.yaml) :

- [qkerman/WikiMastersTools-kzfamily](https://github.com/qkerman/WikiMastersTools-kzfamily) : cotes (`/api/marketplace/cards/{id}/sales?scope=summary`), mise en vente, ouverture de paquets, cookie Supabase
- [Lypningeuh/WikiRemastered](https://github.com/Lypningeuh/WikiRemastered) : marché `sort=ending_soon`, règle de mise minimale, schéma des enchères, défausse groupée, garde-fous réseau
- [ThePommespanzer/wikimasters-plus](https://github.com/ThePommespanzer/wikimasters-plus) : régénération des paquets

Ce qui reste **inconnu**, donc configurable plutôt que deviné :

- **Durées de vente acceptées par l'API.** Le bot n'utilise que celles du formulaire du site (`LISTING_DURATION_OPTIONS` : 10, 30, 60, 180, 360, 720, 1440 min). Aucune vente réelle n'a encore confirmé que l'API accepte les plus longues.
- **Route de statut des paquets.** Le site lit Supabase `profiles`. Sans cette route, le stock est déduit des réponses d'ouverture. Tu peux en déclarer une sous le nom `pack_status`.
- **Paliers de succès de collection** (`ACHIEVEMENT_MILESTONES`) et s'ils comptent les exemplaires ou les cartes distinctes. Sans eux, aucune défausse n'est faite.
- **Module E.** Les exemplaires gardent une `snapshot_rarity` : une carte achetée Commune pourrait rester Commune après la resynchronisation du jeu. C'est pourquoi le module se contente d'alertes par défaut (`TRENDS_AUTO_BUY=false`).

## Installation (serveur Debian)

```bash
git clone <ce dépôt> wikimastered && cd wikimastered
cp .env.example .env        # remplir (voir « Variables à remplir » ci-dessous)
docker compose up -d --build
docker compose logs -f
```

Vérifications :

```bash
docker compose exec wikimastered python -m src.main check     # session + solde + joueur
docker compose exec wikimastered python -m src.main inspect   # compare les vraies réponses aux modèles
```

`inspect` est en lecture seule. Il enregistre les réponses brutes dans `data/inspect/` et signale tout champ qui ne correspond pas à [src/core/models.py](src/core/models.py).

**Portainer :** utilise [portainer-stack.yml](portainer-stack.yml) (variables saisies dans Portainer, volume nommé) et suis le guide pas à pas [docs/PORTAINER.md](docs/PORTAINER.md). Avec `docker compose` en ligne de commande, le volume `./data` doit appartenir à l'UID 1000 (`sudo chown -R 1000:1000 data`).

## Variables à remplir

| Variable | Où la trouver |
|---|---|
| `WM_COOKIE` | DevTools (F12) > Réseau, connecté sur wiki-masters.com > une requête `/api/wikibidous` > En-têtes de requête > `cookie`. Copie la valeur complète entre apostrophes. Elle doit contenir `sb-…-auth-token` (parfois en `.0`, `.1`). |
| `WM_USER_AGENT` | Même requête, en-tête `user-agent` |
| `WM_USER_ID` | Laisser vide : il est lu dans le cookie et affiché par `check`. Il sert à ne jamais miser sur tes propres ventes. |
| `DISCORD_WEBHOOK_URL` | Salon Discord > Paramètres > Intégrations > Webhooks |
| `WIKIMEDIA_USER_AGENT` | Ton contact (URL ou e-mail), exigé par la politique de l'API Wikimedia |
| `DRY_RUN` | Laisse `true` tant que tu n'as pas validé les alertes `[DRY RUN]` |

Optionnels mais conseillés : `ACHIEVEMENT_MILESTONES` (pour pouvoir défausser), `DISCORD_ALERT_WEBHOOK_URL` et `DISCORD_MENTION`. Laisse `SNIPER_RARITY_CAPS` vide (le prix suit les tendances, pas la rareté). `SNIPER_DAILY_BUDGET` doit rester à 0 ou dépasser largement le budget d'une position (~540 WB), sinon il bloque les achats. La liste complète, commentée, est dans [.env.example](.env.example).

Plus rapide : dans DevTools, clic droit sur une requête `/api/…` > *Copier* > *Copier comme cURL (bash)*, puis :

```bash
python -m src.tools.parse_curl --env < requete.curl   # remplit WM_COOKIE, WM_USER_AGENT, WM_EXTRA_HEADERS
```

## Renouveler la session sans redémarrer

Sur 401/403, tous les workers se figent et une alerte critique part sur Discord. Pour repartir :

- colle le nouveau cookie dans `data/session_cookie.txt` : il est chargé en moins de 30 s ;
- ou `python -m src.tools.parse_curl --hot < requete.curl` ;
- ou, depuis un poste avec écran : `python -m src.tools.browser_login` (Playwright, connexion manuelle), puis copie `data/session_cookie.txt` sur le serveur.

Les cookies rafraîchis par le serveur (`Set-Cookie`) sont conservés dans `data/session.json`. Ce fichier est ignoré dès que `WM_COOKIE` change dans `.env`.

**Session autonome.** Avec `SUPABASE_ANON_KEY`, le bot renouvelle lui-même sa session Supabase un peu avant l'expiration du jeton d'accès, ou sur un 401. Il passe par l'API standard de Supabase (`/auth/v1/token`), la même que ton navigateur. La nouvelle session est écrite dans `data/session.json`. Une commande lancée dans la console reprend cette session, et le bot reprend celle qu'une commande aurait renouvelée : les deux processus ne se gênent pas. Une écriture refusée faute de session n'est jamais rejouée : le worker refait sa décision au passage suivant. Le bot doit avoir **sa propre session** : le jeton de renouvellement ne sert qu'une fois, et un navigateur qui partage la même session finirait par l'invalider. Marche à suivre : [docs/PORTAINER.md](docs/PORTAINER.md#session-autonome-recommandé).

## Corriger ou ajouter une route

```bash
python -m src.tools.parse_curl --name place_bid --write < requete.curl
```

La route est écrite dans `config/endpoints.local.yaml`, qui surcharge `endpoints.yaml`. Les UUID sont remplacés par les placeholders attendus par le code (ici `{auction_id}`). Pour découvrir une route inconnue : `python -m src.tools.browser_login --record`, puis navigue sur le site. Chaque appel `/api/` est enregistré dans `data/inspect/capture/`.

## Commandes

```bash
python -m src.main run                          # le bot (défaut du conteneur)
python -m src.main check                        # session, solde, nombre d'exemplaires
python -m src.main inspect                      # cartographie des réponses JSON
python -m src.main inventory scan               # ce qui serait vendu / défaussé, et pourquoi
python -m src.main inventory list --apply       # programme les ventes (fin 20:00-22:30)
python -m src.main inventory discard --apply    # défausse, après vérification des paliers
python -m src.main report                       # bilan du jour sur Discord
python -m src.main trends --days 7              # mesure des signaux de tendance
python -m src.main packs                        # paquets ouverts ces dernières 24 h
python -m src.main collection                   # collection à compléter : possédées / manquantes
```

Dans Docker, préfixe par `docker compose exec wikimastered`. Les ventes programmées sont publiées par le bot en marche, à l'heure prévue.

## Comportement détaillé

**Réseau (A).** Une seule requête à la fois, espacées de 1,5 s plus un aléa. Sur 429, 5xx ou une page HTML de passerelle, une pause globale exponentielle s'applique (30 s / 60 s → 15 min max, `Retry-After` respecté). Un 403 n'est traité comme une perte de session que si une sonde `/api/wikibidous` échoue aussi ; sinon c'est une route refusée (par exemple réservée au PRO). Une écriture n'est **jamais rejouée** : si la réponse est perdue ou inattendue, l'action est marquée « incertaine » et une alerte part. Le bot relit ensuite le résultat sur le site (l'enchère elle-même, ou tes ventes en cours) et envoie « Mise incertaine vérifiée » ou « Mise en vente vérifiée ». Une position dont la mise en vente est refusée 3 fois est gardée (« Revente abandonnée »).

**Paquets (B).** Le bot ouvre ses paquets à un rythme humain : une session toutes les 15 à 90 min, tirées au hasard (`PACK_INTERVAL_MIN_MINUTES`, `PACK_INTERVAL_MAX_MINUTES`), et des paquets espacés de 5 à 15 s. Le site semble demander une vérification humaine quand les actions s'enchaînent trop vite. Le stock se recharge d'un paquet toutes les 10 min, jusqu'à 10 : en 90 min, aucun paquet n'est perdu. En DRY_RUN, les paquets ne sont pas ouverts, sauf avec `LIVE_WRITES=open_pack` : seule cette écriture devient alors réelle. Chaque session d'ouverture envoie un court message Discord, « Boosters ouverts » (nombre de boosters et de cartes, stock restant). Il devient « Boosters ouverts : tirage remarquable ! », avec le détail, s'il y a une UR, une L ou une Shiny. Le bilan du soir donne le nombre de boosters ouverts et les tirages par rareté. `python -m src.main packs` liste les cartes tirées sur 24 h. Le stock est déduit de `packs_remaining` et `packs_last_regen_at`. La limite quotidienne met en pause jusqu'au lendemain. Chaque carte est enregistrée en base.

**Sniper (D).** Le prix d'une carte suit surtout les tendances (actu, pop culture, mèmes), pas sa rareté. Et le site limite à **5 ventes simultanées** (les mises, elles, sont illimitées) : chaque carte achetée doit attendre une de ces 5 places pour être revendue. Le sniper raisonne donc en **capital** : il achète peu, mais gros, et uniquement pour revendre.
- **Estimation** : le sniper observe toutes les fins d'enchères. Le dernier prix vu avant la fin d'une enchère est enregistré comme une vente observée. L'estimation est la médiane des 5 dernières ventes observées de la carte, plafonnée par la moyenne du site, qui peut être très datée (GMMTV : vendue 157, moyenne affichée 391). L'historique détaillé du site est réservé au PRO (403). Le bot n'achète qu'une carte vue vendue au moins `PRICE_MIN_SALES` fois : il commence donc par plusieurs jours d'observation.
- **Budget d'une position** : (WikiBidous + capital déjà engagé) × `PORTFOLIO_CAPITAL_RATIO` / `PORTFOLIO_SLOTS`, soit environ 540 WB pour 3 000 WB.
- **Condition d'achat** : bénéfice attendu (estimation − mise) ≥ 25 % de ce budget, et mise ≤ 70 % de l'estimation.
- **File d'attente** : une position qui trouve une place de vente libre (5 moins tes ventes manuelles) exige le bénéfice normal. Au-delà, elle attend son tour, jusqu'à `PORTFOLIO_BUFFER` (10) positions en attente. Comme son capital reste immobilisé, elle doit rapporter au moins 50 % du budget (`PORTFOLIO_QUEUE_PROFIT_RATIO`), soit environ 270 WB. Le tout est plafonné à `PORTFOLIO_MAX_POSITIONS` (15).
- **Tes collections** : le bot ignore toute carte sur laquelle tu mises toi-même, y compris ses autres ventes (`SNIPER_AVOID_MY_BIDS`). Les cartes que tu gagnes à la main ne sont jamais revendues par le bot.
- **Mise** : toujours le minimum natif (`max(ceil(1,1 × mise), mise + 1)`). Les places libres vont aux plus gros bénéfices attendus.
- **Moment de la mise** : entre 20 et 11 s avant la fin (`SNIPER_WINDOW_SECONDS`, `SNIPER_MIN_SECONDS_LEFT`), de jour comme de nuit. Le site prolonge de 60 s une enchère disputée dans ses 10 dernières secondes : le bot n'y mise jamais. Si quelqu'un d'autre la prolonge, l'enchère repasse dans la fenêtre et le bot peut miser à nouveau, sans dépasser son plafond.
- **Nuit** (02:00–07:30) : mise jusqu'à 80 % de l'estimation au lieu de 70 %.

**Ventes.** Le site accepte des durées de 10 min, 30 min, 1 h, 3 h, 6 h, 12 h ou 24 h. Le bot choisit celle qui se rapproche le plus de 6 h (`LISTING_PREFERRED_DURATION_MINUTES`) tout en finissant entre 20:00 et 22:30 : il publie vers 14 h-16 h 30 pour une fin à l'heure de pointe. Si aucune place n'est libre au moment de publier, la vente est reportée au lendemain. Avec une fin à l'heure de pointe, chaque place sert une fois par jour ; `POSITION_LIST_AT_PEAK=false` vend plus vite (une place toutes les 6 h environ), mais hors des heures de pointe.

**Positions.** Chaque carte gagnée est remise en vente en priorité, avant tes doublons, au prix estimé. Si elle ne se vend pas, elle est remise en vente à −10 % (deux fois au plus), puis gardée. Bénéfice et perte apparaissent dans le bilan quotidien. En DRY_RUN, gains et reventes sont simulés. Une revente simulée n'aboutit que si la médiane des ventes observées de la carte, sur les 3 derniers jours, atteint le prix de départ ; sinon, elle suit les mêmes remises qu'en réel. Le bilan quotidien contient alors un bloc « Validation de la simulation » : marché observé, reventes, invendues comptées comme perdues, et un avis sur le passage en réel.

**Notifications.** Discord ne reçoit pas un message par enchère, mais un résumé toutes les `NOTIFY_DIGEST_MINUTES` (60 par défaut). Arrivent immédiatement : les achats et reventes de positions, les tirages remarquables, les alertes et le bilan quotidien. `NOTIFY_EACH_BID=true` rétablit un message par enchère.

**Inventaire (C).** Le site limite à **5 ventes simultanées** (`maxConcurrentAuctions`) : le bot ne programme que les places libres et reporte au lendemain une vente qui n'a plus de place. Avec `AUTO_LIST=true`, le bot vérifie toutes les 10 min s'il reste une place de vente libre (tes ventes manuelles comptent) et y met le doublon le mieux coté, à partir de `SELL_MIN_VALUE` (15 WB). Avec `SELL_ANYTIME=true` (par défaut), la vente part dans la demi-heure pour 6 h : une place peut ainsi servir plusieurs fois par jour. Une vente qui finirait en pleine nuit vise plutôt 20:00–22:30. La liste des doublons à vendre est gardée en mémoire et relue toutes les 6 h. Les ventes de doublons ne donnent pas de message Discord : le bilan du soir les compte (`doublons_vendus`, `doublons_recette`). Les positions du sniper restent prioritaires. Le premier exemplaire de chaque carte est toujours gardé. La route des cotes étant limitée par le site, le bot demande au plus `SELL_PRICE_LOOKUPS_PER_RUN` (60) cotes par passage, en commençant par les cartes aux plus nombreux doublons, et garde chaque cote 3 jours. Pour les autres cartes, il se sert des ventes qu'il a observées. Le classement s'affine ainsi d'un passage à l'autre. Ne sont jamais vendus ni défaussés : les exemplaires portant une étiquette de `PROTECTED_TAGS`, les favoris, les shiny, les exemplaires engagés dans un échange, ceux déjà en vente, et le dernier exemplaire de chaque carte (`SELL_KEEP_COPIES`, `DISCARD_KEEP_COPIES`). La défausse passe par lots de 50. Elle est bloquée si un palier est à moins de `ACHIEVEMENT_MARGIN` cartes, et ne fait jamais repasser la collection sous un palier déjà atteint.

**Tendances (E).** Le module prend le top fr.wikipedia de la veille : les données du jour ne sont publiées qu'avec environ un jour de retard. Un pic correspond à des vues ≥ 3 × la médiane des 30 jours précédents. Le prix d'une carte suit les tendances : si la carte d'un article en pic est en vente sous le budget d'une position (`TRENDS_MAX_PRICE`, 0 = ce budget), elle est signalée avec sa mise minimale et son estimation (ventes observées). Un seul message Discord par passage horaire, chaque vente n'est signalée qu'une fois. La rareté probable (projection des vues sur 30 jours, Légendaire à 20 000 vues) n'est plus qu'une information.

**Google Trends (F).** Le flux RSS public de Google Trends France (`trends.google.com/trending/rss?geo=FR`) donne les 10 dernières recherches en forte hausse, le jour même, au lieu de la veille pour Wikipédia. Il est lu toutes les 20 min. Chaque nouveau terme (une fois par 24 h) est cherché au marché : même titre, ou titre contenu dans le terme, sur 2 mots au moins. Les cartes sous le budget d'une position sont signalées sur Discord, en un message par passage. Toutes les cartes trouvées, Wikipédia comprise, sont enregistrées comme signaux et mesurées chaque dimanche dans le bilan (voir « Mesure des tendances »).

**Collection (G).** Avec `COLLECTION_SETS=jours`, le bot complète ta collection des 366 jours de l'année. Toutes les 6 h, il cherche dans ta collection les cartes de chaque mois (12 requêtes) et note celles qui te manquent. Le sniper mise alors sur une carte manquante en vente, jusqu'à `COLLECTION_MAX_PRICE` (50 WB), dans sa fenêtre habituelle de 20 à 11 s avant la fin. Il ne mise jamais sur deux ventes de la même carte à la fois, ni sur une carte que tu cherches déjà toi-même. Une carte gagnée est gardée, jamais revendue ni défaussée, et Discord affiche « Collection : carte obtenue ». `COLLECTION_TITLES` ajoute d'autres titres exacts. Le bilan quotidien indique l'avancement (possédées / 366 et les premières manquantes) ; `python -m src.main collection` donne la liste complète. Le format des titres (« 1er janvier », « 14 octobre ») n'est pas encore vérifié sur les cartes du site : la commande signale les titres trouvés mais non reconnus.

**Mesure des tendances.** Chaque dimanche, le bilan indique, pour Google Trends et pour Wikipédia, si les signaux de la semaine auraient rapporté. Le calcul suppose un achat en surenchérissant sur le prix final observé de la vente signalée, puis une revente à la médiane des ventes de la même carte observées dans les 7 jours suivants. Un signal est « gagnant » si le bénéfice atteint celui exigé d'une position (~147 WB). Avec au moins 10 signaux mesurés, le bilan conclut « prometteur » ou « pas rentable ». Le même calcul est disponible à tout moment avec `python -m src.main trends`. Pour comparer les deux sources dans la durée, le bilan de chaque soir contient une ligne « Tendances du jour » : termes Google, cartes trouvées par chaque source, cartes communes aux deux. La mesure du dimanche ajoute un tableau jour par jour : cartes, signaux, mesurés, bénéfice médian, gagnants. Sur Discord, une carte en vente plusieurs fois n'occupe qu'une ligne : nombre de ventes, fourchette des mises, première fin.

## Données

`data/` (volume) contient `wikimastered.db` (SQLite : historique des cotes, tirages, enchères, ventes, défausses), les logs dans `logs/bot.log` (rotation), la session et le `heartbeat` utilisé par le healthcheck Docker. Tout ce répertoire reste hors Git. Après le bilan quotidien, le bot purge ce qui ne sert plus : historique des cotes de plus de 60 jours, ventes observées de plus de 120 jours, vues Wikipédia de plus de 60 jours. Enchères, ventes, tirages et défausses sont gardés.

## Développement

```bash
pip install -r requirements-tools.txt
pytest
```

Les tests couvrent la logique de décision et le client face à un faux serveur (429, 401 puis reprise, 403, DRY_RUN, sniper, paquets). Aucun appel réseau réel n'est fait.
