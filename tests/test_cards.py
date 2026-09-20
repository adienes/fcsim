from __future__ import annotations

import json
from pathlib import Path

import pytest

from fcsim.cards import (CardDatabase, CardsError, normalize_name, parse_decklist, resolve, validate_deck)
from fcsim.cli import main
from fcsim.workshop.check import check_deck, render
from fcsim.workshop.shuffle import SimCard, simulate
from fcsim.workshop.static import analyze, produced_colors, roles

FIXTURE = Path(__file__).parent / "data" / "cards.jsonl"
TALRAND = """1 Sol Ring
1 Counterspell
1 Opt
1 Rhystic Study
1 Cyclonic Rift
1 Arcane Signet
1 Halimar Depths
1 Mystic Sanctuary
1 Sea Gate Restoration
90 Island

1 Talrand, Sky Summoner
"""


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    path = tmp_path_factory.mktemp("cards") / "cards.sqlite"
    objects = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]
    assert CardDatabase.build(objects, path, source="fixture", updated_at="2026-09-20T00:00:00+00:00") == 37
    return CardDatabase(path)


def codes(text: str, database, **options) -> list[str]:
    return [issue.code for issue in validate_deck(resolve(parse_decklist(text, **options), database), database)]


def test_lookup_tolerates_spelling_variants_and_skips_tokens(database):
    assert database.get("sol ring").name == "Sol Ring"
    assert database.get("Lim-Dul's Vault").name == "Lim-Dûl's Vault"
    assert database.get("Fire/Ice") == database.get("Fire // Ice") == database.get("Fire")
    assert database.get("Sea Gate Restoration").layout == "modal_dfc"
    assert database.find("Treasure") is None  # the token, not a card
    with pytest.raises(CardsError, match="Did you mean: Rhystic Study"):
        database.get("Rystic Study")
    assert normalize_name("  Ætherize  ") == "aetherize"


def test_missing_database_says_how_to_get_one(tmp_path):
    with pytest.raises(CardsError, match="fcsim cards update"):
        CardDatabase(tmp_path / "absent.sqlite")


def test_failed_build_keeps_the_existing_database(database, tmp_path):
    path = tmp_path / "cards.sqlite"
    path.write_bytes(database.path.read_bytes())
    with pytest.raises(CardsError, match="no cards"):
        CardDatabase.build([], path, source="empty", updated_at="")
    assert CardDatabase(path).get("Island").is_basic


@pytest.mark.parametrize("text", [
    "1 Sol Ring\n98 Island\n\n1 Talrand, Sky Summoner\n",                               # MTGO
    "Commander\n1 Talrand, Sky Summoner\n\nDeck\n1x Sol Ring (C21) 263 *F*\n98 Island\n",  # Arena, Moxfield
    "1 Sol Ring [Ramp] ^Have,#37d67a^\n98 Island #land\nSB: 1 Talrand, Sky Summoner\n",  # Archidekt, SB:
    "// Sideboard\n1 Talrand, Sky Summoner\n\n// Deck\nSol Ring\n98 Island\n",          # headers, bare name
    "1 Sol Ring\n98 Island\n\nSIDEBOARD:\n1 Opt\n\n1 Talrand, Sky Summoner\n",           # Moxfield's MTGO export
])
def test_export_formats_parse_to_the_same_deck(text, database):
    decklist = parse_decklist(text)
    assert [(e.count, e.name) for e in decklist.commanders] == [(1, "Talrand, Sky Summoner")]
    assert sorted((e.count, e.name) for e in decklist.main) == [(1, "Sol Ring"), (98, "Island")]
    assert codes(text, database) == []


def test_commander_override_and_canonical_output(database):
    text = "1 Talrand, Sky Summoner\n1 Sol Ring\n98 Island\n"
    assert codes(text, database) == ["no_commander"]
    decklist = parse_decklist(text, commanders=["talrand, sky summoner"])
    assert decklist.to_mtgo() == "1 Sol Ring\n98 Island\n\n1 Talrand, Sky Summoner\n"
    assert validate_deck(resolve(decklist, database), database) == []
    with pytest.raises(CardsError, match="Line 1"):
        parse_decklist("3x\n")


