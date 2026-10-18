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
| Succès | `src/workers/achievements.py` | Réclame les récompenses des succès débloqués, chaque soir à 22:50 | 🏆 Succès |
| Valeur | `src/workers/value_scan.py` | Toutes les 12 h, cote tout l'inventaire et signale les cartes de valeur, derniers exemplaires compris | 💎 Valeur |

> **À lire avant de lancer**
> - Automatiser un compte peut être contraire aux conditions d'utilisation de WikiMasters et mener à un bannissement. L'espacement des requêtes limite la charge sur le site ; il ne garantit rien.
> - `DRY_RUN=true` par défaut : aucune enchère, vente, défausse ni ouverture n'est envoyée. Les actions sont journalisées et annoncées sur Discord avec le préfixe `[DRY RUN]`. Le passage en réel se fait worker par worker (voir [Réel ou simulé](#réel-ou-simulé)).
> - Quand le site demande une **vérification humaine** pour les paquets, le bot s'arrête et te prévient. Il ne la contourne jamais : fais-la toi-même dans ton navigateur.

## Autonomie

État au 15/10/2026. Le bot tourne seul ; il n'y a à toucher que ce qui est listé dans « Ce qui demande ton intervention ».

**Vérifié en production, sans intervention :**

| Fonction | Preuve |
|---|---|
| Renouvellement de la session (avec `SUPABASE_ANON_KEY`) | « Session renouvelée » toutes les heures dans les logs |
| Ouverture des paquets | Boosters ouverts toute la journée (hors vérification humaine, voir plus bas) |
| Vente des doublons (`AUTO_LIST`) | Ventes programmées puis publiées (réponse réelle HTTP 201 relevée) |
| Lecture de la collection | 38/366 jours lus toutes les 6 h |
| Récap toutes les 4 h, bilan à 23:00, purge de la base | Messages Discord reçus, « Entretien de la base » dans les logs |
| Signaux de tendance | Wikipédia et Google Trends enregistrés à chaque passage |
| Mise à jour du bot | Chaque PR fusionnée sur `main` est déployée par Portainer en ~5 min |

**En place, à confirmer sur les prochains jours :**

