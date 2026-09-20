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
"cleanup"; seat is the player whose step it is. Example: a permanent that replaces draws.
- step_trigger: an ability that triggers at the beginning of a step. step is one of upkeep, \
draw, main1, begin_combat, end_combat, main2, end; whose is "each" or a seat number.
- event_trigger: an ability that triggers on a game event, described in text.
Write no hook for a permanent that needs none.

Answer in the required JSON form. Seats are numbered from 0 in the order listed."""

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
                "kind": {"type": "string", "enum": ["hold", "step_trigger", "event_trigger"]},
                "handle": {"type": "string", "description": "The permanent's handle, such as c12."},
                "source": {"type": "string", "description": "The card's name."},
                "text": {"type": "string", "description": "What happens, in a sentence."},
                "step": {"type": "string"},
                "seat": {"type": "integer", "description": "For a hold: whose step is held."},
                "whose": {"anyOf": [{"type": "string", "enum": ["each"]}, {"type": "integer"}],
                          "description": "For a step_trigger: \"each\" or a seat number."},
                "controller": {"type": "integer"}}}},
    },
}


def _object_line(obj: dict, names: dict[str, str], combat: dict[str, str]) -> str:
    parts = [obj.get("name", "a face-down permanent") + f" [{obj['handle']}]"]
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


def describe(view: dict) -> str:
    """A public view as text: shorter than the JSON and easier to read a board from."""
    seats = {seat["seat"]: seat for seat in view["seats"]}
    who = lambda index: f"{seats[index]['name']} (seat {index})"  # noqa: E731
    names = {obj["handle"]: obj.get("name", "a face-down permanent") for obj in view["battlefield"]}
    turn, pen = view["turn"], view["pen"]
    waiting = ", ".join(who(s) for s in pen["seats"]) or "nobody"
    lines = [f"Turn {turn['number']}: {who(turn['active'])} is the active player. Step: {turn['step']}. "
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
        lines += [f"    - {_object_line(obj, names, combat)}" for obj in mine]
    if view["ledger"]:
        lines.append("Ledger: " + "; ".join(f"{e.get('kind')}: {e.get('text', '')}" for e in view["ledger"]))
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
        lines += [f"    ruling {r['date']}: {r['text']}" for r in rulings[-rulings_per_card:]]
    return "\n".join(lines)


def materials(table: Table, before: str, since_event: int, database: CardDatabase, before_names: list[str],
              until_event: int | None = None) -> str:
    """The prompt for one audit: the table before, what was done, the table after, the cards."""
    after_view = table.view(None)
    record = []
    for event in table.events[since_event:until_event]:
        if (not event["public"] and not event["lints"]) or event["tool"] in ("setup", "mulligan", "keep"):
            continue  # the table's own bookkeeping, and the mulligans it enforces itself
        actor = table.game.seats[event["seat"]].name if event["seat"] is not None else "the table"
        record.append(f"event {event['n']} ({actor}, {event['tool']}, during {event['step']}): " + " ".join(event["public"]))
        record += [f"    table warning: {lint}" for lint in event["lints"]]
    return "\n\n".join([
        "THE TABLE BEFORE\n" + before,
        "WHAT WAS DONE\n" + "\n".join(record),
        "THE TABLE NOW\n" + describe(after_view),
        "CARDS\n" + card_reference(before_names + _names(after_view), database),
        "Audit the record. Judge only the events listed under WHAT WAS DONE."])


class Auditor:
    def __init__(self, runner, database: CardDatabase, *, context: str = ""):
        """`context` says what kind of game this is, where that changes a ruling."""
        self.runner, self.database, self.context = runner, database, context

    def snapshot(self, table: Table) -> tuple[str, list[str], int]:
        """Take this before the actions to be audited: the board, its card names, the next event number."""
        view = table.view(None)
        return describe(view), _names(view), len(table.events)

    def audit(self, table: Table, snapshot: tuple[str, list[str], int], until_event: int | None = None) -> tuple[dict, Reply]:
        before, names, since = snapshot
        prompt = materials(table, before, since, self.database, names, until_event)
        reply = self.runner.ask(SYSTEM, (f"ABOUT THIS GAME\n{self.context}\n\n" if self.context else "") + prompt,
                                schema=VERDICT_SCHEMA)
        verdict = reply.data
        serious = [f for f in verdict.get("findings", []) if f.get("kind") in SERIOUS]
        verdict["verdict"] = "findings" if serious else "ok"  # the findings decide, whatever the label says
        return verdict, reply
