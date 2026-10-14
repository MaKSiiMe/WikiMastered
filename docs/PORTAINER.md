# Déploiement avec Portainer

Le bot tourne comme une stack Portainer construite depuis ce dépôt GitHub privé. Aucun accès SSH n'est nécessaire.

## 1. Préparer ce qu'il faut (sur ton PC)

**Token GitHub** (le dépôt est privé)
GitHub > photo de profil > *Settings* > *Developer settings* > *Personal access tokens* > *Fine-grained tokens* > *Generate new token*.
- *Repository access* : *Only select repositories* > `MaKSiiMe/WikiMastered`
- *Permissions* > *Repository permissions* > *Contents* : **Read-only**
- Copie le token (`github_pat_…`) : il ne s'affiche qu'une fois.

**Webhook Discord**
Sur ton serveur Discord, ouvre le salon voulu > ⚙️ *Modifier le salon* > *Intégrations* > *Webhooks* > *Nouveau webhook* > *Copier l'URL du webhook*.
Optionnel : crée un deuxième webhook dans un salon dédié aux alertes critiques.

**Cookie et User-Agent WikiMasters**
1. Connecte-toi sur https://www.wiki-masters.com, puis ouvre DevTools (F12) > onglet *Réseau*.
2. Recharge la page et filtre sur `wikibidous`. Clique sur la requête `/api/wikibidous`.
3. Dans *En-têtes de requête*, copie :
   - la valeur complète de `cookie`. Elle doit contenir `sb-…-auth-token`, parfois en plusieurs morceaux `.0`, `.1` : prends tout ;
   - la valeur de `user-agent`.

## 2. Créer la stack

Portainer > ton environnement > *Stacks* > *+ Add stack*

| Champ | Valeur |
|---|---|
| Name | `wikimastered` |
| Build method | **Repository** |
| Repository URL | `https://github.com/MaKSiiMe/WikiMastered` |
| Repository reference | `refs/heads/main` |
| Compose path | `portainer-stack.yml` |
| Authentication | activé : username `MaKSiiMe`, Personal Access Token = le token GitHub |
| GitOps updates | désactivé pour l'instant |

## 3. Variables d'environnement

Dans *Environment variables*, clique sur **Advanced mode** et colle ce bloc, une variable par ligne, **sans apostrophes** :

```
DRY_RUN=true
WM_COOKIE=sb-xxxxxxxx-auth-token=base64-...
WM_USER_AGENT=Mozilla/5.0 (...)
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
WIKIMEDIA_USER_AGENT=WikiMasteredBot/1.0 (https://github.com/MaKSiiMe; ton-email)
```

Optionnel, mais utile dès le départ :

```
DISCORD_ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_MENTION=@here
SNIPER_DAILY_BUDGET=300
SNIPER_RARITY_CAPS={"SR": 15, "UR": 40, "L": 120}
ACHIEVEMENT_MILESTONES=100,250,500,1000,2500,5000
```

Toutes les autres variables sont décrites dans [.env.example](../.env.example). Elles s'ajoutent de la même façon.

## 4. Déployer

Clique sur **Deploy the stack**. Portainer clone le dépôt puis construit l'image, ce qui prend une à deux minutes la première fois.

## 5. Vérifier

1. *Containers* > `wikimastered` : l'état doit passer à **healthy** au bout d'une à deux minutes.
2. Icône *Logs* : tu dois voir `Démarrage en mode DRY RUN … joueur <uuid>`, et Discord doit recevoir **« [DRY RUN] Bot démarré »**.
3. Icône *>_ Console* > *Command* `/bin/sh` > *Connect*, puis :
   ```sh
   python -m src.main check      # Session OK — joueur … — solde … WB
   python -m src.main inspect    # chaque route doit afficher « OK : conforme »
   ```
   Si `inspect` affiche `ÉCART` ou `ERREUR`, envoie-moi sa sortie : il faudra ajuster une route ou un modèle.

## 6. Observer en simulation (1 à 2 jours)

Sur Discord, regarde passer les « Enchère simulée », « Tirage remarquable », etc. Ajuste ensuite les seuils dans l'onglet de la stack > *Environment variables* > **Update the stack**.

Pour voir ce que le module inventaire ferait, ouvre la *Console* :

```sh
python -m src.main inventory scan
```

## 7. Passer en réel

Stack `wikimastered` > *Environment variables* > `DRY_RUN=false` > **Update the stack**.

Pour réactiver plus tard la vente ou la défausse automatiques : `AUTO_LIST=true` et `AUTO_DISCARD=true`. Ces deux options sont désactivées par défaut.

## Session autonome (recommandé)

Sans cette étape, le bot se met en pause dès que sa session expire, et attend que tu lui donnes un nouveau cookie. Avec elle, il renouvelle lui-même sa session, comme le fait ton navigateur.

1. **Donne au bot sa propre session.** Le jeton de renouvellement ne sert qu'une fois. Si ton navigateur habituel et le bot partagent la même session, le premier qui la renouvelle invalide l'autre.
   - Ouvre une **fenêtre de navigation privée** (ou un autre navigateur) et connecte-toi sur https://www.wiki-masters.com.
   - Récupère le cookie comme à l'étape 1.
   - **Ferme la fenêtre sans cliquer sur « Se déconnecter ».** Fermer une fenêtre privée efface le cookie de ton côté, mais la session reste valable pour le bot.
   - Plus tard, évite aussi « Se déconnecter » dans ton navigateur habituel. Par défaut, Supabase ferme alors **toutes** les sessions du compte, celle du bot comprise. Ce comportement n'est pas encore vérifié sur ce site.
2. **Récupère la clé publique Supabase**, dans la même fenêtre : DevTools (F12) > *Réseau* > filtre `supabase.co` > n'importe quelle requête > *En-têtes de requête* > valeur de `apikey` (une longue chaîne qui commence par `eyJ`). Cette clé est envoyée à tous les visiteurs du site : ce n'est pas un mot de passe.
3. Dans la stack > *Environment variables* :
   ```
   WM_COOKIE=<le cookie de la fenêtre privée>
   SUPABASE_ANON_KEY=<la valeur de apikey>
   ```
   puis **Update the stack**.
4. Vérifie dans la *Console* : `python -m src.main check` doit afficher `renouvellement automatique actif`. Le message Discord « Bot démarré » l'indique aussi.

Si Supabase refuse un jour le renouvellement, Discord affiche « Session : renouvellement refusé ». Refais alors les étapes 1 et 3.

## Entretien

**Session expirée.** Discord affiche l'alerte « Session expirée » et le bot se met en pause. Récupère un nouveau cookie (étape 1), remplace `WM_COOKIE` dans la stack, puis **Update the stack**. Autre possibilité, sans redémarrage : dans la *Console*, lance

```sh
cat > /app/data/session_cookie.txt
```

colle le cookie, appuie sur Entrée puis Ctrl+D. Le bot le charge en moins de 30 s.

**Mettre à jour le code.** Stack > **Pull and redeploy**. Si les logs montrent que l'ancien code tourne encore, Portainer a réutilisé l'image existante au lieu de la reconstruire. Dans ce cas : *Images* > supprime `wikimastered:latest`, puis relance *Pull and redeploy*.

**Données.** Elles sont dans le volume `wikimastered_wikimastered_data` (*Volumes*) : base SQLite, logs et session. Ce volume survit aux redéploiements et à la suppression du conteneur. Il n'est effacé que si tu supprimes le volume lui-même.

**Arrêter.** Stack > *Stop*. Le bot termine proprement et envoie « Bot arrêté » sur Discord.
