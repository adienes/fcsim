from __future__ import annotations

import json
import random

import pytest

from fcsim.table import ScriptedRunner, Table, mutations
from fcsim.table.script import ScriptError


def seats(prefix: str = "") -> list[dict]:
    """Three seats whose libraries are 12 Islands and 28 distinct spells, named by seat."""
    return [{"name": name, "commanders": [f"{name}'s Commander"],
             "library": ["Island"] * 12 + [f"{prefix if index else ''}{name} spell {i}" for i in range(28)]}
            for index, name in enumerate(("Ann", "Bob", "Cat"))]


def started(first: int = 0, seed: int = 5, prefix: str = "") -> tuple[Table, ScriptedRunner]:
    table = Table(seats(prefix), seed=seed, first=first)
    runner = ScriptedRunner(table)
    for seat in range(3):
        runner.step(seat, "keep")
    return table, runner


def pass_until(table: Table, runner: ScriptedRunner, step: str, seat: int | None = None) -> None:
    """Everyone passes until the game reaches `step` with `seat` (default: the active seat) holding priority."""
    for _ in range(200):
        holder, kind = table.expected()[0]
        if kind == "priority" and table.game.turn.step == step and holder == (table.game.turn.active if seat is None else seat):
            return
        runner.step(holder, {"priority": "pass_priority", "declare_attackers": "declare_attackers",
                             "combat_damage": "combat_damage", "discard": "finish_step"}[kind])
    raise AssertionError(f"never reached {step}")


def hand(table: Table, seat: int) -> list[str]:
    return [card["name"] for card in table.view(seat)["seats"][seat]["hand"]["cards"]]


def spell_in_hand(table: Table, seat: int) -> str:
    return next(name for name in hand(table, seat) if "spell" in name)


def test_setup_mulligans_and_the_first_turn():
    table = Table(seats(), seed=1, first=2)
    runner = ScriptedRunner(table)
    assert table.expected() == [(0, "mulligan"), (1, "mulligan"), (2, "mulligan")]
    runner.step(1, "mulligan")
    runner.step(1, "keep")                      # the first mulligan is free
    runner.step(0, "mulligan")
    runner.step(0, "mulligan")
    runner.step(0, "keep", error="bad_bottom")  # the second one costs a card
    runner.step(0, "keep", {"bottom": [table.view(0)["seats"][0]["hand"]["cards"][0]["handle"]]})
    runner.step(1, "pass_priority", error="not_your_pen")
    runner.step(2, "keep")
    view = table.view(0)
    assert [seat["hand"]["count"] for seat in view["seats"]] == [6, 7, 7]
    assert view["turn"] == {**view["turn"], "number": 1, "active": 2, "step": "upkeep"} and table.expected() == [(2, "priority")]
    pass_until(table, runner, "main1")
    assert table.view(2)["seats"][2]["hand"]["count"] == 8  # drew for the turn, in multiplayer too


def test_priority_the_stack_and_a_counterspell():
    table, runner = started()
    pass_until(table, runner, "main1")
    threat, answer = spell_in_hand(table, 0), spell_in_hand(table, 1)
    runner.step(0, "play_land", {"card": "@Island"})
    assert runner.step(0, "play_land", {"card": "@Island"})["lints"] == ["This is land number 2 this turn."]
    runner.step(0, "cast", {"card": f"@{threat}", "payment": {"tap": ["@Island"]}, "resolves": "auto_permanent"})
    assert table.expected() == [(0, "priority")]             # the caster keeps priority
    runner.step(1, "cast", {"card": f"@{answer}"}, error="not_your_pen")
    runner.step(0, "pass_priority")
    runner.step(1, "cast", {"card": f"@{answer}", "targets": [{"stack_item": f"${threat}"}], "text": "Counter target spell."})
    assert "targeting " + threat in runner.results[-1]["log"][0]
    for seat in (1, 2, 0):
        runner.step(seat, "pass_priority")
    assert table.expected() == [(1, "resolution")] and table.view(2)["resolving"]["source"] == answer
    runner.step(0, "pass_priority", error="not_your_pen")     # nobody has priority during a resolution
    runner.step(1, "resolve", {"mutations": [{"op": "move", "obj": f"@{threat}", "to": "graveyard"}]})
    runner.step(1, "finish_resolution")
    view = table.view(2)
    assert view["stack"] == [] and table.expected() == [(0, "priority")]  # the active player, after a resolution
    assert [c["name"] for c in view["seats"][0]["graveyard"]] == [threat]
    assert [c["name"] for c in view["seats"][1]["graveyard"]] == [answer]


def test_auto_permanent_combat_and_commander_damage():
    table, runner = started()
    pass_until(table, runner, "main1")
    runner.step(0, "cast", {"card": "@Ann's Commander", "resolves": "auto_permanent"})
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    assert table.view(1)["battlefield"][0]["name"] == "Ann's Commander" and table.expected() == [(0, "priority")]
    pass_until(table, runner, "begin_combat")
    runner.step(0, "pass_priority"), runner.step(1, "pass_priority"), runner.step(2, "pass_priority")
    attack = {"attacks": [{"attacker": "@Ann's Commander", "target": {"seat": 1}}]}
    assert "needs haste" in runner.step(0, "declare_attackers", attack)["lints"][0]
    assert table.view(0)["battlefield"][0]["tapped"]
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    assert table.expected() == [(1, "declare_blockers")]
    runner.step(1, "declare_blockers", {"blocks": []})
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    runner.step(0, "combat_damage", {"assignments": [{"source": "@Ann's Commander", "target": {"seat": 1}, "amount": 21}]})
    view = table.view(2)
    assert view["seats"][1]["life"] == 19 and view["seats"][1]["commander_damage"] == {"Ann's Commander (Ann)": 21}
    assert view["warnings"] == ["Bob has taken 21 commander damage from Ann's Commander (Ann)."]
    runner.step(0, "act", {"mutations": [{"op": "eliminate", "seat": 1, "reason": "21 commander damage"}],
                           "because": "state-based action, rule 704.5v"})
    assert table.view(2)["seats"][1]["status"] == "eliminated" and table.game.alive() == [0, 2]
    runner.step(1, "say", {"text": "gg"}, error="not_playing")
    runner.step(2, "concede")
    assert table.game.winner == 0 and table.expected() == []


