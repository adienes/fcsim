---
name: card-research
description: Research Magic cards for a Commander deck in this repository. Use when asked what to add to or cut from a deck, to find cards with some effect or synergy, to check what a card does, or to judge whether a card has pulled its weight in played games. Searches the local copy of Scryfall's data in Scryfall's own syntax and reads the game records, so every claim can be checked.
---

# Card research

Never state what a card does, costs or is from memory. Look it up. Cards from recent sets are in
the local data even when you have never heard of them, and your memory of older cards is often a
little wrong. Two claims made from impressions in this project's early reviews were false and were
only caught because the owner pushed back.

## Finding cards

```sh
uv run fcsim cards search '<query>' [--deck <list or project>] [--limit 20] [--names | --json]
uv run fcsim cards show "<exact name>"          # everything about one card, rulings included
```

The query is Scryfall's search syntax, over the pinned local data (offline, about a third of a
second). Results are ordered by how much Commander players use the card (EDHREC rank), and each
shows its price and whether it is a Game Changer. A term the search does not understand is
refused, never ignored.

| Term | Meaning |
| --- | --- |
| `word`, `"two words"` | in the name |
| `o:"cast it from exile"`, `o:/you may (play\|cast)/` | oracle text contains; or a regular expression |
| `t:instant`, `kw:flash`, `name:zuko` | type line, keyword, name |
| `id<=rwb`, `id:mardu`, `id=wb`, `id:c` | colour identity within, exactly, colourless |
| `mv<=3`, `pow>=4`, `tou<=2`, `usd<5`, `edhrec<2000` | numbers, with `= < <= > >=` |
| `produces:r`, `is:commander`, `is:land`, `is:permanent`, `is:gamechanger` | |
| `-t:creature` | not |
| `a b or c d` | either group of terms |
| `order:edhrec\|usd\|mv\|name`, `direction:desc`, `include:illegal` | |

`--deck` keeps to that deck's colour identity and leaves out what it already runs, which is what
you want when looking for additions. Useful starts: the deck's own mechanic words
(`o:airbend`, `o:"from exile"`), the hole you are filling (`t:instant o:destroy mv<=2`), cheap
versions of an effect that is stuck in hand (`o:"extra combat" mv<=4`).

## Judging the cards a deck already runs

```sh
uv run fcsim report games/*_pod-* --deck <part of the seat's name>   # each card: games seen in, played, rounds held, stuck at the end
uv run fcsim rubric games/<game_dir>                                  # how the game went, from the log alone
uv run fcsim review <list> games/<a> games/<b>                        # a reviewer's swaps, fact-checked against the record
uv run fcsim deck check <list>                                        # legality, curve, colour sources, shuffle statistics
```

A card seen three times is an anecdote. Say how many times it was seen. Separate a card that was
never castable (its colours, its cost) from one that was castable and passed over, and both from
one that was simply never drawn. Before blaming a card, check the pilot: `THIS SEAT TURN BY TURN`
in a review's material, or the game's `log.md`, says what mana was open. Test a hunch against the
journal before repeating it; say so when it fails.

## What "better" means here

Not stronger. This group plays mid-to-high bracket 3, and wants games of 4 to 10 rounds in which
every player gets to do their thing and players interact. A swap should be legal in Commander,
inside the commander's colour identity, not already in the list, and should keep the deck's
identity: cut what did not pull its weight for this plan, not the pet cards that make it this
deck. Flag a Game Changer as one. Propose few swaps, each with its evidence, and leave the choice
to the owner: `fcsim deck apply` only makes the swaps they tick.
