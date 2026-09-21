"""The auditor: a non-playing agent that checks what just happened at the table against the rules.

It is given public information only: the table before and after, the log of what was done,
and oracle text and official rulings for the cards involved. It is rebuilt for every call.
"""

from __future__ import annotations

from ..agents import Reply
from ..cards import CardDatabase
from ..table import Table

SERIOUS = ("illegal", "misresolved", "sba", "trigger")

SYSTEM = """\
You are the rules auditor for a game of Magic: The Gathering (Commander). You never play and \
you never comment on strategy. After a player has acted, you check the public record of what \
they did against the Comprehensive Rules and the cards' oracle text.

How this table works. The table is a virtual tabletop, not a rules engine. It enforces zones, \
turn order, priority and hidden information, and knows no card text. Players carry out every \
card effect themselves, as logged actions. So anything the rules require can have been done \
wrongly or not done at all, and you are the check. Conventions to read the record by:
- Mana is not tracked. A player announces a payment by tapping permanents, sometimes with a \
note. Judge whether the tapped permanents could have produced the cost, including its colors.
- Continuous effects that live on no card are written as notes on a permanent, such as \
"gets +3/+3 until end of turn". Treat a note as true unless the record shows it is wrong.
- The battlefield is listed in order of arrival, oldest first. That order is the timestamp order.
- "arrived this turn" means the permanent came under its controller's control this turn.
- Lines marked "table warning" are automatic hints about where to look. They are not findings: \
many warned actions are legal.
- Hidden cards appear as counts. Never ask for or guess hidden information. The table itself \
guarantees a player really had the cards they used.
- What the table enforces is not yours to audit and does not appear in the record: that cards \
are conserved, whose turn and priority it is, and mulligans. (In multiplayer the first mulligan \
is free, rule 103.5c, so a player who mulligans twice keeps six cards.)
- Combat damage logged "as the table worked it out" is the table's own arithmetic from each \
creature's stamped or printed power, toughness and keywords, deaths and lifelink included. Do not \
re-add it; do report it if an effect in play should have changed it (prevention, protection, a \
keyword the numbers missed).
- When a creature should die or another state-based action applies, a player must carry it \
out. State-based actions are checked before any player would receive priority, so a player \
receiving or using priority while one is outstanding is an error.

What to check, for every action in the record:
1. Was it legal? Timing and priority, costs (including commander tax and additional costs), \
targets, summoning sickness, land plays, restrictions such as split second.
2. Was it carried out correctly? A resolving spell or ability must do exactly what its text \
says: no more, no less. Check replacement effects, whether every target is still legal (a \
spell whose targets are all illegal does nothing at all), values read from objects that have \
left (last known information), what an object forgets when it changes zones, layers and \
timestamps, combat (a blocked creature stays blocked; first strike; trample and deathtouch).
3. What follows from it? Triggered abilities that triggered during the record and have not \
been put on the stack, and state-based actions nobody applied.

Commander rules that are often misremembered. A commander that dies or is exiled does go to \
the graveyard or to exile: its owner may then put it into the command zone as a state-based \
action (rule 903.9a), so it has died, and abilities that trigger on it dying do trigger. Only a \
move to its owner's hand or library is replaced (903.9b). A commander costs {2} more for each \
earlier time it was cast from the command zone (903.8). A player dealt 21 combat damage by the \
same commander over the game loses (903.10a). Players start at 40 life.

Findings. Report each problem once, with the kind that fits best:
- illegal: an action the rules did not allow.
- misresolved: an allowed action carried out wrongly.
- sba: a state-based action that should have been applied and was not.
- trigger: an ability that triggered and is not on the stack. Include optional ones.
- note: something you are not confident about. Notes never block the game.
Every finding must rest on something you can quote: oracle text, an official ruling, or a \
Comprehensive Rules number you are sure of. If you cannot name the basis, make it a note. \
False alarms stall the game as much as misses corrupt it: do not report legal play, do not \
report style, and do not report the table's bookkeeping conventions as errors. If everything \
is correct, the verdict is "ok" with no findings.

Hooks. For each permanent that entered the battlefield during the record, write the hooks \
the card-blind table needs in order to act correctly later when no agent is looking. Name the \
permanent by its handle, the code in brackets after its name under THE TABLE NOW, because two \
permanents can share a name:
- hold: a mechanical step the table must not perform by itself. step is "draw", "untap" or \
"cleanup"; seat is the player whose step it is. Example: a permanent that replaces draws. Use \
step "loss" for an effect that stops a player losing the game (Platinum Angel): the table \
otherwise removes a player at 0 life, 10 poison, 21 commander damage or a draw from an empty library.
- step_trigger: an ability that triggers at the beginning of a step. step is one of upkeep, \
draw, main1, begin_combat, end_combat, main2, end; whose is "each" or a seat number.
- event_trigger: an ability that triggers on a game event, described in text. Also give `on`, so \
that the table can put the trigger in front of its controller the moment the event happens: \
`event` is cast, enters, dies, leaves or attacks; `who` is "you" (the permanent's controller cast \
the spell or controlled the object), "opponent" or "any"; `type` is a word that must be in the \
object's type line, such as Creature, Dragon, Zombie or Instant; `nontoken` is true if tokens do \
not count; `other` is true if the ability says "another". Leave `on` out when the trigger does not \
fit these words, and describe it in text alone.
- static: a continuous effect that changes other objects, players or the rules while the \
permanent stays: "other Zombie creatures you control get +1/+1", "spells cost {1} less", \
"players can't gain life". Players will read it at the top of their view, so state it exactly.
Write no hook for a permanent that needs none.

Characteristics. Under `computed`, list every creature on the battlefield whose current power or \
toughness differs from what is printed on it, with the values after all continuous effects, \
counters and notes, and the reason. List all of them each time: the list replaces the last one. \
The table stamps these on the permanents, so that players do not each have to work out that a \
3/3 Zombie beside Lord of the Undead is a 4/4.

Disputes and rulings. A line in the record where a player disputes something is a request to \
check it with particular care. Answer each under `disputes`: whether the player is right, and why, \
in a sentence the table will read. If the player is right, also report the finding. RULINGS ALREADY \
MADE IN THIS GAME are a judge's decisions on appeal: they bind you, so never report again what a \
ruling has settled.

Answer in the required JSON form. Seats are numbered from 0 in the order listed."""

