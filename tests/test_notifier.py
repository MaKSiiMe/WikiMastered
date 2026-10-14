"""Notifier Discord : catégorie de chaque message et forme des embeds."""

from src.core.notifier import DiscordNotifier, Level, Topic, _Message

MAIN, ALERT = "https://discord/main", "https://discord/alert"


def notifier(**kwargs) -> DiscordNotifier:
    return DiscordNotifier(MAIN, ALERT, dry_run=False, **kwargs)


def test_category_label():
    n = notifier()
    assert n.embed(_Message(Level.INFO, "Boosters ouverts", "", topic=Topic.PACKS))["author"] == {"name": "🎴 Paquets"}
    assert n.embed(_Message(Level.INFO, "Bot démarré", ""))["author"] == {"name": "⚙️ Système"}


def test_topic_notifier_default_and_override():
    sent = []

    class Spy(DiscordNotifier):
        def send(self, level, title, description="", **kwargs):
            sent.append((title, kwargs.get("topic")))

    packs = Spy("").for_topic(Topic.PACKS)
    packs.info("a")
    packs.warning("b", topic=Topic.SYSTEM)
    assert sent == [("a", Topic.PACKS), ("b", Topic.SYSTEM)]


def test_embed_sections_footer_thumbnail():
    n = notifier(footer="Mode RÉEL")
    embed = n.embed(_Message(Level.SUCCESS, "Titre", "texte", {"solde": 10}, sections=[("Paquets", "3 boosters")],
                             thumbnail="https://img/x.png", url="https://fr.wikipedia.org/wiki/X"))
    assert embed["fields"] == [{"name": "Paquets", "value": "3 boosters", "inline": False},
                               {"name": "solde", "value": "10", "inline": True}]
    assert embed["footer"] == {"text": "Mode RÉEL"}
    assert embed["thumbnail"] == {"url": "https://img/x.png"}
    assert embed["url"] == "https://fr.wikipedia.org/wiki/X"
    assert "[DRY RUN]" not in embed["title"]
    # Une image sans URL web est ignorée
    assert "thumbnail" not in n.embed(_Message(Level.INFO, "t", "", thumbnail="/local.png"))


def test_embed_stays_under_discord_limit():
    n = notifier(footer="Mode RÉEL")
    sections = [(f"Section {i}", "x" * 1500) for i in range(8)]
    embed = n.embed(_Message(Level.INFO, "Bilan", "y" * 4000, sections=sections))
    total = len(embed["title"]) + len(embed.get("description", "")) + len(embed["footer"]["text"])
    total += sum(len(f["name"]) + len(f["value"]) for f in embed["fields"])
    assert total <= 6000
    assert all(len(f["value"]) <= 1024 for f in embed["fields"])


def test_dry_run_prefix():
    n = DiscordNotifier(MAIN, dry_run=True)
    assert n.embed(_Message(Level.INFO, "Enchère", ""))["title"] == "[DRY RUN] Enchère"
    assert n.embed(_Message(Level.INFO, "Boosters", "", live=True))["title"] == "Boosters"
