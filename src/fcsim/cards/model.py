"""The card fields the simulator uses, trimmed from Scryfall card objects."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

COLORS = "WUBRG"
_SYMBOL = re.compile(r"\{([^}]+)\}")
_CARD_TYPES = frozenset("Artifact Battle Creature Enchantment Instant Kindred Land Planeswalker Sorcery".split())


@dataclass(frozen=True)
class Face:
    name: str
    mana_cost: str = ""
    type_line: str = ""
    oracle_text: str = ""
    power: str | None = None
    toughness: str | None = None
    loyalty: str | None = None


@dataclass(frozen=True)
class Card:
    oracle_id: str
    name: str
    layout: str
    mana_cost: str
    mana_value: float
    type_line: str
    color_identity: tuple[str, ...]
    keywords: tuple[str, ...]
    produced_mana: tuple[str, ...]
    legality: str  # Commander legality: legal, banned, not_legal or restricted
    game_changer: bool
    faces: tuple[Face, ...]
    edhrec_rank: int | None = None
    price_usd: float | None = None
    scryfall_uri: str = ""

    @classmethod
    def from_scryfall(cls, obj: dict) -> Card:
        """Build from a Scryfall card object; multi-faced cards keep each face's text."""
        raw_faces = obj.get("card_faces") or [obj]
        faces = tuple(Face(name=f.get("name", obj["name"]), mana_cost=f.get("mana_cost") or "",
                           type_line=f.get("type_line") or "", oracle_text=f.get("oracle_text") or "",
                           power=f.get("power"), toughness=f.get("toughness"), loyalty=f.get("loyalty"))
                      for f in raw_faces)
        price = (obj.get("prices") or {}).get("usd")
        return cls(oracle_id=obj["oracle_id"], name=obj["name"], layout=obj.get("layout", "normal"),
                   mana_cost=obj.get("mana_cost") or faces[0].mana_cost,
                   mana_value=float(obj.get("cmc") or 0),
                   type_line=obj.get("type_line") or " // ".join(f.type_line for f in faces),
                   color_identity=tuple(obj.get("color_identity") or ()),
                   keywords=tuple(obj.get("keywords") or ()),
                   produced_mana=tuple(obj.get("produced_mana") or ()),
                   legality=(obj.get("legalities") or {}).get("commander", "not_legal"),
                   game_changer=bool(obj.get("game_changer")), faces=faces,
                   edhrec_rank=obj.get("edhrec_rank"), price_usd=float(price) if price else None,
                   scryfall_uri=(obj.get("scryfall_uri") or "").split("?")[0])

    @classmethod
    def from_dict(cls, data: dict) -> Card:
        fields = dict(data)
        fields["faces"] = tuple(Face(**face) for face in fields["faces"])
        for key in ("color_identity", "keywords", "produced_mana"):
            fields[key] = tuple(fields[key])
        return cls(**fields)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def front(self) -> Face:
        return self.faces[0]

    @property
    def text(self) -> str:
        """Oracle text of every face, which is what role heuristics should read."""
        return "\n".join(face.oracle_text for face in self.faces if face.oracle_text)

    @property
    def types(self) -> frozenset[str]:
        """Card types of the front face, the one the card is while in a library or hand."""
        return frozenset(self.front.type_line.split("—")[0].split()) & _CARD_TYPES

    @property
    def is_land(self) -> bool:
        return "Land" in self.types

    @property
    def land_face(self) -> Face | None:
        """The face that can be played as a land from hand, including the back of a modal card.

        A transforming card with a land on the back cannot be played as a land, so it has none.
        """
        if self.is_land:
            return self.front
        if self.layout != "modal_dfc":
            return None
        return next((f for f in self.faces if "Land" in f.type_line.split("—")[0].split()), None)

    @property
    def is_basic(self) -> bool:
        return "Basic" in self.front.type_line.split("—")[0].split()

    @property
    def is_permanent(self) -> bool:
        return bool(self.types - {"Instant", "Sorcery", "Kindred"})


def symbols(mana_cost: str) -> list[str]:
    return _SYMBOL.findall(mana_cost)


def color_pips(mana_cost: str) -> dict[str, float]:
    """Colored requirements of a cost. A hybrid symbol is split between its colors."""
    pips = dict.fromkeys(COLORS, 0.0)
    for symbol in symbols(mana_cost):
        colors = [part for part in symbol.split("/") if part in COLORS]
        for color in colors:
            pips[color] += 1 / len(colors)
    return pips