CHARACTERISTICS_SYSTEM = """\
You work out the current power and toughness of the creatures in a game of Magic: The Gathering \
(Commander), for players who are about to declare blockers. You never play and you never comment \
on strategy or on whether earlier play was legal.

How this table works. The table is a virtual tabletop that knows no card text, so it shows what \
is printed on each card and nothing that other cards do to it. Conventions to read it by:
- Effects that live on no card are written as notes on a permanent, such as "gets +3/+3 until \
end of turn". Treat a note as true.
- The battlefield is listed in order of arrival, oldest first. That order is the timestamp order.
- CONTINUOUS EFFECTS lists the static abilities found at the last audit. Permanents that arrived \
since then may have others: read the card texts you are given.
- THIS TURN SO FAR is what has been done this turn. An effect that lasts until end of turn and \
appears there still applies even if nobody wrote it as a note.

List every creature on the battlefield whose current power, toughness or combat keywords differ \
from what is printed on it (for a token, from what it was created with), and every creature whose \
printed value is not a number. Under `keywords` give all the combat keywords the creature has now, \
from this list only: flying, reach, first strike, double strike, deathtouch, trample, lifelink, \
vigilance, menace, haste, defender, indestructible. The table does the arithmetic of combat from \
your numbers, so an Equipment's or an anthem's keyword matters as much as its +1/+1. Apply continuous effects in layer order (rule 613.4: characteristic-defining \
abilities, effects that set values, then modifications and counters, then switching), and use \
timestamps within a layer. Give the values and, briefly, the reason. A creature you do not list is \
taken at its printed values, and your list replaces the last one entirely.

Answer in the required JSON form."""

