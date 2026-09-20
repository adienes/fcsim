from __future__ import annotations

import json
import random

import pytest

from fcsim.table import ScriptedRunner, Table
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
    with pytest.raises(Exception, match="source on the battlefield"):
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
