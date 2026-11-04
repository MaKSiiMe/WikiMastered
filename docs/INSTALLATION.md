# Installation

Du premier lancement en simulation jusqu'au passage en réel, puis l'entretien.

- [Avant de commencer](#avant-de-commencer)
- [1. Récupérer la session du bot](#1-récupérer-la-session-du-bot)
- [2. Créer le webhook Discord](#2-créer-le-webhook-discord)
- [3. Lancer le bot](#3-lancer-le-bot)
- [4. Vérifier](#4-vérifier)
- [5. Choisir les réglages](#5-choisir-les-réglages)
- [6. Passer en réel](#6-passer-en-réel)
- [Commandes Discord et salons](#commandes-discord-et-salons)
- [Entretien](#entretien)
- [Dépannage](#dépannage)

## Avant de commencer

Il te faut :

- une machine allumée en permanence avec Docker et Docker Compose, ou Portainer ;
- un serveur Discord sur lequel tu peux créer des webhooks ;
- un compte WikiMasters, en ayant lu l'[avertissement du README](../README.md#wikimastered).

**Fais un fork plutôt que de déployer ce dépôt directement**, surtout avec Portainer et ses mises à jour automatiques. Sinon, tout ce qui arrive sur `main` ici s'exécuterait tout seul sur ta machine, là où se trouve la session de ton compte. Avec un fork, tu choisis quand récupérer les nouveautés (*Sync fork* sur GitHub).

**Protège tes secrets.** Le cookie donne un accès complet à ton compte :

- ne colle jamais le cookie, `.env` ou le contenu de `data/` dans une issue, sur Discord ou ailleurs ;
- `.env` et `data/` sont ignorés par git : ne force pas leur ajout, car tout ce que tu pousses sur un fork public est public ;
- un webhook Discord ou un token de bot qui fuit se régénère côté Discord ; un cookie qui fuit se révoque en changeant ton mot de passe.

## 1. Récupérer la session du bot

**Le bot doit avoir sa propre session.** Le jeton de renouvellement ne sert qu'une fois : si ton navigateur et le bot partagent la même session, le premier qui la renouvelle déconnecte l'autre.

1. Ouvre une **fenêtre de navigation privée** et connecte-toi sur <https://www.wiki-masters.com>.
2. Ouvre les DevTools (F12) > *Réseau*, recharge la page, filtre sur `wikibidous` et clique sur la requête `/api/wikibidous`. Dans *En-têtes de requête*, copie la valeur complète de `cookie` (elle contient `sb-…-auth-token`, parfois en `.0`, `.1` : prends tout) et celle de `user-agent`.
3. Filtre ensuite sur `supabase.co`, clique sur n'importe quelle requête et copie l'en-tête `apikey` (une longue chaîne qui commence par `eyJ`). Cette clé est envoyée à tous les visiteurs du site : ce n'est pas un mot de passe.
4. **Ferme la fenêtre sans cliquer sur « Se déconnecter ».** La session reste valable pour le bot. Évite aussi « Se déconnecter » dans ton navigateur habituel : Supabase ferme alors en principe toutes les sessions du compte, celle du bot comprise.

Une session reste valable tant qu'elle n'est pas révoquée : déconnexion de toutes les sessions, changement de mot de passe, ou jeton consommé par un autre appareil.

> [!IMPORTANT]
> `SUPABASE_ANON_KEY` (l'`apikey` ci-dessus) est facultative pour démarrer, mais sans elle le bot ne renouvelle pas sa session et la **garde des sanctions**, les étiquettes et les succès ne fonctionnent pas. Renseigne-la dès le début.

## 2. Créer le webhook Discord

Salon voulu > ⚙️ *Modifier le salon* > *Intégrations* > *Webhooks* > *Nouveau webhook* > *Copier l'URL du webhook*.

Un seul salon suffit pour commencer. Tu pourras [répartir les messages par salon](#un-salon-par-catégorie) plus tard. Un second webhook dans un salon réservé aux alertes rouges (`DISCORD_ALERT_WEBHOOK_URL`) est vite utile : c'est le seul salon à laisser sonner.

## 3. Lancer le bot

### Avec Docker Compose

```bash
git clone https://github.com/<ton-pseudo>/WikiMastered.git && cd WikiMastered
cp .env.example .env
```

Dans `.env`, remplis `WM_COOKIE` et `WM_USER_AGENT` (entre apostrophes), `DISCORD_WEBHOOK_URL`, et décommente `SUPABASE_ANON_KEY`. Laisse `DRY_RUN=true`. Puis :

```bash
mkdir -p backups && sudo chown -R 1000:1000 data backups   # le conteneur tourne sous l'UID 1000
docker compose up -d --build
docker compose logs -f
```

Les commandes de ce guide se lancent alors avec `docker compose exec wikimastered <commande>`.

### Avec Portainer

Portainer > ton environnement > *Stacks* > *+ Add stack* :

| Champ | Valeur |
|---|---|
| Name | `wikimastered` |
| Build method | **Repository** |
| Repository URL | `https://github.com/<ton-pseudo>/WikiMastered` |
| Repository reference | `refs/heads/main` |
| Compose path | `portainer-stack.yml` |
| Authentication | désactivé (un dépôt public n'en a pas besoin) |
| GitOps updates | activé, mécanisme **Polling**, intervalle `5m` |

Pourquoi Polling : Portainer vérifie lui-même `main` et redéploie s'il y a du nouveau, sans rien ouvrir sur internet. Le mécanisme *Webhook* exige que GitHub puisse joindre ton Portainer, ce qui est impossible avec une adresse locale ou Tailscale. Les options *Re-pull image* et *Force redeployment* sont réservées à la version Business : laisse-les désactivées.

Dans *Environment variables*, clique sur **Advanced mode** et colle, une variable par ligne, **sans apostrophes** :

```
DRY_RUN=true
WM_COOKIE=<le cookie de la fenêtre privée>
WM_USER_AGENT=<le user-agent>
SUPABASE_ANON_KEY=<la valeur de apikey>
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Puis **Deploy the stack** : Portainer clone le dépôt et construit l'image (une à deux minutes la première fois).

Les commandes de ce guide se lancent dans *Containers* > `wikimastered` > *Console* (*Command* `/bin/sh` > *Connect*).

> [!NOTE]
> Après un changement de variable, clique sur **Pull and redeploy**. *Save settings* enregistre seulement les valeurs : le conteneur garde les anciennes jusqu'au prochain déploiement.

## 4. Vérifier

1. Le conteneur `wikimastered` passe à **healthy** au bout d'une à deux minutes.
2. Discord reçoit « [DRY RUN] Bot démarré », avec l'étiquette ⚙️ Système.
3. Dans la console :
   ```sh
   python -m src.main check      # session OK, joueur, solde, « renouvellement automatique actif »
   python -m src.main inspect    # chaque route doit afficher « OK : conforme »
   ```

Si `inspect` affiche `ÉCART` ou `ERREUR`, le site a sans doute changé : voir [Corriger une route](DEVELOPPEMENT.md#corriger-ou-ajouter-une-route).

## 5. Choisir les réglages

Ce sont les réglages les plus utiles au départ. Tous les autres sont commentés dans [`.env.example`](../.env.example).

| Réglage | Par défaut | À savoir |
|---|---|---|
| `SNIPER_RESALE_RARITIES` | `UR,L` | Raretés achetées pour la revente |
| `SNIPER_MAX_BID` | `0` (aucun) | Plafond absolu d'une mise. Sans lui, le budget d'une position est capital × 0,9 / 5 |
| `SNIPER_DAILY_BUDGET` | `0` (aucun) | Un budget trop petit bloque presque tous les achats : il doit dépasser le prix d'une position |
| `COLLECTION_SETS` | vide | Collections à compléter, ex. `jours,gifs` ([détail](FONCTIONNEMENT.md#collections)). Vide : aucune |
| `COLLECTION_MAX_PRICE` | `50` | Mise maximale pour une carte de collection, en WB |
| `COLLECTION_TAG` | `date` | Étiquette des jours. Crée-la une fois sur le site |
| `PROTECTED_TAGS` | vide | Étiquettes jamais vendues ni défaussées, ex. `date,GIF` |
| `AUTO_LIST` | `false` | Vente automatique des doublons cotés au moins `SELL_MIN_VALUE` (15 WB) |
| `AUTO_DISCARD` | `false` | Défausse des doublons sans valeur. Demande aussi `ACHIEVEMENT_MILESTONES` (`0` si tu n'as aucun palier à protéger) |
| `ENABLE_THEME_TAGS` | `false` | Étiquettes de thème ([détail](FONCTIONNEMENT.md#étiquettes-de-thème)) |
| `MANUAL_RESALE_TAG` | `revente` | Une carte que tu étiquettes ainsi est revendue par le bot |

**Réel ou simulé**, écriture par écriture :

| Réglage | Effet |
|---|---|
| `DRY_RUN=true` | Tout est simulé |
| `DRY_RUN=true` + `LIVE_WRITES=ventes` | Seuls les groupes listés sont réels |
| `DRY_RUN=false` + `SIMULATE=sniper` | Tout est réel sauf les groupes listés |
| `DRY_RUN=false` | Tout est réel |

Groupes : `sniper`, `collection`, `ventes`, `defausse`, `tags`, `themes`, `succes`, `inventory` (ventes + défausse).

## 6. Passer en réel

Par étapes, car le bot dépense alors de vrais WB. Après chaque changement, redémarre : `docker compose up -d` avec Compose, **Pull and redeploy** avec Portainer.

1. **Observe un ou deux jours en simulation.** Regarde les messages `[DRY RUN]`, le bilan du soir et le récap des enchères de 08:00. `python -m src.main inventory scan` montre ce que l'inventaire ferait.
2. **`DRY_RUN=false` et `SIMULATE=sniper`** : ventes, collection et étiquettes deviennent réelles ; seules les mises des positions du sniper restent simulées. Ajoute `AUTO_LIST=true` pour vendre les doublons.
3. **Retire `SIMULATE`** quand le bloc « Validation de la simulation » du bilan dit que le passage en réel est envisageable : au moins 10 reventes simulées, et un bilan positif même en comptant les invendues comme perdues.

À la première écriture réelle de chaque type, Discord affiche « Première réponse réelle : … ». Les 3 premières réponses sont gardées dans `data/inspect/writes/` pour vérifier leur format.

## Commandes Discord et salons

### Activer les commandes

Pour interroger le bot depuis Discord (`/bilan`, `/snipe`… voir [Commandes](COMMANDES.md#discord)) :

1. [Portail développeur Discord](https://discord.com/developers/applications) > **New Application**. Onglet **Bot** : **Reset Token** et copie-le ; laisse les *Privileged Gateway Intents* désactivés et décoche **Public Bot**.
2. Onglet **OAuth2** > **URL Generator** : coche `bot` et `applications.commands`, puis la permission `Send Messages`. Ouvre l'URL et choisis ton serveur.
3. Dans Discord, *Paramètres* > *Avancés* > **Mode développeur**. Clic droit sur ton pseudo > *Copier l'identifiant utilisateur* ; clic droit sur le serveur > *Copier l'identifiant du serveur*.

```
DISCORD_BOT_TOKEN=<le token>
DISCORD_OWNER_ID=<ton identifiant utilisateur>
DISCORD_GUILD_ID=<identifiant du serveur>
```

Seul `DISCORD_OWNER_ID` peut se servir des commandes. Sans `DISCORD_GUILD_ID`, elles mettent jusqu'à une heure à apparaître.

### Un salon par catégorie

Crée un salon et son webhook pour chaque catégorie voulue, puis ajoute son URL. Une catégorie sans salon garde le salon principal (ou d'activité).

| Variable | Le salon reçoit |
|---|---|
| `DISCORD_ALERT_WEBHOOK_URL` | Les alertes, à laisser sonner. Les alertes critiques y restent toujours |
| `DISCORD_WEBHOOK_SNIPER` | Récap des enchères de 08:00, budget, mises à vérifier |
| `DISCORD_WEBHOOK_VENTES` | Un message par position (achetée → en vente → vendue), ventes, échanges |
| `DISCORD_WEBHOOK_COLLECTION` | Cartes de collection, étiquettes, succès |
| `DISCORD_WEBHOOK_BILAN` | Bilan de 23:00, récap des workers toutes les 4 h |
| `DISCORD_WEBHOOK_SYSTEME` | Démarrage, session, réseau, sauvegardes |

`DISCORD_MENTION=@here` ajoute une mention aux alertes critiques. Les commandes répondent dans le salon où tu les tapes : un salon `#commandes` n'a pas besoin de webhook.

## Entretien

### Mises à jour

| Installation | Comment |
|---|---|
| Docker Compose | `git pull && docker compose up -d --build` |
| Portainer | Automatique dès que la branche suivie change (après *Sync fork*, pour un fork), dans les 5 minutes. À la main : stack > **Pull and redeploy** |

Chaque mise à jour redémarre le bot. Le message « Bot démarré » le confirme. Si l'ancien code tourne encore avec Portainer, supprime `wikimastered:latest` dans *Images* et relance *Pull and redeploy*.

### Session perdue

Avec `SUPABASE_ANON_KEY`, le bot renouvelle sa session tout seul. Si Discord affiche « Session expirée » ou « Session : renouvellement refusé », refais l'[étape 1](#1-récupérer-la-session-du-bot), puis au choix :

- sans redémarrer, dans la console du conteneur : `cat > /app/data/session_cookie.txt`, colle le cookie, Entrée puis Ctrl+D. Le bot le charge en moins de 30 s ;
- ou remplace `WM_COOKIE` et redémarre.

### Sanction du site

Au premier avertissement de triche ou à la première sanction lue sur ton profil, la garde arrête le bot : plus aucune requête ne part, même après un redémarrage. Regarde d'abord ce que le site reproche à ton compte. Relancer le bot peut aggraver la sanction.

Pour le relancer malgré tout : `rm data/HALT` dans la console du conteneur, puis redémarre.

### Sauvegardes

Chaque nuit à 04:30 (`BACKUP_TIME`), le bot copie la base à chaud, vérifie la copie, la compresse et garde les 7 plus récentes (`BACKUP_KEEP`), dans un dossier séparé des données. `/sauvegarde` sur Discord en fait une tout de suite, et `/etat` montre la dernière.

Elles restent sur la même machine. Pour en garder une ailleurs : `docker cp wikimastered:/app/backups/<archive> .`

**Restaurer** (base perdue ou abîmée), bot arrêté, en remplaçant le nom de l'archive :

```sh
# Docker Compose
docker compose stop
sudo sh -c 'gunzip -c backups/wikimastered-AAAA-MM-JJ-HHMM.db.gz > data/wikimastered.db && rm -f data/wikimastered.db-wal data/wikimastered.db-shm && chown 1000:1000 data/wikimastered.db'
docker compose start
```

```sh
# Portainer : stack arrêtée (Stop this stack), sur le serveur
docker volume ls | grep wikimastered                      # les noms exacts des deux volumes
docker run --rm -v wikimastered_wikimastered_backups:/b -v wikimastered_wikimastered_data:/d alpine sh -c \
  'ls /b; gunzip -c /b/wikimastered-AAAA-MM-JJ-HHMM.db.gz > /d/wikimastered.db && rm -f /d/wikimastered.db-wal /d/wikimastered.db-shm && chown 1000:1000 /d/wikimastered.db'
```

Les réglages changés sur Discord, les pauses et les cartes gardées sont dans la base : ils reviennent avec elle.

### Données

Tout est dans `data/` : base SQLite, journal, session. Avec Portainer, c'est le volume `wikimastered_wikimastered_data` : il survit aux redéploiements et n'est effacé que si tu supprimes le volume lui-même.

### Arrêter

`docker compose stop`, ou stack > *Stop* avec Portainer. Le bot termine proprement et envoie « Bot arrêté ».

## Dépannage

| Symptôme | Cause et solution |
|---|---|
| « Garde des sanctions inactive » au démarrage | `SUPABASE_ANON_KEY` manquante. Refais l'[étape 1](#1-récupérer-la-session-du-bot) |
| « Session : renouvellement refusé » | Le navigateur et le bot partagent la même session. Reprends un cookie dans une fenêtre privée, [étape 1](#1-récupérer-la-session-du-bot) |
| « Commandes Discord inactives » | `DISCORD_OWNER_ID` manquant ou pas un nombre : il faut ton identifiant, pas ton pseudo |
| Pas de commande `/…` dans Discord | Sans `DISCORD_GUILD_ID`, jusqu'à une heure d'attente |
| Aucun message Discord | Vérifie `DISCORD_WEBHOOK_URL` et lis les logs (`docker logs wikimastered`) : sans webhook, les messages y sont seulement recopiés |
| Le bot mise peu ou plus du tout | Regarde `/etat` : vérification anti-bot en attente, route en pause, sniper en pause. Sinon, `python -m src.main sniper-report --breakdown`. Il faut aussi quelques jours de ventes observées avant que le bot sache estimer les cartes |
| Aucune collection dans le récap | `COLLECTION_SETS` est vide |
| Une étiquette n'est pas posée | Elle doit d'abord être créée sur le site. `python -m src.main etiquettes` liste celles qui manquent |
| Conteneur `unhealthy` | Le bot n'écrit plus son `heartbeat` : lis les logs. Avec Docker Compose, vérifie les droits de `data/` (UID 1000) |
| Sauvegarde impossible (Docker Compose) | `backups/` doit appartenir à l'UID 1000 : `sudo chown -R 1000:1000 backups` |

> [!NOTE]
> Si plusieurs d'entre vous font tourner le bot, vous visez les mêmes cartes avec les mêmes règles : vous ferez monter vos prix mutuellement. Pensez à varier `SNIPER_RESALE_RARITIES`, les collections ou les plafonds.