VERDICT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["verdict", "findings", "hooks"],
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "findings"]},
        "findings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "summary", "basis", "fix", "confidence"],
            "properties": {
                "kind": {"type": "string", "enum": [*SERIOUS, "note"]},
                "event": {"type": "integer", "description": "The number of the log event at fault, if one is."},
                "seat": {"type": "integer", "description": "The seat that must act on this: the controller of a trigger, "
                                                           "or the player whose action was wrong."},
                "summary": {"type": "string", "description": "One or two sentences: what is wrong."},
                "basis": {"type": "string", "description": "The quoted oracle text, ruling or rule number it rests on."},
                "fix": {"type": "string", "description": "What should happen instead, concretely."},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]}}}},
        "hooks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["kind", "handle", "source", "text"],
            "properties": {
                "kind": {"type": "string", "enum": ["hold", "step_trigger", "event_trigger", "static"]},
                "handle": {"type": "string", "description": "The permanent's handle, such as c12."},
                "source": {"type": "string", "description": "The card's name."},
                "text": {"type": "string", "description": "What happens, in a sentence."},
                "step": {"type": "string"},
                "seat": {"type": "integer", "description": "For a hold: whose step is held."},
                "whose": {"anyOf": [{"type": "string", "enum": ["each"]}, {"type": "integer"}],
                          "description": "For a step_trigger: \"each\" or a seat number."},
                "controller": {"type": "integer"},
                "on": {"type": "object", "additionalProperties": False, "required": ["event"], "properties": {
                    "event": {"type": "string", "enum": ["cast", "enters", "dies", "leaves", "attacks"]},
                    "who": {"type": "string", "enum": ["you", "opponent", "any"]},
                    "type": {"type": "string"}, "nontoken": {"type": "boolean"}, "other": {"type": "boolean"}}}}}},
        "disputes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["event", "upheld", "answer"],
            "properties": {"event": {"type": "integer", "description": "The number of the event in which the player raised the dispute."},
                           "upheld": {"type": "boolean"}, "answer": {"type": "string"}}}},
        "computed": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["handle", "power", "toughness", "because"],
            "properties": {"handle": {"type": "string", "description": "The creature's handle, such as c12."},
                           "power": {"type": "string"}, "toughness": {"type": "string"},
                           "keywords": {"type": "array", "items": {"type": "string"},
                                        "description": "Every combat keyword it has now, if that differs from what is printed."},
                           "because": {"type": "string", "description": "What changes it, briefly."}}}},
    },
}


COMPUTED_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["computed"],
                   "properties": {"computed": VERDICT_SCHEMA["properties"]["computed"]}}


def _object_line(obj: dict, names: dict[str, str], combat: dict[str, str], database: CardDatabase | None = None) -> str:
    parts = [obj.get("name", "a face-down permanent") + f" [{obj['handle']}]"]
    card = database.find(obj["name"]) if database is not None and obj.get("name") and not obj.get("token") else None
    if card is not None:  # what is printed on it, so nobody has to look the card up to read the board
        face = card.front
        parts.append(face.type_line + (f" {face.power}/{face.toughness} printed" if face.power is not None else ""))
    if obj.get("computed"):
        now = obj["computed"]
        parts.append(f"NOW {now['power']}/{now['toughness']} ({now['because']}; worked out {now['as_of']})")
    token = obj.get("token")
    if token:
        parts.append("token " + ", ".join(f"{k}: {v}" for k, v in token.items() if k != "name"))
    parts += [word for word, on in (("tapped", obj.get("tapped")), ("arrived this turn", obj.get("entered_this_turn")),
                                    ("commander", obj.get("is_commander")), ("phased out", obj.get("phased_out")),
                                    ("transformed", obj.get("transformed"))) if on]
    if obj["handle"] in combat:
        parts.append(combat[obj["handle"]])
    if obj.get("counters"):
        parts.append("counters: " + ", ".join(f"{n} {kind}" for kind, n in obj["counters"].items()))
    if obj.get("damage"):
        parts.append(f"{obj['damage']} damage marked")
    if obj.get("attached_to"):
        parts.append(f"attached to {names.get(obj['attached_to'], 'something')} [{obj['attached_to']}]")
    parts += [f"note: \"{a['text']}\"" + (f" (until {a['expires']})" if a.get("expires") else "")
              for a in obj.get("annotations", [])]
    if obj.get("owner") != obj.get("controller"):
        parts.append(f"owned by seat {obj['owner']}")
    return "; ".join(parts)


