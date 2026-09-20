# fcsim

Playtest and tune Commander decks with LLM agents; `PLAN.md` is the design. Built
so far, each described below: an offline Comprehensive Rules reference, a deck
check, the game table, a rules auditor with a scenario suite that measures it, and
an audited solo goldfish in which an agent pilots a deck through tools served over
MCP, and a first multiplayer driver. Disputes, rewinds and the deck workshop are not
built yet. There
are no runtime dependencies outside Python 3.12+ and its SQLite FTS5 support; model
calls go through your local Claude Code or Codex CLI login.

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
Players already get `lookup_rule` this way, over the MCP server in `fcsim.mcp`.

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

## The table

`fcsim.table` is the game table that agents will play at (PLAN.md §4 to §6). It
knows zones, turns and priority, and no card text: seats carry out the rules
through calls, and a later auditor checks them. There is no command for it yet;
it is driven from Python, and by written scripts in the tests.

```python
from fcsim.table import ScriptedRunner, Table

table = Table([{"name": "Ann", "commanders": ["Talrand, Sky Summoner"], "library": [...]}, ...], seed=1)
table.expected()                       # [(0, "mulligan"), (1, "mulligan"), ...]: who must act, and how
table.act(0, "keep")                   # every call returns a result or {"error": {code, message}}
table.view(0)                          # what seat 0 may see; table.view(None) is the public view
Table.replay(table.config, table.events)   # the event log rebuilds the same game
```

- **Hidden information.** Internal ids never leave the table. Each seat has its
  own handles, only for things it can tell apart; another seat's hand or library
  is a count. A handle dies when its card changes zones or is shuffled away. A
  property test plays pairs of games that differ only in cards one seat cannot
  see, and requires everything that seat observes to be identical.
- **The pen.** Exactly the seats in `expected()` may change the table. A caster
  keeps priority; after a resolution the active player gets it; waiting triggers
  go on the stack before anyone receives priority.
- **Resolutions are state.** An effect is carried out in as many batches as it
  needs ("draw two", then choose the discard), can stop to ask other seats a
  question in APNAP order, and ends with `finish_resolution`.
- **Mechanical steps** (untap, draw, cleanup) run by themselves unless a hook in
  the ledger holds them for a seat, as when a draw is replaced.
- **Audit checkpoints.** A `gated` table stops whenever something auditable has
  happened and the pen would move on or a mechanical step would run, and at the
  end of every turn; the driver audits what it wants and calls `release`. So a
  turn's hooks exist before the next untap and upkeep.
- **The log** records each call with handles resolved and every random outcome.
  `rewind` returns to an earlier event and reshuffles what nobody knew; `patch`
  repairs the state going forward. Both kill open wakes, and a repeated call id
  inside a wake acts once.

## The auditor

Players carry out the rules themselves, so a non-playing auditor checks each
stretch of play (PLAN.md §8). It is shown public information only: the board
before and after, the record of what was done and in which step, and oracle text
and official rulings for the cards involved. It answers with findings (`illegal`,
`misresolved`, `sba`, `trigger`, or a non-blocking `note`), each naming the text
or rule it rests on, and with the hooks the card-blind table needs for permanents
that just arrived, such as "Mana Vault does not untap".

Model calls go through a selected CLI backend in headless mode, using its local
login. Claude Code remains the default; `--backend codex` selects Codex. Each
call is a fresh session, with coding tools and unrelated MCP servers disabled.

```sh
uv run fcsim audit list                        # the scenarios
uv run fcsim audit show blood-moon-beats-urborg   # exactly what the auditor is shown; no model call
uv run fcsim audit suite --tier weird --max-calls 20 --out report.json
uv run fcsim audit suite --model claude-sonnet-5 --only first-strike-ignored
```

The suite is how the auditor is measured. Each scenario sets up a board, plays a
short script, and knows the right answer: a planted error, or a legal play that
looks wrong. Misses and false alarms both count, because a false alarm stalls a
game as surely as a miss corrupts one. The ordinary tier covers summoning
sickness, land drops, timing, costs, commander tax, a missed trigger, state-based
actions, a replaced draw and hooks. The weird tier covers fizzling and partial
fizzling, combat-trick timing, a removed blocker, Humility with Opalescence,
Blood Moon with Urborg, what a blinked creature forgets, last known information,
split second, first strike, and trample with deathtouch. Every run has a hard
limit on calls. Claude-only runs also default to a limit on Claude Code's cost
estimate; on a subscription that estimate is plan usage at list prices, not a
charge. Codex reports tokens but no dollar estimate; see backend configuration below.

Measured on 2026-09-20 over all 41 scenarios: `claude-opus-5` passed 41 with no
false alarms, about 11 seconds and 4 cents (estimated) an audit; `claude-sonnet-5`
passed 39, missing one of two triggers on a busy board and a tapped land played
untapped, at half the time and a third of the usage.

## Goldfish

```sh
uv run fcsim goldfish examples/zuko.txt --turns 5 --max-calls 16
```

