"""La doc reste d'accord avec le code : un réglage, une commande ou un lien oublié fait échouer la CI.

Chaque message d'échec dit quoi corriger. Les valeurs citées dans les phrases de docs/FONCTIONNEMENT.md ne sont pas
vérifiées : à relire quand un réglage par défaut change.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from src.config import Settings

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"
REGLAGES = ROOT / "docs" / "REGLAGES.md"
# .env.example ne montre que l'essentiel ; docs/REGLAGES.md les montre tous.
SETTINGS_FILES = [ENV_EXAMPLE, REGLAGES]
COMMANDS = ROOT / "docs" / "COMMANDES.md"
PAGES = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]

# Valeurs à remplacer par les tiennes : pas des valeurs par défaut.
PLACEHOLDERS = {"WM_COOKIE", "DISCORD_WEBHOOK_URL"}
# Mots en majuscules cités dans la doc qui ne sont pas des réglages : texte affiché par `inspect`, constante du code.
NOT_SETTINGS = {"ERREUR", "WRITE_GROUPS"}
# Guide d'une extension communautaire, cité comme source d'une route (config/endpoints.yaml) : pas un guide d'ici.
EXTERNAL_GUIDES = {"docs/TECHNIQUE.md"}


def settings_in(path: Path) -> dict[str, str]:
    """Variables d'un fichier de réglages, commentées ou non : nom -> valeur, sans commentaire de fin ni apostrophes."""
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if match := re.match(r"\s*(?:#\s*)?([A-Z][A-Z0-9_]+)=(.*)$", line):
            value = re.split(r"\s+#", match[2])[0].strip().strip("'\"")
            found.setdefault(match[1], value)
    return found


def default(name: str):
    return Settings.model_fields[name.lower()].get_default(call_default_factory=True)


def without_code(path: Path) -> str:
    return re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)


def anchor(title: str) -> str:
    """Ancre GitHub d'un titre : minuscules, sans ponctuation, espaces en tirets, accents gardés."""
    return re.sub(r"[^\w\- ]", "", title.strip().lower()).replace(" ", "-")


def slash_commands() -> dict[str, list[str]]:
    """Commandes Discord du code : nom -> paramètres."""
    commands = {}
    for name in ("discord_bot.py", "discord_cards.py"):
        for node in ast.walk(ast.parse((ROOT / "src" / name).read_text(encoding="utf-8"))):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and getattr(decorator.func, "attr", "") == "command":
                    named = {k.arg: k.value.value for k in decorator.keywords if isinstance(k.value, ast.Constant)}
                    commands[named.get("name", node.name)] = [a.arg for a in node.args.args if a.arg != "interaction"]
    return commands


def console_commands() -> list[str]:
    return re.findall(r'add_parser\(\s*"([^"]+)"', (ROOT / "src" / "main.py").read_text(encoding="utf-8"))


def test_every_setting_is_documented():
    missing = {name.upper() for name in Settings.model_fields} - set(settings_in(REGLAGES))
    assert not missing, f"À ajouter dans docs/REGLAGES.md, avec la valeur par défaut : {sorted(missing)}"


@pytest.mark.parametrize("path", SETTINGS_FILES, ids=lambda p: p.name)
def test_no_dead_setting(path):
    dead = set(settings_in(path)) - {name.upper() for name in Settings.model_fields}
    assert not dead, f"À retirer de {path.name}, le code ne les lit plus : {sorted(dead)}"


@pytest.mark.parametrize("path", SETTINGS_FILES, ids=lambda p: p.name)
def test_settings_show_the_real_defaults(path, monkeypatch):
    """Lue comme le ferait le bot, la valeur affichée doit donner le défaut du code. Un exemple se marque « ex. »."""
    wrong = {}
    for name, shown in settings_in(path).items():
        if name in PLACEHOLDERS or name.lower() not in Settings.model_fields:
            continue
        monkeypatch.setenv(name, shown)
        read = getattr(Settings(_env_file=None), name.lower())
        monkeypatch.delenv(name)
        if read != default(name):
            wrong[name] = f"affiche {shown!r}, défaut du code {default(name)!r}"
    assert not wrong, f"Valeurs fausses dans {path.name} : {wrong}"


def test_env_example_loads():
    assert Settings(_env_file=ENV_EXAMPLE).dry_run, "Copié tel quel, .env.example doit rester en simulation"