def describe(view: dict, database: CardDatabase | None = None) -> str:
    """A public view as text: shorter than the JSON and easier to read a board from. With a card
    database, each permanent shows its printed type line and power and toughness."""
    seats = {seat["seat"]: seat for seat in view["seats"]}
    who = lambda index: f"{seats[index]['name']} (seat {index})"  # noqa: E731
    names = {obj["handle"]: obj.get("name", "a face-down permanent") for obj in view["battlefield"]}
    turn, pen = view["turn"], view["pen"]
    waiting = ", ".join(who(s) for s in pen["seats"]) or "nobody"
    lines = [f"Round {turn.get('round', '?')}, {who(turn['active'])}'s turn (table turn {turn['number']}). Step: {turn['step']}. "
             f"Lands played this turn: {turn['land_plays']}. The table is waiting for {waiting} "
             f"({pen.get('action') or pen.get('step') or pen['kind']})."]

    def target(t: dict) -> str:
        if "seat" in t:
            return who(t["seat"])
        return "a target that is gone" if t.get("gone") else f"{t.get('name') or 'an object'} [{t.get('object') or t.get('stack_item')}]"

    lines.append("Stack, top first:" if view["stack"] else "Stack: empty.")
    for item in [*reversed(view["stack"])] + ([view["resolving"]] if view.get("resolving") else []):
        label = " (resolving now)" if item is view.get("resolving") else ""
        aims = f" targeting {', '.join(target(t) for t in item['targets'])}" if item["targets"] else ""
        extra = "".join(f"; {key}: {item[key]}" for key in ("x", "modes", "payment") if item.get(key))
        lines.append(f"  - [{item['handle']}] {item['kind']} {item['source']} controlled by {who(item['controller'])}"
                     f"{aims}{label}{(': ' + item['text']) if item['text'] else ''}{extra}")

    combat = {a["attacker"]: f"attacking {target(a['target'])}" for a in turn["attackers"]}
    combat |= {b["blocker"]: f"blocking {names.get(b['attacker'], 'an attacker')} [{b['attacker']}]" for b in turn["blockers"]}
    for index, seat in seats.items():
        extras = [f"{n} {kind} counters" for kind, n in seat["counters"].items()]
        extras += [f"{n} commander damage from {source}" for source, n in seat["commander_damage"].items()]
        extras += [f"has cast {name} from the command zone {n} time(s)" for name, n in seat["commander_casts"].items()]
        lines.append(f"{who(index)}: {seat['status']}, {seat['life']} life, {seat['hand']['count']} cards in hand, "
                     f"{seat['library']['count']} in library" + "".join(f"; {e}" for e in extras) + ".")
        for zone in ("command", "graveyard", "exile"):
            if seat[zone]:
                order = " (top first)" if zone == "graveyard" else ""
                lines.append(f"  {zone}{order}: " + ", ".join(o.get("name", "a face-down card") for o in seat[zone]))
        for zone in ("hand", "library"):
            if seat[zone].get("known"):
                lines.append(f"  revealed in {zone}: " + ", ".join(o.get("name", "?") for o in seat[zone]["known"]))
        mine = [obj for obj in view["battlefield"] if obj.get("controller") == index]
        lines.append("  controls, in order of arrival:" if mine else "  controls nothing.")
        lines += [f"    - {_object_line(obj, names, combat, database)}" for obj in mine]
    effects = [e for e in view["ledger"] if e.get("kind") == "static"]
    if effects:
        lines = ["CONTINUOUS EFFECTS IN PLAY (they change what is printed on other cards)",
                 *(f"  - {names.get(e.get('source'), 'a permanent')} [{e.get('source')}], {who(e['controller'])}: {e.get('text', '')}"
                   for e in effects), ""] + lines
    others = [e for e in view["ledger"] if e.get("kind") != "static"]
    if others:
        lines.append("Ledger: " + "; ".join(f"{e.get('kind')}: {e.get('text', '')}" for e in others))
    lines += [f"Table warning: {warning}" for warning in view.get("warnings", [])]
    return "\n".join(lines)


def _names(view: dict) -> list[str]:
    found = [obj.get("name") for obj in view["battlefield"]]
    found += [item["source"] for item in view["stack"]] + ([view["resolving"]["source"]] if view.get("resolving") else [])
    for seat in view["seats"]:
        found += [obj.get("name") for zone in ("command", "graveyard", "exile") for obj in seat[zone]]
    return [name for name in found if name]


def card_reference(names: list[str], database: CardDatabase, *, rulings_per_card: int = 6) -> str:
    lines = []
    for name in sorted(set(names)):
        card = database.find(name)
        if card is None:
            continue
        for face in card.faces:
            stats = f" {face.power}/{face.toughness}" if face.power is not None else \
                f" loyalty {face.loyalty}" if face.loyalty else ""
            lines.append(f"{face.name} {face.mana_cost} — {face.type_line}{stats}")
            lines += [f"    {text}" for text in face.oracle_text.splitlines()]
        rulings = database.rulings(card)
        lines += [f"    ruling {r['date']}: {r['text']}" for r in (rulings[-rulings_per_card:] if rulings_per_card else [])]
    return "\n".join(lines)