def test_a_resolution_can_draw_then_choose_and_can_ask_other_seats():
    table, runner = started()
    pass_until(table, runner, "main1")
    runner.step(0, "cast", {"card": f"@{spell_in_hand(table, 0)}", "text": "Draw two, discard one; each opponent chooses."})
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    drew = runner.step(0, "resolve", {"mutations": [{"op": "draw", "count": 2}]})
    assert len(drew["shown"]) == 2 and drew["private"][0].startswith("Drew ")  # sees them before choosing
    runner.step(0, "resolve", {"mutations": [{"op": "move", "obj": drew["shown"][0]["handle"], "to": "graveyard"}]})
    runner.step(0, "resolve", {"mutations": [{"op": "request_choice", "seats": [2, 1], "prompt": "Pay 2 life?",
                                               "options": ["yes", "no"]}, {"op": "shuffle"}]}, error="bad_batch")
    runner.step(0, "resolve", {"mutations": [{"op": "request_choice", "seats": [2, 1], "prompt": "Pay 2 life?",
                                               "options": ["yes", "no"]}]})
    assert table.expected() == [(1, "request")] and table.game.resolution["status"] == "suspended"  # APNAP order
    question = table.view(1)["questions"][0]["handle"]
    runner.step(1, "answer", {"request": question, "answer": "maybe"}, error="bad_answer")
    runner.step(1, "answer", {"request": question, "answer": "yes"})
    runner.step(2, "answer", {"request": table.view(2)["questions"][0]["handle"], "answer": "no"})
    assert table.expected() == [(0, "resolution")] and table.view(0)["questions"][0]["answers"] == {"1": "yes", "2": "no"}
    runner.step(0, "resolve", {"mutations": [{"op": "life", "seat": 1, "delta": -2}]})
    runner.step(0, "finish_resolution")
    assert table.view(0)["seats"][1]["life"] == 38 and table.view(0)["questions"] == []


def test_hidden_cards_have_no_handles_and_handles_die_on_zone_changes():
    table, runner = started()
    pass_until(table, runner, "main1")
    view = table.view(1)
    assert view["seats"][0]["hand"] == {"count": 8} and view["seats"][0]["library"] == {"count": 32}
    assert "Ann spell" not in json.dumps(view)
    island = runner.handle(0, "@Island")
    runner.step(1, "act", {"mutations": [{"op": "tap", "objs": [island]}], "because": "guessing"}, error="not_your_pen")
    runner.step(0, "play_land", {"card": island})
    runner.step(0, "play_land", {"card": island}, error="gone")           # the old handle died with the zone change
    secret = spell_in_hand(table, 0)
    runner.step(0, "act", {"mutations": [{"op": "reveal", "obj": f"@{secret}"}], "because": "test"})
    assert [c["name"] for c in table.view(1)["seats"][0]["hand"]["known"]] == [secret]
    runner.step(0, "act", {"mutations": [{"op": "move", "obj": f"@{secret}", "to": "library", "position": "top"}],
                           "because": "test"})
    assert table.view(1)["seats"][0]["library"]["known"][0]["name"] == secret   # everyone saw where it went
    runner.step(0, "act", {"mutations": [{"op": "shuffle"}], "because": "test"})
    assert table.view(1)["seats"][0]["library"] == {"count": 33} == table.view(0)["seats"][0]["library"]
    looked = runner.step(0, "act", {"mutations": [{"op": "look", "from": {"seat": 1, "zone": "library", "count": 2}}],
                                    "because": "test"}, error="because_required")
    looked = runner.step(0, "act", {"mutations": [{"op": "look", "from": {"seat": 1, "zone": "library", "count": 2},
                                                   "because": "Telepathy"}], "because": "test"})
    assert len(looked["shown"]) == 2 and "Ann look(s) at 2 card(s) in Bob's library (Telepathy)." in looked["log"]
    assert "known" not in table.view(2)["seats"][1]["library"] and "private" not in table.act(2, "get_log")["events"][-1]


def test_holds_and_step_triggers_hand_mechanical_steps_to_a_seat():
    table, runner = started()
    table.game.ledger += [{"kind": "hold", "step": "draw", "seat": 1, "text": "Bob's draws are replaced"},
                          {"kind": "step_trigger", "step": "upkeep", "whose": 1, "controller": 2, "text": "Cat's upkeep trigger"}]
    pass_until(table, runner, "end")
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    assert table.expected() == [(0, "discard")]   # eight cards in hand at cleanup
    assert runner.step(0, "finish_step")["lints"] == ["Ann ends the turn with 8 cards in hand."]
    assert table.game.turn.active == 1 and table.expected() == [(2, "triggers")]  # before anyone has priority
    waiting = table.view(2)["waiting_triggers"][0]["handle"]
    runner.step(2, "add_trigger", {"text": "x"}, error="pending_required")
    runner.step(2, "add_trigger", {"pending": waiting})
    assert table.expected() == [(1, "priority")] and table.view(0)["stack"][0]["text"] == "Cat's upkeep trigger"
    for seat in (1, 2, 0):
        runner.step(seat, "pass_priority")
    runner.step(2, "finish_resolution")
    for seat in (1, 2, 0):
        runner.step(seat, "pass_priority")
    assert table.expected() == [(1, "held_step")] and table.view(1)["seats"][1]["hand"]["count"] == 7  # no automatic draw
    runner.step(1, "act", {"mutations": [{"op": "mill", "count": 2}], "because": "the replacement"})
    runner.step(1, "finish_step")
    assert table.expected() == [(1, "priority")] and table.game.turn.step == "draw"


def play_a_little(table: Table, runner: ScriptedRunner) -> None:
    pass_until(table, runner, "main1")
    runner.step(0, "play_land", {"card": "@Island"})
    runner.step(0, "write_notes", {"text": "Bob looks dangerous."})
    runner.step(0, "act", {"because": "test", "mutations": [
        {"op": "roll", "sides": 20, "count": 3}, {"op": "flip_coin"}, {"op": "discard_random", "seat": 1, "because": "Mind Twist"},
        {"op": "create_token", "token": {"name": "Bird", "power": 1, "toughness": 1}, "count": 2},
        {"op": "draw", "seat": 2, "count": 2}, {"op": "shuffle", "seat": 2}]})
    runner.step(2, "say", {"text": "Truce until Bob is dealt with?", "to": [0]})


def test_replaying_the_log_rebuilds_the_same_game():
    table, runner = started()
    play_a_little(table, runner)
    copy = Table.replay(table.config, json.loads(json.dumps(table.events)))   # survives a trip through JSON
    assert copy.game.to_dict() == table.game.to_dict() and copy.notes == table.notes and copy.rev == table.rev
    assert [e["public"] for e in copy.events] == [e["public"] for e in table.events]


def test_rewind_restores_the_past_reshuffles_the_unknown_and_kills_wakes():
    table, runner = started()
    pass_until(table, runner, "main1")
    before, hand_before, kept = len(table.events) - 1, sorted(hand(table, 0)), runner.handle(0, "@Island")
    library_before = [table.game.objects[i].name for i in table.game.seats[0].library]
    play_a_little(table, runner)
    wake, handle = table.open_wake(0), runner.handle(0, "@Bird")   # a handle from the line about to be abandoned
    table.rewind(before, "Ann played a land she did not have")
    assert sorted(hand(table, 0)) == hand_before and table.view(0)["battlefield"] == [] and table.notes[0] == ""
    assert table.call(wake, "1", "pass_priority")["error"]["code"] == "stale_wake"
    assert table.act(0, "act", {"mutations": [{"op": "tap", "objs": [handle]}], "because": "x"})["error"]["code"] == "gone"
    assert table.act(0, "play_land", {"card": kept})["ok"]   # a handle from before the rewind point still means that card
    library_after = [table.game.objects[i].name for i in table.game.seats[0].library]
    assert sorted(library_after) == sorted(library_before) and library_after != library_before
    copy = Table.replay(table.config, table.events)
    assert copy.game.to_dict() == table.game.to_dict()
    table.patch([{"op": "life", "seat": 0, "set": 30}], "missed trigger")
    assert table.view(0)["seats"][0]["life"] == 30 and Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()


