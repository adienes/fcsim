"""Game state for the table: a virtual tabletop that knows zones and turns, and no card text."""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field

STEPS = ("untap", "upkeep", "draw", "main1", "begin_combat", "declare_attackers", "declare_blockers",
         "combat_damage", "end_combat", "main2", "end", "cleanup")
COMBAT = STEPS[4:9]
PUBLIC_ZONES = ("battlefield", "stack", "graveyard", "exile", "command")
HIDDEN_ZONES = ("library", "hand")
SEAT_ZONES = ("library", "hand", "graveyard", "exile", "command")


class TableError(ValueError):
    """A rejected call. The message tells an agent what was wrong and what to do instead."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": str(self)}}


class Dice:
    """All randomness at the table. Live, it draws from a seeded generator and records each
    outcome; replaying, it returns the recorded outcomes, so a replay never depends on the
    generator being called in the same order."""

    def __init__(self, seed: int | None):
        self._rng = random.Random(seed)
        self.recorded: list = []
        self._replaying: list | None = None

    def begin(self, recorded: list | None = None) -> None:
        self.recorded = []
        self._replaying = list(recorded) if recorded is not None else None
        self._at_begin = self._rng.getstate()

    def rollback(self) -> None:
        """The call was rejected: it used no randomness, as it changed nothing else."""
        self._rng.setstate(self._at_begin)

    def _outcome(self, make):
        value = make()  # always drawn, so a replayed table's generator is where the original's was
        if self._replaying is not None:
            value = self._replaying.pop(0)
        self.recorded.append(value)
        return value

    def order(self, count: int) -> list[int]:
        """A random permutation of range(count)."""
        return self._outcome(lambda: self._rng.sample(range(count), count))

    def pick(self, count: int) -> int:
        return self._outcome(lambda: self._rng.randrange(count))


@dataclass
class Obj:
    """A card or token in some zone. `id` changes whenever the object could no longer be tracked
    at a real table (rule 400.7 zone changes, and shuffles); `uid` is the physical card and is
    only ever written to the full-information log."""
    id: int
    uid: int
    owner: int
    controller: int
    zone: str
    name: str
    token: dict | None = None
    type_line: str = ""        # carried for matching triggers and for display; the table never interprets it
    is_commander: bool = False
    tapped: bool = False
    face_down: bool = False
    transformed: bool = False
    phased_out: bool = False
    counters: dict[str, int] = field(default_factory=dict)
    damage: int = 0
    attached_to: int | None = None
    annotations: list[dict] = field(default_factory=list)
    entered_turn: int | None = None
    computed: dict | None = None                      # current power and toughness as the auditor last worked them out
    public: bool = False                              # everyone knows its face
    known_by: set[int] = field(default_factory=set)   # seats that know its face while it is not public

    def face_known_to(self, viewer: int | None) -> bool:
        return self.public or viewer in self.known_by

    def addressable_by(self, viewer: int | None) -> bool:
        """Whether the viewer can point at this object: anything in a public zone, even face
        down, but in a hand or library only what the viewer can tell apart from the rest."""
        return self.zone in PUBLIC_ZONES or self.public or viewer in self.known_by


@dataclass
class StackItem:
    id: int
    kind: str                  # spell, activated or triggered
    controller: int
    source_name: str
    text: str
    source: int | None = None  # the permanent it came from, if it is still the same object
    obj: int | None = None     # the card on the stack, for a spell
    targets: list[dict] = field(default_factory=list)
    x: int | None = None
    modes: list[str] = field(default_factory=list)
    payment: str = ""
    auto_permanent: bool = False  # declared at cast: enters with nothing to decide, so no wake


@dataclass
class Seat:
    index: int
    name: str
    life: int = 40
    counters: dict[str, int] = field(default_factory=dict)
    commander_damage: dict[str, int] = field(default_factory=dict)  # by commander name and owner
    commander_casts: dict[str, int] = field(default_factory=dict)
    status: str = "alive"      # alive, eliminated or conceded
    library: list[int] = field(default_factory=list)  # index 0 is the top
    hand: list[int] = field(default_factory=list)
    graveyard: list[int] = field(default_factory=list)  # index 0 is the top
    exile: list[int] = field(default_factory=list)
    command: list[int] = field(default_factory=list)
    mulligans: int = 0
    turns_taken: int = 0       # this seat's own turn count: the "round" a player means by "turn five"
    response_policy: str = "ask"   # ask, pass_unless_targeted or pass_always; private, and reset on the seat's own turn
    kept: bool = False
    drew_from_empty: bool = False
    yield_to: dict | None = None   # {"turn", "step"}: pass by itself until that step, while the stack stays empty


@dataclass
class Turn:
    number: int = 0
    active: int = 0
    step: str = "setup"
    remaining: list[str] = field(default_factory=list)
    passed: list[int] = field(default_factory=list)
    land_plays: int = 0
    extra_turns: list[int] = field(default_factory=list)
    attackers: list[dict] = field(default_factory=list)
    blockers: list[dict] = field(default_factory=list)


@dataclass
class Control:
    """Who holds the pen, and why. Exactly the seats listed may change the table."""
    kind: str                  # setup, priority, resolution, triggers, request, turn_action, held_step, over
    seats: list[int] = field(default_factory=list)
    detail: dict = field(default_factory=dict)


@dataclass
class Game:
    seats: list[Seat]
    objects: dict[int, Obj] = field(default_factory=dict)
    battlefield: list[int] = field(default_factory=list)
    stack: list[StackItem] = field(default_factory=list)  # the last item is the top
    turn: Turn = field(default_factory=Turn)
    control: Control = field(default_factory=lambda: Control("setup"))
    resolution: dict | None = None      # the item resolving, its progress, and whether it is suspended
    requests: list[dict] = field(default_factory=list)
    pending_triggers: list[dict] = field(default_factory=list)
    ledger: list[dict] = field(default_factory=list)
    rulings: list[dict] = field(default_factory=list)
    designations: dict[str, int | str | None] = field(default_factory=dict)
    winner: int | None = None
    next_id: int = 1
    facts: list[dict] = field(default_factory=list)  # what has happened since triggers were last collected
    audit_pending: bool = False   # something auditable has happened since the last checkpoint was released
    gated: bool = False           # stop at audit checkpoints, and at the end of every turn

    def new_id(self) -> int:
        self.next_id += 1
        return self.next_id - 1

    def alive(self) -> list[int]:
        return [seat.index for seat in self.seats if seat.status == "alive"]

    def turn_order_from(self, start: int) -> list[int]:
        """Living seats in turn order beginning with `start` (APNAP order when it is the active seat)."""
        count = len(self.seats)
        return [i % count for i in range(start, start + count) if self.seats[i % count].status == "alive"]

    def to_dict(self) -> dict:
        data = asdict(self)
        for obj in data["objects"].values():
            obj["known_by"] = sorted(obj["known_by"])
        return data
