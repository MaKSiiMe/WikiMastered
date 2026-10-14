# Déploiement avec Portainer

Le bot tourne comme une stack Portainer construite depuis ce dépôt GitHub privé, sans accès SSH. Une fois en place, il se met à jour tout seul à chaque fusion sur `main`.

## 1. Préparer (sur ton PC)

**Token GitHub** (le dépôt est privé). GitHub > photo de profil > *Settings* > *Developer settings* > *Personal access tokens* > *Fine-grained tokens* > *Generate new token* :
- *Repository access* : *Only select repositories* > `MaKSiiMe/WikiMastered` ;
- *Repository permissions* > *Contents* : **Read-only** ;
- copie le token (`github_pat_…`) : il ne s'affiche qu'une fois.

**Webhook Discord.** Salon voulu > ⚙️ *Modifier le salon* > *Intégrations* > *Webhooks* > *Nouveau webhook* > *Copier l'URL du webhook*. Facultatif : un second webhook dans un salon réservé aux alertes rouges.

**Session WikiMasters, propre au bot.** Le jeton de renouvellement de session ne sert qu'une fois : si ton navigateur et le bot partagent la même session, le premier qui la renouvelle déconnecte l'autre.
1. Ouvre une **fenêtre de navigation privée** et connecte-toi sur https://www.wiki-masters.com.
2. DevTools (F12) > *Réseau*, recharge la page, filtre sur `wikibidous` et clique sur la requête `/api/wikibidous`. Dans *En-têtes de requête*, copie la valeur complète de `cookie` (elle contient `sb-…-auth-token`, parfois en `.0`, `.1` : prends tout) et celle de `user-agent`.
3. Filtre ensuite sur `supabase.co`, clique sur n'importe quelle requête et copie l'en-tête `apikey` (une longue chaîne qui commence par `eyJ`). Cette clé est envoyée à tous les visiteurs du site : ce n'est pas un mot de passe.
4. **Ferme la fenêtre sans cliquer sur « Se déconnecter ».** La session reste valable pour le bot. Évite aussi « Se déconnecter » dans ton navigateur habituel : Supabase ferme alors en principe toutes les sessions du compte, celle du bot comprise.

## 2. Créer la stack

Portainer > ton environnement > *Stacks* > *+ Add stack* :

| Champ | Valeur |
|---|---|
| Name | `wikimastered` |
| Build method | **Repository** |
| Repository URL | `https://github.com/MaKSiiMe/WikiMastered` |
| Repository reference | `refs/heads/main` |
| Compose path | `portainer-stack.yml` |
| Authentication | activé : username `MaKSiiMe`, Personal Access Token = le token GitHub |
| GitOps updates | activé, mécanisme **Polling**, intervalle `5m` |

Pourquoi Polling : Portainer vérifie lui-même `main` et redéploie s'il y a du nouveau, sans rien ouvrir sur internet. Le mécanisme *Webhook* exige que GitHub puisse joindre ton Portainer ; une adresse Tailscale (`100.x.x.x`) ou locale ne l'est pas. Les options *Re-pull image* et *Force redeployment* sont réservées à la version Business : laisse-les désactivées.

## 3. Variables d'environnement

Dans *Environment variables*, clique sur **Advanced mode** et colle, une variable par ligne, **sans apostrophes** :

```
DRY_RUN=true
WM_COOKIE=<le cookie de la fenêtre privée>
WM_USER_AGENT=<le user-agent>
SUPABASE_ANON_KEY=<la valeur de apikey>
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
WIKIMEDIA_USER_AGENT=WikiMasteredBot/1.0 (https://github.com/MaKSiiMe; ton-email)
```

Facultatif :

```
DISCORD_ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_MENTION=@here
ACHIEVEMENT_MILESTONES=100,250,500,1000,2500,5000
COLLECTION_SETS=jours
PROTECTED_TAGS=date
```

À ne **pas** ajouter : `SNIPER_RARITY_CAPS` (achats par rareté, contraires à la stratégie : le prix suit les tendances) et un petit `SNIPER_DAILY_BUDGET` (une position coûte jusqu'à ~540 WB : un budget de 300 bloquerait presque tous les achats).

Toutes les autres variables sont décrites dans [.env.example](../.env.example).

## 4. Déployer et vérifier

1. **Deploy the stack.** Portainer clone le dépôt et construit l'image (une à deux minutes la première fois).
2. *Containers* > `wikimastered` : l'état passe à **healthy** au bout d'une à deux minutes.
3. Discord reçoit « [DRY RUN] Bot démarré », avec l'étiquette ⚙️ Système.
4. *Console* (*Command* `/bin/sh` > *Connect*) :
   ```sh
   python -m src.main check      # session OK, joueur, solde, « renouvellement automatique actif »
   python -m src.main inspect    # chaque route doit afficher « OK : conforme »
   ```
   Si `inspect` affiche `ÉCART` ou `ERREUR`, une route ou un modèle est à ajuster.

## 5. Passer en réel, par étapes

1. Observe un ou deux jours en simulation : résumés « Sniper : enchères simulées », boosters, bilan du soir. `python -m src.main inventory scan` montre ce que l'inventaire ferait.
2. `DRY_RUN=false` et `SIMULATE=sniper` : paquets, ventes de doublons, collection et étiquettes deviennent réels ; seules les mises des positions du sniper restent simulées. Ajoute `AUTO_LIST=true` pour la vente automatique des doublons.
3. Retire `SIMULATE` quand le bloc « Validation de la simulation » du bilan dit que le passage en réel est envisageable : au moins 10 reventes simulées, et un bilan positif même en comptant les invendues comme perdues.

Chaque changement : stack > *Environment variables* > **Update the stack**.

À la première écriture réelle de chaque type, Discord affiche « Première réponse réelle : … ». Les 3 premières réponses sont gardées dans le volume (`cat /app/data/inspect/writes/*.json` dans la *Console*) pour vérifier leur format.

## Entretien

**Mises à jour.** Automatiques : une PR fusionnée sur `main` est déployée dans les 5 minutes. Le message « Bot démarré » le confirme. À la main : stack > **Pull and redeploy**. Si l'ancien code tourne encore (Portainer a réutilisé l'image), supprime `wikimastered:latest` dans *Images* et relance *Pull and redeploy*.

**Session perdue.** Avec `SUPABASE_ANON_KEY`, le bot renouvelle sa session tout seul. Si Discord affiche « Session expirée » ou « Session : renouvellement refusé », refais l'étape « Session WikiMasters » ci-dessus, puis :
- soit remplace `WM_COOKIE` dans la stack et **Update the stack** ;
- soit, sans redémarrage, dans la *Console* : `cat > /app/data/session_cookie.txt`, colle le cookie, Entrée puis Ctrl+D. Le bot le charge en moins de 30 s.

Une session reste valable tant qu'elle n'est pas révoquée : déconnexion de toutes les sessions, changement de mot de passe, ou jeton consommé par un autre appareil.

**Données.** Volume `wikimastered_wikimastered_data` (*Volumes*) : base SQLite, logs et session. Il survit aux redéploiements et à la suppression du conteneur ; il n'est effacé que si tu supprimes le volume lui-même.

**Arrêter.** Stack > *Stop*. Le bot termine proprement et envoie « Bot arrêté ».