def test_a_repeated_call_acts_once_and_a_closed_wake_cannot_act():
    table, runner = started()
    pass_until(table, runner, "main1")
    wake, count = table.open_wake(0), len(table.events)
    island = runner.handle(0, "@Island")
    first = table.call(wake, "call-1", "play_land", {"card": island})
    assert first["ok"] and table.call(wake, "call-1", "play_land", {"card": island}) is first  # a retry
    assert len(table.events) == count + 1
    table.close_wake(wake)
    assert table.call(wake, "call-2", "pass_priority")["error"]["code"] == "stale_wake"
    assert table.act(0, "cast", {"card": 12})["error"]["code"] == "unknown_handle"
    assert table.act(0, "combat_damage", {"assignments": "nonsense"})["error"]["code"] == "not_your_pen"
    assert table.act(0, "act", {"mutations": [{"op": "life"}], "because": "x"})["error"]["code"] == "bad_seat"
    assert len(table.events) == count + 1  # rejected calls change nothing


def observations(table: Table, viewer: int, results: list) -> str:
    return json.dumps({"view": table.view(viewer), "log": table.act(viewer, "get_log"), "results": results}, sort_keys=True)


def hidden_play(leak: bool, seed: int) -> list[str]:
    """Two games that differ only in what Ann (seat 0) cannot see: the names of Bob's and Cat's
    spells. Every seat makes the same random calls in both. Returns all Ann can observe in each:
    her view, her log, the results of her own calls, and what happens when she guesses handles."""
    rng = random.Random(seed)
    games = [started(first=1, seed=seed)[0], started(first=1, seed=seed, prefix="other ")[0]]
    seen: list[list] = [[], []]
    for _ in range(60):
        actor, kind = games[0].expected()[0]
        hands = [[c["handle"] for c in table.view(actor)["seats"][actor]["hand"]["cards"]] for table in games]
        assert hands[0] == hands[1]   # the actor looks at the table before acting, as an agent does
        in_hand = hands[0]
        guess = [f"c{rng.randint(1, 400)}"]
        menu = [
            ("pass_priority", {}),
            ("act", {"because": "hidden", "mutations": [{"op": "draw", "count": rng.randint(1, 2)}]}),
            ("act", {"because": "hidden", "mutations": [{"op": "move", "obj": rng.choice(in_hand or guess), "to": "library",
                                                          "position": rng.choice(["top", "bottom"])}, {"op": "shuffle"}]}),
            ("act", {"because": "hidden", "mutations": [{"op": "look", "from": {"seat": actor, "zone": "library", "count": 3}}]}),
            ("act", {"because": "hidden", "mutations": [{"op": "look", "because": "peek", "viewers": [rng.choice([1, 2])],
                                                          "from": {"seat": rng.choice([1, 2]), "zone": "library", "count": 2}}]}),
            ("act", {"because": "public", "mutations": [{"op": "life", "seat": rng.randrange(3), "delta": -rng.randint(1, 5)},
                                                          {"op": "create_token", "token": {"name": "Spirit"}},
                                                          {"op": "roll", "sides": 6}]}),
            ("act", {"because": "guess", "mutations": [{"op": "tap", "objs": guess}]}),
        ]
        if leak and actor != 0:
            menu = [("act", {"because": "leak", "mutations": [{"op": "mill", "count": 1}]})] + menu[:1]
        other = {"declare_attackers": "declare_attackers", "combat_damage": "combat_damage", "discard": "finish_step"}
        tool, args = rng.choice(menu) if kind == "priority" else (other[kind], {})
        probe = {"because": "probe", "mutations": [{"op": "tap", "objs": [f"c{rng.randint(1, 400)}"]}]}
        for index, table in enumerate(games):
            assert table.expected()[0] == (actor, kind)
            result = table.act(actor, tool, args)
            seen[index] += [result if actor == 0 else None, table.act(0, "act", probe)]
    return [observations(table, 0, results) for table, results in zip(games, seen, strict=True)]


@pytest.mark.parametrize("seed", range(8))
def test_a_seat_observes_nothing_about_cards_hidden_from_it(seed):
    first, second = hidden_play(leak=False, seed=seed)
    assert first == second


def test_the_observation_check_notices_a_real_difference():
    first, second = hidden_play(leak=True, seed=0)
    assert first != second  # milling shows Ann card names that differ between the two games


def test_scripts_fail_loudly():
    table, runner = started()
    with pytest.raises(ScriptError, match="cannot see a card named"):
        runner.step(0, "play_land", {"card": "@Black Lotus"})
    with pytest.raises(ScriptError, match="expected success"):
        runner.step(1, "pass_priority")


def test_auditor_hooks_hold_a_step_until_their_source_leaves():
    table, runner = started()
    pass_until(table, runner, "main1")
    vault = spell_in_hand(table, 0)
    runner.step(0, "cast", {"card": f"@{vault}", "resolves": "auto_permanent"})
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    hook = {"kind": "hold", "source": vault, "step": "untap", "text": "does not untap during its controller's untap step"}
    assert table.apply_hooks([hook, hook])["log"] == [f"Ledger: {vault}: {hook['text']}"]   # a repeat adds nothing
    assert table.view(1)["ledger"][0]["seat"] == 0 and table.view(1)["ledger"][0]["source"].startswith("c")
    with pytest.raises(Exception, match="0 permanents on the battlefield are called 'Not Here'"):
        table.apply_hooks([{"kind": "hold", "source": "Not Here", "step": "draw"}])
    runner.step(0, "act", {"mutations": [{"op": "tap", "objs": [f"@{vault}"]}], "because": "for mana"})
    for _ in range(3):   # round the table to Ann's next untap step
        pass_until(table, runner, "end")
        for seat in table.game.turn_order_from(table.game.turn.active):
            runner.step(seat, "pass_priority")
        if table.expected()[0][1] == "discard":
            runner.step(table.expected()[0][0], "finish_step")
    assert table.expected() == [(0, "held_step")] and table.view(0)["pen"]["step"] == "untap"
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()
    runner.step(0, "finish_step")                      # Ann untaps nothing: the vault stays tapped
    assert table.view(0)["battlefield"][0]["tapped"] and table.game.turn.step == "upkeep"
    runner.step(0, "act", {"mutations": [{"op": "move", "obj": f"@{vault}", "to": "graveyard"}], "because": "sacrificed"})
    assert table.view(0)["ledger"] == []               # the hook left with its source


