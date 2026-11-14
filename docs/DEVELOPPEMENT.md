# Développement

Pour lire le code, le modifier, ou corriger le bot quand le site change. Pour les règles de décision du bot, voir [Fonctionnement](FONCTIONNEMENT.md).

## Lancer les tests

Python 3.12 :

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest==8.4.2 pytest-asyncio==1.2.0
.venv/bin/python -m pytest
```

Les tests n'appellent ni le site ni Discord. Ils fixent leurs propres réglages dans [`tests/conftest.py`](../tests/conftest.py), indépendants des valeurs par défaut. Une PR passe par la CI ([`ci.yml`](../.github/workflows/ci.yml)) : tests et construction de l'image Docker.

## Organisation du code

```
src/
├── main.py              lance le bot, commandes de console
├── config.py            tous les réglages (Settings), lus dans l'environnement
├── core/
│   ├── client.py        HTTP : files de requêtes, reculs, session, simulation des écritures
│   ├── endpoints.py     lit config/endpoints.yaml
│   ├── api.py           appels au site, typés
│   ├── models.py        modèles pydantic des réponses
│   ├── database.py      SQLite : ventes observées, positions, ventes programmées…
│   ├── pricing.py       estimation du prix d'une carte
│   ├── notifier.py      messages Discord par webhook
│   ├── journal.py       logs de la console et journal JSONL
│   ├── stats.py         relevé des passages et des requêtes
│   └── wikidata.py      lecture de Wikidata pour les thèmes
├── workers/             une boucle par worker (voir README), base commune dans base.py
├── discord_bot.py       commandes slash et boutons : pilotage
├── discord_cards.py     commandes slash et boutons : cartes et ventes
├── tools/               parse_curl, browser_login, gif_list ; inspect_api sert la commande inspect
└── …                    bilan (account.py), récap (recap.py), sauvegarde (backup.py), échanges (trades.py)…
config/
├── endpoints.yaml       routes du site
├── collections.yaml     collections à compléter (gifs_animes.txt : liste des GIF)
└── etiquettes.yaml      règles des thèmes
```

Les docstrings en tête des modules, en particulier `auction_sniper.py`, `inventory_manager.py` et `pricing.py`, expliquent les choix et les relevés qui les justifient.

## Garde-fous à respecter

- **Toute écriture passe par `client.mutate()`** : c'est lui qui applique `DRY_RUN`, `LIVE_WRITES` et `SIMULATE`. Une nouvelle écriture doit rejoindre un groupe de `WRITE_GROUPS` (`src/config.py`).
- **Une écriture n'est jamais rejouée.** Si sa réponse est perdue, elle est marquée « incertaine » et le bot relit le résultat sur le site.
- **Un worker ne fait pas tomber les autres** : il hérite de `Worker` (`src/workers/base.py`) et chaque tâche est relancée à part.
- **Les requêtes passent par la file commune.** Ne réduis pas les écarts entre requêtes, et marque comme requêtes de fond (`background=True`) les lectures qui ne sont pas urgentes.
- **La vérification humaine du site reste manuelle.** Le bot la détecte, prévient et attend. Il ne doit pas chercher à la contourner.

## Ajouter un réglage

1. Ajoute le champ dans `Settings` (`src/config.py`) ; son nom en majuscules devient la variable d'environnement.
2. Documente-le dans [Réglages](REGLAGES.md), avec sa valeur par défaut ; dans [`.env.example`](../.env.example) aussi s'il sert à presque tout le monde. [`tests/test_docs.py`](../tests/test_docs.py) échoue sinon, comme pour une commande non documentée ou un lien cassé.
3. Mets à jour [Fonctionnement](FONCTIONNEMENT.md) ou [Installation](INSTALLATION.md) s'il change un comportement décrit.

Un défaut qui change modifie le comportement de toute instance qui n'a pas renseigné ce réglage : préviens-les, et ajoute l'ancienne valeur à `tests/conftest.py` si des tests en dépendent.

## Ajouter un worker

Hérite de `Worker`, écris `step()`, qui retourne le délai avant le prochain passage en secondes, et ajoute-le dans `run_bot` (`src/main.py`). Son `name` sert aux logs, au récap et à `/pause`.

## Ajouter une commande Discord

Ajoute-la dans `src/discord_bot.py` ou `src/discord_cards.py`, puis dans [Commandes](COMMANDES.md). La vérification de `DISCORD_OWNER_ID` s'applique à tout l'arbre de commandes.

## Corriger ou ajouter une route

Le site peut changer sans prévenir. `python -m src.main inspect` compare ses réponses aux modèles.

| Besoin | Commande |
|---|---|
| Corriger une route | Copier la requête depuis les DevTools (« Copier comme cURL (bash) »), puis `python -m src.tools.parse_curl --name place_bid --write < requete.curl` : écrit `config/endpoints.local.yaml`, prioritaire et ignoré par git |
| Découvrir une route | `python -m src.tools.browser_login --record`, puis naviguer : les appels vont dans `data/inspect/capture/` |

Une correction durable va dans [`config/endpoints.yaml`](../config/endpoints.yaml), et une réponse différente dans `src/core/models.py`. Chaque route y cite sa source : garde cette habitude, et n'invente aucune route.

## Ajouter une collection

Dans [`config/collections.yaml`](../config/collections.yaml) : un nom, une étiquette, les titres exacts des articles (ou un fichier avec un titre par ligne), puis active sa clé dans `COLLECTION_SETS`. Pour les GIF, `python -m src.tools.gif_list` régénère la liste.