def materials(table: Table, before: str, since_event: int, database: CardDatabase, before_names: list[str],
              until_event: int | None = None) -> str:
    """The prompt for one audit: the table before, what was done, the table after, the cards."""
    after_view = table.view(None)
    rulings = [f"- (turn {r['turn']}) {r['text']}" for r in table.game.rulings]
    return "\n\n".join([
        *(["RULINGS ALREADY MADE IN THIS GAME\n" + "\n".join(rulings)] if rulings else []),
        "THE TABLE BEFORE\n" + before,
        "WHAT WAS DONE\n" + "\n".join(record(table, since_event, until_event)),
        "THE TABLE NOW\n" + describe(after_view, database),
        "CARDS\n" + card_reference(before_names + _names(after_view), database),
        "Audit the record. Judge only the events listed under WHAT WAS DONE."])


def record(table: Table, since_event: int, until_event: int | None = None) -> list[str]:
    """The public record of a stretch of play, as the auditor and the judge read it."""
    lines, passing = [], []

    def flush_passes() -> None:
        if passing:  # a run of passes is one line: who passed matters, a line each does not
            lines.append(f"events {passing[0][0]}-{passing[-1][0]} (during {passing[-1][2]}): "
                          + ", ".join(dict.fromkeys(name for _, name, _ in passing)) + " passed priority.")
            passing.clear()

    for event in table.events[since_event:until_event]:
        if (not event["public"] and not event["lints"]) or event["tool"] in ("setup", "mulligan", "keep", "write_notes"):
            continue  # the table's own bookkeeping, private notes, and the mulligans it enforces itself
        if event["tool"] == "pass_priority" and not event["lints"]:
            passing.append((event["n"], table.game.seats[event["seat"]].name, event["step"]))
            continue
        flush_passes()
        actor = table.game.seats[event["seat"]].name if event["seat"] is not None else "the table"
        lines.append(f"event {event['n']} ({actor}, {event['tool']}, during {event['step']}): " + " ".join(event["public"]))
        lines += [f"    table warning: {lint}" for lint in event["lints"]]
    flush_passes()
    return lines


class Auditor:
    def __init__(self, runner, database: CardDatabase, *, context: str = ""):
        """`context` says what kind of game this is, where that changes a ruling."""
        self.runner, self.database, self.context = runner, database, context

    def snapshot(self, table: Table) -> tuple[str, list[str], int]:
        """Take this before the actions to be audited: the board, its card names, the next event number."""
        view = table.view(None)
        return describe(view, self.database), _names(view), len(table.events)

    def audit(self, table: Table, snapshot: tuple[str, list[str], int], until_event: int | None = None) -> tuple[dict, Reply]:
        before, names, since = snapshot
        prompt = materials(table, before, since, self.database, names, until_event)
        reply = self.runner.ask(SYSTEM, (f"ABOUT THIS GAME\n{self.context}\n\n" if self.context else "") + prompt,
                                schema=VERDICT_SCHEMA)
        verdict = reply.data
        serious = [f for f in verdict.get("findings", []) if f.get("kind") in SERIOUS]
        verdict["verdict"] = "findings" if serious else "ok"  # the findings decide, whatever the label says
        return verdict, reply

    def characteristics(self, table: Table) -> tuple[list[dict], Reply]:
        """Current power and toughness only, for the moment before blocks: audits are a turn apart,
        and what was cast this turn has changed the numbers since the last one."""
        view, turn = table.view(None), table.game.turn.number
        done = [f"[{e['step']}] " + " ".join(e["public"]) for e in table.events
                if e["turn"] == turn and e["public"] and e["tool"] not in ("pass_priority", "say")]
        reply = self.runner.ask(CHARACTERISTICS_SYSTEM, "\n\n".join([
            "THE TABLE NOW\n" + describe(view, self.database), "THIS TURN SO FAR\n" + ("\n".join(done) or "(nothing)"),
            "CARDS\n" + card_reference([o.get("name") for o in view["battlefield"] if o.get("name")], self.database, rulings_per_card=3),
            "Work out the creatures' current power and toughness."]), schema=COMPUTED_SCHEMA)
        return list((reply.data or {}).get("computed") or []), reply