def test_passing_until_a_step_skips_the_steps_between_and_stops_when_something_happens():
    table, runner = started()
    assert table.game.turn.step == "upkeep"
    runner.step(0, "pass_priority", {"until": "upkeep"}, error="bad_step")
    count = len(table.events)
    runner.step(0, "pass_priority", {"until": "main1"})
    runner.step(1, "pass_priority"), runner.step(2, "pass_priority")     # upkeep ends; Ann's draw step passes by itself
    runner.step(1, "pass_priority"), runner.step(2, "pass_priority")
    assert table.game.turn.step == "main1" and table.expected() == [(0, "priority")] and len(table.events) == count + 5
    assert table.game.seats[0].yield_to is None                         # reached: the request is spent
    runner.step(0, "pass_priority", {"until": "end"})
    runner.step(1, "cast", {"card": f"@{spell_in_hand(table, 1)}"})     # something happens: Ann is asked again
    runner.step(1, "pass_priority"), runner.step(2, "pass_priority")
    assert table.expected() == [(0, "priority")] and table.game.turn.step == "main1" and table.game.stack
    runner.step(0, "pass_priority")                                     # everyone has now passed: Bob's spell resolves
    runner.step(1, "finish_resolution")
    runner.step(0, "pass_priority", {"until": "end"})                   # through combat: attacks with nothing
    for _ in range(12):
        if table.game.turn.step == "end":
            break
        holder = table.expected()[0][0]
        assert holder != 0
        runner.step(holder, "pass_priority")
    assert table.game.turn.step == "end" and table.game.turn.attackers == [] and table.expected() == [(0, "priority")]
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()


def test_a_mutation_written_with_its_name_as_the_key_is_understood():
    table, runner = started()
    pass_until(table, runner, "main1")
    result = runner.step(0, "act", {"mutations": [{"life": {"seat": 1, "delta": -3}}], "because": "test"})
    assert result["log"][-1] == "Bob is at 37 life."


def test_a_wrong_commander_damage_total_can_be_corrected():
    table, runner = started()
    pass_until(table, runner, "main1")
    commander = "@Ann's Commander"
    runner.step(0, "act", {"because": "test", "mutations": [{"op": "commander_damage", "seat": 1, "commander": commander, "amount": 16}]})
    runner.step(0, "act", {"because": "double-counted", "mutations": [{"op": "commander_damage", "seat": 1, "commander": commander, "set": 8}]})
    assert table.view(2)["seats"][1]["commander_damage"] == {"Ann's Commander (Ann)": 8}
    runner.step(0, "act", {"because": "x", "mutations": [{"op": "commander_damage", "seat": 1, "commander": commander, "amount": -8}]},
                error="bad_count")


def test_a_face_down_permanents_name_never_reaches_another_seat():
    table, runner = started()
    pass_until(table, runner, "begin_combat")
    secret = spell_in_hand(table, 0)
    runner.step(0, "act", {"because": "morph", "mutations": [{"op": "move", "obj": f"@{secret}", "to": "battlefield", "face_down": True}]})
    mine = table.view(0)["battlefield"][0]
    assert mine["name"] == secret and "name" not in table.view(1)["battlefield"][0]
    runner.step(0, "act", {"because": "x", "mutations": [{"op": "counters", "obj": mine["handle"], "kind": "+1/+1", "delta": 1},
                                                         {"op": "tap", "objs": [mine["handle"]]}, {"op": "untap", "objs": [mine["handle"]]}]})
    table.game.objects[table.game.battlefield[0]].entered_turn = 0          # as if it had been there a turn
    runner.step(0, "pass_priority"), runner.step(1, "pass_priority"), runner.step(2, "pass_priority")
    runner.step(0, "declare_attackers", {"attacks": [{"attacker": mine["handle"], "target": {"seat": 1}, "tap": False}]})
    assert not table.view(0)["battlefield"][0]["tapped"]                    # `tap: false` is vigilance, not a handle
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    face_down = table.view(1)["battlefield"][0]["handle"]
    runner.step(1, "declare_blockers", {"blocks": []})
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    runner.step(0, "combat_damage", {"assignments": [{"source": mine["handle"], "target": {"seat": 1}, "amount": 2}]})
    assert face_down.startswith("c") and secret not in json.dumps([table.act(1, "get_log"), table.view(1), table.view(None)])
    assert "a face-down card deals 2 to Bob" in json.dumps(table.act(1, "get_log"))


def test_the_record_keeps_what_was_declared_when_a_spell_was_cast():
    logs = []
    for x in (1, 5):
        table, runner = started()
        pass_until(table, runner, "main1")
        runner.step(0, "cast", {"card": f"@{spell_in_hand(table, 0)}", "x": x, "modes": ["second"], "targets": [{"seat": 1}],
                                "text": "X damage divided as I choose"})
        logs.append(" ".join(table.events[-1]["public"]))
    assert logs[0] != logs[1] and "[X=5; modes: second; announced: \"X damage divided as I choose\"]" in logs[1]


def test_a_rejected_call_uses_no_randomness_and_a_replayed_table_rolls_on_as_the_original_would():
    def rolls(table: Table) -> list:
        return table.act(0, "act", {"because": "x", "mutations": [{"op": "roll", "sides": 100, "count": 6}]})["values"]

    table, runner = started()
    pass_until(table, runner, "main1")
    other, other_runner = started()
    pass_until(other, other_runner, "main1")
    runner.step(0, "act", {"because": "x", "mutations": [{"op": "roll", "sides": 100, "count": 9}, {"op": "nonsense"}]},
                error="unknown_op")
    assert rolls(table) == rolls(other)                     # the rejected batch consumed nothing
    copy = Table.replay(table.config, table.events)
    assert rolls(copy) == rolls(table)                      # and a replay continues with the same dice


def test_the_turn_goes_on_without_its_active_player():
    table, runner = started()
    pass_until(table, runner, "main1")
    runner.step(0, "pass_priority")
    bolt = spell_in_hand(table, 1)
    runner.step(1, "cast", {"card": f"@{bolt}", "targets": [{"seat": 2}]})
    runner.step(0, "concede")                               # Ann leaves during her own turn, with Bob's spell on the stack
    view = table.view(2)
    assert [item["source"] for item in view["stack"]] == [bolt] and view["turn"]["active"] == 0 and view["turn"]["number"] == 1
    assert table.expected() == [(1, "priority")]
    runner.step(1, "pass_priority"), runner.step(2, "pass_priority")
    assert table.expected() == [(1, "resolution")]
    runner.step(1, "finish_resolution")
    assert table.expected() == [(1, "priority")]            # the next player in turn order, in place of the missing active player
    for _ in range(40):                                     # the turn runs to its end with no attack and no discard
        if table.game.turn.number == 2:
            break
        runner.step(table.expected()[0][0], "pass_priority")
    assert table.game.turn.number == 2 and table.game.turn.active == 1 and table.game.turn.step == "upkeep"
    assert all(obj.zone != "stack" for obj in table.game.objects.values())


