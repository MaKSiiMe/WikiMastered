# WikiMastered

[![CI](https://github.com/MaKSiiMe/WikiMastered/actions/workflows/ci.yml/badge.svg)](https://github.com/MaKSiiMe/WikiMastered/actions/workflows/ci.yml)

Bot pour [WikiMasters](https://www.wiki-masters.com) : il repère les cartes vendues sous leur prix, les achète pour les revendre, complète tes collections et vend tes doublons. Il tourne dans Docker, passe par l'API JSON du site, sans navigateur, et rend compte sur Discord.

> [!CAUTION]
> **Ton compte est en jeu.** Les règles de la communauté WikiMasters (section 3) interdisent les bots et les scripts qui ouvrent des paquets, misent ou échangent à la place du joueur, avec des sanctions qui vont jusqu'au bannissement définitif. Ce projet automatise des mises et des ventes : il est concerné. Lis ces règles avant de te lancer. Si tu le fais tourner, le risque est pour ton compte, et rien ici ne le supprime.

Par défaut, `DRY_RUN=true` : rien n'est envoyé au site, le bot annonce seulement ce qu'il ferait, avec le préfixe `[DRY RUN]`.

## Ce que fait le bot

| Worker | Rôle | Par défaut |
|---|---|---|
| [`sniper`](src/workers/auction_sniper.py) | Achète des UR et des L sous leur prix, pour les revendre ; mise aussi pour les collections | actif |
| [`inventory`](src/workers/inventory_manager.py) | Revend les achats ; vend les doublons et défausse les cartes sans valeur si tu l'actives | actif (`AUTO_LIST`, `AUTO_DISCARD` coupés) |
| [`collection`](src/workers/collection.py) | Suit tes collections : jours de l'année, GIF animés, ou ta propre liste | dès que `COLLECTION_SETS` est rempli |
| [`etiquettes`](src/workers/themes.py) | Range la collection par thème (Personne, Pays, Musique…) grâce à Wikidata | coupé (`ENABLE_THEME_TAGS`) |
| [`succes`](src/workers/achievements.py) | Réclame les récompenses de succès, chaque soir à 22:50 | actif |
| [`echanges`](src/workers/trade_watch.py) | Évalue les échanges qu'on te propose et suggère une contre-offre ; ne répond jamais à ta place | actif |
| [`cotes`](src/workers/value_scan.py) | Cote toute la collection toutes les 12 h, pour la défausse des derniers exemplaires | actif |
| [`valeur`](src/workers/account_value.py) | Calcule la valeur du compte (`/valeur`) toutes les 6 h | actif |
| [`garde`](src/workers/profile_guard.py) | Relit ton profil et arrête tout le bot à la première sanction du site | actif avec `SUPABASE_ANON_KEY` |

Les règles détaillées (estimation des prix, mises, remises en vente…) sont dans [docs/FONCTIONNEMENT.md](docs/FONCTIONNEMENT.md).

## Ce qu'il ne fait pas

- **Il ne passe pas la vérification anti-bot.** Le site la redemande environ toutes les 9 à 10 mises acceptées. C'est à toi de la faire dans ton navigateur : le bot te prévient sur Discord et attend, et ses mises restent bloquées tant qu'elle n'est pas faite. Compte donc sur plusieurs interventions par jour si tu veux qu'il achète régulièrement.
- **Il n'ouvre pas de paquets** et ne répond pas aux échanges.
- **Il ne garantit aucun gain.** Une revente peut partir à perte, et une carte invendue finit à 1 WB ([détail](docs/FONCTIONNEMENT.md#positions--revendre)).

## Démarrer

Il te faut une machine allumée en permanence avec Docker, un serveur Discord et un compte WikiMasters.

```bash
git clone https://github.com/MaKSiiMe/WikiMastered.git && cd WikiMastered
cp .env.example .env                       # à remplir : session du site et webhook Discord
mkdir -p backups && sudo chown -R 1000:1000 data backups
docker compose up -d --build
docker compose exec wikimastered python -m src.main check
```

La récupération de la session demande quelques précautions, et Portainer est aussi possible : suis le [guide d'installation](docs/INSTALLATION.md).

## Documentation

| Page | Pour |
|---|---|
| [Installation](docs/INSTALLATION.md) | Installer, régler, passer en réel, entretenir, dépanner |
| [Commandes](docs/COMMANDES.md) | Commandes Discord (`/bilan`, `/snipe`…), console, outils |
| [Fonctionnement](docs/FONCTIONNEMENT.md) | Comprendre comment le bot décide |
| [Développement](docs/DEVELOPPEMENT.md) | Lire ou modifier le code, lancer les tests |
| [`.env.example`](.env.example) | Tous les réglages, commentés |

## Quand intervenir

Le bot tourne seul. Une alerte 🔴 sur Discord signale les cas où il faut agir :

| Alerte | Quoi faire |
|---|---|
| Vérification anti-bot demandée | Faire une mise à la main dans le navigateur, puis `/verif-faite` ou le bouton **C'est fait** |
| Session expirée ou renouvellement refusé | [Donner une nouvelle session au bot](docs/INSTALLATION.md#session-perdue) |
| Bot arrêté par la garde | Une sanction du site a été détectée : [voir ici](docs/INSTALLATION.md#sanction-du-site) avant de relancer quoi que ce soit |

## Crédits

Les routes du site ne sont pas devinées : elles viennent du code des extensions communautaires [WikiMastersTools](https://github.com/qkerman/WikiMastersTools-kzfamily), [WikiRemastered](https://github.com/Lypningeuh/WikiRemastered), [wikimasters-plus](https://github.com/ThePommespanzer/wikimasters-plus), [wikimasters-farm](https://github.com/3d3n-pyc/wikimasters-farm) et [wikimasters-bot](https://github.com/LockBlock-dev/wikimasters-bot). Thèmes tirés de [Wikidata](https://www.wikidata.org), liste des GIF tirée de [Wikipédia](https://fr.wikipedia.org).

## Licence

[MIT](LICENSE), sans aucune garantie. La police DejaVu Sans garde [sa propre licence](src/assets/DejaVuSans-LICENSE.txt). La licence couvre ce code, pas son usage sur WikiMasters : les règles du site restent les siennes.
