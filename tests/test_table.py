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
    played = next(o["handle"] for o in table.view(0)["battlefield"] if o["name"] == "Island")
    runner.step(0, "act", {"mutations": [{"op": "move", "obj": played, "to": "hand"}], "because": "bounced"})
    for viewer in (1, 2, None):   # everyone saw it go there, the auditor too; what was drawn unseen stays unseen
        assert [c["name"] for c in table.view(viewer)["seats"][0]["hand"]["known"]] == ["Island"]
    runner.step(0, "act", {"mutations": [{"op": "draw", "count": 1}], "because": "test"})
    assert len(table.view(1)["seats"][0]["hand"]["known"]) == 1
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()


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
    runner.step(2, "add_trigger", {})   # the only trigger waiting: no need to say which
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
    runner.step(0, "pass_priority", {"until": "sometime"}, error="bad_step")
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
    # The next combat starts clean: on Ann's next turn the table works the damage out again.
    for _ in range(80):
        seat, kind = table.expected()[0]
        if table.game.turn.active == 0 and table.game.turn.step == "begin_combat" and table.game.turn.number > 1:
            break
        runner.step(seat, {"priority": "pass_priority", "discard": "finish_step", "declare_attackers": "declare_attackers"}.get(kind, kind))
    assert table.game.turn.damage_steps == 0 and table.game.turn.attackers == []
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    runner.step(0, "declare_attackers", {"attacks": [{"attacker": f"@{n['A2']}", "target": {"seat": 2}}]})
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    while table.expected() and table.expected()[0][1] != "combat_damage":
        seat, kind = table.expected()[0]
        runner.step(seat, {"priority": "pass_priority"}.get(kind, kind))
    assert table.proposed_damage(0)["lines"] == [f"{n['A2']} deals 3 to Cat"]


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
    offered = table.affordable(0)                                                     # what the briefing will say
    assert [a["name"] for a in offered] == [blue] and len(offered[0]["tap"]) == 2 and offered[0]["card"].startswith("c")
    from fcsim.table.mana import parse_cost, plan
    rainbow, forest, swamp = {"colors": set("WUBRG"), "amount": 1, "id": 1}, {"colors": {"G"}, "amount": 1, "id": 2}, {"colors": {"B"}, "amount": 1, "id": 3}
    assert [s["id"] for s in plan(parse_cost("{1}{G}"), [rainbow, forest, swamp])] == [2, 3]    # the land that makes any color is kept back
    assert plan(parse_cost("{G}{G}"), [forest, swamp]) is None
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


def test_an_event_trigger_can_depend_on_where_the_spell_was_cast_from():
    table, runner = started()
    pass_until(table, runner, "main1")
    engine, first, second = [name for name in hand(table, 0) if "spell" in name][:3]
    runner.step(0, "act", {"because": "test set-up", "mutations": [{"op": "move", "obj": f"@{engine}", "to": "battlefield"},
                                                                  {"op": "move", "obj": f"@{second}", "to": "exile"}]})
    handle = next(o["handle"] for o in table.view(None)["battlefield"] if o["name"] == engine)
    table.apply_hooks([{"kind": "event_trigger", "handle": handle, "text": "Whenever you cast a spell from exile, put a counter on each creature.",
                        "on": {"event": "cast", "who": "you", "from": "exile"}}])
    runner.step(0, "cast", {"card": f"@{first}"})
    runner.step(0, "pass_priority")
    assert table.view(0)["waiting_triggers"] == [] and table.expected() == [(1, "priority")]      # from hand: nothing to decline
    for seat in (1, 2):
        runner.step(seat, "pass_priority")
    runner.step(0, "finish_resolution")
    runner.step(0, "cast", {"card": table.view(0)["seats"][0]["exile"][0]["handle"], "because": "exiled with Prosper"})
    runner.step(0, "pass_priority")                                                               # triggers go on before anyone gets priority
    assert table.expected() == [(0, "triggers")]
    assert [t["text"] for t in table.view(0)["waiting_triggers"]] == [f"Whenever you cast a spell from exile, put a counter on each creature. (because {second} was cast)"]


def test_get_log_tells_a_run_of_passes_as_one_entry():
    table, runner = started()
    pass_until(table, runner, "main1")
    runner.step(0, "play_land", {"card": "@Island"})
    shown = table.act(1, "get_log", {"since": 0})["events"]
    runs = [e for e in shown if e.get("passes")]
    assert runs and runs[-1]["passes"] >= 6 and runs[-1]["text"] == ["Ann, Bob, Cat passed priority."] and runs[-1]["to"] > runs[-1]["n"]
    assert shown[-1]["tool"] == "play_land" and all(e["tool"] != "pass_priority" or e.get("passes") or e.get("private") for e in shown)
    mine = table.act(0, "get_log", {"since": 0})["events"]
    assert any(e["tool"] == "pass_priority" and e.get("private") for e in mine)    # the pass that drew Ann's card stays its own entry, for Ann


def test_a_trigger_on_the_permanent_itself_is_not_offered_when_something_else_enters_or_attacks():
    table, runner = started()
    pass_until(table, runner, "main1")
    moth, other = [name for name in hand(table, 0) if "spell" in name][:2]
    runner.step(0, "act", {"because": "test set-up", "mutations": [{"op": "move", "obj": f"@{moth}", "to": "battlefield"}]})
    handle = next(o["handle"] for o in table.view(None)["battlefield"] if o["name"] == moth)
    table.apply_hooks([{"kind": "event_trigger", "handle": handle, "text": "Whenever this enters or attacks, each player gets a rad counter.",
                        "on": {"event": "enters", "who": "you", "self": True}}])
    runner.step(0, "play_land", {"card": "@Island"})
    runner.step(0, "act", {"because": "test set-up", "mutations": [{"op": "move", "obj": f"@{other}", "to": "battlefield"}]})
    runner.step(0, "pass_priority")
    # Its own arrival is its business, once; the land and the other creature that entered are not.
    assert [t["text"] for t in table.view(0)["waiting_triggers"]] == [f"Whenever this enters or attacks, each player gets a rad counter. (because {moth} entered)"]
    # Rewriting a permanent's hooks replaces what the ledger held for it, and nothing else.
    table.rewrite_hooks(handle, [{"kind": "event_trigger", "text": "Whenever a creature enters under your control, gain 1 life.", "on": {"event": "enters", "who": "you"}}])
    assert [e["text"] for e in table.game.ledger] == ["Whenever a creature enters under your control, gain 1 life."]
    assert Table.replay(table.config, table.events).game.ledger == table.game.ledger