def test_a_hook_binds_to_one_permanent_even_when_two_share_a_name():
    table, runner = started()
    pass_until(table, runner, "main1")
    for seat in (0, 1):
        table.patch([{"op": "create_token", "controller": seat, "token": {"name": "Howling Mine"}}], "set up")
    anns, bobs = [c["handle"] for c in table.view(None)["battlefield"]]
    hook = {"kind": "step_trigger", "source": "Howling Mine", "step": "draw", "whose": "each", "text": "draws an additional card"}
    with pytest.raises(Exception, match="2 permanents"):
        table.apply_hooks([hook])                                   # the name alone is ambiguous
    with pytest.raises(Exception, match="is not Sol Ring"):
        table.apply_hooks([{**hook, "handle": anns, "source": "Sol Ring"}])
    table.apply_hooks([{**hook, "handle": anns}])
    assert [e["controller"] for e in table.game.ledger] == [0]
    table.patch([{"op": "move", "obj": table.handles[None].resolve("c", bobs), "to": "graveyard"}], "Bob's is destroyed")
    assert len(table.game.ledger) == 1                              # Ann's hook does not leave with Bob's permanent
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()


def test_a_gated_table_stops_for_an_audit_before_the_pen_moves_or_a_step_runs():
    journal = []
    table = Table(seats(), seed=5, first=0, gated=True, on_event=journal.append)
    runner = ScriptedRunner(table)
    for seat in range(3):
        runner.step(seat, "keep")
    for _ in range(12):                                     # passing is not auditable: no checkpoint on the way to main 1
        if table.game.turn.step == "main1":
            break
        runner.step(table.expected()[0][0], "pass_priority")
    assert not table.awaiting_audit and journal == table.events
    runner.step(0, "play_land", {"card": "@Island"})
    assert table.expected() == [(0, "priority")]            # the actor keeps the pen: nothing to stop yet
    runner.step(0, "pass_priority")
    assert table.awaiting_audit and table.expected() == [] and table.view(1)["pen"]["kind"] == "audit"
    assert table.act(1, "pass_priority")["error"]["code"] == "not_your_pen"
    table.apply_hooks([{"kind": "hold", "source": "Island", "step": "untap", "text": "a test hook"}])
    table.release()
    assert table.expected() == [(1, "priority")] and not table.game.audit_pending
    with pytest.raises(Exception, match="not waiting"):
        table.release()
    runner.step(1, "pass_priority"), runner.step(2, "pass_priority")
    assert table.game.turn.step == "begin_combat" and not table.awaiting_audit
    for _ in range(40):                                     # however little happens, the turn still ends at a checkpoint
        if table.awaiting_audit and table.game.control.detail["resume"]["do"] == "next_turn":
            break
        if table.awaiting_audit:                            # declaring no attackers is an action too
            table.release()
            continue
        seat, kind = table.expected()[0]
        runner.step(seat, {"priority": "pass_priority", "declare_attackers": "declare_attackers", "discard": "finish_step"}[kind])
    assert table.game.control.detail == {"resume": {"do": "next_turn"}} and table.game.turn.number == 1
    table.release()
    assert table.game.turn.number == 2 and table.expected() == [(1, "held_step")] or table.expected() == [(1, "priority")]
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()


def test_at_a_checkpoint_a_trigger_can_be_queued_and_a_finding_corrected():
    table = Table(seats(), seed=5, first=0, gated=True)
    runner = ScriptedRunner(table)
    for seat in range(3):
        runner.step(seat, "keep")
    for _ in range(12):
        if table.game.turn.step == "main1":
            break
        runner.step(table.expected()[0][0], "pass_priority")
    runner.step(0, "act", {"because": "a spell's effect", "mutations": [{"op": "life", "seat": 1, "delta": -5}]})
    runner.step(0, "pass_priority")
    assert table.awaiting_audit
    table.open_correction(0, "Bob should have lost 3 life, not 5.")
    assert table.expected() == [(0, "correction")] and table.act(1, "act", {"mutations": [], "because": "x"})["error"]["code"] == "not_your_pen"
    runner.step(0, "pass_priority", error="not_your_pen")               # a correction is not a chance to play
    runner.step(0, "act", {"because": "correction", "mutations": [{"op": "life", "seat": 1, "delta": 2}]})
    runner.step(0, "finish_step")
    assert table.awaiting_audit and table.view(2)["seats"][1]["life"] == 37
    table.queue_trigger(2, "Soul Warden: you gain 1 life")
    table.release()
    assert table.expected() == [(2, "triggers")]                        # before anyone receives priority
    runner.step(2, "add_trigger", {"pending": table.view(2)["waiting_triggers"][0]["handle"]})
    assert table.awaiting_audit                                         # putting a trigger on the stack is auditable too
    table.release()
    assert table.expected() == [(1, "priority")]
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()


def test_the_auditors_computed_characteristics_and_static_effects_are_written_on_the_board():
    from fcsim.audit import describe
    table, runner = started()
    pass_until(table, runner, "main1")
    for name in ("Lord of the Undead", "Zombie"):
        table.patch([{"op": "create_token", "controller": 0, "token": {"name": name}}], "set up")
    lord, zombie = [c["handle"] for c in table.view(None)["battlefield"]]
    table.apply_hooks([{"kind": "static", "handle": lord, "source": "Lord of the Undead", "text": "Other Zombie creatures get +1/+1."}])
    table.apply_computed([{"handle": zombie, "power": "3", "toughness": "3", "because": "Lord of the Undead"},
                          {"handle": "c999", "power": "9", "toughness": "9", "because": "no such permanent"}])
    seen = table.view(2)["battlefield"][1]["computed"]
    assert seen == {"power": "3", "toughness": "3", "because": "Lord of the Undead", "as_of": "turn 1, main1"}
    text = describe(table.view(2))
    assert text.startswith("CONTINUOUS EFFECTS IN PLAY") and "Ann (seat 0): Other Zombie creatures get +1/+1." in text
    assert "NOW 3/3 (Lord of the Undead; worked out turn 1, main1)" in text
    table.apply_computed([])                                   # each list replaces the last
    assert "computed" not in table.view(2)["battlefield"][1]
    table.apply_computed([{"handle": zombie, "power": "3", "toughness": "3", "because": "Lord of the Undead"}])
    runner.step(0, "act", {"because": "bounced", "mutations": [{"op": "move", "obj": "@Lord of the Undead", "to": "graveyard"}]})
    assert "CONTINUOUS EFFECTS" not in describe(table.view(2))  # the effect leaves with its source
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()