| Fonction | Ce qui le confirmera |
|---|---|
| Repérage des ventes par le sniper et mises sur les jours manquants (réelles) | Lignes `collection : … en vente … (dans X min)` puis `… misée (collection)` ; ligne « Repérage » du récap sans « pas relue à temps » |
| Succès réclamés chaque soir à 22:50 | Message « Succès réclamés », ou `data/inspect/writes/claim_achievement-1.json` (routes pas encore vérifiées) |
| Cotes protégées (ventes aberrantes, factices, sans concurrence) | Rien à voir : elles rendent seulement les estimations plus prudentes |
| Suivi des vérifications humaines | `python -m src.main packs` après quelques jours |
| Meilleure heure de fin pour revendre | `python -m src.main hours` (ventes observées par heure de fin, UR et L ; `--rarity` pour d'autres) |
| Étiquette « date » posée sur les jours obtenus | Message « Collection : étiquettes ajoutées » au prochain jour gagné |

**Simulé, en attente de ta décision :** la revente du sniper (UR et L). Le bilan du soir contient un bloc « Validation de la simulation » ; passer en réel = retirer `sniper` de `SIMULATE`.

**Ce qui demande ton intervention (alerte 🔴 sur Discord) :**
- **Vérification humaine des paquets** : à faire toi-même dans ton navigateur, le bot ne la contourne jamais. Rien n'est perdu tant que tu la fais avant que le stock atteigne 10 paquets (~1 h 30). Un redémarrage du bot semble la déclencher.
- **Vérification anti-bot des enchères** : les mises réelles sont refusées tant que tu ne l'as pas faite dans ton navigateur (mise à la main sur le marché, ou page Paquets si elle la demande). En attendant, le bot ne tente qu'une mise toutes les 5 min et te prévient dès qu'une mise repasse.
- **Sanction du site** : le bot relit ton profil toutes les 15 min. Au premier avertissement de triche (`cheat_strikes`), blocage d'activité ou sanction, il s'arrête net (plus aucune requête, même après un redémarrage) et t'alerte. Pour reprendre : `rm data/HALT` dans la console du conteneur, puis redémarre la stack.
- **Session perdue** : seulement si le renouvellement est refusé (par exemple après « Déconnexion » sur le site avec la même session). Colle un nouveau cookie dans `data/session_cookie.txt`.

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

Groupes acceptés : `sniper`, `collection`, `packs`, `ventes`, `defausse`, `tags`, `succes`, `inventory` (= ventes + défausse). Tant que le sniper est simulé, le bilan du soir contient un bloc « Validation de la simulation » qui dit quand le passage en réel est envisageable. Une fois les mises réelles, `SNIPER_SHADOW=true` garde un sniper de test : il simule les achats que le sniper réel ne fait pas (au-dessus de `SNIPER_MAX_BID`, achats épuisés, plafond atteint, raretés `SNIPER_SHADOW_RARITIES`), avec ses propres limites (`SNIPER_SHADOW_MAX_BID`, `SNIPER_SHADOW_MAX_POSITIONS`), pour continuer à mesurer la stratégie. `NOTIFY_SIMULATED=false` réserve Discord au réel. Chaque matin à 08:00 (`SNIPER_REPORT_TIME`), un récap liste toutes les enchères misées sur 24 h (`SNIPER_REPORT_HOURS`) : réelles, sniper de test et collection, avec la plus-value latente des cartes gagnées, le gain réalisé, et le gain non acquis des enchères perdues (« dans nos limites » quand le gagnant a payé moins que notre plafond).

## Discord

Tout arrive dans un seul salon. Chaque message porte en tête l'étiquette du worker qui l'envoie (tableau du haut), et en pied le mode du bot (par exemple « Mode RÉEL (simulé : sniper) »).

La couleur indique l'importance :

| Couleur | Sens | Exemples |
|---|---|---|
| 🔵 Bleu | Information | Bot démarré, boosters ouverts, résumé des enchères, bilan du soir |
| 🟢 Vert | Bonne nouvelle | Position achetée, revente réussie, tirage UR / L / Shiny, carte de collection obtenue |
| 🟠 Orange | À surveiller | Position invendue, mise en vente refusée, erreurs répétées, bot arrêté |
| 🔴 Rouge | Il faut agir | Session expirée, vérification humaine des paquets, enchère non confirmée |

Les messages rouges partent dans `DISCORD_ALERT_WEBHOOK_URL` s'il est défini, avec la mention `DISCORD_MENTION`. Avec `DISCORD_ACTIVITY_WEBHOOK_URL` (un 2e salon), le salon principal (à laisser sonner) ne garde que l'essentiel : alertes, achats, mises en vente et ventes réelles, cartes de valeur de l'inventaire, bilan du soir et récap du sniper ; tout le reste va dans le salon d'activité (à mettre en sourdine) : paquets et tirages, cartes de collection gagnées, succès, digests d'enchères, récaps périodiques, tendances, simulé (`off` : logs seulement).

**Ce qui n'envoie pas de message à chaque fois :**
- les enchères : un résumé toutes les `NOTIFY_DIGEST_MINUTES` (60) ; `NOTIFY_EACH_BID=true` envoie un message par enchère ;
- les ventes de doublons : comptées dans le bilan du soir ;
- les tendances : enregistrées et mesurées, sans message ; `TRENDS_NOTIFY=true` rétablit un message par passage.

**Le récap** (🩺, toutes les `RECAP_INTERVAL_HOURS` heures, 4 par défaut, aux heures rondes) montre ce que chaque worker a fait depuis le précédent, s'il tourne encore et ses erreurs, et ce que le sniper a fait des enchères de sa fenêtre de mise (pas assez vue vendue, bénéfice trop faible, misée…). S'il trouve une anomalie (worker arrêté, positions au-dessus du plafond, marché plus lu, session en pause), il passe en orange avec un bloc « À vérifier » en tête.

**Le bilan du soir** (`DAILY_REPORT_TIME`, 23:00) donne le solde puis un bloc par activité : Paquets, Sniper, Ventes, Positions, Validation de la simulation, Collection, Tendances du jour. Le dimanche, il ajoute « Tendances de la semaine ».

Les tirages remarquables, positions achetées et cartes de collection obtenues montrent l'image de la carte (si le site la fournit) et un lien vers son article Wikipédia.

## Commandes

```bash
python -m src.main run                             # le bot (commande par défaut du conteneur)
python -m src.main check                           # session, solde, renouvellement automatique
python -m src.main inspect                         # compare les vraies réponses aux modèles (lecture seule)
python -m src.main profile                         # profil du joueur lu sur le site : solde, paquets, pity…
python -m src.main report                          # envoie le bilan du jour sur Discord
python -m src.main sniper-report --hours 12        # toutes les enchères misées : gagnées, perdues, gain latent et manqué
python -m src.main inventory scan                  # ce qui serait vendu / défaussé, et pourquoi
python -m src.main inventory report --lookups 300  # toute la collection cotée : résumé + data/inventaire-<date>.csv
python -m src.main inventory list --apply          # programme les ventes
python -m src.main inventory discard --apply       # défausse, après vérification des paliers
python -m src.main value [--lookups 100]           # cartes de valeur de l'inventaire, derniers exemplaires compris
python -m src.main packs                           # vérifications humaines, paquets ouverts ces dernières 24 h
python -m src.main collection                      # avancement des collections, cartes manquantes
python -m src.main trends --days 7                 # les tendances auraient-elles rapporté ? (détail)
python -m src.main hours [--rarity UR,L]           # à quelle heure de fin les enchères se vendent le mieux
python -m src.main gifs                            # les cartes GIF se vendent-elles mieux ? (ventes observées)
python -m src.main prices "Titre exact"            # ventes observées d'une carte, estimation du bot, ventes en cours
python -m src.main market [--rarity UR]            # où sont les fins imminentes sur le marché (diagnostic)
python -m src.main reset-simulation [--apply]      # efface les mises et ventes simulées (données réelles gardées)
```

Dans Portainer : *Containers* > `wikimastered` > *Console*. Avec Docker Compose : préfixe par `docker compose exec wikimastered`.

## Fonctionnement

**Réseau.** Une requête à la fois, espacées de 1,5 s plus un aléa. Sur 429 : pause globale exponentielle (jusqu'à 15 min, `Retry-After` respecté). Un 403 avec `Retry-After` (la route des cotes, par exemple) ne met en pause que sa route ; un refus isolé reste dans les logs, un message part au 3e refus de suite. Un 5xx ou un délai dépassé isolé (lecture comme écriture) ne met en pause que la route concernée ; des 5xx sur deux routes sans succès entre les deux (ou une page de passerelle) déclenchent la pause globale. Un 403 n'est une perte de session que si une sonde `/api/wikibidous` échoue aussi ; sinon c'est une route refusée (réservée au PRO, par exemple). Une écriture n'est **jamais rejouée** : si sa réponse est perdue, elle est marquée « incertaine », puis le bot relit le résultat sur le site et envoie « … vérifiée ».

**Workers indépendants.** Chaque worker tourne à part : une erreur, même au démarrage, n'arrête que lui, et il réessaie seul. Une tâche qui s'arrête quand même est relancée (30 s, puis de plus en plus espacé, 10 min au plus) avec une alerte, sans toucher aux autres : si l'ouverture des paquets tombe en panne, le sniper continue. Seuls la perte de session, un 429 et une coupure réseau mettent tout le bot en pause, car ils touchent toutes les routes.

**Session.** Sur 401/403, tous les workers se figent et une alerte rouge part. Avec `SUPABASE_ANON_KEY`, le bot renouvelle lui-même sa session avant l'expiration du jeton, comme ton navigateur ; il lui faut alors **sa propre session** (le jeton de renouvellement ne sert qu'une fois). Sinon, un nouveau cookie collé dans `data/session_cookie.txt` est chargé en moins de 30 s, sans redémarrage. Le bot ne se connecte jamais tout seul : le formulaire du site est protégé par Cloudflare Turnstile.

**Paquets.** Une session d'ouverture toutes les 15 à 90 min (au hasard), paquets espacés de 5 à 15 s : le site semble demander une vérification humaine quand les actions s'enchaînent trop vite. Le stock se recharge d'un paquet toutes les 10 min, jusqu'à 10 : rien n'est perdu. La limite quotidienne met en pause jusqu'au lendemain. Chaque vérification humaine est enregistrée ; `python -m src.main packs` montre combien de boosters et de temps les séparent, et combien suivent un démarrage du bot. Le récap affiche « ⏸️ » tant qu'une vérification attend.

**Sniper.** Le prix d'une carte suit surtout les tendances (actu, pop culture, mèmes), pas sa rareté. Et le site limite à **5 ventes simultanées** (les mises, elles, sont illimitées). Le sniper raisonne donc en capital : chaque achat est une « position » qui attendra une de ces 5 places pour être revendue.
- **Repérage** : environ 12 enchères finissent chaque seconde, et le tri « fin imminente » du site reste encombré d'enchères terminées : le sniper repère donc les ventes à l'avance. Il lit les nouvelles ventes des raretés revendues (`SNIPER_RESALE_RARITIES`, UR et L) toutes les 10 s, et cherche les jours manquants un par un (un toutes les 10 s). Une vente intéressante est suivie, puis relue sur sa page juste avant de miser.
- **Estimation** : médiane des 5 dernières ventes que le bot a lui-même observées (il lit les ventes tout juste terminées des raretés revendues), plafonnée par la moyenne du site, qui peut être très datée. Une carte doit avoir été vue vendue au moins `PRICE_MIN_SALES` (2) fois. Contre les cotes gonflées : une vente 3 fois au-dessus ou au-dessous de la médiane est écartée ; une vente entre un vendeur et un acheteur qui ont déjà fait affaire (ou vendeur = acheteur) ne compte pas ; une vente sans concurrence (une seule mise, au prix de départ) ne peut pas faire monter l'estimation.
- **Budget d'une position** : capital × `PORTFOLIO_CAPITAL_RATIO` (0,9) / 5, soit ~540 WB pour 3 000 WB.
- **Achat** si le bénéfice attendu atteint 25 % de ce budget (~135 WB) et si la mise reste sous 70 % de l'estimation (80 % la nuit, 02:00–07:30). Au-delà des places de vente libres, une position attend son tour (10 au plus, 15 positions en tout) et doit rapporter 50 % du budget.
- **Mise** : toujours le minimum du site (`max(⌈1,1 × mise⌉, mise + 1)`), entre 20 et 11 s avant la fin. Le site prolonge de 60 s une enchère disputée dans ses 10 dernières secondes : le bot n'y mise jamais, mais relit à sa fin une enchère qu'il a misée et la rejoue si elle a été prolongée.
- **Tes enchères** : le bot ignore toute carte sur laquelle tu mises toi-même (`SNIPER_AVOID_MY_BIDS`) et ne revend jamais une carte gagnée à la main.
- **Enchère introuvable** : le site renvoie parfois « Enchère introuvable » juste après la fin (le 18/10 au soir, site instable). Une mise qui était en tête est alors gagnée si la carte arrive dans ta collection ; sinon le bot la relit toutes les 5 min pendant 30 min avant de la compter perdue (« Mise à vérifier »). Au démarrage, il retrouve les mises comptées perdues ainsi ces 2 derniers jours dont la carte est arrivée.

**Positions.** Une carte gagnée est remise en vente en priorité, publiée dès qu'elle est arrivée pour 6 h (`POSITION_LIST_DURATION_MINUTES`). La mise d'une position n'est pas le minimum : c'est le minimum + la moitié de l'écart jusqu'à notre plafond (`SNIPER_BID_PUSH=0.5`). Surenchérie, la mise suivante va droit au plafond, une seule fois. `SNIPER_LIVE_START` / `SNIPER_LIVE_END` (ex. 23:00 / 09:00) limitent les mises réelles des positions à ces heures. En dehors, elles sont simulées pour comparaison, et la collection reste réelle. Le jour, le chasseur de l'extension achète alors les L depuis le navigateur, où la vérification anti-bot passe seule. Les rivaux surenchérissent d'un pas en 20-30 s, et chaque mise use le quota de la vérification anti-bot. Le récap de 08:00 et `sniper-report --summary` donnent les chiffres pour régler ce choix : mises par enchère, perdues dans nos limites, prix final face à notre mise, refus par cause, revente face à l'estimation. `POSITION_POST_NOW=false` revient à une publication entre `POSITION_POST_START` et `POSITION_POST_END` (11:00-12:00 par défaut, fin vers 23 h-minuit). `SNIPER_MAX_BUYS` limite le nombre total d'achats réels (0 = illimité) : atteint, le sniper ne rachète plus rien à revendre. C'est une enchère : le prix de départ est le prix d'achat + la moitié du bénéfice attendu à l'achat (`POSITION_LIST_PROFIT_SHARE`), au moins + 15 % (`POSITION_LIST_MARGIN`), commission comprise ; les joueurs le font monter. Les enchères montent peu (prix final / départ : médiane ×1,11 en UR, ×1,23 en L), d'où un départ assez haut. Invendue, elle repart à −10 % (deux fois au plus), jamais sous le prix d'achat, puis elle est gardée. Au moment de la publier, le bot lit les autres ventes de la même carte et se place juste sous la moins chère. Si cette concurrente est sous le prix d'achat, la revente attend (`POSITION_UNDERCUT_WAIT_MINUTES`, 6 h), puis part au prix d'achat. Beaucoup de cartes partent au prix demandé : à l'achat aussi, la revente estimée ne dépasse jamais la vente concurrente la moins chère encore en cours une heure après. Le bot n'achète que si, parmi les 5 dernières ventes observées, au moins 3 ont été disputées (`SNIPER_MIN_CONTESTED_SALES`) et la plus haute ne vaut pas plus de 2 fois la plus basse (`SNIPER_MAX_PRICE_SPREAD`). En simulation, une revente aboutit si les ventes observées de la carte sur 3 jours atteignent le prix de départ, et se vend alors à leur médiane (le prix du marché).

**Inventaire.** Avec `AUTO_LIST=true`, le bot vérifie toutes les 10 min s'il reste une place de vente (tes ventes manuelles comptent) et y met le doublon le mieux coté, à partir de `SELL_MIN_VALUE` (15 WB), pour 6 h. Les positions du sniper passent avant. Ne sont **jamais** vendus ni défaussés : le dernier exemplaire de chaque carte, les favoris, les shiny, les exemplaires portant une étiquette de `PROTECTED_TAGS`, ceux engagés dans un échange ou déjà en vente. La défausse (`AUTO_DISCARD`) est bloquée près d'un palier de succès (`ACHIEVEMENT_MILESTONES`, `ACHIEVEMENT_MARGIN`) et ne fait jamais repasser la collection sous un palier atteint.

**Valeur de l'inventaire.** La vente automatique ne vend que des doublons : une C ou une PC en un seul exemplaire n'est jamais cotée, alors qu'elle peut valoir cher (le prix suit les tendances). Toutes les `VALUE_SCAN_HOURS` (12 h), le bot lit toute la collection et cote les cartes à la suite, en requêtes de fond : une cote ne part que si aucune autre requête n'attend, le sniper passe toujours avant. La route des cotes est limitée par le site (le 18/10, refusée 3 fois en 4 min au seul sniper) : le rythme vise une cote toutes les `VALUE_SCAN_LOOKUP_SECONDS` (3 s), ralentit de moitié à chaque refus et réaccélère doucement (×0,75) après 20 cotes acceptées. Pendant un refus, l'ancienne moyenne du site en cache plafonne toujours les estimations du sniper. Les cartes jamais cotées passent d'abord, les plus vues sur Wikipédia en tête, puis les cotes les plus anciennes. Une cote de moins de `VALUE_SCAN_MAX_AGE_DAYS` (7) jours est gardée, une carte sans aucune vente n'est recotée qu'après `VALUE_SCAN_UNSOLD_DAYS` (30) jours, et une carte cotée sous `VALUE_SCAN_FREEZE_BELOW` (50 WB, toutes ses raretés possédées) garde cette cote pour toujours : plus jamais redemandée, ni par l'analyse ni par la vente des doublons. Après la première analyse, un passage ne recote donc presque que les cartes à 50 WB ou plus. Le bot signale sur Discord (💎 Valeur) les cartes cotées au moins `VALUE_SCAN_MIN_VALUE` (100 WB) pas encore signalées, ou dont la cote a monté de moitié depuis, à la fin du passage et toutes les 3 h pendant un long passage : cote et source (ventes observées ou moyenne du site, parfois datée), dernier exemplaire ou doublon, et les ventes concurrentes en cours (nombre, la moins chère). Il ne vend rien : à toi de décider. Les favoris et les exemplaires protégés (étiquettes `PROTECTED_TAGS`, échanges, ventes en cours) ne sont pas signalés. `python -m src.main value` liste toutes les cartes de valeur d'après les cotes en cache (`--lookups N` en demande N de plus au site). Un redémarrage pendant un passage ne perd rien : les cotes déjà demandées restent en cache et le passage reprend.

**Collection.** Avec `COLLECTION_SETS=jours`, le bot relit ta collection toutes les 6 h et note les jours qui te manquent (seuls les articles exacts comptent : « 14 octobre », pas « 14 octobre 1066 »). Le sniper mise sur une carte manquante en vente jusqu'à `COLLECTION_MAX_PRICE`, jamais sur deux ventes de la même carte ni sur une carte que tu cherches toi-même. Chaque vérification anti-bot laisse passer environ 9 à 10 mises : la collection n'en prend que `COLLECTION_BIDS_PER_CHECK` (2), le reste est gardé pour les L. Un jour manquant est cherché sur le marché toutes les `COLLECTION_SEARCH_SECONDS` (30 s), pour laisser les requêtes au repérage des L. Une carte gagnée est gardée. Le bot pose aussi l'étiquette `COLLECTION_TAG` (« date ») sur le meilleur exemplaire de chaque jour possédé (favori, puis shiny, puis plus haute rareté) ; l'étiquette doit déjà exister sur ton compte. Avec `PROTECTED_TAGS=date`, cet exemplaire n'est jamais vendu. Le récap, le bilan et `python -m src.main collection` montrent l'avancement de chaque collection (« Jours de l'année : 38/366 (10,4 %) ») et les cartes obtenues ces derniers jours.

**Succès.** Chaque soir à `ACHIEVEMENTS_TIME` (22:50), le bot demande au site de recalculer tes succès et réclame les récompenses débloquées, une par une.

**Tendances.** Wikipédia : le top fr.wikipedia de la veille (publié avec un jour de retard), un pic étant ≥ 3 × la médiane des 30 jours précédents. Google : le flux RSS des recherches en hausse en France, lu toutes les 20 min. Les cartes en vente qui correspondent sont enregistrées comme signaux. Chaque dimanche, le bilan dit si les signaux de la semaine auraient rapporté : achat en surenchérissant sur le prix observé, revente à la médiane des ventes des 7 jours suivants, « gagnant » si le bénéfice atteint celui exigé d'une position. Avec au moins 10 signaux mesurés, il conclut « prometteur » ou « pas rentable ». `python -m src.main trends` donne le détail jour par jour.

## Routes du site

Aucune route n'est inventée. Toutes viennent du code des extensions communautaires (relevé le 14/10/2026) et sont décrites dans [config/endpoints.yaml](config/endpoints.yaml) :

- [qkerman/WikiMastersTools-kzfamily](https://github.com/qkerman/WikiMastersTools-kzfamily) : cotes, mise en vente, ouverture de paquets, cookie Supabase
- [Lypningeuh/WikiRemastered](https://github.com/Lypningeuh/WikiRemastered) : marché, règle de mise minimale, schéma des enchères, défausse groupée
- [ThePommespanzer/wikimasters-plus](https://github.com/ThePommespanzer/wikimasters-plus) : régénération des paquets
- [3d3n-pyc/wikimasters-farm](https://github.com/3d3n-pyc/wikimasters-farm), [LockBlock-dev/wikimasters-bot](https://github.com/LockBlock-dev/wikimasters-bot) : succès, historique des mises d'une enchère (relevé le 15/10/2026)

Relevés sur le vrai site (`python -m src.main market`, 15/10/2026) : environ 12 enchères finissent par seconde ; le tri `ending_soon` garde en tête les enchères terminées non clôturées (jusqu'à 3 min), même filtré par `rarity` ; `limit` au-delà de 50 est refusé (403).

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