def test_the_rad_counter_trigger_is_the_tables_and_an_older_journal_replays_without_it():
    table, runner = started()
    pass_until(table, runner, "main1")
    runner.step(0, "act", {"because": "test", "mutations": [{"op": "counters", "seat": 1, "kind": "rad", "delta": 2}]})
    pass_until(table, runner, "end", seat=0)
    for _ in range(40):  # on to Bob's turn: the table stops for his trigger as his first main phase begins
        seat, kind = table.expected()[0]
        if kind == "triggers":
            break
        runner.step(seat, {"priority": "pass_priority", "discard": "finish_step"}.get(kind, kind))
    assert table.expected() == [(1, "triggers")] and table.game.turn.step == "main1" and table.game.turn.active == 1
    assert table.view(1)["waiting_triggers"][0]["text"].startswith("Rad counters (rule 728.1): mill 2 card(s)")
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()
    old = Table(seats(), seed=5, first=0, mechanics=0)     # as a journal from before this rule was the table's would be rebuilt
    assert old.config["mechanics"] == 0 and table.config["mechanics"] >= 1


def test_a_mutation_named_by_a_key_is_understood():
    table, runner = started()
    pass_until(table, runner, "main1")
    before = table.view(0)["seats"][0]["library"]["count"]
    for shape in ({"mill": {}, "seat": 0, "count": 1}, {"mill": {"seat": 0, "count": 1}}, {"op": "mill", "seat": 0, "count": 1}):
        assert "error" not in table.act(0, "act", {"because": "test", "mutations": [shape]})
    assert table.view(0)["seats"][0]["library"]["count"] == before - 3
    assert "error" not in table.act(0, "act", {"because": "test", "mutations": [{"move": True, "from": {"seat": 0, "zone": "library", "position": "top", "count": 2}, "to": "exile"}]})
    assert "error" not in table.act(0, "act", {"because": "test", "mutations": [{"life": {"seat": 0}, "delta": -1}]}) and table.view(0)["seats"][0]["life"] == 39
    assert table.act(0, "act", {"because": "test", "mutations": [{"tap": {}, "untap": {}}]})["error"]["code"] == "unknown_op"   # two names: no single meaning


def test_mills_and_combat_damage_are_events_a_trigger_can_wait_for():
    table, runner = started()
    pass_until(table, runner, "main1")
    moth = spell_in_hand(table, 0)
    runner.step(0, "act", {"because": "test set-up", "mutations": [{"op": "move", "obj": f"@{moth}", "to": "battlefield"}]})
    handle = next(o["handle"] for o in table.view(None)["battlefield"] if o["name"] == moth)
    table.apply_hooks([{"kind": "event_trigger", "handle": handle, "text": "Whenever one or more nonland cards are milled, put a counter on a creature.",
                        "on": {"event": "milled", "who": "any", "nonland": True, "once": True}},
                       {"kind": "event_trigger", "handle": handle, "text": "Whenever this creature deals combat damage to a player, they get four rad counters.",
                        "on": {"event": "deals_combat_damage", "self": True}}])
    runner.step(0, "act", {"because": "test", "mutations": [{"op": "mill", "seat": 1, "count": 12}]})   # twelve cards, some of them spells
    runner.step(0, "pass_priority")
    milled = [t["text"] for t in table.view(0)["waiting_triggers"]]
    assert len(milled) == 1 and milled[0].startswith("Whenever one or more nonland cards are milled")            # once, not once a card
    table.game.objects[next(o.id for o in table.game.objects.values() if o.name == moth)].entered_turn = 0
    runner.step(0, "skip_trigger", {"pending": table.view(0)["waiting_triggers"][0]["handle"], "because": "test"})
    pass_until(table, runner, "begin_combat")
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    runner.step(0, "declare_attackers", {"attacks": [{"attacker": f"@{moth}", "target": {"seat": 1}}]})
    while table.expected()[0][1] != "combat_damage":
        seat, kind = table.expected()[0]
        runner.step(seat, {"priority": "pass_priority"}.get(kind, kind))
    runner.step(0, "combat_damage", {"assignments": [{"source": f"@{moth}", "target": {"seat": 1}, "amount": 2}]})
    assert [t["text"] for t in table.view(0)["waiting_triggers"]] == [f"Whenever this creature deals combat damage to a player, they get four rad counters. (because {moth} dealt 2 combat damage to Bob)"]