def test_the_table_queues_a_trigger_the_moment_its_event_happens():
    lookup = {"Ann spell 0": "Creature — Zombie", "Blood Artist": "Creature — Vampire"}
    table = Table(seats(), seed=5, first=0, card_lookup=lambda name: {"type_line": lookup.get(name, "Instant")})
    runner = ScriptedRunner(table)
    for seat in range(3):
        runner.step(seat, "keep")
    pass_until(table, runner, "main1")
    table.patch([{"op": "create_token", "controller": 1, "token": {"name": "Blood Artist", "types": "Creature — Vampire"}},
                 {"op": "create_token", "controller": 0, "token": {"name": "Zombie", "types": "Creature — Zombie"}, "count": 2},
                 {"op": "create_token", "controller": 0, "token": {"name": "Treasure", "types": "Artifact — Treasure"}}], "set up")
    artist = next(c["handle"] for c in table.view(None)["battlefield"] if c["name"] == "Blood Artist")
    table.apply_hooks([{"kind": "event_trigger", "handle": artist, "source": "Blood Artist", "text": "Target player loses 1 life and you gain 1.",
                        "on": {"event": "dies", "type": "Creature", "who": "any"}},
                       {"kind": "event_trigger", "handle": artist, "source": "Blood Artist", "text": "a made-up cast trigger",
                        "on": {"event": "cast", "who": "opponent", "type": "Instant"}}])
    assert table.view(0)["ledger"][0]["on"] == {"event": "dies", "type": "Creature", "who": "any"}

    runner.step(0, "act", {"because": "sacrificed", "mutations": [{"op": "move", "obj": "@Treasure", "to": "graveyard"}]})
    runner.step(0, "pass_priority")
    assert table.expected() == [(1, "priority")]                       # an artifact leaving is not a creature dying
    runner.step(1, "pass_priority"), runner.step(2, "pass_priority")

    runner.step(0, "act", {"because": "sacrificed", "mutations": [{"op": "move", "obj": "@Zombie", "to": "graveyard"}]})
    runner.step(0, "pass_priority")
    assert table.expected() == [(1, "triggers")]                       # Bob is asked before anyone receives priority
    waiting = table.view(1)["waiting_triggers"]
    assert len(waiting) == 1 and waiting[0]["text"] == "Target player loses 1 life and you gain 1. (because Zombie died)"
    runner.step(1, "add_trigger", {"pending": waiting[0]["handle"], "targets": [{"seat": 0}]})
    assert table.expected() == [(1, "priority")] and table.view(0)["stack"][0]["controller"] == 1
    for seat in (1, 2, 0):
        runner.step(seat, "pass_priority")
    runner.step(1, "finish_resolution")

    runner.step(0, "cast", {"card": f"@{spell_in_hand(table, 0)}"})     # "Ann spell N": an Instant to the lookup, cast by Bob's opponent
    runner.step(0, "pass_priority")
    assert table.expected() == [(1, "triggers")] and "made-up cast trigger" in table.view(1)["waiting_triggers"][0]["text"]
    runner.step(1, "skip_trigger", {"pending": table.view(1)["waiting_triggers"][0]["handle"], "because": "does not apply"})
    assert table.expected() == [(1, "priority")]
    assert Table.replay(table.config, table.events, card_lookup=table.card_lookup).game.to_dict() == table.game.to_dict()


def test_a_trigger_put_on_the_stack_by_hand_is_not_asked_for_again():
    table, runner = started()
    pass_until(table, runner, "main1")
    table.patch([{"op": "create_token", "controller": 0, "token": {"name": "Lozhan", "types": "Creature — Dragon"}}], "set up")
    lozhan = table.view(None)["battlefield"][0]["handle"]
    table.apply_hooks([{"kind": "event_trigger", "handle": lozhan, "source": "Lozhan", "text": "deals damage equal to its mana value",
                        "on": {"event": "cast", "who": "you"}}])
    runner.step(0, "cast", {"card": f"@{spell_in_hand(table, 0)}"})
    runner.step(0, "add_trigger", {"source": "@Lozhan", "text": "Lozhan deals 5 damage", "targets": [{"seat": 1}]})
    runner.step(0, "pass_priority")
    assert table.expected() == [(1, "priority")] and len(table.view(0)["stack"]) == 2 and table.game.pending_triggers == []


def test_a_contested_finding_waits_for_the_judge_and_the_ruling_is_kept():
    table = Table(seats(), seed=5, first=0, gated=True)
    runner = ScriptedRunner(table)
    for seat in range(3):
        runner.step(seat, "keep")
    for _ in range(12):
        if table.game.turn.step == "main1":
            break
        runner.step(table.expected()[0][0], "pass_priority")

    def wrong_finding(text: str) -> None:
        for _ in range(12):  # round to Ann again
            if table.awaiting_audit:
                table.release()
            elif table.expected()[0] == (0, "priority"):
                break
            else:
                seat, kind = table.expected()[0]
                runner.step(seat, {"priority": "pass_priority", "discard": "finish_step"}.get(kind, kind))
        runner.step(0, "act", {"because": "a spell's effect", "mutations": [{"op": "life", "seat": 1, "delta": -1}]})
        if not table.awaiting_audit:
            runner.step(0, "pass_priority")
        table.open_correction(0, text)

    wrong_finding("Bob should have lost 3 life.")
    runner.step(1, "contest", {"reason": "not mine"}, error="not_your_pen")     # only the seat the finding is about
    runner.step(0, "contest", {"reason": ""}, error="empty")
    runner.step(0, "contest", {"reason": "The card says 1 damage."})
    assert table.game.control.kind == "dispute" and table.expected() == [] and not table.awaiting_audit
    runner.step(0, "act", {"because": "x", "mutations": []}, error="not_your_pen")   # nobody holds the pen until the judge rules
    assert "contests the auditor's finding: The card says 1 damage. The judge will rule." in table.events[-1]["public"][0]
    table.rule("overrule", "The card deals 1 damage.", ruling="Shock-alike deals 1 damage.")
    assert table.awaiting_audit and table.view(2)["rulings"] == [{"turn": 1, "text": "Shock-alike deals 1 damage."}]
    assert table.game.seats[0].failed_disputes == 0 and "overrules the auditor" in table.events[-1]["public"][0]
    table.release()

    wrong_finding("Bob should have lost 2 life.")
    runner.step(0, "contest", {"reason": "It says 1."})
    table.rule("uphold", "The card says 2.")
    assert table.expected() == [(0, "correction")] and table.game.control.detail["final"] and table.game.seats[0].failed_disputes == 1
    runner.step(0, "contest", {"reason": "Still 1."}, error="ruling_final")     # the ruling is final
    runner.step(0, "act", {"because": "the judge's ruling", "mutations": [{"op": "life", "seat": 1, "delta": -1}]})
    runner.step(0, "finish_step")
    table.release()

    wrong_finding("Bob should have gained life.")
    runner.step(0, "contest", {"reason": "No lifelink here."})
    table.rule("amend", "There is a problem, but not that one.", fix="Bob loses 1 more life.")
    assert table.game.control.detail["finding"] == "Bob loses 1 more life." and table.game.seats[0].failed_disputes == 1
    assert "What Ann must do: Bob loses 1 more life." in table.events[-1]["public"][0]
    runner.step(0, "finish_step")
    table.release()

    runner.step(2, "dispute", {"claim": "Ann never paid for that.", "event": 9999}, error="bad_event")
    runner.step(2, "dispute", {"claim": "Ann never paid for that.", "event": 5})
    assert "Cat disputes event 5: Ann never paid for that." in table.events[-1]["public"][0] and table.game.audit_pending
    table.answer_dispute(2, False, "Nothing had a cost.")
    assert table.game.seats[2].failed_disputes == 1 and "The auditor rejects Cat's dispute" in table.events[-1]["public"][0]
    table.answer_dispute(2, False, "No.")
    table.answer_dispute(2, False, "No.")
    runner.step(2, "dispute", {"claim": "And another thing."}, error="no_more_disputes")
    with pytest.raises(ValueError, match="No contested finding"):
        table.rule("uphold", "nothing to rule on")
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()


