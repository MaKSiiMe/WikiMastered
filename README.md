# WikiMastered

Bot headless (Docker) d'automatisation et d'analyse de marché pour [WikiMasters](https://www.wiki-masters.com).
Il parle directement aux routes JSON du site, sans navigateur, et rend compte sur Discord.

| Worker | Fichier | Rôle | Étiquette Discord |
|---|---|---|---|
| Client | `src/core/client.py` | Session, espacement des requêtes, pauses sur 429/5xx, renouvellement de session | ⚙️ Système |
| Paquets | `src/workers/pack_opener.py` | Ouvre les paquets gratuits à un rythme humain | 🎴 Paquets |
| Sniper | `src/workers/auction_sniper.py` | Achète peu mais gros, uniquement pour revendre | 🎯 Sniper |
| Inventaire | `src/workers/inventory_manager.py` | Revend les positions et les doublons, défausse protégée | 💰 Ventes |
| Collection | `src/workers/collection.py` | Complète la collection des 366 jours, pose l'étiquette « date » | 📅 Collection |
| Tendances | `src/workers/wiki_trends.py`, `google_trends.py` | Pics Wikipédia et recherches Google croisés avec le marché | 📈 Tendances |

> **À lire avant de lancer**
> - Automatiser un compte peut être contraire aux conditions d'utilisation de WikiMasters et mener à un bannissement. L'espacement des requêtes limite la charge sur le site ; il ne garantit rien.
> - `DRY_RUN=true` par défaut : aucune enchère, vente, défausse ni ouverture n'est envoyée. Les actions sont journalisées et annoncées sur Discord avec le préfixe `[DRY RUN]`. Le passage en réel se fait worker par worker (voir [Réel ou simulé](#réel-ou-simulé)).
> - Quand le site demande une **vérification humaine** pour les paquets, le bot s'arrête et te prévient. Il ne la contourne jamais : fais-la toi-même dans ton navigateur.

## Installer

Le bot tourne comme une stack Portainer construite depuis ce dépôt : suis [docs/PORTAINER.md](docs/PORTAINER.md). Il se met ensuite à jour tout seul à chaque fusion sur `main` (voir [Développement](#développement)).

Sans Portainer, sur un serveur avec Docker :

```bash
git clone <ce dépôt> wikimastered && cd wikimastered
cp .env.example .env                       # remplir, voir ci-dessous
sudo chown -R 1000:1000 data               # le conteneur tourne avec l'UID 1000
docker compose up -d --build
docker compose exec wikimastered python -m src.main check
```

### Variables obligatoires

| Variable | Où la trouver |
|---|---|
| `WM_COOKIE` | DevTools (F12) > *Réseau*, connecté sur wiki-masters.com > une requête `/api/wikibidous` > *En-têtes de requête* > `cookie`. Valeur complète ; elle contient `sb-…-auth-token` (parfois en `.0`, `.1`). |
| `WM_USER_AGENT` | Même requête, en-tête `user-agent`. |
| `DISCORD_WEBHOOK_URL` | Salon Discord > *Modifier le salon* > *Intégrations* > *Webhooks*. |
| `WIKIMEDIA_USER_AGENT` | Ton contact (URL ou e-mail), exigé par l'API Wikimedia. |

Conseillées : `SUPABASE_ANON_KEY` (le bot renouvelle lui-même sa session, voir [docs/PORTAINER.md](docs/PORTAINER.md)), `DISCORD_ALERT_WEBHOOK_URL` et `DISCORD_MENTION` (alertes critiques), `ACHIEVEMENT_MILESTONES` (sans eux, aucune défausse). Toutes les variables sont commentées dans [.env.example](.env.example).

Raccourci : dans DevTools, clic droit sur une requête `/api/…` > *Copier comme cURL (bash)*, puis `python -m src.tools.parse_curl --env < requete.curl` remplit `WM_COOKIE`, `WM_USER_AGENT` et `WM_EXTRA_HEADERS`.

### Réel ou simulé

| Réglage | Effet |
|---|---|
| `DRY_RUN=true` | Tout est simulé. |
| `DRY_RUN=true` + `LIVE_WRITES=packs` | Seules les écritures listées sont réelles. |
| `DRY_RUN=false` + `SIMULATE=sniper` | Tout est réel sauf les mises des positions du sniper. |
| `DRY_RUN=false` | Tout est réel. |

Groupes acceptés : `sniper`, `collection`, `packs`, `ventes`, `defausse`, `tags`, `inventory` (= ventes + défausse). Tant que le sniper est simulé, le bilan du soir contient un bloc « Validation de la simulation » qui dit quand le passage en réel est envisageable.

## Discord

Tout arrive dans un seul salon. Chaque message porte en tête l'étiquette du worker qui l'envoie (tableau du haut), et en pied le mode du bot (par exemple « Mode RÉEL (simulé : sniper) »).

La couleur indique l'importance :

| Couleur | Sens | Exemples |
|---|---|---|
| 🔵 Bleu | Information | Bot démarré, boosters ouverts, résumé des enchères, bilan du soir |
| 🟢 Vert | Bonne nouvelle | Position achetée, revente réussie, tirage UR / L / Shiny, carte de collection obtenue |
| 🟠 Orange | À surveiller | Position invendue, mise en vente refusée, erreurs répétées, bot arrêté |
| 🔴 Rouge | Il faut agir | Session expirée, vérification humaine des paquets, enchère non confirmée |

Les messages rouges partent dans `DISCORD_ALERT_WEBHOOK_URL` s'il est défini, avec la mention `DISCORD_MENTION`.

**Ce qui n'envoie pas de message à chaque fois :**
- les enchères : un résumé toutes les `NOTIFY_DIGEST_MINUTES` (60) ; `NOTIFY_EACH_BID=true` envoie un message par enchère ;
- les ventes de doublons : comptées dans le bilan du soir ;
- les tendances : enregistrées et mesurées, sans message ; `TRENDS_NOTIFY=true` rétablit un message par passage.

**Le bilan du soir** (`DAILY_REPORT_TIME`, 23:00) donne le solde puis un bloc par activité : Paquets, Sniper, Ventes, Positions, Validation de la simulation, Collection, Tendances du jour. Le dimanche, il ajoute « Tendances de la semaine ».

Les tirages remarquables, positions achetées et cartes de collection obtenues montrent l'image de la carte (si le site la fournit) et un lien vers son article Wikipédia.

## Commandes

```bash
python -m src.main run                          # le bot (commande par défaut du conteneur)
python -m src.main check                        # session, solde, renouvellement automatique
python -m src.main inspect                      # compare les vraies réponses aux modèles (lecture seule)
python -m src.main inventory scan               # ce qui serait vendu / défaussé, et pourquoi
python -m src.main inventory list --apply       # programme les ventes
python -m src.main inventory discard --apply    # défausse, après vérification des paliers
python -m src.main report                       # envoie le bilan du jour sur Discord
python -m src.main trends --days 7              # les tendances auraient-elles rapporté ? (détail)
python -m src.main packs                        # paquets ouverts ces dernières 24 h
python -m src.main collection                   # collection à compléter : possédées / manquantes
```

Dans Portainer : *Containers* > `wikimastered` > *Console*. Avec Docker Compose : préfixe par `docker compose exec wikimastered`.

## Fonctionnement

**Réseau.** Une requête à la fois, espacées de 1,5 s plus un aléa. Sur 429, 5xx ou page de passerelle : pause globale exponentielle (jusqu'à 15 min, `Retry-After` respecté). Un 403 n'est une perte de session que si une sonde `/api/wikibidous` échoue aussi ; sinon c'est une route refusée (réservée au PRO, par exemple). Une écriture n'est **jamais rejouée** : si sa réponse est perdue, elle est marquée « incertaine », puis le bot relit le résultat sur le site et envoie « … vérifiée ».

**Session.** Sur 401/403, tous les workers se figent et une alerte rouge part. Avec `SUPABASE_ANON_KEY`, le bot renouvelle lui-même sa session avant l'expiration du jeton, comme ton navigateur ; il lui faut alors **sa propre session** (le jeton de renouvellement ne sert qu'une fois). Sinon, un nouveau cookie collé dans `data/session_cookie.txt` est chargé en moins de 30 s, sans redémarrage. Le bot ne se connecte jamais tout seul : le formulaire du site est protégé par Cloudflare Turnstile.

**Paquets.** Une session d'ouverture toutes les 15 à 90 min (au hasard), paquets espacés de 5 à 15 s : le site semble demander une vérification humaine quand les actions s'enchaînent trop vite. Le stock se recharge d'un paquet toutes les 10 min, jusqu'à 10 : rien n'est perdu. La limite quotidienne met en pause jusqu'au lendemain.

**Sniper.** Le prix d'une carte suit surtout les tendances (actu, pop culture, mèmes), pas sa rareté. Et le site limite à **5 ventes simultanées** (les mises, elles, sont illimitées). Le sniper raisonne donc en capital : chaque achat est une « position » qui attendra une de ces 5 places pour être revendue.
- **Estimation** : médiane des 5 dernières ventes que le bot a lui-même observées (il voit toutes les fins d'enchères), plafonnée par la moyenne du site, qui peut être très datée. Une carte doit avoir été vue vendue au moins `PRICE_MIN_SALES` (2) fois.
- **Budget d'une position** : capital × `PORTFOLIO_CAPITAL_RATIO` (0,9) / 5, soit ~540 WB pour 3 000 WB.
- **Achat** si le bénéfice attendu atteint 25 % de ce budget (~135 WB) et si la mise reste sous 70 % de l'estimation (80 % la nuit, 02:00–07:30). Au-delà des places de vente libres, une position attend son tour (10 au plus, 15 positions en tout) et doit rapporter 50 % du budget.
- **Mise** : toujours le minimum du site (`max(⌈1,1 × mise⌉, mise + 1)`), entre 20 et 11 s avant la fin. Le site prolonge de 60 s une enchère disputée dans ses 10 dernières secondes : le bot n'y mise jamais.
- **Tes enchères** : le bot ignore toute carte sur laquelle tu mises toi-même (`SNIPER_AVOID_MY_BIDS`) et ne revend jamais une carte gagnée à la main.

**Positions.** Une carte gagnée est remise en vente en priorité, au prix estimé, avec une fin entre 20:00 et 22:30 (`POSITION_LIST_AT_PEAK`). Invendue, elle repart à −10 % (deux fois au plus), puis elle est gardée. En simulation, une revente n'aboutit que si les ventes observées de la carte sur 3 jours atteignent le prix de départ.

**Inventaire.** Avec `AUTO_LIST=true`, le bot vérifie toutes les 10 min s'il reste une place de vente (tes ventes manuelles comptent) et y met le doublon le mieux coté, à partir de `SELL_MIN_VALUE` (15 WB), pour 6 h. Les positions du sniper passent avant. Ne sont **jamais** vendus ni défaussés : le dernier exemplaire de chaque carte, les favoris, les shiny, les exemplaires portant une étiquette de `PROTECTED_TAGS`, ceux engagés dans un échange ou déjà en vente. La défausse (`AUTO_DISCARD`) est bloquée près d'un palier de succès (`ACHIEVEMENT_MILESTONES`, `ACHIEVEMENT_MARGIN`) et ne fait jamais repasser la collection sous un palier atteint.

**Collection.** Avec `COLLECTION_SETS=jours`, le bot relit ta collection toutes les 6 h et note les jours qui te manquent (seuls les articles exacts comptent : « 14 octobre », pas « 14 octobre 1066 »). Le sniper mise sur une carte manquante en vente jusqu'à `COLLECTION_MAX_PRICE`, jamais sur deux ventes de la même carte ni sur une carte que tu cherches toi-même. Une carte gagnée est gardée. Le bot pose aussi l'étiquette `COLLECTION_TAG` (« date ») sur le meilleur exemplaire de chaque jour possédé (favori, puis shiny, puis plus haute rareté) ; l'étiquette doit déjà exister sur ton compte. Avec `PROTECTED_TAGS=date`, cet exemplaire n'est jamais vendu.

**Tendances.** Wikipédia : le top fr.wikipedia de la veille (publié avec un jour de retard), un pic étant ≥ 3 × la médiane des 30 jours précédents. Google : le flux RSS des recherches en hausse en France, lu toutes les 20 min. Les cartes en vente qui correspondent sont enregistrées comme signaux. Chaque dimanche, le bilan dit si les signaux de la semaine auraient rapporté : achat en surenchérissant sur le prix observé, revente à la médiane des ventes des 7 jours suivants, « gagnant » si le bénéfice atteint celui exigé d'une position. Avec au moins 10 signaux mesurés, il conclut « prometteur » ou « pas rentable ». `python -m src.main trends` donne le détail jour par jour.

## Routes du site

Aucune route n'est inventée. Toutes viennent du code des extensions communautaires (relevé le 14/10/2026) et sont décrites dans [config/endpoints.yaml](config/endpoints.yaml) :

- [qkerman/WikiMastersTools-kzfamily](https://github.com/qkerman/WikiMastersTools-kzfamily) : cotes, mise en vente, ouverture de paquets, cookie Supabase
- [Lypningeuh/WikiRemastered](https://github.com/Lypningeuh/WikiRemastered) : marché, règle de mise minimale, schéma des enchères, défausse groupée
- [ThePommespanzer/wikimasters-plus](https://github.com/ThePommespanzer/wikimasters-plus) : régénération des paquets

Pour corriger ou ajouter une route : `python -m src.tools.parse_curl --name place_bid --write < requete.curl` l'écrit dans `config/endpoints.local.yaml`, qui surcharge `endpoints.yaml`. Pour découvrir une route inconnue : `python -m src.tools.browser_login --record`, puis navigue sur le site ; chaque appel `/api/` est enregistré dans `data/inspect/capture/`.

**Encore inconnu, donc configurable plutôt que deviné :**
- les durées de vente acceptées par l'API au-delà de celles déjà utilisées (`LISTING_DURATION_OPTIONS` reprend le formulaire du site) ;
- une route de statut des paquets : le stock est déduit des réponses d'ouverture (déclarable sous le nom `pack_status`) ;
- les paliers de succès de collection, et s'ils comptent les exemplaires ou les cartes distinctes (`ACHIEVEMENT_COUNT_MODE`).

## Données

Le volume `data/` contient `wikimastered.db` (SQLite : cotes, tirages, enchères, ventes, défausses, signaux), `logs/bot.log`, la session et le `heartbeat` du healthcheck Docker. Il survit aux redéploiements et reste hors Git. Après chaque bilan, le bot purge les cotes de plus de 60 jours, les ventes observées de plus de 120 jours et les vues Wikipédia de plus de 60 jours.

## Développement

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pytest-asyncio
.venv/bin/python -m pytest
```

Les tests couvrent la logique de décision et le client face à un faux serveur (429, 401 puis reprise, 403, DRY_RUN, sniper, paquets, Discord). Aucun appel réseau réel.

**Chemin d'une modification jusqu'au bot :**
1. Une branche, une PR vers `main`.
2. GitHub Actions ([ci.yml](.github/workflows/ci.yml)) lance les tests et construit l'image. La règle « protéger main » exige qu'ils passent.
3. L'auto-merge fusionne la PR dès que c'est vert.
4. Portainer sonde `main` toutes les 5 min (GitOps, mode Polling) et redéploie la stack.

[deploy.yml](.github/workflows/deploy.yml) peut aussi appeler un webhook Portainer après chaque fusion (secret `PORTAINER_WEBHOOK_URL`), si Portainer est joignable depuis internet. Sans ce secret, il ne fait rien.