def test_a_trigger_is_a_pattern_over_what_the_table_sees_so_new_conditions_need_no_new_code():
    from fcsim.table import triggers
    table, runner = started()
    pass_until(table, runner, "main1")
    watcher = spell_in_hand(table, 0)
    runner.step(0, "act", {"because": "test set-up", "mutations": [{"op": "move", "obj": f"@{watcher}", "to": "battlefield"}]})
    handle = next(o["handle"] for o in table.view(None)["battlefield"] if o["name"] == watcher)
    table.apply_hooks([
        {"kind": "event_trigger", "handle": handle, "text": "Whenever an opponent discards a card, ...", "on": {"event": "moved", "from": "hand", "to": "graveyard", "who": "opponent"}},
        {"kind": "event_trigger", "handle": handle, "text": "Whenever you gain life, ...", "on": {"event": "gains_life", "who": "you"}},
        {"kind": "event_trigger", "handle": handle, "text": "Whenever an opponent loses 3 or more life, ...", "on": {"event": "life", "sign": "loss", "who": "opponent", "min": 3}},
        {"kind": "event_trigger", "handle": handle, "text": "Whenever a +1/+1 counter is put on this, ...", "on": {"event": "counters_added", "kind": "+1/+1", "self": True}},
        {"kind": "event_trigger", "handle": handle, "text": "Whenever you draw a card, ...", "on": {"event": "drawn", "who": "you"}},
        {"kind": "event_trigger", "handle": handle, "text": "Whenever this becomes tapped, ...", "on": {"event": "tapped", "self": True}}])
    runner.step(0, "pass_priority")   # its own arrival matched none of them
    assert table.expected() == [(1, "priority")]
    for seat in (1, 2):
        runner.step(seat, "pass_priority")
    runner.step(0, "act", {"because": "test", "mutations": [
        {"op": "discard_random", "seat": 1, "count": 1}, {"op": "discard_random", "seat": 0, "count": 1},
        {"op": "life", "seat": 0, "delta": 2}, {"op": "life", "seat": 1, "delta": -2}, {"op": "life", "seat": 2, "delta": -4},
        {"op": "counters", "obj": f"@{watcher}", "kind": "+1/+1", "delta": 1}, {"op": "counters", "obj": f"@{watcher}", "kind": "stun", "delta": 1},
        {"op": "draw", "count": 1}, {"op": "tap", "objs": [f"@{watcher}"]}]})
    runner.step(0, "pass_priority")
    waiting = [t["text"] for t in table.view(0)["waiting_triggers"]]
    assert [w.split(",")[0] for w in waiting] == ["Whenever an opponent discards a card", "Whenever you gain life", "Whenever an opponent loses 3 or more life",
                                                  "Whenever a +1/+1 counter is put on this", "Whenever you draw a card", "Whenever this becomes tapped"]
    assert "(because a card was drawn)" in waiting[4] and "was discarded)" in waiting[0] and "(because Cat lost 4 life)" in waiting[2]   # a hidden card is not named
    assert triggers.pattern({"event": "dies", "type": "Zombie", "nonland": True}) == {"event": "moved", "from": "battlefield", "to": "graveyard",
                                                                                      "type": ["Creature", "Zombie"], "not_type": ["Land"], "nonland": True}


def test_a_delayed_trigger_comes_back_at_its_step_once():
    table, runner = started()
    pass_until(table, runner, "main1")
    norin = spell_in_hand(table, 0)
    runner.step(0, "act", {"because": "Norin the Wary's trigger resolves", "mutations": [
        {"op": "move", "obj": f"@{norin}", "to": "exile"},
        {"op": "delay", "step": "end", "text": f"Return {norin} to the battlefield under its owner's control."}]})
    assert "will have a trigger at the beginning of the next end step" in table.events[-1]["public"][-1]
    assert table.act(0, "act", {"because": "x", "mutations": [{"op": "delay", "step": "sometime", "text": "x"}]})["error"]["code"] == "bad_delay"
    for _ in range(40):
        seat, kind = table.expected()[0]
        if kind == "triggers":
            break
        runner.step(seat, {"priority": "pass_priority", "declare_attackers": "declare_attackers"}.get(kind, kind))
    assert table.expected() == [(0, "triggers")] and table.game.turn.step == "end"
    assert table.view(0)["waiting_triggers"][0]["text"].startswith(f"Return {norin} to the battlefield")
    assert not [e for e in table.game.ledger if e.get("kind") == "delayed"]                     # used up
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()


def test_one_ability_is_one_hook_and_triggers_that_happen_together_are_one_item():
    table, runner = started()
    pass_until(table, runner, "main1")
    norin, *attackers = [name for name in hand(table, 0) if "spell" in name][:4]
    runner.step(0, "act", {"because": "test set-up", "mutations": [{"op": "move", "obj": f"@{name}", "to": "battlefield"} for name in (norin, *attackers)]})
    handle = next(o["handle"] for o in table.view(None)["battlefield"] if o["name"] == norin)
    table.apply_hooks([{"kind": "event_trigger", "handle": handle, "text": "When a creature attacks, exile Norin.", "on": {"event": "attacks", "who": "any"}}])
    table.apply_hooks([{"kind": "event_trigger", "handle": handle, "text": "Whenever a player casts a spell or a creature attacks, exile Norin.",
                        "on": {"who": "any", "any_of": [{"event": "cast"}, {"event": "attacks"}]}}])
    assert [e["text"] for e in table.game.ledger] == ["Whenever a player casts a spell or a creature attacks, exile Norin."]   # described again: replaced
    pass_until(table, runner, "begin_combat")
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    runner.step(0, "declare_attackers", {"attacks": [{"attacker": f"@{name}", "target": {"seat": 1}} for name in attackers]})
    waiting = table.view(0)["waiting_triggers"]
    assert len(waiting) == 1 and waiting[0]["count"] == 3 and waiting[0]["text"].endswith("[triggered 3 times]")      # three attackers, one item
    runner.step(0, "add_trigger", {"pending": waiting[0]["handle"]})                                                   # one of them goes on the stack
    assert table.view(0)["waiting_triggers"][0]["count"] == 2 and table.expected() == [(0, "triggers")]
    runner.step(0, "skip_trigger", {"pending": table.view(0)["waiting_triggers"][0]["handle"], "because": "the first exiles Norin; the rest do nothing"})
    assert table.view(0)["waiting_triggers"] == [] and len(table.view(0)["stack"]) == 1                                # two calls, not six
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()


def test_a_game_recorded_under_older_table_rules_is_upgraded_at_a_point_the_journal_records():
    old = Table(seats(), seed=5, first=0, mechanics=1)
    runner = ScriptedRunner(old)
    for seat in range(3):
        runner.step(seat, "keep")
    pass_until(old, runner, "main1")
    creature = spell_in_hand(old, 0)
    runner.step(0, "act", {"because": "test", "mutations": [{"op": "move", "obj": f"@{creature}", "to": "battlefield"}]})
    handle = next(o["handle"] for o in old.view(None)["battlefield"] if o["name"] == creature)
    for _ in range(2):   # under the old rules a second description of the same permanent piled up beside the first
        old.apply_hooks([{"kind": "event_trigger", "handle": handle, "text": f"A description written at audit {_}.", "on": {"event": "cast", "who": "any", "once": bool(_)}}])
    assert len(old.game.ledger) == 2
    old.upgrade()
    old.apply_hooks([{"kind": "event_trigger", "handle": handle, "text": "The one description.", "on": {"event": "cast", "who": "any"}}])
    assert [e["text"] for e in old.game.ledger] == ["The one description."] and old.game.mechanics >= 2
    rebuilt = Table.replay(old.config, old.events)
    assert rebuilt.game.to_dict() == old.game.to_dict() and old.config["mechanics"] == 1      # the journal, not the config, carries the change