def test_a_land_is_played_only_in_ones_own_main_phase_but_an_old_journal_still_replays():
    table, runner = started()
    assert table.game.turn.step == "upkeep"
    land = next(c["handle"] for c in table.view(0)["seats"][0]["hand"]["cards"] if c["name"] == "Island")
    refused = table.act(0, "play_land", {"card": land})
    assert refused["error"]["code"] == "not_now" and 'pass priority until "main1"' in refused["error"]["message"]
    # A journal written before the table checked this holds such a play, and must still rebuild its game.
    table._replaying = True
    assert "error" not in table.act(0, "play_land", {"card": land})
    table._replaying = False
    assert table.events[-1]["lints"] == ["A land is normally played in your own main phase with an empty stack."]
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()


def test_casting_from_an_unusual_zone_needs_the_player_to_say_what_allows_it():
    table, runner = started()
    pass_until(table, runner, "main1")
    spell = spell_in_hand(table, 0)
    runner.step(0, "act", {"because": "test", "mutations": [{"op": "move", "obj": f"@{spell}", "to": "exile"}]})
    refused = table.act(0, "cast", {"card": table.view(0)["seats"][0]["exile"][0]["handle"]})
    assert refused["error"]["code"] == "say_why" and "Ann's exile" in refused["error"]["message"]      # a slip of a handle changes nothing
    assert "error" not in table.act(0, "cast", {"card": table.view(0)["seats"][0]["exile"][0]["handle"], "because": "Exiled with Prosper: I may play it."})
    assert "(allowed by: Exiled with Prosper: I may play it.)" in table.events[-1]["public"][0]
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()


def test_a_card_that_is_not_an_instant_needs_a_reason_to_be_cast_off_its_timing():
    kinds: dict[str, dict] = {}
    table = Table(seats(), seed=5, first=0, card_lookup=lambda name: kinds.get(name))
    runner = ScriptedRunner(table)
    for seat in range(3):
        runner.step(seat, "keep")
    creature, instant, flash = [name for name in hand(table, 0) if "spell" in name][:3]
    kinds.update({creature: {"types": ["Creature"], "keywords": []}, instant: {"types": ["Instant"], "keywords": []},
                  flash: {"types": ["Creature"], "keywords": ["Flash"]}})
    pass_until(table, runner, "begin_combat", seat=0)

    def cast(name: str, **more) -> dict:
        return table.act(0, "cast", {"card": next(c["handle"] for c in table.view(0)["seats"][0]["hand"]["cards"] if c["name"] == name), **more})
    refused = cast(creature)
    assert refused["error"]["code"] == "say_why" and "begin_combat step of Ann's turn" in refused["error"]["message"]
    assert "error" not in cast(instant) and "error" not in cast(flash)             # an instant; a creature with flash, even with a spell on the stack
    assert "error" not in cast(creature, because="Vedalken Orrery gives my spells flash.")
    assert "(allowed by: Vedalken Orrery gives my spells flash.)" in table.events[-1]["public"][0]


def test_a_land_can_be_played_from_exile_when_the_player_names_what_allows_it():
    table, runner = started()
    pass_until(table, runner, "main1")
    runner.step(0, "act", {"because": "test: exile the top card, you may play it", "mutations": [{"op": "move", "obj": "@Island", "to": "exile"}]})
    exiled = table.view(0)["seats"][0]["exile"][0]["handle"]
    assert table.act(0, "play_land", {"card": exiled})["error"]["code"] == "say_why"
    assert "error" not in table.act(0, "play_land", {"card": exiled, "because": "Exiled with Prosper: I may play it this turn."})
    assert table.events[-1]["public"][0] == "Ann plays Island from Ann's exile (allowed by: Exiled with Prosper: I may play it this turn.)."
    assert table.view(0)["turn"]["land_plays"] == 1


def combat_table(facts: dict) -> tuple[Table, ScriptedRunner, dict]:
    """Ann and Bob each get two creatures from their hands onto the battlefield on Ann's first main
    phase; `facts` maps A1, A2, B1, B2 to what the table is told about them. Returns their names too."""
    told: dict[str, dict] = {}
    table = Table(seats(), seed=5, first=0, card_lookup=lambda name: told.get(name))
    runner = ScriptedRunner(table)
    for seat in range(3):
        runner.step(seat, "keep")
    pass_until(table, runner, "main1")
    names = {}
    for seat, keys in ((0, ("A1", "A2")), (1, ("B1", "B2"))):
        spells = [n for n in hand(table, seat) if "spell" in n][:2]
        for key, name in zip(keys, spells, strict=True):
            names[key] = name
            told[name] = {"types": ["Creature"], "keywords": [], **facts.get(key, {"power": "1", "toughness": "1"})}
            handle = next(c["handle"] for c in table.view(seat)["seats"][seat]["hand"]["cards"] if c["name"] == name)
            if seat == 0:
                assert "error" not in table.act(0, "act", {"because": "test set-up", "mutations": [{"op": "move", "obj": handle, "to": "battlefield"}]})
            else:  # Bob's hand is hidden from Ann, who holds the pen: the test reaches into the state directly
                mutations.move(table.game, next(o for o in table.game.objects.values() if o.name == name), "battlefield", controller=seat)
    for obj in table.game.objects.values():
        if obj.zone == "battlefield":
            obj.entered_turn = 0  # they have been here a while
    return table, runner, names


def to_damage(table: Table, runner: ScriptedRunner, attacks: list[tuple[str, int]], blocks: list[tuple[str, str]]) -> None:
    pass_until(table, runner, "begin_combat")
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    runner.step(0, "declare_attackers", {"attacks": [{"attacker": f"@{name}", "target": {"seat": seat}} for name, seat in attacks]})
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    if table.expected() == [(1, "declare_blockers")]:
        runner.step(1, "declare_blockers", {"blocks": [{"blocker": f"@{b}", "attacker": f"@{a}"} for b, a in blocks]})
        for seat in (0, 1, 2):
            runner.step(seat, "pass_priority")
    assert table.expected() == [(0, "combat_damage")]


def test_the_table_does_the_arithmetic_of_combat():
    table, runner, n = combat_table({"A1": {"power": "4", "toughness": "4", "keywords": ["Trample"]},
                                     "A2": {"power": "2", "toughness": "2", "keywords": ["Lifelink"]},
                                     "B1": {"power": "1", "toughness": "2", "keywords": ["Deathtouch"]}})
    to_damage(table, runner, [(n["A1"], 1), (n["A2"], 1)], [(n["B1"], n["A1"])])
    offered = table.proposed_damage(0)
    assert offered["lines"] == [f"{n['A1']} deals 2 to {n['B1']}", f"{n['A1']} deals 2 to Bob", f"{n['B1']} deals 1 to {n['A1']}",
                                f"{n['A2']} deals 2 to Bob", "Ann gains 2 (lifelink)", f"{n['B1']} dies", f"{n['A1']} dies"]
    assert offered["basis"] == ["printed values and counters"] and offered["needs_your_choice"] == []
    done = runner.step(0, "combat_damage", {"as_proposed": True})
    assert done["log"][0] == "Combat damage as the table worked it out from printed values and counters:"
    view = table.view(2)
    assert view["seats"][1]["life"] == 36 and view["seats"][0]["life"] == 42
    assert [c["name"] for c in view["seats"][0]["graveyard"]] == [n["A1"]] and [c["name"] for c in view["seats"][1]["graveyard"]] == [n["B1"]]
    assert table.game.turn.step != "combat_damage" or table.expected() != [(0, "combat_damage")]   # one step: nobody had first strike


