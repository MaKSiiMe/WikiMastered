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
| [Cotes](src/workers/value_scan.py) | Cote toute la collection toutes les 12 h | 💰 |

Le détail des règles (estimation des prix, mises, remises en vente, défausse…) est dans [docs/FONCTIONNEMENT.md](docs/FONCTIONNEMENT.md).

## Collections

Une collection est une liste de titres Wikipédia à posséder. Le bot mise sur les cartes manquantes (jusqu'à `COLLECTION_MAX_PRICE`), les garde et leur pose une étiquette.

- `jours` (les 366 jours de l'année) est intégrée.
- Les autres se décrivent dans [config/collections.yaml](config/collections.yaml) : un nom, une étiquette, les titres.
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
| `/roi [jours]` | Bénéfice et ROI des reventes, jour par jour (7 jours par défaut) |
| `/solde` | Solde, capital dans les positions, total |
| `/positions` | Cartes achetées pour la revente, et où en est leur vente |
| `/ventes` | Ventes programmées et en cours |
| `/prix <carte>` | Ventes observées, estimation du bot, ventes en cours |
| `/collection` | Avancement des collections, cartes manquantes |
| `/sniper [heures]` | Récap des enchères misées |
| `/snipe <carte> <prix_max> [rareté]` | Demander une carte au sniper : mise au minimum jusqu'à `prix_max`, gardée (pas revendue). Confirmée par un bouton |
| `/snipes` | Cartes demandées |
| `/annuler <carte>` | Ne plus demander une carte |

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