NORIN = [{"op": "move", "what": "self", "to": "exile"},
         {"op": "delay", "step": "end", "text": "Return Norin to the battlefield under its owner's control.",
          "script": [{"op": "move", "what": "self", "to": "battlefield"}]}]


def test_a_trusted_script_puts_its_trigger_on_the_stack_and_resolves_it_with_nobody_woken():
    table, runner = started()
    pass_until(table, runner, "main1")
    norin, bobs = spell_in_hand(table, 0), spell_in_hand(table, 1)
    runner.step(0, "act", {"because": "test set-up", "mutations": [{"op": "move", "obj": f"@{norin}", "to": "battlefield"}]})
    handle = next(o["handle"] for o in table.view(None)["battlefield"] if o["name"] == norin)
    table.apply_hooks([{"kind": "event_trigger", "handle": handle, "text": "When a player casts a spell or a creature attacks, exile Norin. Return it at the next end step.",
                        "on": {"who": "any", "any_of": [{"event": "cast"}, {"event": "attacks"}]}, "script": NORIN, "scripted": True}])
    runner.step(0, "cast", {"card": f"@{spell_in_hand(table, 0)}", "resolves": "auto_permanent"})
    runner.step(0, "pass_priority")
    stack = table.view(1)["stack"]
    assert [i.get("resolves_by_script", False) for i in stack] == [False, True] and table.expected() == [(1, "priority")]   # above the spell, and nobody asked to put it there
    for seat in (1, 2, 0):
        runner.step(seat, "pass_priority")
    assert any("resolves by script" in line and "to Ann's exile" in line for line in table.events[-1]["public"])
    assert norin in [c["name"] for c in table.view(1)["seats"][0]["exile"]] and [e["kind"] for e in table.game.ledger] == ["delayed"]
    seen = set()
    for _ in range(60):   # through to the end step: the return stacks and resolves by itself too
        seat, kind = table.expected()[0]
        seen.add(kind)
        if table.game.turn.number == 2:
            break
        runner.step(seat, {"priority": "pass_priority", "declare_attackers": "declare_attackers", "discard": "finish_step"}.get(kind, kind))
    assert norin in [o["name"] for o in table.view(1)["battlefield"]] and seen <= {"priority", "declare_attackers", "discard"}   # no trigger or resolution was anybody's to do
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()


def test_only_a_script_in_the_tables_own_words_is_kept_and_an_untrusted_one_waits_for_its_player():
    from fcsim.table import scripts
    assert scripts.problem(NORIN) is None and scripts.problem([{"op": "counters", "on": "each_player", "kind": "rad", "delta": 1}]) is None
    assert "is not one of" in scripts.problem([{"op": "shuffle"}]) and "takes only" in scripts.problem([{"op": "life", "who": "you", "delta": 1, "obj": "c1"}])
    assert "one of" in scripts.problem([{"op": "move", "what": "target creature", "to": "exile"}]) and scripts.problem([]) is not None
    assert "cannot delay again" in scripts.problem([{"op": "delay", "step": "end", "text": "x", "script": [{"op": "delay", "step": "end", "text": "y", "script": NORIN[:1]}]}])
    table, runner = started()
    pass_until(table, runner, "main1")
    norin = spell_in_hand(table, 0)
    runner.step(0, "act", {"because": "test set-up", "mutations": [{"op": "move", "obj": f"@{norin}", "to": "battlefield"}]})
    handle = next(o["handle"] for o in table.view(None)["battlefield"] if o["name"] == norin)
    table.apply_hooks([{"kind": "event_trigger", "handle": handle, "text": "When a player casts a spell, exile Norin.", "on": {"event": "cast", "who": "any"},
                        "script": NORIN},                                        # a script, but nobody has vouched for it
                       {"kind": "event_trigger", "handle": handle, "text": "Something else.", "on": {"event": "attacks", "self": True},
                        "script": [{"op": "shuffle"}], "scripted": True}])       # vouched for, but not a script the table runs
    assert [(e.get("scripted"), "script" in e) for e in table.game.ledger] == [(False, True), (None, False)]
    runner.step(0, "cast", {"card": f"@{spell_in_hand(table, 0)}", "resolves": "auto_permanent"})
    runner.step(0, "pass_priority")
    assert table.expected() == [(0, "triggers")]                                 # as before: its controller puts it on the stack


def test_lethal_damage_kills_before_anyone_gets_priority_where_the_table_is_sure():
    table, runner, n = combat_table({"A1": {"power": "2", "toughness": "2"}, "A2": {"power": "3", "toughness": "3"},
                                     "B1": {"power": "1", "toughness": "1", "keywords": ["Indestructible"]}, "B2": {"power": "4", "toughness": "4"}})
    for obj in table.game.objects.values():     # the helper reaches into the state for Bob's creatures; give them their type lines too
        if obj.zone == "battlefield":
            obj.type_line = "Creature — Test"
    runner.step(0, "act", {"because": "a burn spell resolves", "mutations": [
        {"op": "damage", "obj": f"@{n['A1']}", "amount": 2}, {"op": "damage", "obj": f"@{n['A2']}", "amount": 2},
        {"op": "damage", "obj": f"@{n['B1']}", "amount": 5}, {"op": "annotate", "obj": f"@{n['B2']}", "text": "prevent all damage to it this turn"},
        {"op": "damage", "obj": f"@{n['B2']}", "amount": 9}]})
    runner.step(0, "pass_priority")     # the next time anyone would receive priority, the table applies what it is sure of
    told = " ".join(table.events[-1]["public"])
    assert f"{n['A1']} has lethal damage marked on it and is destroyed (rule 704.5g)." in told
    on_board = [o["name"] for o in table.view(0)["battlefield"]]
    assert n["A1"] not in on_board and n["A2"] in on_board          # 2 damage on a 3/3 is not lethal
    assert n["B1"] in on_board and n["B2"] in on_board              # indestructible; and a creature carrying a note is left to the players
    runner.step(1, "act", {"because": "-3/-3 until end of turn", "mutations": [{"op": "counters", "obj": f"@{n['A2']}", "kind": "-1/-1", "delta": 3}]})
    runner.step(1, "pass_priority")
    assert n["A2"] not in [o["name"] for o in table.view(0)["battlefield"]] and "toughness 0 or less" in " ".join(table.events[-1]["public"])