One agent pilots the deck against a seat, Goldfish, that never acts and is passed
for mechanically. The pilot is a fresh session in the selected backend, given
the table's tools over MCP with a token for its seat (`fcsim.mcp`,
`fcsim.tools`). Each pilot turn is one wake, and the auditor reviews it before the
game goes on, so its hooks are in the ledger before any later step runs. A game
directory under `games/` gets the configuration, the deck as played, card text,
the event log, a readable `log.md`, state, audits, the pilot's notes and
transcript, usage, and `report.md`: lands, spells and attacks by turn, the pilot's
own account of each turn, and rules health. Findings are recorded but not yet
corrected. A three-turn run took about four minutes and seven model calls.

## Pod

```sh
uv run fcsim pod examples/zuko.txt examples/talrand.txt --cycles 3 --max-calls 80
```

A multiplayer game: one agent per deck, each a fresh session in the selected backend with
only its own seat's tools. The scheduler wakes whichever seat the table is waiting
for and tells it what has happened since its last wake. A seat with priority on
someone else's turn and nothing on the stack is passed for without a wake. At every
checkpoint where something auditable happened, the auditor reviews it before the
game goes on: its hooks go into the ledger, a trigger it reports is put in front of
its controller, and a seat whose action it faults gets a correction wake to put
the table right. Players can talk with `say`. Every wake opens with a briefing (the
table with the seat's handles, its hand and the cards in play with their text, and
its notes), so an agent can act without first fetching any of it. When something is
on the stack, every seat still to act is asked at once, with one tool-less question,
whether it wants to respond; a no is passed for mechanically and only a yes gets a
wake. When a seat passes and nobody responds, its session is not ended: the call
returns the table as it now stands and the same wake carries on, so a turn is a few
sessions rather than twenty. `fcsim pod --resume <game_dir> --cycles N` plays N more
rounds of a saved game by replaying its journal, and `fcsim status <game_dir>` shows
where a game stands and where its time went, and `fcsim summarize <game_dir>` writes
`narrative.md`, the story of the game told with full information, round by round. `FINDINGS.md` logs what real games have
shown about the harness. Not built yet: disputes and a judge,
rewinds, reply wakes for table talk, narrower watches and triage, and the
postgame narrative. It has been tested with scripted seats only, not yet with
models.

## Model backends

The game logic, prompts and seat-bound MCP tools are shared across backends.
Select the CLI separately for players and the auditor:

```sh
# Codex pilots and audits, using its built-in default model.
uv run fcsim goldfish examples/zuko.txt --backend codex --turns 3 --max-calls 12

# Codex players, Claude auditor.
uv run fcsim pod examples/zuko.txt examples/talrand.txt --backend codex --audit-backend claude

# Measure the Codex auditor against the same rules scenarios.
uv run fcsim audit suite --backend codex --tier ordinary --max-calls 10
```

`--backend` defaults to `claude` and applies to every player in a pod.
`--audit-backend` inherits it unless supplied. `--model` and `--audit-model` are
independent: each defaults to its backend's model, not to the other role's choice.
Claude's default remains `claude-opus-5`; Codex uses its built-in default, ignoring
personal model settings. Pass explicit model IDs when comparing runs. Reasoning
effort is optional via `--effort` and `--audit-effort`; supported values depend on
the backend and model. Game configurations record the backend and requested model
for each role (`codex-default` when no model was specified).

Install and log in to the CLIs you select. Codex must support `exec` with
`--ignore-user-config`, `--ignore-rules`, `--ephemeral` and structured output;
tested with **0.155.1**. fcsim retains the CLI's authentication but skips personal
configuration, project instructions, skills, plugins and unrelated tools. Codex's
generic MCP discovery helpers may still appear; the server exposes no resources.
Shell, file, web, app and delegation tools are disabled so agents cannot read
game files or other seats' private state. Older incompatible CLIs fail with an
upgrade message. Tool isolation should be rechecked when upgrading Codex.

`--max-calls` bounds model attempts, including failures; each call also has a
timeout (30 minutes for players, 10 for auditors). For Claude-only runs,
`--max-cost` defaults to 10 USD for an audit suite, 15 for a goldfish, and 40 for
a pod. Any run containing Codex defaults to no dollar limit and rejects an
explicit finite `--max-cost`, because Codex cannot report or enforce it. Use
`--max-cost none` to disable it explicitly. This is a call limit, not a token or
spending cap within one call. Reports mark the overall dollar estimate as
unavailable when any call is unpriced, preserving the known subtotal separately.
Input token counts exclude cache reads/writes so mixed-backend totals remain
comparable; reasoning tokens are a subset of output tokens, not an extra charge.

New backends implement `Runner.ask(system, prompt, schema=..., tools=...)` in
`fcsim.agents` and join the factory there. Drivers hand them a neutral
`McpConnection`; adapter code owns CLI flags, tool configuration and output
parsing. Python callers can already pass a different runner for each pod seat.
No reusable client skill or outside-client seat workflow is implemented yet.

The optional CLI compatibility test uses the installed Codex with a **local mock
model endpoint**, calls the real MCP server, and verifies the offered tools,
structured output and accounting. It makes no paid model requests:

```sh
FCSIM_TEST_CODEX=1 uv run pytest tests/test_codex_cli.py
```

```sh
uv run pytest
uv build
```

Wheels include the reference files, so installed lookups also work outside this
checkout. `PLAN.md` describes the broader project.
