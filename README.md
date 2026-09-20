# fcsim

Commander playtesting is still in design. The first implemented component is an
offline, versioned Comprehensive Rules reference. It has no runtime dependencies
outside Python 3.12+ and its SQLite FTS5 support.

```sh
uv sync
uv run fcsim rules info
uv run fcsim rules get 117.3c
uv run fcsim rules search "replacement effects drawing cards" --limit 5
uv run fcsim rules section 117 --limit 20
uv run fcsim rules glossary "Summoning Sickness Rule"
```

Every command returns JSON with the rules version, effective date, source URL,
file hashes, and source-text line numbers. Exact lookup includes the rule's
section, parent rule, child identifiers, and references. Search covers numbered
rules and the glossary, uses ranked lexical matching with English stemming, and
returns exact passages rather than generated answers. Section browsing is
paginated; use `next_offset` for the next page. Results are limited to 100 entries
per request. It is a reference, not a rules engine or a semantic search model.

`lookup` accepts either a rule/section number, an exact glossary term, or a text
query:

```sh
uv run fcsim rules lookup "117.3c"
uv run fcsim rules lookup "priority after casting a spell"
```

Agents can use the same read methods when the tool registry is built:

```python
from fcsim.rules import RulesReference

rules = RulesReference()
rules.lookup_rule("117.3c")
rules.get_rule("903.9a")
rules.get_section("614", offset=0, limit=10)
rules.search_rules("replacement effects drawing cards", limit=5)
```

Construct the reference in the host and bind only its read methods as agent
tools. Choosing a source directory/version and exporting files are host actions.
An HTTP MCP server and the rest of the simulator are not implemented yet.

To give a game its own self-contained reference:

```sh
uv run fcsim rules export games/example/rules
uv run fcsim rules --directory games/example/rules get 117.3c
```

Export requires a new directory and copies the original source, extracted text,
and a manifest containing only the selected version. Loading verifies both
hashes. There is no network access or automatic update during lookup. The default
is pinned in the manifest; an explicitly selected future-effective version needs
`--allow-future`. `--as-of YYYY-MM-DD` checks applicability for a historical game
but does not automatically choose another version. All global rules options go
before the subcommand.

See [rules/README.md](rules/README.md) for provenance, extraction, and updates.

## Deck check

The second component reads a decklist and reports on it without playing a game:
Commander legality, composition and curve, color sources against costs, card
roles guessed from oracle text, power signals (Game Changers, fast mana, tutors;
reported, never enforced), and a shuffle simulation of the opening turns under a
stated mulligan and land policy.

```sh
uv run fcsim cards update                  # once: download Scryfall's Oracle cards (about 25 MB)
uv run fcsim deck check examples/talrand.txt
uv run fcsim deck check mydeck.txt --commander "Atraxa, Praetors' Voice" --verbose
uv run fcsim deck check mydeck.txt --json  # the same content for tools
uv run fcsim cards show "Rhystic Study"
```

Decklists are MTGO text (`1 Card Name`, the commander after a blank line), and
the parser also accepts Moxfield, Archidekt and Arena exports. Card data lives in
`data/scryfall/` (or `$FCSIM_DATA`) and only `cards update` uses the network. The
exit status is 0 for a legal deck, 1 for an illegal one, and 2 for an error.
This is deliberately shallow: what a deck is really like comes from playing it.

```sh
uv run pytest
uv build
```

Wheels include the reference files, so installed lookups also work outside this
checkout. `PLAN.md` describes the broader project.