def test_a_lone_blocker_cannot_block_a_creature_with_menace():
    table, runner, n = combat_table({"A1": {"power": "3", "toughness": "3", "keywords": ["Menace"]}, "B1": {"power": "1", "toughness": "1"}, "B2": {"power": "1", "toughness": "1"}})
    pass_until(table, runner, "begin_combat")
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    runner.step(0, "declare_attackers", {"attacks": [{"attacker": f"@{n['A1']}", "target": {"seat": 1}}]})
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    handles = {o["name"]: o["handle"] for o in table.view(1)["battlefield"]}
    alone = table.act(1, "declare_blockers", {"blocks": [{"blocker": handles[n["B1"]], "attacker": handles[n["A1"]]}]})
    assert alone["error"]["code"] == "say_why" and "menace" in alone["error"]["message"] and table.game.turn.blockers == []
    assert "error" not in table.act(1, "declare_blockers", {"blocks": [{"blocker": handles[n[b]], "attacker": handles[n["A1"]]} for b in ("B1", "B2")]})
    # What everyone is told must be what happened: for a whole batch of games every block was announced as "no blocks",
    # and the auditor, who reads the announcement, had correct combat "corrected".
    assert table.events[-1]["public"] == [f"Bob: {n['B1']} blocks {n['A1']}, {n['B2']} blocks {n['A1']}."]


def test_the_table_counts_each_players_spells_so_second_spell_triggers_can_be_a_pattern():
    table, runner = started()
    pass_until(table, runner, "main1")
    whale, first, second, third = [name for name in hand(table, 0) if "spell" in name][:4]
    runner.step(0, "act", {"because": "test set-up", "mutations": [{"op": "move", "obj": f"@{whale}", "to": "battlefield"}]})
    handle = next(o["handle"] for o in table.view(None)["battlefield"] if o["name"] == whale)
    table.apply_hooks([{"kind": "event_trigger", "handle": handle, "text": "Whenever a player casts their second spell each turn, proliferate.",
                        "on": {"event": "cast", "who": "any", "nth": 2}}])
    offered = []
    for name in (first, second, third):
        runner.step(0, "cast", {"card": f"@{name}", "resolves": "auto_permanent"})
        runner.step(0, "pass_priority")
        offered.append(len(table.view(0)["waiting_triggers"]))
        if table.expected() == [(0, "triggers")]:
            runner.step(0, "skip_trigger", {"pending": table.view(0)["waiting_triggers"][0]["handle"], "because": "test"})
        while table.expected()[0] != (0, "priority") or table.view(0)["stack"]:
            seat, kind = table.expected()[0]
            runner.step(seat, "pass_priority")
    assert offered == [0, 1, 0]                                              # the second, and only the second


def test_a_player_can_say_what_its_trigger_does_and_the_table_resolves_it():
    table, runner = started()
    pass_until(table, runner, "main1")
    artist = spell_in_hand(table, 0)
    runner.step(0, "act", {"because": "test set-up", "mutations": [{"op": "move", "obj": f"@{artist}", "to": "battlefield"}]})
    handle = next(o["handle"] for o in table.view(None)["battlefield"] if o["name"] == artist)
    table.apply_hooks([{"kind": "event_trigger", "handle": handle, "text": "Whenever you cast a spell, each opponent loses 1 life and you gain 1 life.",
                        "on": {"event": "cast", "who": "you"}}])
    runner.step(0, "cast", {"card": f"@{spell_in_hand(table, 0)}", "resolves": "auto_permanent"})
    runner.step(0, "pass_priority")
    pending = table.view(0)["waiting_triggers"][0]["handle"]
    assert table.act(0, "add_trigger", {"pending": pending, "script": [{"op": "shuffle"}]})["error"]["code"] == "bad_script"
    drain = [{"op": "life", "who": "each_opponent", "delta": -1}, {"op": "life", "who": "you", "delta": 1}]
    done = runner.step(0, "add_trigger", {"pending": pending, "script": drain})
    assert done["log"][0].endswith("to resolve by the script they gave.") and artist in done["log"][0]      # named from what the table knew triggered
    for seat in (1, 2, 0):
        runner.step(seat, "pass_priority")
    assert [s["life"] for s in table.view(0)["seats"]] == [41, 39, 39] and table.expected() == [(0, "priority")]   # nobody was handed a resolution
    assert any("resolves by script" in line for line in table.events[-1]["public"])
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()


def test_a_slip_with_one_possible_meaning_is_understood_and_a_stale_handle_says_where_its_card_went():
    table, runner = started()
    pass_until(table, runner, "main1")
    spell = spell_in_hand(table, 0)
    old = next(c["handle"] for c in table.view(0)["seats"][0]["hand"]["cards"] if c["name"] == spell)
    assert "error" not in table.act(0, "act", {"because": "test", "mutations": [{"op": "move", "obj": spell, "to": "exile"}]})   # a name, where a handle belongs
    stale = table.act(0, "cast", {"card": old, "because": "exiled: I may play it"})
    now = table.view(0)["seats"][0]["exile"][0]["handle"]
    assert stale["error"]["code"] == "gone" and f"It is now {now}, in Ann's exile." in stale["error"]["message"]
    several = table.act(0, "play_land", {"card": "Island"})
    assert several["error"]["code"] == "unknown_handle" and "cards you can point at are called 'Island'" in several["error"]["message"]
    assert table.act(0, "play_land", {"card": "An Island nobody has"})["error"]["code"] == "unknown_handle"