def test_deck_construction_rules(database):
    base = "\n\n1 Talrand, Sky Summoner\n"
    assert codes("2 Sol Ring\n97 Island" + base, database) == ["singleton"]
    assert codes("1 Lightning Bolt\n98 Island" + base, database) == ["color_identity"]
    assert codes("1 Black Lotus\n98 Island" + base, database) == ["banned"]
    assert codes("97 Island" + base, database) == ["deck_size"]
    assert codes("1 Sol Rign\n98 Island" + base, database) == ["unknown_card"]
    assert codes("50 Island\n49 Snow-Covered Island" + base, database) == []
    swamps = "\n\n1 Tymna the Weaver\n1 Thrasios, Triton Hero\n"
    assert codes("40 Relentless Rats\n7 Seven Dwarves\n51 Forest" + swamps, database) == ["color_identity"]
    assert codes("8 Seven Dwarves\n91 Island" + base, database) == ["singleton", "color_identity"]
    assert "not_a_commander" in codes("99 Island\n\n1 Sol Ring\n", database)


@pytest.mark.parametrize(("pair", "expected"), [
    (("Tymna the Weaver", "Thrasios, Triton Hero"), []),
    (("Pir, Imaginative Rascal", "Toothy, Imaginary Friend"), []),
    (("Wilson, Refined Grizzly", "Raised by Giants"), []),
    (("Pir, Imaginative Rascal", "Thrasios, Triton Hero"), ["commander_pairing"]),
    (("Talrand, Sky Summoner", "Thrasios, Triton Hero"), ["commander_pairing"]),
])
def test_commander_pairs(pair, expected, database):
    text = "98 Forest\n\n" + "".join(f"1 {name}\n" for name in pair)
    assert [c for c in codes(text, database) if c != "color_identity"] == expected


def test_lone_background_and_vehicle_commanders(database):
    assert "not_a_commander" in codes("99 Forest\n\n1 Raised by Giants\n", database)
    assert "not_a_commander" not in codes("99 Island\n\n1 Shorikai, Genesis Engine\n", database)


def test_roles_and_mana_sources(database):
    blue = frozenset("U")
    assert roles(database.get("Cultivate")) == {"ramp"}
    assert roles(database.get("Demonic Tutor")) == {"tutor", "game_changer"}
    assert {"removal"} <= roles(database.get("Swords to Plowshares"))
    assert roles(database.get("Wrath of God")) == {"wipe"}
    assert roles(database.get("Armageddon")) == {"mass_land_denial"}
    assert roles(database.get("Time Warp")) == {"extra_turn"}
    assert roles(database.get("Sol Ring")) == {"ramp", "fast_mana"}
    assert roles(database.get("Polluted Delta")) == set()
    assert produced_colors(database.get("Command Tower"), blue) == {"U"}
    assert produced_colors(database.get("Polluted Delta"), blue) == {"U"}
    assert produced_colors(database.get("Evolving Wilds"), frozenset("UG")) == {"U", "G"}
    assert database.get("Sea Gate Restoration").land_face.name == "Sea Gate, Reborn"
    assert database.get("Growing Rites of Itlimoc").land_face is None  # transforms; not playable as a land
    assert SimCard.of(database.get("Halimar Depths"), blue).tapped
    assert not SimCard.of(database.get("Mystic Sanctuary"), blue).tapped  # conditional counts as untapped
    assert SimCard.of(database.get("Sol Ring"), blue).amount == 2
    assert SimCard.of(database.get("Llanowar Elves"), frozenset("G")).kind == "dork"


