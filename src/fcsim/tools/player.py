"""The tools a player's agent gets, bound to one seat. The descriptions are the agent's manual."""

from __future__ import annotations

from ..cards import CardDatabase
from ..rules import RulesError, RulesReference
from ..table import Table

_HANDLE = {"type": "string", "description": "A handle from get_view, such as \"c12\"."}
_TARGETS = {"type": "array", "description": "Each target is {\"object\": handle}, {\"seat\": number} or {\"stack_item\": handle}.",
            "items": {"type": "object"}}
_PAYMENT = {"type": "object", "description": "How the cost is paid. `tap` lists permanents you control to tap for it: the table "
            "checks that they can make the cost, colors included, and refuses if not. `note` covers anything else (life paid, "
            "cards discarded, a Treasure sacrificed, a cost reducer); with a note the table leaves the sums to the auditor.",
            "properties": {"tap": {"type": "array", "items": _HANDLE}, "note": {"type": "string"}}}
_ENTERS = {"type": "object", "description": "How a permanent enters: {\"tapped\": true, \"counters\": {\"+1/+1\": 2}, "
           "\"attached_to\": handle}."}
MUTATIONS = """\
A mutation is one object in a list that is applied all at once or not at all. Objects are named by handle \
with `obj` (one) or `objs` (several), or by place with `from`: {"seat": n, "zone": "library", "position": "top" | \
"bottom", "count": k} or {"seat": n, "zone": "hand", "position": "random", "count": k}. Reaching into another \
player's hand or library needs a `because` naming the effect. The mutations:
move {obj|objs|from, to: zone, position?: "top"|"bottom" (library), controller?, enters?, face_down?, reveal?} - \
zones are library, hand, graveyard, exile, command, battlefield, stack. Cards always go to their owner's zones; a \
token that leaves the battlefield is gone. The card gets a new handle.
tap / untap {objs} · counters {obj | seat, kind, delta | set} · life {seat, delta | set} · damage {obj, amount}
create_token {token: {name, types, power, toughness, text}, count?, controller?, enters?, copy_of?: handle}
attach {obj, host: handle | null} · annotate {obj, text, expires?: "eot"} · clear_annotations {obj}
set_controller {obj, controller} · face {obj, face_down?, transformed?} · phase {obj, out}
draw {seat?, count} · mill {seat?, count} · shuffle {seat?} · discard_random {seat?, count}
look {from | seat (a whole hand), viewers?: [seats], because?} - the viewers, by default you, learn the cards
reveal {obj | objs | from | seat (a whole hand)} - everyone learns the cards
roll {sides, count} · flip_coin {count} · designate {key: "monarch" and the like, seat | null}
commander_damage {seat, commander: handle, amount | set} - only for damage the table did not count itself, or \
with `set` to correct a wrong total · eliminate {seat, reason}
extra_turn {seat?} · extra_combat · end_turn · skip_step {step} · remove_from_stack {item: handle}
delay {step, text, whose?: "mine" | seat} - a delayed trigger ("return it at the beginning of the next end step", "at the \
beginning of your next upkeep, sacrifice it"): the table puts it in front of you when that step next begins, on anyone's turn unless \
`whose` says otherwise, so that you need not remember it. Steps: upkeep, draw, main1, begin_combat, end_combat, main2, end.
request_choice {seats, prompt, options?} - asks other players something an effect makes them choose. It must be \
last in its list; your wake then ends and you are woken again with the answers.
Use annotate for effects that live on no card ("gets +3/+3 until end of turn", expires "eot"). You carry out \
state-based actions yourself, except that the table destroys a creature with lethal damage marked on it, or with no toughness \
left, when it knows the creature's numbers and nothing in play speaks of prevention, regeneration or indestructibility: look before \
you move a creature that may already have gone."""


def _tool(name: str, description: str, properties: dict | None = None, required: tuple[str, ...] = ()) -> dict:
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties or {}, "required": list(required)}}


