"""Notifier Discord : salon par thème et forme des embeds."""

from src.core.notifier import DiscordNotifier, Level, Topic, _Message

MAIN, ALERT, PACKS = "https://discord/main", "https://discord/alert", "https://discord/packs"


def notifier(**kwargs) -> DiscordNotifier:
    return DiscordNotifier(MAIN, ALERT, dry_run=False, topic_webhooks={Topic.PACKS: PACKS, Topic.TRENDS: ""},
                           **kwargs)


def test_topic_routing():
    n = notifier()
    assert n._url_for(_Message(Level.INFO, "t", "", topic=Topic.PACKS)) == PACKS
    assert n._url_for(_Message(Level.INFO, "t", "", topic=Topic.TRENDS)) == MAIN  # salon vide : principal
    assert n._url_for(_Message(Level.INFO, "t", "")) == MAIN
    # Une alerte critique part toujours dans le salon des alertes
    assert n._url_for(_Message(Level.CRITICAL, "t", "", topic=Topic.PACKS)) == ALERT


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


def test_report_sections():
    from src.main import as_section, report_sections

    stats = {"balance": 3120, "boosters_ouverts": 6, "cards_pulled": 30, "shiny_pulled": 1, "bids_placed": 0,
             "bids_simulated": 42, "auctions_won": 0, "spent_won": 0, "listings_posted": 2, "listings_scheduled": 1,
             "doublons_vendus": 4, "doublons_recette": 96, "discarded": 0, "positions_sold": 0, "profit": 0,
             "profit_simulated": 310, "positions_held": 3, "capital_in_positions": 820}
    description, sections = report_sections(stats, {"UR": 1, "C": 20, "R": 9}, live_bids=False)
    assert description == "Solde : **3120 WB**"
    blocks = dict(sections)
    assert blocks["Paquets"] == "6 booster(s), 30 carte(s)\nC 20 · R 9 · UR 1\n✨ 1 shiny"  # ordre des raretés
    assert blocks["Sniper"] == "42 mise(s) simulée(s)"
    assert "4 doublon(s) vendu(s) pour 96 WB" in blocks["Ventes"]
    assert "Bénéfice simulé : +310 WB" in blocks["Positions"]
    assert report_sections({**stats, "balance": "?"}, {}, live_bids=True)[0] == "Solde : inconnu"

    assert as_section("**Collection** : 33/366 — il manque : 2 janvier") == ("Collection", "33/366 — il manque : 2 janvier")
    assert as_section("**Validation de la simulation**\nMarché observé : 3") == ("Validation de la simulation",
                                                                                 "Marché observé : 3")