def test_alternatives_alone_decide_whatever_event_is_written_beside_them():
    from fcsim.table import triggers
    zuko = {"event": "cast", "any_of": [{"event": "cast", "from": "exile", "who": "you"}, {"event": "moved", "from": "exile", "to": "battlefield", "who": "you"}]}
    hook = {"controller": 0, "source": 7}
    from_hand = {"event": "cast", "from": "hand", "controller": 1, "was_controller": 1, "type_line": "", "id": 9, "old_id": None}
    mine_from_exile = {**from_hand, "from": "exile", "controller": 0, "was_controller": 0}
    assert not triggers.matches(zuko, from_hand, hook) and triggers.matches(zuko, mine_from_exile, hook)
    assert triggers.matches(zuko, from_hand, hook, alternatives_alone=False)        # how it was read, kept only to replay games recorded then
    shared = {"who": "you", "any_of": [{"event": "cast"}, {"event": "attacks"}]}     # a filter beside the alternatives narrows each of them
    assert triggers.matches(shared, mine_from_exile, hook) and not triggers.matches(shared, from_hand, hook)


def test_combat_damage_to_a_planeswalker_takes_its_loyalty_and_the_arithmetic_copes():
    table, runner, n = combat_table({"A1": {"power": "3", "toughness": "3"}, "B1": {"power": "0", "toughness": "0"}})
    oko = next(o for o in table.game.objects.values() if o.name == n["B1"])
    oko.type_line, oko.counters = "Legendary Planeswalker — Oko", {"loyalty": 4}
    pass_until(table, runner, "begin_combat")
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    runner.step(0, "declare_attackers", {"attacks": [{"attacker": f"@{n['A1']}", "target": {"object": f"@{n['B1']}"}}]})
    while table.expected()[0][1] != "combat_damage":
        seat, kind = table.expected()[0]
        runner.step(seat, {"priority": "pass_priority"}.get(kind, kind))
    assert table.proposed_damage(0)["lines"] == [f"{n['A1']} deals 3 to {n['B1']}"]          # this raised, and took a real game down with it
    done = runner.step(0, "combat_damage", {"as_proposed": True})
    assert f"{n['A1']} deals 3 to {n['B1']}, which has 1 loyalty left." in done["log"] and oko.id in table.game.objects


def test_a_script_written_with_its_operation_as_a_key_is_understood():
    from fcsim.table import scripts
    wizard = {"name": "Wizard", "types": "Creature — Wizard", "power": 0, "toughness": 1, "text": "Black."}
    written = [{"life": {"who": "each_opponent", "delta": -2}}, {"counters": {"on": "each_creature_you_control", "kind": "+1/+1", "delta": 1}},
               {"create_token": {"token": wizard, "count": 1}}, {"create_token": wizard}, {"draw": {"who": "you", "count": 1}}, {"untap": {"what": "self"}}]
    meant = scripts.shaped(written)
    assert scripts.problem(written) is not None and scripts.problem(meant) is None
    assert meant[0] == {"op": "life", "who": "each_opponent", "delta": -2} and meant[3] == {"op": "create_token", "token": wizard}
    assert scripts.shaped([{"op": "delay", "step": "end", "text": "x", "script": [{"move": {"what": "self", "to": "battlefield"}}]}])[0]["script"] == \
        [{"op": "move", "what": "self", "to": "battlefield"}]
    table, runner = started()
    pass_until(table, runner, "main2")
    assert "error" not in table.act(0, "pass_priority", {"until": "main2"})       # already there: a pass, not an error


def test_floating_mana_lives_in_a_pool_that_payments_spend_and_steps_empty():
    told: dict[str, dict] = {"Island": {"types": ["Land"], "produces": ["U"], "mana_amount": 1, "mana_cost": ""}}
    table = Table(seats(), seed=5, first=0, card_lookup=lambda name: told.get(name))
    runner = ScriptedRunner(table)
    for seat in range(3):
        runner.step(seat, "keep")
    pass_until(table, runner, "main1")
    red, big = [name for name in hand(table, 0) if "spell" in name][:2]
    told[red] = {"types": ["Creature"], "keywords": [], "mana_cost": "{R}{R}", "produces": [], "mana_amount": 1}
    told[big] = {"types": ["Instant"], "keywords": [], "mana_cost": "{2}{R}", "produces": [], "mana_amount": 1}   # castable over the first spell
    runner.step(0, "play_land", {"card": "@Island"})
    added = runner.step(0, "act", {"because": "firebending 3", "mutations": [{"op": "mana", "add": "RRR", "until": "end_of_combat"}]})
    assert added["log"][-1] == "Ann adds {R}{R}{R} (until end of combat) (firebending 3); their mana pool holds {R}{R}{R}." or "holds {R}{R}{R}" in added["log"][-1]
    assert table.view(1)["seats"][0]["mana_pool"] == "{R}{R}{R}"                                  # everyone can see a mana pool
    offered = {a["name"]: a for a in table.affordable(0)}
    assert offered[red]["pool"] == "RR" and offered[red]["tap"] == [] and set(offered) == {red, big}   # the briefing's ready-made payment spends it
    refused = table.act(0, "cast", {"card": next(c["handle"] for c in table.view(0)["seats"][0]["hand"]["cards"] if c["name"] == red), "payment": {"pool": "RG"}})
    assert refused["error"]["code"] in ("cannot_pay", "not_in_pool") and table.view(0)["seats"][0]["mana_pool"] == "{R}{R}{R}"   # nothing was spent
    cast = runner.step(0, "cast", {"card": f"@{red}", "payment": {"pool": "RR"}, "resolves": "auto_permanent"})
    assert "spending {R}{R} from their mana pool" in cast["log"][0] and table.view(0)["seats"][0]["mana_pool"] == "{R}"
    assert table.act(0, "cast", {"card": next(c["handle"] for c in table.view(0)["seats"][0]["hand"]["cards"] if c["name"] == big),
                                 "payment": {"pool": "RR", "tap": [table.view(0)["battlefield"][0]["handle"]]}})["error"]["code"] == "not_in_pool"   # spent twice: the old error
    pass_until(table, runner, "main2")                                                             # combat is over
    assert "mana_pool" not in table.view(0)["seats"][0]
    assert any("Ann's unspent {R} empties from their mana pool." in line for e in table.events for line in e["public"])
    runner.step(0, "act", {"because": "a ritual", "mutations": [{"op": "mana", "add": "{R}{R}"}, {"mana": {"add": "B", "until": "kept"}}]})
    runner.step(0, "pass_priority", {"until": "end"})
    while table.game.turn.step != "end":
        seat, kind = table.expected()[0]
        runner.step(seat, "pass_priority")
    assert table.view(0)["seats"][0]["mana_pool"] == "{B}"                                        # mana an effect says is never lost stays
    assert table.act(0, "act", {"because": "x", "mutations": [{"op": "mana", "add": "purple"}]})["error"]["code"] == "bad_mana"
    assert Table.replay(table.config, table.events, card_lookup=table.card_lookup).game.to_dict() == table.game.to_dict()