_BATCH = {"type": "array", "items": {"type": "object"}, "description": "A list of mutations."}
PLAYER_TOOLS = [
    _tool("get_view", "Everything you may see: the turn and who the table is waiting for (`pen`), every player's "
          "life and zones, the battlefield, the stack, your own hand, questions and triggers waiting for you, the "
          "ledger of continuing effects, and warnings. Other players' hands and libraries are counts. Handles "
          "change when a card changes zones, so look again after acting."),
    _tool("get_log", "What has happened, as the table saw it, plus anything private to you (cards you drew or "
          "looked at). A run of passes of priority is one entry.", {"since": {"type": "integer", "description": "First event number wanted; use the last `next`."}}),
    _tool("lookup_card", "Oracle text, type, cost and official rulings for a card by name. Always read a card "
          "before relying on what it does.", {"name": {"type": "string"}}, ("name",)),
    _tool("lookup_rule", "Look up the Comprehensive Rules by rule number (\"117.3c\"), glossary term, or a search "
          "phrase.", {"query": {"type": "string"}}, ("query",)),
    _tool("mulligan", "Shuffle your hand into your library and draw seven. The first mulligan is free."),
    _tool("keep", "Keep your hand. After more than one mulligan, `bottom` must list one card per extra mulligan.",
          {"bottom": {"type": "array", "items": _HANDLE}}),
    _tool("pass_priority", "Pass priority. When every player passes in succession, the top of the stack resolves, "
          "or if the stack is empty the game moves to the next step. With `until`, you keep passing by yourself "
          "until that later step of this turn, stopping early if anything goes on the stack; passing through "
          "combat this way attacks with nothing. Use it to skip steps you have nothing to do in: until "
          "\"main2\" to skip combat, or until \"cleanup\" when you are finished, which passes through the rest of "
          "your turn including your end step.",
          {"until": {"type": "string", "enum": ["draw", "main1", "begin_combat", "declare_attackers", "declare_blockers",
                                                "combat_damage", "end_combat", "main2", "end", "cleanup"]}}),
    _tool("play_land", "Play a land: from your hand, or from anywhere else an effect lets you play it (a card exiled with \"you may "
          "play it\", the top of your library), in which case name that effect in `because`. It uses your land play for the turn.",
          {"card": _HANDLE, "enters": _ENTERS, "because": {"type": "string"}}, ("card",)),
    _tool("cast", "Cast a spell: it goes on the stack and you keep priority. Set `resolves` to \"auto_permanent\" "
          "only for a permanent that enters with nothing to decide (no enters-tapped, counters, choices or copy); "
          "it then enters by itself when it resolves. Otherwise you will be woken to resolve it.",
          {"card": _HANDLE, "targets": _TARGETS, "payment": _PAYMENT, "x": {"type": "integer"},
           "modes": {"type": "array", "items": {"type": "string"}},
           "text": {"type": "string", "description": "What the spell will do as cast, including how damage is divided."},
           "resolves": {"type": "string", "enum": ["auto_permanent"]}, "face_down": {"type": "boolean"},
           "because": {"type": "string", "description": "Needed only for a cast the table would not expect: a card from anywhere but "
                       "your hand or command zone (flashback, \"exiled with Prosper: I may play it this turn\"), or a card that is "
                       "not an instant and has no flash cast outside your own main phase or with something on the stack."}}, ("card",)),
    _tool("activate", "Activate an ability of a permanent. A mana ability (`mana_ability`: true) does not use the "
          "stack; anything else goes on the stack and you keep priority.",
          {"source": _HANDLE, "ability": {"type": "string", "description": "The ability's text."}, "targets": _TARGETS,
           "payment": _PAYMENT, "x": {"type": "integer"}, "mana_ability": {"type": "boolean"}}, ("source", "ability")),
    _tool("add_trigger", "Put a triggered ability on the stack. If the table is waiting for your triggers, name "
          "which one with `pending` (a handle from `waiting_triggers`). A waiting trigger that says it triggered several "
          "times stands for that many: add_trigger puts one of them on the stack and the rest go on waiting, and "
          "skip_trigger declines all that remain (say why: \"the first one exiles it; the rest do nothing\").",
          {"source": _HANDLE, "text": {"type": "string"}, "targets": _TARGETS, "pending": {"type": "string"}}),
    _tool("skip_trigger", "Decline a waiting trigger: a \"may\" you do not want, or one whose condition is false.",
          {"pending": {"type": "string"}, "because": {"type": "string"}}, ("pending", "because")),
    _tool("resolve", "Carry out part of the effect of your spell or ability that is resolving. Call it as many "
          "times as the effect needs: draw first, see the cards, then choose. " + MUTATIONS, {"mutations": _BATCH}, ("mutations",)),
    _tool("finish_resolution", "The effect is done. A spell's card still on the stack goes to `card_to`: "
          "\"graveyard\" by default, \"battlefield\" for a permanent.",
          {"card_to": {"type": "string"}, "enters": _ENTERS}),
    _tool("act", "Change the table outside a resolution: a special action, a state-based action, a cost, a step "
          "the table handed you. `because` must name the rule or effect. The mutations are those of `resolve`.",
          {"mutations": _BATCH, "because": {"type": "string"}}, ("mutations", "because")),
    _tool("finish_step", "You have finished a step the table handed you (a held untap or draw, or the cleanup discard)."),
    _tool("declare_attackers", "Declare attackers; an empty list attacks with nothing. Attackers are tapped unless "
          "`tap` is false (vigilance). The table refuses a tapped creature, one with defender, and one that arrived this turn "
          "without haste, unless `because` names the effect that lets it attack.",
          {"because": {"type": "string"}, "attacks": {"type": "array", "items": {"type": "object"},
                       "description": "Each is {\"attacker\": handle, \"target\": {\"seat\": n} or {\"object\": planeswalker handle}, \"tap\"?: false}."}}),
    _tool("declare_blockers", "Declare blockers; an empty list blocks nothing. The table refuses a tapped blocker, and one "
          "without flying or reach against a flier, unless `because` names the effect that allows it.",
          {"because": {"type": "string"}, "blocks": {"type": "array", "items": {"type": "object"}, "description": "Each is {\"blocker\": handle, \"attacker\": handle}."}}),
    _tool("combat_damage", "As the attacking player, deal this combat damage step's damage for every creature in combat. "
          "The table does the arithmetic: it shows you what it worked out from each creature's current power, toughness and "
          "keywords (first strike, deathtouch, trample, lifelink, indestructible), and `as_proposed`: true deals exactly that, "
          "moves the creatures that die, gains lifelink life and handles a first-strike step. Accept it unless a card changes "
          "how this combat's damage works (prevention, protection, a different division among several blockers): then give "
          "`assignments` yourself. The table updates life totals, damage marked on creatures and commander damage from the "
          "assignments, so never repeat them as mutations; `then` is only for what follows that the table did not do. Set "
          "`another_step` after a first-strike step you dealt by hand.",
          {"as_proposed": {"type": "boolean"}, "assignments": {"type": "array", "items": {"type": "object"},
                           "description": "Each is {\"source\": handle, \"target\": {\"seat\": n} or {\"object\": handle}, \"amount\": n}."},
           "then": _BATCH, "another_step": {"type": "boolean"}}),
    _tool("answer", "Answer a question another player's effect asks you (from `questions`).",
          {"request": {"type": "string"}, "answer": {}}, ("request", "answer")),
    _tool("say", "Say something to the table. Everyone sees it. Deals are not binding. Players you name in `to` are asked for an "
          "answer straight away, and what they say comes back in `replies`; a few times a turn at most.",
          {"text": {"type": "string"}, "to": {"type": "array", "items": {"type": "integer"}, "description": "Seats addressed."}}, ("text",)),
    _tool("set_response_policy", "Tell the table, privately, whether to ask you each time another player puts something "
          "on the stack. \"pass_always\" passes for you every time; \"pass_unless_targeted\" passes unless the spell or "
          "ability targets you or something you control; \"ask\" (the default) asks about every spell and activated ability, and "
          "about a triggered ability only if it targets you or yours; \"ask_everything\" asks about every triggered ability too "
          "(you hold something that answers them). It lasts until your next "
          "turn begins. Use it when you are tapped out or hold nothing to respond with: being asked costs time and "
          "changes nothing. Blocks, triggers and questions still come to you, and unless you pass always you are still "
          "asked when creatures attack and when blocks are declared. Set `hold_for_end_step` when you hold an instant, a "
          "flash card or an ability you mean to use at the end of the turn before yours: the table otherwise passes for "
          "you whenever the stack is empty on another player's turn.",
          {"policy": {"type": "string", "enum": ["ask", "ask_everything", "pass_unless_targeted", "pass_always"]},
           "hold_for_end_step": {"type": "boolean"}}, ("policy",)),
    _tool("contest", "Only when the auditor has asked you to correct the table and you are sure it is wrong: say why, quoting "
          "the card text or the rule. An independent judge rules, and the ruling is final: if it goes against you, you make "
          "the correction. Do not correct the table and contest as well. After three lost contests or disputes you may raise no more.",
          {"reason": {"type": "string"}}, ("reason",)),
    _tool("dispute", "Say that another player broke the rules or carried an effect out wrongly: what was wrong and why, with "
          "the event number from get_log if you have it. The auditor checks it at its next audit and answers in public; if "
          "it agrees, the player has to put it right. For real rules errors, not for strategy or table talk.",
          {"claim": {"type": "string"}, "event": {"type": "integer"}}, ("claim",)),
    _tool("get_notes", "Your private notes. They are your only memory between turns."),
    _tool("write_notes", "Replace your private notes: plans, threats, deals, what opponents have shown.",
          {"text": {"type": "string"}}, ("text",)),
    _tool("concede", "Leave the game."),
]


