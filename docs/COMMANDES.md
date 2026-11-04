# Commandes

- [Discord](#discord) : à taper dans ton serveur
- [Console](#console) : dans le conteneur
- [Outils](#outils) : sur ton poste

## Discord

Ces commandes demandent un bot Discord ([installation](INSTALLATION.md#activer-les-commandes)). Le bot ne répond qu'à `DISCORD_OWNER_ID`. Entre crochets : paramètres facultatifs.

### Suivre

| Commande | Réponse |
|---|---|
| `/etat` | Santé du bot : session, vérification anti-bot, routes du site en pause, chaque worker. Boutons : vérification faite, pause ou reprise du sniper, actualiser |
| `/bilan` | Le bilan du jour, sans attendre 23:00 |
| `/roi [jours]` | Bénéfice et ROI des reventes, jour par jour (7 jours par défaut), avec un graphique |
| `/solde` | Solde, capital dans les positions, total |
| `/valeur` | Valeur du compte : solde, cartes possédées (estimation du bot), cartes en vente (prix de départ) et mises en tête ; évolution depuis la veille, collection par rareté, cartes les plus chères. Boutons : 📈 courbe de la valeur, 🔄 recompter la collection |
| `/positions` | Cartes achetées pour la revente, et où en est leur vente |
| `/ventes` | Ventes programmées et en cours |
| `/sniper [heures]` | Récap des enchères misées |

### Cartes et marché

| Commande | Réponse |
|---|---|
| `/carte <carte>` | Tes exemplaires, les ventes observées, ce que le bot en fait. Boutons : garder, vendre, demander au sniper |
| `/prix <titre> [shiny]` | Ventes observées, estimation du bot, ventes en cours, et la mise conseillée pour acheter et revendre : la plus petite dont la relance minimale (+10 %) atteint la revente, avec le bénéfice |
| `/top [nombre] [shiny]` | Les 10, 25 ou 50 cartes les plus chères en moyenne sur les ventes observées des 7 derniers jours (au moins 3 ventes), avec la mise conseillée |
| `/opportunites` | Les ventes que le sniper surveille et son plafond pour chacune |
| `/shiny [tri] [prix_max] [nombre] [rentables]` | Les shiny en vente vus par le sniper, avec mise minimale, cote et fin, sans requête au site. 💰 : revente rentable d'après la cote, avec son plafond. Un menu sous le message demande ces cartes au sniper, plafond modifiable |
| `/echange <je_donne> <je_recois> […]` | Un échange envisagé est-il équitable ? Jusqu'à 3 cartes et des WB de chaque côté (« Titre », « Titre (L) », « Titre ✨ »). Valeur de chaque carte, ventes par semaine, verdict et, s'il est défavorable, la contre-offre à envoyer |

### Agir

| Commande | Effet |
|---|---|
| `/snipe <titre> <prix_max> [rarete] [shiny]` | Demander une carte au sniper : mise au minimum jusqu'à `prix_max`, gardée, pas revendue. `shiny` : seulement un exemplaire shiny. Confirmée par un bouton |
| `/snipes` | Voir les cartes demandées |
| `/annuler <titre>` | Ne plus demander une carte |
| `/vendre <carte> <prix> [rarete]` | Vendre ton exemplaire le plus récent (hors favori, shiny, gardé) à ce prix, à la première place libre, avant les autres ventes. Confirmée par un bouton |
| `/garder <carte> [rarete]` | Ne jamais vendre ni défausser cette carte ; arrête sa revente si le bot la revendait |
| `/gardees` | Voir les cartes gardées |
| `/liberer <carte>` | Ne plus garder une carte |

Une carte demandée par `/snipe` est cherchée par son titre, comme une carte de collection, passe en priorité dans la fenêtre de mise et ignore `SNIPER_MAX_BID`. Gagnée, elle n'est plus demandée. Avec `SIMULATE=collection`, la mise est simulée.

### Collections

| Commande | Réponse |
|---|---|
| `/collection` | Avancement des collections, cartes manquantes |
| `/collection-joueur <pseudo> [etiquette]` | Collection publique d'un joueur (une de ses étiquettes au choix), comparée à la liste des GIF |
| `/plafond-collection [wb_max]` | Voir ou changer la mise maximale d'une carte de collection manquante, sans redéployer |

### Piloter

| Commande | Effet |
|---|---|
| `/pause [quoi] [duree]` | Met en pause `sniper` (par défaut), `ventes` (reventes comprises), `cotes` ou `tout` (sauf la garde). 1 h par défaut, ou 30 min, 2 h, 6 h, jusqu'à `/reprise`. Tient après un redémarrage |
| `/reprise [quoi]` | Reprend ce qui est en pause (tout, par défaut) |
| `/verif-faite` | Vérification anti-bot faite dans le navigateur : la prochaine mise part tout de suite pour la tester |
| `/defausse [etat]` | Voir, activer ou désactiver la défausse automatique. Coupée, le worker `cotes`, qui ne sert qu'à elle, est mis en pause. Les reventes continuent |
| `/doublons [etat]` | Voir, activer ou désactiver la vente automatique des doublons. Coupée, les doublons programmés sont annulés |
| `/sauvegarde` | Sauvegarde la base maintenant ([restauration](INSTALLATION.md#sauvegardes)) |

### Boutons sur les messages

Le bot n'envoie jamais de message privé : tout arrive dans les salons des webhooks. Avec le bot Discord connecté, certains messages portent des boutons, qui marchent encore après un redémarrage :

| Message | Boutons |
|---|---|
| Vérification anti-bot demandée (rappel toutes les heures) | **C'est fait** ; quand une mise repasse, le message devient « ✅ Vérification levée » |
| Revente sans acheteur | **Changer le prix**, **Brader à 1 WB**, **Garder** (tant que la remise en vente n'est pas publiée) |
| Revente à 1 WB | **Changer le prix**, **Garder** |
| Position invendue, Vente sans acheteur | **Remettre en vente…** |

## Console

À lancer dans le conteneur : `docker compose exec wikimastered <commande>`, ou la console de Portainer. Sauf mention contraire, ces commandes ne font que lire. Liste complète : `python -m src.main --help`.

**Vérifier**

| Commande | Effet |
|---|---|
| `python -m src.main check` | Session, joueur, solde |
| `python -m src.main inspect [--out DOSSIER]` | Capture les vraies réponses du site et les compare aux modèles du code |
| `python -m src.main profile` | Profil lu sur le site : solde, paquets, pity… |

**Résultats**

| Commande | Effet |
|---|---|
| `python -m src.main report` | Envoie le bilan du jour sur Discord |
| `python -m src.main roi [--days 7]` | Bénéfice et ROI des reventes, jour par jour |
| `python -m src.main sniper-report [--hours 24] [--summary] [--breakdown]` | Toutes les enchères misées ; `--breakdown` : taux de victoire par critère |
| `python -m src.main journal [--since 6h] [--ev stats,enchere] [--events]` | Lignes JSON du journal détaillé (`90m`, `6h`, `2j`) |

**Marché et prix**

| Commande | Effet |
|---|---|
| `python -m src.main prices "Titre exact" [--shiny]` | Ventes observées d'une carte, estimation du bot, ventes en cours |
| `python -m src.main market [--pages 4] [--sort ending_soon] [--rarity UR]` | Où sont les fins imminentes dans le marché |
| `python -m src.main hours [--rarity UR,L]` | À quelle heure de fin les enchères se vendent le mieux |
| `python -m src.main gifs` | Les cartes GIF se vendent-elles mieux ? |
| `python -m src.main sniper-backtest [--days 7] [--capital 20000] [--resale 1] [--rarity L]` | Autres réglages rejoués sur les ventes observées, sans requête au site |

**Collection et inventaire**

| Commande | Effet |
|---|---|
| `python -m src.main collection` | Cartes possédées et manquantes |
| `python -m src.main etiquettes` | Thèmes de toute la collection, cartes sans thème, étiquettes à créer, sans rien écrire |
| `python -m src.main inventory scan` | Ce qui serait vendu ou défaussé |
| `python -m src.main inventory report [--lookups N]` | Toute la collection cotée, détail dans `data/inventaire-<date>.csv` |
| `python -m src.main inventory list --apply` | **Écrit** : programme la vente des doublons (simulé si `DRY_RUN=true`) |
| `python -m src.main inventory discard --apply` | **Écrit** : défausse (simulée si `DRY_RUN=true`) |
| `python -m src.main reset-simulation [--apply]` | Compte les mises et ventes simulées ; **avec `--apply`, les efface** de la base |

## Outils

À lancer sur un poste, hors du conteneur, avec les dépendances de [développement](DEVELOPPEMENT.md#lancer-les-tests).

| Commande | Effet |
|---|---|
| `python -m src.tools.parse_curl [--name ROUTE --write] [--env] [--hot] < requete.curl` | Importe une requête copiée depuis les DevTools (« Copier comme cURL (bash) ») : corrige une route dans `config/endpoints.local.yaml`, met à jour `.env` (`--env`) ou dépose le cookie à chaud (`--hot`) |
| `python -m src.tools.browser_login [--record]` | Ouvre un navigateur (Playwright, poste avec écran) où tu te connectes toi-même, puis écrit le cookie dans `data/session_cookie.txt`. `--record` : enregistre aussi les appels `/api/` dans `data/inspect/capture/` |
| `python -m src.tools.gif_list` | Régénère `config/gifs_animes.txt` depuis Wikipédia (aucune requête au site du jeu) |

`browser_login` demande en plus `pip install -r requirements-tools.txt && playwright install chromium`.