def test_a_trigger_on_what_a_permanent_is_attached_to_fires_for_that_and_nothing_else():
    table, runner = started()
    pass_until(table, runner, "main1")
    blade, bearer, bystander = [name for name in hand(table, 0) if "spell" in name][:3]
    runner.step(0, "act", {"because": "test set-up", "mutations": [{"op": "move", "obj": f"@{name}", "to": "battlefield"} for name in (blade, bearer, bystander)]})
    handles = {o["name"]: o["handle"] for o in table.view(None)["battlefield"]}
    table.apply_hooks([
        {"kind": "event_trigger", "handle": handles[blade], "text": "Whenever equipped creature becomes tapped, ...", "on": {"event": "tapped", "attached": True}},
        {"kind": "event_trigger", "handle": handles[blade], "text": "Whenever equipped creature dies, ...", "on": {"event": "moved", "from": "battlefield", "to": "graveyard", "attached": True}}])
    runner.step(0, "act", {"because": "test", "mutations": [{"op": "tap", "objs": [f"@{bearer}"]}]})   # it lies unattached: Pip-Boy 3000 asked about every attack at the table
    runner.step(0, "pass_priority")
    assert table.expected() == [(1, "priority")] and not table.view(0)["waiting_triggers"]
    for seat in (1, 2):
        runner.step(seat, "pass_priority")
    runner.step(0, "act", {"because": "test", "mutations": [{"op": "untap", "objs": [f"@{bearer}"]}, {"op": "attach", "obj": f"@{blade}", "host": f"@{bearer}"},
                                                            {"op": "tap", "objs": [f"@{bearer}", f"@{bystander}"]}]})
    runner.step(0, "pass_priority")
    waiting = table.view(0)["waiting_triggers"]
    assert [w["text"].split(",")[0] for w in waiting] == ["Whenever equipped creature becomes tapped"] and "triggered" not in waiting[0]["text"] and bystander not in waiting[0]["text"]
    runner.step(0, "skip_trigger", {"pending": waiting[0]["handle"], "because": "test"})
    pass_until(table, runner, "main1", seat=0)
    runner.step(0, "act", {"because": "test", "mutations": [{"op": "move", "obj": f"@{bearer}", "to": "graveyard"}, {"op": "move", "obj": f"@{bystander}", "to": "graveyard"}]})
    runner.step(0, "pass_priority")   # the move let go of the blade, and still says what was attached
    waiting = table.view(0)["waiting_triggers"]
    assert [w["text"].split(",")[0] for w in waiting] == ["Whenever equipped creature dies"] and f"(because {bearer} " in waiting[0]["text"]
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()


def test_a_script_can_speak_of_the_player_and_the_amount_its_trigger_was_about():
    """Mindcrank: "Whenever an opponent loses life, that player mills that many cards." Its controller wrote a mill of
    nothing for everybody, having no word for either; the table counted it resolved, and the auditor put it right a turn later."""
    from fcsim.table import scripts
    assert scripts.problem([{"op": "mill", "who": "that_player", "count": "that_many"}]) is None
    assert scripts.problem([{"op": "life", "who": "that_player", "delta": "minus_that_many"}]) is None
    assert "must be int" in scripts.problem([{"op": "mill", "who": "you", "count": "minus_that_many"}])   # nobody mills a negative number
    table, runner = started()
    pass_until(table, runner, "main1")
    crank = spell_in_hand(table, 0)
    runner.step(0, "act", {"because": "test set-up", "mutations": [{"op": "move", "obj": f"@{crank}", "to": "battlefield"}]})
    handle = next(o["handle"] for o in table.view(None)["battlefield"] if o["name"] == crank)
    table.apply_hooks([{"kind": "event_trigger", "handle": handle, "text": "Whenever an opponent loses life, that player mills that many cards.",
                        "on": {"event": "loses_life", "who": "opponent"}}])
    runner.step(0, "act", {"because": "test", "mutations": [{"op": "life", "seat": 2, "delta": -3}]})
    runner.step(0, "pass_priority")
    waiting = table.view(0)["waiting_triggers"][0]
    before = table.view(None)["seats"][2]["library"]["count"]
    runner.step(0, "add_trigger", {"pending": waiting["handle"], "script": [{"op": "mill", "who": "that_player", "count": "that_many"}]})
    for _ in range(3):
        runner.step(table.expected()[0][0], "pass_priority")
    assert table.view(None)["seats"][2]["library"]["count"] == before - 3 and "Cat mills" in " ".join(table.events[-1]["public"])
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()


