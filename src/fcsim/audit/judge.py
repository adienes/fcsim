"""The judge: the appeal from the auditor. It is asked only when a player contests a finding.

Like the auditor it sees public information only and is rebuilt for every call. It is given both
sides, the stretch of play in question, the cards with their official rulings, and the text of
every rule either side cited, so that a rule number cannot be bluffed.
"""

from __future__ import annotations

import re

from ..agents import Reply
from ..cards import CardDatabase
from ..rules import RulesError, RulesReference
from ..table import Table
from .auditor import _names, card_reference, describe, record

SYSTEM = """\
You are the judge for a game of Magic: The Gathering (Commander) played by AI agents. You never \
play and you never comment on strategy. A rules auditor checks each stretch of play. It has \
reported a problem, and the player it concerns says the auditor is wrong. You decide, under the \
Comprehensive Rules and the cards' oracle text, and your decision is final.

How this table works. The table is a virtual tabletop, not a rules engine: it enforces zones, \
turn order, priority and hidden information, and knows no card text. Players carry out every \
card effect themselves, as logged actions. Mana is not tracked: a payment is announced by tapping \
permanents, sometimes with a note. Effects that live on no card are written as notes on a \
permanent. What the table enforces is nobody's error: that cards are conserved, whose turn and \
priority it is, and mulligans (in multiplayer the first mulligan is free, rule 103.5c). Hidden \
cards appear as counts, and the table guarantees a player really had the cards they used.

Neither side has standing with you: the auditor raises false alarms and players argue their own \
corner. Go by what you can quote. RULES CITED gives the text of each rule number either side \
named, so check that it says what they claim. Decide:
- uphold: the auditor is right. The player must make the fix the auditor described.
- overrule: the play was correct, or the table's conventions explain it. Nothing changes.
- amend: there is a real problem, but the auditor's account or fix is wrong. Give the right fix in \
`fix`, as instructions the player can carry out now. Prefer a repair going forward over undoing \
play, and the smallest one that puts the game right.

`explanation` is two or three sentences for the table, quoting the text you relied on. `ruling` is \
one sentence stating the point in general terms ("In multiplayer the first mulligan is free, so \
a player who mulligans once keeps seven cards"). It is shown to every player and to the auditor \
for the rest of the game, so that the same question is not argued twice; leave it empty if the \
dispute was about what happened rather than about a rule.

Answer in the required JSON form."""

DECISION_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["decision", "explanation", "fix", "ruling"],
    "properties": {"decision": {"type": "string", "enum": ["uphold", "overrule", "amend"]}, "explanation": {"type": "string"},
                   "fix": {"type": "string"}, "ruling": {"type": "string"}},
}
_RULE_NUMBER = re.compile(r"\b(\d{3}\.\d+[a-z]?)\b")


class Judge:
    def __init__(self, runner, database: CardDatabase, *, rules: RulesReference | None = None, context: str = ""):
        self.runner, self.database, self.rules, self.context = runner, database, rules, context

    def cited(self, *texts: str) -> list[str]:
        """The text of every rule number named in `texts`, from the pinned rules; a number that does not exist says so."""
        found = []
        for number in dict.fromkeys(n for text in texts for n in _RULE_NUMBER.findall(text)):
            if self.rules is None:
                break
            try:
                found.append(self.rules.get_rule(number)["rule"]["text"])
            except (RulesError, KeyError):
                found.append(f"{number}: there is no rule with this number.")
        return found

    def rule(self, table: Table, since_event: int) -> tuple[dict, Reply]:
        """Decide the contested finding the table is waiting on. `since_event` is where the audited stretch began."""
        detail, view = table.game.control.detail, table.view(None)
        if table.game.control.kind != "dispute":
            raise ValueError("No contested finding is waiting for a ruling.")
        rulings = [f"- (turn {r['turn']}) {r['text']}" for r in table.game.rulings]
        cited = self.cited(detail["finding"], detail["objection"])
        reply = self.runner.ask(SYSTEM, "\n\n".join([
            *([f"ABOUT THIS GAME\n{self.context}"] if self.context else []),
            f"THE AUDITOR'S FINDING (about {table.game.seats[detail['seat']].name})\n{detail['finding']}",
            f"THE PLAYER'S OBJECTION\n{detail['objection']}",
            *(["RULES CITED\n" + "\n".join(cited)] if cited else []),
            *(["RULINGS ALREADY MADE IN THIS GAME\n" + "\n".join(rulings)] if rulings else []),
            "WHAT WAS DONE\n" + "\n".join(record(table, since_event)),
            "THE TABLE NOW\n" + describe(view, self.database),
            "CARDS\n" + card_reference(_names(view), self.database),
            "Rule on the finding."]), schema=DECISION_SCHEMA)
        decision = reply.data
        if decision["decision"] == "amend" and not decision.get("fix", "").strip():
            decision["decision"] = "uphold"  # a problem with no other fix on offer is the auditor's finding
        return decision, reply
