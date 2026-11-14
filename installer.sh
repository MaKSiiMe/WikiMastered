#!/usr/bin/env bash
# Installe WikiMastered avec Docker Compose, ou le reconstruit s'il est déjà installé.
#
#   ./installer.sh
#
# Demande la session du site et le webhook Discord (Entrée garde la valeur déjà dans .env),
# lance le bot en simulation (DRY_RUN=true) et vérifie la session. Relançable sans risque :
# il ne touche à rien d'autre dans .env. Guide : docs/INSTALLATION.md
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# --- Docker ---------------------------------------------------------------------------
command -v docker >/dev/null || die "Docker est introuvable. Installe-le : https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || die "Docker Compose (la commande « docker compose ») est introuvable."
docker info >/dev/null 2>&1 || die "Pas d'accès à Docker. Lance le script avec sudo, ou ajoute-toi au groupe docker :
  sudo usermod -aG docker \$USER   (puis déconnecte-toi et reconnecte-toi)"

# --- .env -----------------------------------------------------------------------------
fresh=0
if [ ! -f .env ]; then
    cp .env.example .env
    fresh=1
    say "Fichier .env créé à partir de .env.example."
fi
chmod 600 .env  # il contient la session de ton compte

# Valeur actuelle d'une variable de .env, sans apostrophes ; vide si c'est encore l'exemple.
current() {
    local value
    value=$(grep -m1 "^$1=" .env | cut -d= -f2- || true)
    value=${value#\'}; value=${value%\'}
    case "$value" in *xxxxxxxx* | *...) value="" ;; esac
    printf '%s' "$value"
}

# Écrit NOM='valeur' dans .env, à la place de la ligne existante (commentée ou non).
set_var() {
    local tmp
    tmp=$(mktemp .env.XXXXXX)
    NAME="$1" VALUE="$2" awk '
        BEGIN { n = ENVIRON["NAME"]; line = n "=\047" ENVIRON["VALUE"] "\047" }
        !done && $0 ~ "^(# *)?" n "=" { print line; done = 1; next }
        { print }
        END { if (!done) print line }
    ' .env > "$tmp"
    mv "$tmp" .env
    chmod 600 .env
}

# Retire ce qu'on recopie souvent avec la valeur : espaces, « cookie: » devant, guillemets autour.
clean() {
    local value=$1
    value=${value#"${value%%[![:space:]]*}"}
    value=${value%"${value##*[![:space:]]}"}
    shopt -s nocasematch
    if [[ "$value" =~ ^$2[[:space:]]*:[[:space:]]*(.*)$ ]]; then value=${BASH_REMATCH[1]}; fi
    shopt -u nocasematch
    value=${value#[\"\']}; value=${value%[\"\']}
    printf '%s' "$value"
}

# Demande une valeur ; Entrée garde l'actuelle. read -e (readline) : un cookie dépasse
# souvent 4 096 caractères, la limite d'une ligne tapée dans un terminal sans readline.
ask() {
    local name=$1 prompt=$2 prefix=$3 check=$4 hint=$5 value old
    old=$(current "$name")
    [ "$fresh" = 1 ] && old=""  # première installation : tout demander, même le user-agent d'exemple
    while true; do
        if [ -n "$old" ]; then
            read -r -e -p "$prompt (Entrée : garder l'actuel) : " value || die "Installation interrompue."
            [ -z "$value" ] && return 0
        else
            read -r -e -p "$prompt : " value || die "Installation interrompue."
        fi
        value=$(clean "$value" "$prefix")
        if [[ "$value" == *"'"* ]]; then
            echo "  Cette valeur contient une apostrophe : recopie-la depuis les DevTools."
        elif [[ "$value" =~ $check ]]; then
            set_var "$name" "$value"
            return 0
        else
            echo "  $hint"
        fi
    done
}

say "1/3  Session du bot"
cat <<'EOF'
Dans une FENÊTRE PRIVÉE, connecte-toi sur https://www.wiki-masters.com, puis F12 > Réseau,
recharge la page, filtre sur « wikibidous » et clique sur la requête /api/wikibidous.
Dans « En-têtes de requête », copie la valeur de « cookie », puis celle de « user-agent ».
Ensuite, ferme la fenêtre SANS te déconnecter. (Détail : docs/INSTALLATION.md, étape 1)
EOF
ask WM_COOKIE "Cookie" "cookie" "sb-[^=]*-auth-token" \
    "Ce n'est pas le bon cookie : il doit contenir « sb-…-auth-token »."
ask WM_USER_AGENT "User-agent" "user-agent" "^Mozilla/" \
    "Un user-agent commence par « Mozilla/ »."

say "2/3  Webhook Discord"
echo "Sur ton serveur Discord : Modifier le salon > Intégrations > Webhooks > Nouveau webhook > Copier l'URL."
ask DISCORD_WEBHOOK_URL "URL du webhook" "url" "^https://([a-z]+\.)?discord(app)?\.com/api/webhooks/" \
    "Une URL de webhook commence par « https://discord.com/api/webhooks/ »."

# --- Lancement ------------------------------------------------------------------------
say "3/3  Construction et lancement (une à deux minutes la première fois)"
mkdir -p data backups
# Le conteneur tourne sous l'UID 1000 : il doit pouvoir écrire dans data/ et backups/.
if [ "$(uname -s)" = Linux ] && { [ "$(stat -c %u data)" != 1000 ] || [ "$(stat -c %u backups)" != 1000 ]; }; then
    echo "Droits de data/ et backups/ donnés à l'UID 1000 (celui du conteneur) :"
    if [ "$(id -u)" = 0 ]; then chown -R 1000:1000 data backups; else sudo chown -R 1000:1000 data backups; fi
fi
docker compose up -d --build

say "Vérification de la session"
sleep 5
if docker compose exec -T wikimastered python -m src.main check; then
    say "✓ WikiMastered tourne, en simulation : il ne mise et ne vend rien pour de vrai."
    cat <<'EOF'
Discord doit afficher « [DRY RUN] Bot démarré ».

Ensuite :
  - choisis ce que le bot doit faire : docs/INSTALLATION.md, « Choisir les réglages » ;
  - modifie .env, puis relance ./installer.sh (ou docker compose up -d) ;
  - suis les logs : docker compose logs -f
EOF
else
    die "La vérification a échoué (message ci-dessus). Si la session est refusée, reprends un cookie dans une
fenêtre privée et relance ./installer.sh. Logs du bot : docker compose logs --tail 50"
fi