def test_first_strike_makes_two_steps_only_when_someone_is_left_to_strike_second():
    table, runner, n = combat_table({"A1": {"power": "2", "toughness": "2", "keywords": ["First strike"]},
                                     "A2": {"power": "3", "toughness": "3"}, "B1": {"power": "2", "toughness": "2"}})
    to_damage(table, runner, [(n["A1"], 1), (n["A2"], 1)], [(n["B1"], n["A1"])])
    assert table.proposed_damage(0)["first_strike_step"] and table.proposed_damage(0)["lines"] == [f"{n['A1']} deals 2 to {n['B1']}", f"{n['B1']} dies"]
    runner.step(0, "combat_damage", {"as_proposed": True})
    for seat in (0, 1, 2):
        if table.expected() and table.expected()[0] == (seat, "priority"):
            runner.step(seat, "pass_priority")
    assert table.expected() == [(0, "combat_damage")]                       # the 3/3 has yet to strike
    assert table.proposed_damage(0)["lines"] == [f"{n['A2']} deals 3 to Bob"]
    runner.step(0, "combat_damage", {"as_proposed": True})
    assert table.view(0)["seats"][1]["life"] == 37 and n["A1"] in [c["name"] for c in table.view(0)["battlefield"]]


def test_the_auditors_stamp_a_commander_and_what_the_table_refuses():
    table, runner, n = combat_table({"A1": {"power": "2", "toughness": "2"}, "A2": {"power": "1", "toughness": "1", "keywords": ["Flying"]},
                                     "B1": {"power": "3", "toughness": "3"}, "B2": {"power": "*", "toughness": "*"}})
    handles = {o["name"]: o["handle"] for o in table.view(None)["battlefield"]}
    table.apply_computed([{"handle": handles[n["A1"]], "power": "5", "toughness": "5", "keywords": ["indestructible"], "because": "an Equipment"}])
    pass_until(table, runner, "begin_combat")
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    table.game.objects[next(o.id for o in table.game.objects.values() if o.name == n["A2"])].entered_turn = table.game.turn.number
    sick = table.act(0, "declare_attackers", {"attacks": [{"attacker": table.view(0)["battlefield"][1]["handle"], "target": {"seat": 1}}]})
    assert sick["error"]["code"] == "say_why" and "no haste" in sick["error"]["message"]
    table.game.objects[next(o.id for o in table.game.objects.values() if o.name == n["A2"])].entered_turn = 0
    runner.step(0, "declare_attackers", {"attacks": [{"attacker": f"@{n['A1']}", "target": {"seat": 1}}, {"attacker": f"@{n['A2']}", "target": {"seat": 1}}]})
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    grounded = table.act(1, "declare_blockers", {"blocks": [{"blocker": table.view(1)["battlefield"][2]["handle"], "attacker": table.view(1)["battlefield"][1]["handle"]}]})
    assert grounded["error"]["code"] == "say_why" and "has flying" in grounded["error"]["message"]
    runner.step(1, "declare_blockers", {"blocks": [{"blocker": f"@{n['B1']}", "attacker": f"@{n['A1']}"}]})
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    offered = table.proposed_damage(0)
    assert f"{n['A1']} deals 5 to {n['B1']}" in offered["lines"] and f"{n['B1']} dies" in offered["lines"] and f"{n['A1']} dies" not in offered["lines"]
    assert any(b.startswith("the auditor's numbers as of turn 1") for b in offered["basis"])
    # A creature the table has no numbers for (a printed "*") in combat: no proposal, and the player deals damage by hand.
    table.game.turn.blockers.append({"blocker": next(o.id for o in table.game.objects.values() if o.name == n["B2"]), "attacker": table.game.turn.attackers[1]["attacker"]})
    assert table.proposed_damage(0) is None and table.act(0, "combat_damage", {"as_proposed": True})["error"]["code"] == "no_proposal"


def test_the_table_checks_that_what_was_tapped_can_pay_the_cost():
    told: dict[str, dict] = {"Island": {"types": ["Land"], "produces": ["U"], "mana_amount": 1, "mana_cost": ""}}
    table = Table(seats(), seed=5, first=0, card_lookup=lambda name: told.get(name))
    runner = ScriptedRunner(table)
    for seat in range(3):
        runner.step(seat, "keep")
    pass_until(table, runner, "main1")
    green, blue = [name for name in hand(table, 0) if "spell" in name][:2]
    told[green] = {"types": ["Creature"], "keywords": [], "mana_cost": "{1}{G}", "produces": [], "mana_amount": 1}
    told[blue] = {"types": ["Creature"], "keywords": [], "mana_cost": "{U}{U}", "produces": [], "mana_amount": 1}
    for _ in range(2):
        runner.step(0, "play_land", {"card": "@Island"})
    assert table.affordable(0) == [blue]                                              # what the briefing will say
    refused = table.act(0, "cast", {"card": next(c["handle"] for c in table.view(0)["seats"][0]["hand"]["cards"] if c["name"] == green),
                                    "payment": {"tap": [c["handle"] for c in table.view(0)["battlefield"]]}})
    assert refused["error"]["code"] == "cannot_pay" and "{1}{G} cannot be paid with what you tapped (U, U)" in refused["error"]["message"]
    assert not any(c["tapped"] for c in table.view(0)["battlefield"])                 # a refused call changes nothing
    runner.step(0, "cast", {"card": f"@{green}", "payment": {"tap": ["@Island", "@Island#2"], "note": "Chromatic Lantern makes my lands any color"}})
    table2_mana = __import__("fcsim.table.mana", fromlist=["x"])
    assert table2_mana.pays(table2_mana.parse_cost("{2}{G}{G}"), [{"colors": {"G"}, "amount": 1}, {"colors": {"B", "G"}, "amount": 1}, {"colors": {"C"}, "amount": 2}])
    assert not table2_mana.pays(table2_mana.parse_cost("{2}{G}{G}"), [{"colors": {"B"}, "amount": 1}] * 2 + [{"colors": {"C"}, "amount": 1}, {"colors": {"G"}, "amount": 1}])
    assert table2_mana.pays(table2_mana.parse_cost("{R/W}{X}", x=2), [{"colors": {"W"}, "amount": 1}, {"colors": {"C"}, "amount": 2}])
    assert table2_mana.parse_cost("{Q}") is None
