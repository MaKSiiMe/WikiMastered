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
    # Positions simulées, collection réelle (SIMULATE=sniper) : les deux comptes, les cartes gagnées sont réelles.
    mixed = {**stats, "bids_placed": 14, "bids_simulated": 76, "won_collection": 12, "spent_collection": 164}
    assert dict(report_sections(mixed, {}, live_bids=False)[1])["Sniper"] == \
        "14 mise(s) placée(s), 76 mise(s) simulée(s)\nGagnées : 12 carte(s) de collection pour 164 WB"
    # Bilan du 16/10 : refus comptés comme placées, positions et collection mêlées, positions simulées comptées.
    live = {**stats, "bids_placed": 120, "bids_rejected": 465, "bids_simulated": 19, "auctions_won": 2,
            "spent_won": 474, "won_collection": 23, "spent_collection": 1091, "positions_held": 5,
            "capital_in_positions": 1338, "positions_held_sim": 5, "capital_simulated": 1273}
    blocks = dict(report_sections(live, {}, live_bids=True)[1])
    assert blocks["Sniper"] == ("120 mise(s) placée(s), 465 refusée(s) par le site, 19 mise(s) simulée(s)\n"
                                "Gagnées : 2 position(s) pour 474 WB, 23 carte(s) de collection pour 1091 WB")
    assert blocks["Positions"].startswith("5 réelle(s) détenue(s), 1338 WB immobilisés\n"
                                          "5 simulée(s) (sniper de test, 1273 WB fictifs)")
    assert dict(report_sections({**stats, "bids_simulated": 0}, {}, live_bids=True)[1])["Sniper"] == \
        "0 mise(s) placée(s)"

    assert as_section("**Collection** : 33/366 — il manque : 2 janvier") == ("Collection", "33/366 — il manque : 2 janvier")
    assert as_section("**Validation de la simulation**\nMarché observé : 3") == ("Validation de la simulation",
                                                                                 "Marché observé : 3")


def test_card_links():
    from src.core.models import Card
    from src.workers.pack_opener import highlight_line

    assert Card(id="1", wikipedia_title="14 octobre").page_url == "https://fr.wikipedia.org/wiki/14_octobre"
    assert Card(id="1", wikipedia_title="X", wikipedia_url="https://en.wikipedia.org/wiki/X").page_url.startswith("https://en.")
    assert Card(id="1").page_url is None
    assert highlight_line(Card(id="1", wikipedia_title="Céline Dion", rarity="UR"), True) == \
        "**UR** ✨ Shiny — [Céline Dion](https://fr.wikipedia.org/wiki/C%C3%A9line_Dion)"


def test_log_line_carries_fields_and_sections(caplog):
    caplog.set_level("INFO")
    n = DiscordNotifier("", dry_run=False)
    n.info("Boosters ouverts", boosters=7, cartes=35)
    n.warning("Récap", "Depuis 08:00", sections=[("À vérifier", "• vérification humaine en attente")])
    lines = [r.getMessage() for r in caplog.records]
    assert "[INFO] Boosters ouverts boosters : 7, cartes : 35" in lines
    assert "[WARNING] Récap Depuis 08:00 | À vérifier : • vérification humaine en attente" in lines


def test_two_channels_main_keeps_money_and_alerts():
    """« Beaucoup trop de messages » (16/10) : le salon principal (le seul qui sonne) garde l'argent et les alertes ;
    le salon d'activité, muet, garde le reste, réussites ordinaires comprises."""
    activity = "https://discord/activity"
    n = notifier(activity_webhook_url=activity)
    route = lambda level, title, **kw: n._webhook_for(_Message(level, title, "", **kw))  # noqa: E731
    assert route(Level.SUCCESS, "Position achetée !", topic=Topic.SNIPER, important=True) == MAIN
    assert route(Level.SUCCESS, "Revente réussie", topic=Topic.SALES, important=True) == MAIN
    assert route(Level.INFO, "Carte mise en vente", topic=Topic.SALES, important=True) == MAIN
    assert route(Level.WARNING, "Mise en vente refusée", topic=Topic.SALES) == MAIN
    assert route(Level.INFO, "Bilan du 16/10", topic=Topic.REPORT) == MAIN
    assert route(Level.CRITICAL, "Sniper : vérification anti-bot demandée") == ALERT
    assert route(Level.INFO, "Boosters ouverts", topic=Topic.PACKS) == activity
    assert route(Level.INFO, "Sniper : 5 enchères", topic=Topic.SNIPER) == activity
    assert route(Level.WARNING, "Récap des 4 dernières heures", topic=Topic.RECAP) == activity
    assert route(Level.SUCCESS, "Collection : carte obtenue !", topic=Topic.COLLECTION) == activity
    assert route(Level.SUCCESS, "Boosters ouverts : tirage remarquable !", topic=Topic.PACKS) == activity
    assert route(Level.WARNING, "Serveurs WikiMasters instables", important=False) == activity
    # Sans salon d'activité : tout dans le principal, comme avant ; « off » : logs seulement.
    assert notifier()._webhook_for(_Message(Level.INFO, "Boosters ouverts", "", topic=Topic.PACKS)) == MAIN
    assert notifier(activity_webhook_url="off")._webhook_for(_Message(Level.INFO, "Boosters", "")) == ""
