"""Notifier Discord : catégorie de chaque message et forme des embeds."""

from src.core.notifier import DiscordNotifier, Level, Topic, _Message

MAIN, ALERT = "https://discord/main", "https://discord/alert"


def notifier(**kwargs) -> DiscordNotifier:
    return DiscordNotifier(MAIN, ALERT, dry_run=False, **kwargs)


def test_category_label():
    n = notifier()
    assert n.embed(_Message(Level.INFO, "Carte obtenue", "", topic=Topic.COLLECTION))["author"] == {"name": "📅 Collection"}
    assert n.embed(_Message(Level.INFO, "Bot démarré", ""))["author"] == {"name": "⚙️ Système"}


def test_topic_notifier_default_and_override():
    sent = []

    class Spy(DiscordNotifier):
        def send(self, level, title, description="", **kwargs):
            sent.append((title, kwargs.get("topic")))

    sales = Spy("").for_topic(Topic.SALES)
    sales.info("a")
    sales.warning("b", topic=Topic.SYSTEM)
    assert sent == [("a", Topic.SALES), ("b", Topic.SYSTEM)]


def test_embed_sections_footer_thumbnail():
    n = notifier(footer="Mode RÉEL")
    embed = n.embed(_Message(Level.SUCCESS, "Titre", "texte", {"solde": 10}, sections=[("Ventes", "3 vendues")],
                             thumbnail="https://img/x.png", url="https://fr.wikipedia.org/wiki/X"))
    assert embed["fields"] == [{"name": "Ventes", "value": "3 vendues", "inline": False},
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

    stats = {"balance": 3120, "bids_placed": 0,
             "bids_simulated": 42, "auctions_won": 0, "spent_won": 0, "listings_posted": 2, "listings_scheduled": 1,
             "doublons_vendus": 4, "doublons_recette": 96, "discarded": 0, "positions_sold": 0, "profit": 0,
             "profit_simulated": 310, "positions_held": 3, "capital_in_positions": 820}
    description, sections = report_sections(stats, live_bids=False)
    assert description == "Solde : **3 120 WB**"
    blocks = {name.split(" ", 1)[1]: body for name, body in sections}
    assert blocks["Achats"] == "42 mises simulées"
    assert blocks["Reventes"].replace("\u202f", " ") == (
        "3 positions en stock · 820 WB immobilisés\nBénéfice simulé : +310 WB\n4 doublons vendus (96 WB)")
    assert report_sections({**stats, "balance": "?"}, live_bids=True)[0] == "Solde : inconnu"

    def block(values: dict, name: str, live: bool = False) -> str:
        found = {n.split(" ", 1)[1]: b for n, b in report_sections(values, live_bids=live)[1]}[name]
        return found.replace("\u202f", " ")

    # Positions simulées, collection réelle (SIMULATE=sniper) : les deux comptes, les cartes gagnées sont réelles.
    mixed = {**stats, "bids_placed": 14, "bids_simulated": 76, "won_collection": 12, "spent_collection": 164}
    assert block(mixed, "Achats") == "12 cartes de collection (164 WB)\n14 mises placées · 76 mises simulées"
    # Bilan du 16/10 : refus comptés à part, positions et collection séparées, positions simulées comptées.
    # 12/11 : le résultat d'abord (achats, reventes), les ventes en cours ne sont plus répétées.
    live = {**stats, "bids_placed": 120, "bids_rejected": 465, "bids_simulated": 19, "auctions_won": 2,
            "spent_won": 474, "won_collection": 23, "spent_collection": 1091, "positions_held": 5,
            "capital_in_positions": 1338, "positions_held_sim": 5, "capital_simulated": 1273}
    assert block(live, "Achats", True) == ("**2 achats** (474 WB) · 23 cartes de collection (1 091 WB)\n"
                                           "120 mises placées · 465 refusées par le site · 19 mises simulées")
    assert block(live, "Reventes", True).startswith("5 positions en stock · 1 338 WB immobilisés\n"
                                                    "5 simulées (1 273 WB fictifs)")
    assert block({**stats, "bids_simulated": 0}, "Achats", True) == "0 mise placée"
    sold = {**stats, "positions_sold": 23, "profit": 13155, "profit_simulated": 0, "doublons_vendus": 0}
    assert block(sold, "Reventes") == ("**23 revendues** · bénéfice **+13 155 WB**\n"
                                       "3 positions en stock · 820 WB immobilisés")

    assert as_section("**Collection** : 33/366 — il manque : 2 janvier") == ("Collection", "33/366 — il manque : 2 janvier")
    assert as_section("**Validation de la simulation**\nMarché observé : 3") == ("Validation de la simulation",
                                                                                 "Marché observé : 3")


def test_card_links():
    from src.core.models import Card

    assert Card(id="1", wikipedia_title="14 octobre").page_url == "https://fr.wikipedia.org/wiki/14_octobre"
    assert Card(id="1", wikipedia_title="X", wikipedia_url="https://en.wikipedia.org/wiki/X").page_url.startswith("https://en.")
    assert Card(id="1").page_url is None


def test_log_line_carries_fields_and_sections(caplog):
    caplog.set_level("INFO")
    n = DiscordNotifier("", dry_run=False)
    n.info("Boosters ouverts", boosters=7, cartes=35)
    n.warning("Récap", "Depuis 08:00", sections=[("À vérifier", "• vérification humaine en attente")])
    lines = [r.getMessage() for r in caplog.records]
    assert "Système · Boosters ouverts — boosters : 7, cartes : 35" in lines
    assert "Système · Récap — Depuis 08:00 | À vérifier : • vérification humaine en attente" in lines


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
    assert route(Level.INFO, "Défausse terminée", topic=Topic.SALES) == activity
    assert route(Level.INFO, "Sniper : 5 enchères", topic=Topic.SNIPER) == activity
    assert route(Level.WARNING, "Récap des 4 dernières heures", topic=Topic.RECAP) == activity
    assert route(Level.SUCCESS, "Collection : carte obtenue !", topic=Topic.COLLECTION) == activity
    assert route(Level.WARNING, "Serveurs WikiMasters instables", important=False) == activity
    # Sans salon d'activité : tout dans le principal, comme avant ; « off » : logs seulement.
    assert notifier()._webhook_for(_Message(Level.INFO, "Défausse terminée", "", topic=Topic.SALES)) == MAIN
    assert notifier(activity_webhook_url="off")._webhook_for(_Message(Level.INFO, "Boosters", "")) == ""


def test_each_category_can_have_its_own_channel():
    n = DiscordNotifier(MAIN, ALERT, dry_run=False, activity_webhook_url="https://discord/activite",
                        channel_webhooks={"ventes": "https://discord/ventes", "bilan": "https://discord/bilan",
                                          "sniper": " "})
    route = n._webhook_for
    assert route(_Message(Level.INFO, "Revente réussie", "", topic=Topic.SALES)) == "https://discord/ventes"
    assert route(_Message(Level.INFO, "Échange proposé", "", topic=Topic.TRADES)) == "https://discord/ventes"
    assert route(_Message(Level.WARNING, "Récap", "", topic=Topic.RECAP)) == "https://discord/bilan"
    # Les alertes critiques restent dans le salon d'alertes ; une catégorie sans salon garde l'ancien tri.
    assert route(_Message(Level.CRITICAL, "Vente refusée", "", topic=Topic.SALES)) == ALERT
    assert route(_Message(Level.INFO, "Enchères", "", topic=Topic.SNIPER)) == "https://discord/activite"
    assert route(_Message(Level.WARNING, "Budget", "", topic=Topic.SNIPER)) == MAIN
    # Sans salon principal, le premier salon de catégorie le remplace.
    assert DiscordNotifier("", channel_webhooks={"ventes": "https://discord/ventes"})._webhook == "https://discord/ventes"