def test_a_script_its_player_gave_that_does_nothing_is_theirs_to_resolve_by_hand():
    table, runner = started()
    pass_until(table, runner, "main1")
    table.queue_trigger(0, "When the creature died, its counters go on The Ozolith.")
    runner.step(0, "pass_priority")
    assert table.expected() == [(0, "triggers")]
    gone = [{"op": "counters", "on": "it", "kind": "+1/+1", "delta": 1}]      # "it" is nothing the table knows here: the script will find nothing to do
    runner.step(0, "add_trigger", {"pending": table.view(0)["waiting_triggers"][0]["handle"], "script": gone})
    assert table.game.stack[-1].given
    for _ in range(3):
        runner.step(table.expected()[0][0], "pass_priority")
    assert table.expected() == [(0, "resolution")] and "does nothing as written, so it is theirs to resolve by hand" in " ".join(table.events[-1]["public"])
    runner.step(0, "resolve", {"because": "by hand", "mutations": [{"op": "life", "seat": 0, "delta": 1}]})
    runner.step(0, "finish_resolution")
    assert table.view(None)["seats"][0]["life"] == 41 and not table.game.stack
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()
    old = Table(seats(), seed=5, first=0, mechanics=5)     # a journal from before this was the rule replays as it was played
    assert old.config["mechanics"] == 5


def test_a_spell_cast_while_another_resolves_waits_on_the_stack_and_no_card_is_moved_there_by_hand():
    """Goliath Daydreamer had Fire Lord Zuko's pilot cast Hex Magic from exile during its trigger. The table allowed no
    cast then, so the card was moved to the stack by hand, with no spell to go with it, and was in no zone at all."""
    table, runner = started()
    pass_until(table, runner, "main1")
    first, second = [name for name in hand(table, 0) if "spell" in name][:2]
    runner.step(0, "cast", {"card": f"@{first}"})
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    assert table.expected() == [(0, "resolution")]
    runner.step(0, "resolve", {"because": "test", "mutations": [{"op": "move", "obj": f"@{second}", "to": "stack"}]}, error="cast_it")
    runner.step(0, "cast", {"card": f"@{second}"}, error="say_why")
    runner.step(0, "cast", {"card": f"@{second}", "because": f"{first}: cast a spell from your hand without paying its mana cost",
                            "payment": {"note": "without paying its mana cost"}})
    assert table.expected() == [(0, "resolution")] and [i.source_name for i in table.game.stack] == [second]   # still resolving the first
    runner.step(0, "finish_resolution")
    assert table.expected() == [(0, "priority")] and [i.source_name for i in table.game.stack] == [second]
    for seat in (0, 1, 2):
        runner.step(seat, "pass_priority")
    assert table.expected() == [(0, "resolution")] and table.game.resolution["item"].source_name == second
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()


def test_a_pool_is_left_alone_while_its_owners_own_effect_speaks_of_unspent_mana():
    table, runner = started()
    pass_until(table, runner, "main1")
    ozai = spell_in_hand(table, 0)
    runner.step(0, "act", {"because": "test set-up", "mutations": [{"op": "move", "obj": f"@{ozai}", "to": "battlefield"}]})
    handle = next(o["handle"] for o in table.view(None)["battlefield"] if o["name"] == ozai)
    table.apply_hooks([{"kind": "static", "handle": handle, "text": "If you would lose unspent mana, that mana becomes red instead."}])
    runner.step(0, "act", {"because": "firebending", "mutations": [{"op": "mana", "add": "RRG", "until": "end_of_combat"}, {"op": "mana", "seat": 1, "add": "U"}]})
    pass_until(table, runner, "main2")
    said = [line for e in table.events for line in e["public"] if "unspent" in line and not line.startswith("Ledger:")]
    assert table.view(1)["seats"][0]["mana_pool"] == "{R}{R}{G}" and "mana_pool" not in table.view(0)["seats"][1]      # Bob has no such effect
    assert len(said) == 2 and f"would empty now, but {ozai} speaks of unspent mana" in said[1] and "Bob's unspent {U} empties" in said[0]
    runner.step(0, "act", {"because": "Ozai: it becomes red", "mutations": [{"op": "mana", "remove": "G"}, {"op": "mana", "add": "R", "until": "kept"}]})
    runner.step(0, "act", {"because": "test", "mutations": [{"op": "move", "obj": f"@{ozai}", "to": "graveyard"}]})
    pass_until(table, runner, "end")
    assert table.view(0)["seats"][0]["mana_pool"] == "{R}"           # the effect is gone: what was only held went with the step, what was kept stays
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()
    old = Table(seats(), seed=5, first=0, mechanics=5)
    assert old.game.mechanics == 5


def test_an_additional_combat_made_during_combat_comes_after_that_combat_not_in_the_middle_of_it():
    table, runner = started()
    pass_until(table, runner, "main1")
    pass_until(table, runner, "declare_attackers")
    assert table.game.turn.step == "declare_attackers"
    combat = ["begin_combat", "declare_attackers", "declare_blockers", "combat_damage", "end_combat"]
    before = list(table.game.turn.remaining)
    rest = before.index("end_combat") + 1                  # what is left of this combat
    assert rest >= 1 and before[rest:] == ["main2", "end", "cleanup"]
    runner.step(0, "act", {"because": "Great Train Heist: after this phase, there is an additional combat phase", "mutations": [{"op": "extra_combat"}]})
    assert table.game.turn.remaining == before[:rest] + combat + ["main2"] + before[rest:]
    runner.step(0, "act", {"because": "Hellkite Charger again", "mutations": [{"op": "extra_combat"}]})       # a second one also waits for this combat to end
    assert table.game.turn.remaining == before[:rest] + (combat + ["main2"]) * 2 + before[rest:]
    assert Table.replay(table.config, table.events).game.to_dict() == table.game.to_dict()
    main = Table(seats(), seed=5, first=0)
    other = ScriptedRunner(main)
    for seat in range(3):
        other.step(seat, "keep")
    pass_until(main, other, "main1")
    other.step(0, "act", {"because": "Aggravated Assault: after this main phase", "mutations": [{"op": "extra_combat"}]})
    assert main.game.turn.remaining[:7] == ["begin_combat", "declare_attackers", "declare_blockers", "combat_damage", "end_combat", "main2", "begin_combat"]
    old = Table(seats(), seed=5, first=0, mechanics=5)      # a journal from before replays as it was played, wrong as that was
    before = ScriptedRunner(old)
    for seat in range(3):
        before.step(seat, "keep")
    pass_until(old, before, "declare_attackers")
    before.step(0, "act", {"because": "as it was", "mutations": [{"op": "extra_combat"}]})
    assert old.game.turn.remaining[:2] == ["begin_combat", "declare_attackers"]