def test_every_discord_command_is_documented():
    text = COMMANDS.read_text(encoding="utf-8")
    rows = {m[1]: m[2] for m in re.finditer(r"^\| `/([a-z-]+)([^`]*)`", text, re.M)}
    code = slash_commands()
    assert code, "Aucune commande Discord trouvée : le test ne lit plus le bon code"
    assert not set(code) - set(rows), f"À ajouter dans docs/COMMANDES.md : {sorted(set(code) - set(rows))}"
    assert not set(rows) - set(code), f"À retirer de docs/COMMANDES.md : {sorted(set(rows) - set(code))}"
    for name, params in code.items():
        if "…" in rows[name]:  # « […] » : trop de paramètres pour la ligne, décrits dans la réponse
            continue
        missing = [p for p in params if p not in rows[name]]
        assert not missing, f"/{name} : paramètres {missing} à ajouter dans docs/COMMANDES.md"


def test_every_console_command_is_documented():
    text = COMMANDS.read_text(encoding="utf-8")
    missing = [c for c in console_commands() if c != "run" and f"python -m src.main {c}" not in text]
    assert not missing, f"À ajouter dans docs/COMMANDES.md : {missing}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_console_commands_and_options_exist(page):
    text = page.read_text(encoding="utf-8")
    main = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
    uses = re.findall(r"python -m src\.main ([^|`\n]*)", text)
    unknown = {u.split()[0] for u in uses if u.split() and u.split()[0] not in [*console_commands(), "--help"]}
    assert not unknown, f"{page.name} cite des commandes inconnues : {sorted(unknown)}"
    options = {o for u in uses for o in re.findall(r"--[a-z]+", u)} - {"--help"}
    missing = {o for o in options if f'"{o}"' not in main}
    assert not missing, f"{page.name} cite des options inconnues : {sorted(missing)}"
    tools = set(re.findall(r"python -m src\.tools\.([a-z_]+)", text))
    gone = {t for t in tools if not (ROOT / "src" / "tools" / f"{t}.py").exists()}
    assert not gone, f"{page.name} cite des outils absents : {sorted(gone)}"


def test_every_worker_is_in_the_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    names = {m for path in (ROOT / "src" / "workers").glob("*.py")
             for m in re.findall(r'^    name = "([a-z]+)"', path.read_text(encoding="utf-8"), re.M)} - {"worker"}
    assert names, "Aucun worker trouvé : le test ne lit plus le bon code"
    missing = [name for name in names if f"`{name}`" not in readme]
    assert not missing, f"Workers à ajouter dans le tableau du README : {sorted(missing)}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_settings_cited_in_the_doc_exist(page):
    cited = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})(?:=[^`]*)?`", page.read_text(encoding="utf-8")))
    unknown = cited - {name.upper() for name in Settings.model_fields} - NOT_SETTINGS
    assert not unknown, f"{page.name} cite des réglages inconnus : {sorted(unknown)}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_links_and_anchors_work(page):
    broken = []
    for link in re.findall(r"\]\(([^)\s]+)\)", without_code(page)):
        if link.startswith(("http://", "https://")):
            continue
        name, _, fragment = link.partition("#")
        target = (page.parent / name).resolve() if name else page
        if not target.exists():
            broken.append(link)
        elif fragment and target.suffix == ".md":
            titles = re.findall(r"^#{1,6} (.+)$", without_code(target), re.M)
            if fragment not in {anchor(title) for title in titles}:
                broken.append(link)
    assert not broken, f"{page.name} : liens cassés {broken}"


def test_guides_cited_by_the_code_exist():
    sources = [*ROOT.glob("src/**/*.py"), *ROOT.glob("config/*.yaml"), ENV_EXAMPLE,
               ROOT / "portainer-stack.yml", ROOT / "docker-compose.yml"]
    gone = {f"{path.relative_to(ROOT)} : {guide}" for path in sources
            for guide in re.findall(r"docs/[A-Za-z]+\.md", path.read_text(encoding="utf-8"))
            if guide not in EXTERNAL_GUIDES and not (ROOT / guide).exists()}
    assert not gone, f"Guides cités mais absents : {sorted(gone)}"


def test_installer_fills_env_example():
    """installer.sh remplace des lignes non commentées de .env.example, et doit rester exécutable."""
    script = ROOT / "installer.sh"
    asked = re.findall(r"^ask ([A-Z_]+)", script.read_text(encoding="utf-8"), re.M)
    assert asked, "Aucune question trouvée dans installer.sh : le test ne lit plus le bon code"
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    missing = [name for name in asked if not any(line.startswith(f"{name}=") for line in lines)]
    assert not missing, f"installer.sh demande {missing}, absents (ou commentés) de .env.example"
    assert os.access(script, os.X_OK), "installer.sh doit rester exécutable : git update-index --chmod=+x installer.sh"