class SeatTools:
    """One seat's tools for one wake. Table calls go through the wake, so a late or repeated
    call cannot act twice or on a corrected board."""

    def __init__(self, table: Table, seat: int, wake: str, *, database: CardDatabase | None = None,
                 rules: RulesReference | None = None):
        self.table, self.seat, self.wake, self.database, self.rules = table, seat, wake, database, rules
        self.calls: list[dict] = []
        self.definitions = [t for t in PLAYER_TOOLS if (t["name"] != "lookup_card" or database)
                            and (t["name"] != "lookup_rule" or rules)]

    def call(self, request_id: str, name: str, arguments: dict) -> dict:
        if name == "lookup_card":
            card = self.database.find(str(arguments.get("name", "")))
            if card is None:
                hint = ", ".join(self.database.suggest(str(arguments.get("name", ""))))
                result = {"error": {"code": "unknown_card", "message": "No such card." + (f" Did you mean: {hint}?" if hint else "")}}
            else:
                result = {"name": card.name, "mana_cost": card.mana_cost, "mana_value": card.mana_value,
                          "faces": [{k: v for k, v in vars(face).items() if v not in (None, "")} for face in card.faces],
                          "color_identity": list(card.color_identity), "rulings": self.database.rulings(card)[-8:]}
        elif name == "lookup_rule":
            try:
                result = self.rules.lookup_rule(str(arguments.get("query", "")), limit=5)
            except RulesError as error:
                result = {"error": {"code": "rules_error", "message": str(error)}}
        else:
            result = self.table.call(self.wake, request_id, name, arguments)
        self.calls.append({"tool": name, "arguments": arguments, "error": (result.get("error") or {}).get("code")})
        return result
