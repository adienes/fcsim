"""The arithmetic of combat, which depends on no card's text once the numbers are known.

The table knows no card text, but it is told each card's printed power, toughness and keywords,
it counts +1/+1 and -1/-1 counters itself, and the auditor stamps creatures with their current
values when other cards change them. From those it can work out who deals what to whom, who dies
and who gains life. It offers that as a proposal: the attacking player accepts it, or deals the
damage by hand when a card changes how combat works (prevention, protection, "assigns damage equal
to its toughness"), which the table cannot see. No model does sums it does not have to.
"""

from __future__ import annotations

from collections.abc import Callable

from .model import Game, Obj

KEYWORDS = ("flying", "reach", "first strike", "double strike", "deathtouch", "trample", "lifelink", "vigilance",
            "menace", "haste", "defender", "indestructible")


def _number(value) -> int | None:
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def stats(game: Game, obj: Obj, lookup: Callable[[str], dict | None] | None) -> dict | None:
    """A creature's current power, toughness and combat keywords as far as the table can tell, or
    None when it cannot (a printed "*", a card it was told nothing about). The auditor's stamp, which
    already counts everything, wins over the printed numbers plus counters."""
    if obj.face_down:
        printed, keywords = {"power": 2, "toughness": 2}, set()
    elif obj.token is not None:
        printed = obj.token
        keywords = {k for k in KEYWORDS if k in str(obj.token.get("text", "")).lower()} | \
            {str(k).lower() for k in obj.token.get("keywords", [])}
    else:
        printed = (lookup(obj.name) if lookup else None) or {}
        keywords = {str(k).lower() for k in printed.get("keywords", [])}
    stamp = obj.computed or {}
    if stamp:
        power, toughness = _number(stamp.get("power")), _number(stamp.get("toughness"))
        keywords = {str(k).lower() for k in stamp["keywords"]} if isinstance(stamp.get("keywords"), list) else keywords
    else:
        power, toughness = _number(printed.get("power")), _number(printed.get("toughness"))
        if power is not None and toughness is not None:
            grown = obj.counters.get("+1/+1", 0) - obj.counters.get("-1/-1", 0)
            power, toughness = power + grown, toughness + grown
    if power is None or toughness is None:
        return None
    return {"power": max(power, 0), "toughness": toughness, "keywords": keywords & set(KEYWORDS),
            "basis": f"the auditor's numbers as of {stamp['as_of']}" if stamp else "printed values and counters"}


def propose_damage(game: Game, lookup: Callable[[str], dict | None] | None) -> dict | None:
    """This combat damage step as the numbers give it, or None if any creature in combat has numbers
    the table does not know. Ids are internal; `needs_choice` lists attackers with several blockers,
    whose division of damage is their controller's to make (the default kills blockers in the order
    they were declared)."""
    turn = game.turn
    fighting: dict[int, dict] = {}
    for entry in turn.attackers:
        if entry["attacker"] in game.objects and game.objects[entry["attacker"]].zone == "battlefield":
            fighting[entry["attacker"]] = {"target": entry["target"], "blockers": [], "blocked": False}
    for entry in turn.blockers:
        if entry["attacker"] in fighting:
            fighting[entry["attacker"]]["blocked"] = True
            if entry["blocker"] in game.objects and game.objects[entry["blocker"]].zone == "battlefield":
                fighting[entry["attacker"]]["blockers"].append(entry["blocker"])
    known = {}
    for obj_id in [*fighting, *(b for f in fighting.values() for b in f["blockers"])]:
        known[obj_id] = stats(game, game.objects[obj_id], lookup)
        if known[obj_id] is None:
            return None
    quick = {i for i, s in known.items() if s["keywords"] & {"first strike", "double strike"}}
    first_step = bool(quick) and turn.damage_steps == 0
    if turn.damage_steps > (1 if quick else 0):
        return None  # this combat's damage has been dealt

    def deals(obj_id: int) -> bool:
        if first_step:
            return obj_id in quick
        return obj_id not in quick or "double strike" in known[obj_id]["keywords"] if quick else True

    assignments, new_damage, touched, gains, needs_choice = [], {}, set(), {}, []

    def hit(source: int, target: dict, amount: int) -> None:
        if amount <= 0:
            return
        assignments.append({"source": source, "target": target, "amount": amount})
        if "obj" in target:
            new_damage[target["obj"]] = new_damage.get(target["obj"], 0) + amount
            touched.update({target["obj"]} if "deathtouch" in known[source]["keywords"] else set())
        if "lifelink" in known[source]["keywords"]:
            controller = game.objects[source].controller
            gains[controller] = gains.get(controller, 0) + amount

    for attacker, fight in fighting.items():
        mine = known[attacker]
        if deals(attacker):
            left = mine["power"]
            if not fight["blocked"]:
                hit(attacker, fight["target"], left)
            else:
                if len(fight["blockers"]) > 1:
                    needs_choice.append(attacker)
                for index, blocker in enumerate(fight["blockers"]):
                    lethal = 1 if "deathtouch" in mine["keywords"] else max(0, known[blocker]["toughness"] - game.objects[blocker].damage)
                    last = index == len(fight["blockers"]) - 1
                    share = min(left, lethal) if "trample" in mine["keywords"] or not last else left
                    hit(attacker, {"obj": blocker}, share)
                    left -= share
                if "trample" in mine["keywords"]:
                    hit(attacker, fight["target"], left)
        for blocker in fight["blockers"]:
            if deals(blocker):
                hit(blocker, {"obj": attacker}, known[blocker]["power"])
    deaths = [obj_id for obj_id, amount in new_damage.items()
              if "indestructible" not in known[obj_id]["keywords"]
              and (obj_id in touched or game.objects[obj_id].damage + amount >= known[obj_id]["toughness"])]
    return {"assignments": assignments, "deaths": deaths, "gains": gains, "another_step": first_step, "needs_choice": needs_choice,
            "first_strike_step": first_step, "basis": sorted({s["basis"] for s in known.values()})}


def attack_problem(game: Game, obj: Obj, lookup) -> str | None:
    """Why this creature cannot be declared as an attacker, as far as the table can tell."""
    known = stats(game, obj, lookup)
    keywords = known["keywords"] if known else set()
    if obj.tapped:
        return f"{obj.name} is tapped."
    if known and "defender" in keywords:
        return f"{obj.name} has defender."
    if known and obj.entered_turn == game.turn.number and "haste" not in keywords:  # a card it was told nothing about may have haste
        return f"{obj.name} came under your control this turn and the table knows of no haste."
    return None


def block_problem(game: Game, blocker: Obj, attacker: Obj, lookup) -> str | None:
    mine, theirs = stats(game, blocker, lookup), stats(game, attacker, lookup)
    if blocker.tapped:
        return f"{blocker.name} is tapped."
    if mine and theirs and "flying" in theirs["keywords"] and not mine["keywords"] & {"flying", "reach"}:
        return f"{attacker.name} has flying and {blocker.name} has neither flying nor reach."
    return None
