# WikiMastered

Bot Docker pour [WikiMasters](https://www.wiki-masters.com) : il achète des cartes pour les revendre, complète des collections et vend les doublons. Il passe directement par l'API JSON du site, sans navigateur, et rend compte sur Discord.

> [!WARNING]
> Automatiser un compte peut être contraire aux conditions d'utilisation du site et mener à un bannissement.
> Par défaut `DRY_RUN=true` : rien n'est envoyé au site, les actions sont seulement annoncées avec le préfixe `[DRY RUN]`.

## Workers

| Worker | Rôle | Discord |
|---|---|---|
| [Sniper](src/workers/auction_sniper.py) | Achète des UR et des L sous leur prix pour les revendre | 🎯 |
| [Inventaire](src/workers/inventory_manager.py) | Revend les achats et les doublons, défausse les cartes sans valeur | 💰 |
| [Collection](src/workers/collection.py) | Complète des collections de cartes : jours de l'année, éléments, pays… | 📅 |
| [Succès](src/workers/achievements.py) | Réclame les récompenses, chaque soir à 22:50 | 🏆 |
| [Cotes](src/workers/value_scan.py) | Cote toute la collection toutes les 12 h, pour la défausse des derniers exemplaires | 💰 |
| [Valeur](src/workers/account_value.py) | Compte la valeur du compte (`/valeur`) toutes les 6 h, sans dépendre de la défausse | — |

Le détail des règles (estimation des prix, mises, remises en vente, défausse…) est dans [docs/FONCTIONNEMENT.md](docs/FONCTIONNEMENT.md).

## Collections

Une collection est une liste de titres Wikipédia à posséder. Le bot mise sur les cartes manquantes (jusqu'à `COLLECTION_MAX_PRICE`), les garde et leur pose une étiquette. Il mise aussi sur une carte déjà possédée quand un exemplaire de rareté plus haute est en vente (`COLLECTION_UPGRADE`), jusqu'à avoir chaque carte dans sa plus haute rareté.

- `jours` (les 366 jours de l'année) est intégrée.
- Les autres se décrivent dans [config/collections.yaml](config/collections.yaml) : un nom, une étiquette, les titres.
- `gifs` : les articles dont l'image principale est un GIF animé. `python -m src.tools.gif_list` régénère la liste depuis Wikipédia.
- `COLLECTION_SETS=jours,elements` choisit celles qui sont actives.

## Installer

Avec Portainer : suivre [docs/PORTAINER.md](docs/PORTAINER.md). Avec Docker seul :

```bash
cp .env.example .env              # remplir les variables ci-dessous
sudo chown -R 1000:1000 data
docker compose up -d --build
docker compose exec wikimastered python -m src.main check
```

| Variable | Où la trouver |
|---|---|
| `WM_COOKIE` | DevTools > *Réseau* > une requête `/api/wikibidous` > en-tête `cookie` |
| `WM_USER_AGENT` | Même requête, en-tête `user-agent` |
| `DISCORD_WEBHOOK_URL` | Salon Discord > *Intégrations* > *Webhooks* |
| `SUPABASE_ANON_KEY` | Conseillée : le bot renouvelle seul sa session ([détails](docs/PORTAINER.md)) |

Toutes les autres variables sont commentées dans [.env.example](.env.example).

## Réel ou simulé

| Réglage | Effet |
|---|---|
| `DRY_RUN=true` | Tout est simulé |
| `DRY_RUN=true` + `LIVE_WRITES=ventes` | Seuls les groupes listés sont réels |
| `DRY_RUN=false` + `SIMULATE=sniper` | Tout est réel sauf les groupes listés |
| `DRY_RUN=false` | Tout est réel |

Groupes : `sniper`, `collection`, `ventes`, `defausse`, `tags`, `succes`, `inventory` (ventes + défausse).

## Quand intervenir

Le bot tourne seul. Une alerte 🔴 sur Discord signale les trois cas où il faut agir :

- **Vérification anti-bot** : faire une mise à la main dans le navigateur.
- **Sanction du site** : le bot s'arrête net. Pour le relancer, `rm data/HALT` puis redémarrer la stack.
- **Session perdue** : coller un nouveau cookie dans `data/session_cookie.txt`.

## Discord

| Couleur | Sens |
|---|---|
| 🔵 | Information : démarrage, résumés, bilan du soir |
| 🟢 | Bonne nouvelle : achat, revente, carte obtenue |
| 🟠 | À surveiller : invendu, erreurs répétées |
| 🔴 | Il faut agir |

Un récap toutes les 4 h, un bilan à 23:00, le récap des enchères à 08:00.

### Commandes slash

Avec un bot Discord (`DISCORD_BOT_TOKEN`, `DISCORD_OWNER_ID`, `DISCORD_GUILD_ID` : voir [docs/PORTAINER.md](docs/PORTAINER.md)), le bot répond à ces commandes, et à toi seul :

| Commande | Réponse |
|---|---|
| `/bilan` | Le bilan du jour, sans attendre 23:00 |
| `/roi [jours]` | Bénéfice et ROI des reventes, jour par jour (7 jours par défaut), avec un graphique en barres |
| `/solde` | Solde, capital dans les positions, total |
| `/valeur` | Valeur du compte : solde + cartes possédées (estimation du bot) + cartes en vente (prix de départ) + mises en tête, l'évolution depuis la veille, la collection par rareté et les cartes les plus chères. Boutons : 📈 évolution (courbe de la valeur, notée chaque jour), 🔄 recompter la collection |
| `/positions` | Cartes achetées pour la revente, et où en est leur vente |
| `/ventes` | Ventes programmées et en cours |
| `/prix <carte>` | Ventes observées, estimation du bot, ventes en cours, et la mise conseillée pour l'acheter et la revendre (normale et shiny) : la plus petite dont la relance minimale (+10 %) atteint la revente, avec le bénéfice |
| `/collection` | Avancement des collections, cartes manquantes |
| `/collection-joueur` | Collection publique d'un joueur (une de ses étiquettes au choix), comparée à notre liste de GIF |
| `/sniper [heures]` | Récap des enchères misées |
| `/snipe <carte> <prix_max> [rareté] [shiny]` | Demander une carte au sniper : mise au minimum jusqu'à `prix_max`, gardée (pas revendue). `shiny` : seulement un exemplaire shiny (sinon shiny ou non). Confirmée par un bouton |
| `/snipes` | Cartes demandées |
| `/annuler <carte>` | Ne plus demander une carte |
| `/etat` | Santé du bot : session, vérification anti-bot, routes du site en pause, chaque worker. Boutons : vérification faite, pause / reprise du sniper, actualiser |
| `/verif-faite` | Vérification anti-bot faite dans le navigateur : la prochaine mise part tout de suite pour la tester |
| `/sauvegarde` | Sauvegarde la base maintenant (une est faite chaque nuit à 04:30, 7 gardées : [restauration](docs/PORTAINER.md)) |
| `/pause [quoi] [durée]` | Met en pause le sniper (par défaut), les ventes (reventes du sniper comprises), les cotes ou tout (sauf la garde), 1 h par défaut ou jusqu'à `/reprise`. Gardée après un redémarrage |
| `/reprise [quoi]` | Reprend ce qui est en pause |

| `/garder <carte> [rareté]` | Ne jamais vendre ni défausser cette carte ; arrête sa revente si le bot la revendait |
| `/gardees`, `/liberer <carte>` | Voir les cartes gardées, en libérer une |
| `/vendre <carte> <prix> [rareté]` | Vendre ton exemplaire le plus récent (hors favori, shiny, gardé) à ce prix, à la première place libre, avant les autres ventes. Confirmée par un bouton |
| `/defausse [activer\|désactiver]` | Défausse automatique ; coupée, le worker `cotes` (qui ne sert qu'à elle) est mis en pause. Les reventes continuent |
| `/doublons [activer\|désactiver]` | Vente automatique des doublons ; coupée, les doublons programmés sont annulés |
| `/plafond-collection [wb]` | Voir ou changer la mise maximale d'une carte de collection manquante, sans redéployer |
| `/carte <carte>` | Tes exemplaires, les ventes observées, ce que le bot en fait. Boutons : garder, vendre, demander au sniper |
| `/opportunites` | Les ventes que le sniper surveille et son plafond pour chacune |
| `/top [nombre] [shiny]` | Les 10, 25 ou 50 cartes les plus chères en moyenne sur les ventes observées des 7 derniers jours (au moins 3 ventes), normales ou shiny, avec la mise conseillée pour les revendre : la plus petite dont la relance minimale (+10 %) atteint le prix de revente, et le bénéfice si tu la gagnes |
| `/shiny [tri] [prix_max] [nombre] [rentables]` | Les shiny en vente vus par le sniper, avec mise minimale, cote et fin, sans requête au site. 💰 : revente rentable d'après la cote, avec le `prix_max` à donner à `/snipe` |
| `/echange <je_donne> <je_recois> […]` | Un échange envisagé est-il équitable ? Jusqu'à 3 cartes et des WB de chaque côté (« Titre », « Titre (L) », « Titre ✨ »). Valeur de chaque carte (médiane des ventes observées sur 7 jours), ventes par semaine, verdict et, s'il est défavorable, la contre-offre à envoyer |

Le bot n'envoie jamais de message privé : tout arrive dans les salons des webhooks. Avec le bot Discord connecté :
- « Vérification anti-bot demandée » (et son rappel toutes les heures) arrive dans le salon des alertes avec la mention et le bouton **C'est fait** ; quand une mise repasse, le message devient « ✅ Vérification levée » ;
- « Revente sans acheteur » arrive avec les boutons **Changer le prix**, **Brader à 1 WB** et **Garder** (tant que la remise en vente n'est pas publiée) ;
- « Revente à 1 WB » arrive avec **Changer le prix** et **Garder** ;
- « Position invendue » et « Vente sans acheteur » arrivent avec **Remettre en vente…**.

Ces boutons marchent encore après un redémarrage du bot.

Une carte demandée est cherchée par son titre, comme un jour manquant, passe en priorité dans la fenêtre de mise et ignore `SNIPER_MAX_BID`. Gagnée, elle n'est plus demandée. Avec les mises de collection simulées (`SIMULATE=collection`), la mise est simulée.

## Commandes

À lancer dans la console du conteneur (Portainer > *Containers* > `wikimastered` > *Console*).

```bash
python -m src.main check                  # session et solde
python -m src.main report                 # bilan du jour sur Discord
python -m src.main sniper-report --hours 24
python -m src.main inventory scan         # ce qui serait vendu ou défaussé
python -m src.main collection             # avancement de la collection
python -m src.main prices "Titre exact"   # ventes observées d'une carte
python -m src.main roi --days 7           # bénéfice et ROI des reventes, jour par jour
```

Liste complète : `python -m src.main --help`.

## Développement

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pytest-asyncio
.venv/bin/python -m pytest
```

Chaque PR fusionnée sur `main` (tests verts, auto-merge) est redéployée par Portainer en ~5 min.