def test_analysis_and_simulation_of_a_deck(database):
    deck = resolve(parse_decklist(TALRAND), database)
    analysis = analyze(deck)
    assert analysis["cards"] == 100 and analysis["color_identity"] == ["U"]
    assert analysis["lands"] == {"lands": 92, "modal_land_faces": 1, "colorless_only": 0}
    assert analysis["curve"]["1"] == 2 and sum(analysis["curve"].values()) == 8
    assert analysis["power_signals"]["game_changer"]["cards"] == ["Cyclonic Rift", "Rhystic Study"]
    assert any("lands is high" in note for note in analysis["notes"])

    first = simulate(deck, trials=300, turns=6, seed=7)
    assert first == simulate(deck, trials=300, turns=6, seed=7)  # reproducible
    assert first != simulate(deck, trials=300, turns=6, seed=8)
    assert first["by_turn"][3]["made_land_drop"] > 0.99  # 92 lands do not miss drops
    assert first["commanders"][0]["castable_by_turn"]["4"] > 0.95
    assert first["flooded"]["rate"] > 0.9
    with pytest.raises(ValueError, match="too few"):
        simulate(resolve(parse_decklist("9 Island\n\n1 Talrand, Sky Summoner\n"), database), turns=10)


def test_check_report_and_cli(database, tmp_path, capsys, monkeypatch):
    good, bad = tmp_path / "talrand.txt", tmp_path / "bad.txt"
    good.write_text(TALRAND, encoding="utf-8")
    bad.write_text("1 Lightning Bolt\n1 Sol Rign\n\n1 Talrand, Sky Summoner\n", encoding="utf-8")

    result = check_deck(good, database, trials=50, seed=1)
    assert result["legal"] and "shuffle_simulation" in result
    assert "LEGALITY  legal" in render(result) and "policy:" in render(result, verbose=True)
    broken = check_deck(bad, database, trials=50)
    assert not broken["legal"] and "shuffle_simulation" not in broken
    assert "Did you mean: Sol Ring" in render(broken)

    monkeypatch.setenv("FCSIM_DATA", str(tmp_path / "data"))
    assert main(["deck", "check", str(good)]) == 2  # no card data yet: says how to get it
    assert "fcsim cards update" in json.loads(capsys.readouterr().err)["error"]["message"]
    (tmp_path / "data" / "scryfall").mkdir(parents=True)
    (tmp_path / "data" / "scryfall" / "cards.sqlite").write_bytes(database.path.read_bytes())
    assert main(["deck", "check", str(good), "--trials", "20", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["analysis"]["commanders"] == ["Talrand, Sky Summoner"]
    assert main(["deck", "check", str(bad), "--trials", "0"]) == 1
    assert "3 problems" in capsys.readouterr().out
    assert main(["cards", "show", "opt"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "Opt"


def test_an_alternate_printed_name_is_read_from_the_alias_file_and_typos_are_not(database, tmp_path):
    path = tmp_path / "cards.sqlite"
    path.write_bytes(database.path.read_bytes())
    (tmp_path / "aliases.json").write_text(json.dumps({"bayo, irritable instructor": "Opt", "nobody": None}))
    local = CardDatabase(path)
    assert local.alias("Bayo, Irritable Instructor").name == "Opt" and local.alias("Nobody") is None
    assert local.alias("Sol Rign", online=True) is None   # close to a real name: reported as a typo, never asked about
    deck = resolve(parse_decklist("1 Bayo, Irritable Instructor\n98 Island\n\n1 Talrand, Sky Summoner\n"), local)
    assert deck.aliases == (("Bayo, Irritable Instructor", "Opt"),) and not deck.unknown and validate_deck(deck, local) == []


def test_two_copies_of_the_commander_are_counted_and_refused(database):
    assert codes("99 Island\n\n2 Talrand, Sky Summoner\n", database) == ["deck_size", "singleton"]


def test_mana_spent_on_ramp_is_not_spent_again_on_the_commander(database):
    from fcsim.workshop import shuffle
    deck = resolve(parse_decklist("1 Sol Ring\n98 Island\n\n1 Talrand, Sky Summoner\n"), database)   # Talrand costs four
    cards = [shuffle.SimCard.of(card, deck.identity) for card in deck.library()]
    ring, island = next(c for c in cards if c.name == "Sol Ring"), next(c for c in cards if c.name == "Island")
    assert shuffle._payable(4, (), [island, island, ring], spent=0)
    assert not shuffle._payable(4, (), [island, island, ring], spent=1)     # 4 mana in play, 1 already used on the ring
    assert shuffle.simulate(deck, trials=50, turns=4, seed=2)["commanders"][0]["mana_value"] == 4
