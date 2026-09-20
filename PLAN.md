# fcsim — playtest and tune Commander decks with LLM agents

Status: design, 2026-09-20, revised once after an outside review. Built so far:
the rules reference (`fcsim rules`), M0 (`fcsim cards`, `fcsim deck check`), M1
(the table, `fcsim.table`) and most of M2 (the auditor and its scenario suite,
`fcsim audit`); see README.md.

## 1. Goal

Build and tune Commander decks that make for good games: fun and interactive,
every player gets to shine, and most games finish within 4–10 turns. Power
sits around mid-to-high bracket 3, but the bracket is a rough guide to pace and
feel, not a rulebook to enforce. Raw power is not the target and cEDH is out of
scope.

Two halves serve that. The **table** gives a deck a realistic multiplayer game
without finding three humans: one agent per seat plays to win — sequencing,
threat assessment, politics and all — and each game leaves a directory with the
full log, a report and a readable narrative. The **workshop** (§12) reads those
games, evaluates both the deck and how it was played, and proposes changes to
the list for you to accept or reject.

Non-goals:

- **No rules engine.** The server never knows what a card does. Agents read
  oracle text and carry out the rules themselves, an auditor agent checks each
  action, and a judge agent settles disagreements.
- **No win-rate statistics.** A 4-player game is a few hundred LLM calls, so a
  tuning round gets a handful of games, not thousands. What pod games give is
  closer to "notes from a playtest night". The numbers that can be had in bulk
  for free — opening hands, land drops — come from shuffle simulation (§12.3).

## 2. Verdict on the original sketch

The shape is right: per-seat agents with private context, a real process for
randomness, agents rather than an engine enforcing rules, files for state,
sequential turn-taking, and a third-party judge. I kept all of that. Five
changes, each because the original version has a specific failure:

| Original idea | Change | Why |
| --- | --- | --- |
| Agents read and write shared state files | Agents touch state **only through tools**; the server writes the files | An agent with file access can read another seat's hand. With tools only, hidden information is enforced by construction rather than by asking nicely. The files still exist, for you to read and for debugging (§11). |
| Agents coordinate turn and priority order among themselves | The server owns the **turn skeleton**: whose turn, which step, who has priority, when the stack resolves | This is about 200 lines of bookkeeping with no card knowledge in it, and it removes the most likely way for four LLMs to deadlock or talk past each other. It is also the "lock": exactly one seat may change the table at a time. |
| Agents are long-running and wait for their turn | The driver **wakes** an agent only when it has a decision to make, and agent sessions are disposable | A Commander game is mostly waiting. Idle agents burn tokens and long-blocked tool calls time out. Short wakes also make crash recovery trivial. |
| Disputes between players are the only rules check | An **auditor** agent checks every action as it happens, and the judge becomes the appeal (§8) | Players are busy trying to win, an error in your own favor is easy to overlook, and four agents can all miss the same interaction. One wrong board state quietly spoils everything after it. |
| Opponent agent invents a decklist from a commander | Seed from an **EDHREC average deck**, falling back to an LLM deckbuilder with a validator; then every deck, opponents included, is tuned from its games (§12) | A real aggregate list is a better starting point than whatever the model recalls, and it cannot contain hallucinated or off-color cards. It is only a starting point. |

Terms used below: a **seat** is a player position driven by one agent. The
**table** is the server-side game state plus the mechanical operations on it (a
virtual tabletop). **The pen** is the right to change the table, held by one
seat at a time. A **wake** is one invocation of a seat's agent, with a reason. A
**watch** is a seat's standing request to be woken when some kind of event
happens. A **lint** is a non-binding warning attached to a logged action. The
**auditor** is a non-playing agent that checks each action against the rules,
and the **judge** is a second one that settles contested calls.

## 3. Architecture

```
                ┌───────────────────────── fcsim process ─────────────────────────┐
 decklists ───▶ │ deck pipeline ─▶ TABLE  state · zones · RNG · event log         │
 Scryfall       │                   │      turn skeleton · priority · lints       │
 EDHREC         │                   ▼                                             │
                │             tool registry   (roles: player, judge, host)        │
                │                   ▲                                             │
                │   SCHEDULER  next_wake(state) ─▶ one runner per seat            │
                └──────┬─────────────┬─────────────┬─────────────┬──────────┬─────┘
                       ▼             ▼             ▼             ▼          ▼
                    seat 0        seat 1        seat 2        seat 3    auditor · judge
                  own tools,       ...                                 public view only,
                  own notes                                            never play
```

One process holds the table. Each seat's agent gets a tool set bound to that
seat, so "who is calling" is never something an agent can claim — it is fixed by
which tool set it was handed. The scheduler is a pure function of table state:
given the state, it says which seat must act next and why. The same tool
registry is exported two ways: as tool definitions for the agents the driver
runs itself, and as a standalone HTTP MCP server so an outside client (a Claude
Code session, or you playing a seat from a chat window) can start games, watch
them, or sit down at the table (§7.4).

## 4. The table

### 4.1 State

```
Game    id, seed, config, turn{number, active, step, priority, passed[], land_plays},
        designations{monarch, initiative, day_night}, ledger[], rulings[]
Seat    life=40, counters{poison, energy, experience, …}, commander_damage{source→n},
        commander_casts{card→n}, status alive|eliminated|conceded, watches[]
Zones   per seat: library (ordered), hand, graveyard (ordered), exile, command
        shared:   battlefield, stack (ordered)
Object  id, card name | token definition, owner, controller, zone, tapped,
        face{down, transformed, flipped}, phased_out, counters{}, attached_to,
        annotations[{text, expires}], entered_turn, is_commander, known_by
Stack   id, kind spell|activated|triggered, source, controller, targets[],
  item  text as announced, x, modes, payment as announced
```

Three details carry the hidden-information design:

- **Internal ids never leave the server.** A seat refers to an object by a
  **handle** issued to that seat, and it holds a handle only for an object it
  can currently tell apart from the others. A seat's handles for an object are
  revoked whenever tracking it would stop being possible at a real table: it
  changes zones (rule 400.7), its library is shuffled, or it goes into a hand
  the seat cannot see. Two seats never share a handle, so handles cannot be
  compared across seats either.
- **A hidden zone is a count.** Another seat's hand is "5 cards" and a library
  is "62 cards", with no per-card entries to watch. Effects reach into them by
  position or through server operations ("top three", `discard_random`), and a
  reveal is what hands out handles.
- **`known_by` is per object**, either `all` or a list of seats, and decides who
  may be issued a handle and shown a face. Scry adds the seat for the cards it
  looked at; a shuffle clears it for the whole library. Face-down permanents,
  foretold cards and cards exiled face down are public objects with a hidden
  face, and use the same field.

`annotations` are free text for whatever the server cannot model: "gets +3/+3
until end of turn", "goaded by seat 2", "doesn't untap during its controller's
next untap step". An annotation with `expires: eot` is cleared at cleanup.

### 4.2 Randomness

The table owns a seeded RNG. Shuffle, random discard, coin flip, die roll and
"choose at random" are tools; agents never produce a random result. Every
random outcome is written into the event that caused it, so replaying the log
does not depend on the RNG being called in the same order. Drawing is just
"move the top card", which is why the library order must live where no agent
can see it.

### 4.3 Turn skeleton and the pen

The server steps through untap → upkeep → draw → main 1 → beginning of combat →
declare attackers → declare blockers → damage → end of combat → main 2 → end →
cleanup, and tracks priority as rule 117 has it. A player who casts a spell or
activates an ability keeps priority afterwards and must pass it explicitly.
After something resolves, the active player gets priority. Passes go round in
turn order; all-pass with a non-empty stack resolves the top item; all-pass
with an empty stack advances the step.

**The priority checkpoint.** Before any seat is given priority, in this order:
the audit of whatever just happened is in (§8.1); state-based actions it found
are applied, repeatedly until none remain; triggered abilities that are waiting
go on the stack in APNAP order. Only then does the seat receive priority. The
server enforces the order. It needs agents for the content.

**Mechanical steps are automatic unless held.** The server does these itself,
waking nobody:

- Untap step: untap the active seat's permanents.
- Draw step: the active seat draws (in multiplayer, including on turn 1).
- A resolving permanent spell moves to the battlefield under its controller.
- Cleanup: clear damage and `eot` annotations; wake the active seat only if its
  hand exceeds seven.
- Beginning of a step: nothing.

Any of them can be wrong for a particular board: a draw that is replaced, a
permanent that does not untap, one that enters tapped or as a copy, an upkeep
trigger. Finding out afterwards is too late for a draw, since a player cannot
unsee a card. So each mechanical step first consults the **hooks** in the
ledger (§8.2): entries the auditor wrote when the relevant card arrived, such
as "seat 2's draws are replaced (Notion Thief)" or "at seat 1's upkeep,
Phyrexian Arena triggers". If a hook matches, the server does not perform the
step. It wakes the seat concerned to carry it out, or to put the trigger on the
stack. If none matches, it proceeds, and the step is included in the next
audit. For a permanent spell the decision is made when it is cast: the audit of
the cast marks it plain, or marks it as needing its controller on resolution
(enters tapped, with counters, as a copy, with a choice). Unmarked means the
controller is woken.

Anything that bends the skeleton — extra turns, extra combats, skipped steps,
ending the turn — is an agent-declared action, logged and open to dispute like
any other.

The pen is held by the seat with priority, or the controller of the item that
is resolving, or a seat answering a request (blocks, a choice, a mulligan).
Mutating calls from any other seat are rejected. The pen holder may change any
*public* object, because effects routinely touch other players' things. Hidden
zones are different: cards leave another seat's hand or library only through
server operations (`draw`, `mill`, `discard_random`, `look`, `reveal`), each of
which requires a `because` naming the effect and is logged publicly — "seat 2
looked at seat 3's hand (Thoughtseize)". The server guarantees that every flow
of hidden information is declared in public; whether it was legitimate is for
the agents and the judge.

### 4.4 Lints

The server attaches a warning to an action when something cheap to detect looks
off. Warnings never block. They go into the public log and to the auditor (§8)
as hints about where to look, and the actor is asked to state a justification
when acting through one ("Exploration").

Initial set: a second land this turn; attacking or paying a tap cost with a
creature that arrived this turn; fewer tapped sources than the spell's mana
value; commander cast without the tax; life at or below zero, ten poison, or 21
commander damage with the player still alive; a draw from an empty library;
more than seven cards in hand after cleanup.

## 5. Tools

One log entry per game action, each with a plain-language `announce` string so
the log reads like a game.

| Group | Tools |
| --- | --- |
| Read | `get_view`, `get_log(since)`, `lookup_card(name)` (oracle text and Scryfall rulings), `lookup_rule(query)` (Comprehensive Rules search), `get_notes` |
| Flow | `pass_priority`, `set_watches`, `mulligan`, `keep(bottom[])`, `concede` |
| Actions | `play_land(card, enters)`, `cast(card, targets, x, modes, payment)`, `activate(source, ability, targets, payment)`, `add_trigger(source, text, targets)`, `resolve(mutations[])`, `finish_resolution`, `declare_attackers`, `declare_blockers`, `combat_damage`, `declare_loop(description, iterations, net_effect)`, `act(mutations[], because)` |
| Social | `say(text, to?)`, `ask_judge(question)`, `dispute(event, claim, remedy)`, `write_notes` |

`resolve` and `act` take a batch of **mutations**, the primitive vocabulary:
`move`, `tap`, `untap`, `counters`, `life`, `create_token`, `copy`, `attach`,
`annotate`, `set_controller`, `face`, `draw`, `mill`, `shuffle`, `look`,
`reveal`, `discard_random`, `roll`, `flip_coin`, `designate`,
`commander_damage`, `eliminate`, `extra_turn`, `request_choice`. A batch is
applied atomically and logged as one event. Anything that puts a permanent on
the battlefield takes `enters{tapped, counters, attached_to, choices}`. `act` is
the escape hatch for special actions, state-based actions and anything the
semantic tools do not cover.

**A resolution is state, not a single call.** "Draw two cards, then discard a
card" cannot be one batch, because the choice depends on what was drawn. When a
stack item starts to resolve, the table opens a resolution record: the item,
its controller, the batches applied so far, any pending request and who must
answer it, and a status of open, suspended or done. The controller applies as
many batches as the effect needs, seeing the result of each, and closes it with
`finish_resolution`. Nobody receives priority while a resolution is open. The
record is part of the saved state, so a crash in the middle of a resolution
resumes in the middle.

`request_choice(seats, prompt)` suspends the resolution, and `say` with an
addressee parks the conversation. Neither blocks: they end the caller's wake,
the scheduler wakes the other seats (in APNAP order for choices), then wakes
the caller again with the answers. This avoids tool calls that hang for minutes
while another agent thinks.

**Every mutating call is bound to a wake and is idempotent.** The runner
attaches two things the agent never sees: the token of the wake it belongs to,
and a call id (the model's tool-use id). A wake token dies when the wake ends
or is cancelled, and whenever the table changes underneath it — a correction, a
patch, a rewind — so a slow wake that finishes late cannot act on a board that
has since been corrected. A repeated call id returns the original result
instead of applying twice, so a retry after a timeout is safe.

Invalid calls return a structured error that says what was wrong and what to do
instead, so the agent corrects itself within the same wake.

The auditor role gets the read tools plus `verdict(findings[], ledger_ops[])`.
The judge role gets the read tools plus `get_dispute` and
`rule(decision, remedy)`. The host role (you, or a Claude Code session) gets
`new_game`, `status`, `spectate` (full information), `pause`, `abort`, `report`.

## 6. Scheduling

### 6.1 The loop

```
while not game.over:
    wake = next_wake(state)      # which seat, and why
    runners[wake.seat].run(wake) # agent acts until it gives up the pen
    ensure_progress(wake)        # nudge, then force a pass
```

`next_wake` checks, in order: an open dispute or correction, outstanding
requests (blocks, choices, replies), seats with a trigger the auditor reported
or a watch that fired (APNAP order), the controller of a resolving item, and
finally the seat with priority. A seat with priority and no reason to be woken
is passed automatically.

Each time the pen changes hands, what that seat just did, and any mechanical
steps since, go to the auditor (§8.1), and the priority checkpoint (§4.3) waits
for the verdict. To hide the wait, the next seats' wakes and triage may start
early against the unaudited board. They are speculation: if the verdict changes
the board or adds a trigger, their wake tokens die and they are run again.

A wake ends when the agent stops calling tools. If the pen has not moved, the
driver nudges once ("you still have priority: act or pass"), and after a second
failure passes for the seat and logs that it did.

| Wake | The agent is asked to |
| --- | --- |
| `mulligan` | keep or mulligan (London, first one free); all seats in parallel |
| `priority` | act or pass |
| `resolve` | carry out the effect of its spell or ability |
| `trigger` | put a triggered ability on the stack, after a hook or the auditor reported it |
| `held_step` | carry out a mechanical step that a hook held: a replaced draw, an untap with exceptions |
| `correction` | accept or contest an auditor finding against its action |
| `block` | declare blockers |
| `choice` | answer another seat's effect: sacrifice, discard, vote, pay or decline |
| `talk` | reply to table talk; no pen |
| `dispute_response` | concede or contest a dispute against its action |
| `postmortem` | reflect on the finished game |

Each wake prompt carries the reason, the log since that seat's last wake
(actions, lints, table talk, rulings), and a compact view: step, stack, life
totals, what changed on the battlefield, and the seat's hand with oracle text
for cards it has not been shown before.

### 6.2 Watches

There are two reasons to wake a seat that does not hold priority. One is rules:
its Rhystic Study just triggered. The server cannot know that by itself, so it
comes from the hooks the auditor wrote when the card arrived (§8.2), with each
audit as a backstop. The other is strategy: a seat holding a counterspell wants to see
every opposing spell, and a tapped-out seat does not. That is what watches are
for. A watch is a filter over logged event kinds: spell cast, ability
activated, object changed zones (with from, to and card types), attackers
declared, life changed, card drawn, step began.

Defaults: an opponent adds to the stack; I or something I control is targeted;
I am attacked; each step of my own turn; the end step before my turn. Agents
narrow these when they cannot act anyway ("tapped out with nothing free: wake
me only if targeted or attacked, until my untap") and widen them when they are
waiting for something. Watches are private, so changing them leaks nothing.
With auditing turned off, agents must also add watches for their own triggers.

Milestone 4 ships with the defaults only, which is correct but expensive.
Narrowing comes in milestone 8.

### 6.3 Triage

Most priority passes are automatic in a real game, and they are the dominant
cost here: every spell gives three opponents priority. Instead of a full wake,
each opponent first gets a single cheap request — the event, its untapped mana,
its hand, the question "respond, add a trigger, or pass?" — and all three run
**in parallel**. If all pass, the game moves on. If any wants to act, full wakes
run sequentially in APNAP order and the other triage answers are discarded,
since those seats get priority again anyway. Only the pass/act decision is
speculative; nothing is mutated in parallel.

## 7. Agents

### 7.1 Isolation

An agent's entire world is its seat-bound tool set. It has no file, shell or web
tools, no project settings, and no memory shared with other seats. The auditor
and the judge are separate agents with a read-only public view. The deckbuilder
never plays.

All seats get the same prompt and none is told which deck is under test.
Otherwise opponents either gang up on the test deck or go easy on it.

### 7.2 Sessions and memory

The table holds all game truth, so an agent can be rebuilt at any point from:
the static prefix (role, tool guide, its own decklist with oracle text, the
deck's primer from §12.4), its private notes, the current view, and the recent
log. That makes sessions
disposable. The default is **one session per seat per turn**: wakes inside a
turn share context, so a sequencing plan survives from main phase to combat;
at the turn boundary the session is dropped. `write_notes` is the durable
memory — threat assessments, deals made and broken, cards opponents have
revealed — and the last wake of each turn reminds the agent to update it.

This keeps context near 20k tokens for the whole game instead of growing past
100k, which matters because a seat is often idle for longer than the 5-minute
prompt-cache lifetime and would otherwise pay full price to re-read a long
history. The static prefix is cached with the 1-hour lifetime. Session scope is
configurable: `wake`, `turn` (default) or `game`.

### 7.3 Conduct

The player prompt asks for strong, self-interested play: evaluate threats,
negotiate, make and break deals, hold interaction for what matters. It draws
one line. Bluffing about hidden information and intentions is part of the game;
misrepresenting the public game state or the rules is cheating. As under
tournament rules, a player who notices an error must point it out, including
one in its own favor.

The goal of fun games does not soften this. Fun is designed into the decklists
(§12.1); at the table every pilot plays to win, as a good bracket-3 player does.

LLM agents tend toward politeness, and a pod that agrees not to attack each
other is a useless test. Whether the prompt is enough to prevent that is
something to measure in milestone 4, and optional per-seat personas
(aggressive, political, vengeful) are the first remedy to try.

### 7.4 Runtime

A small runner interface, with these implementations:

- `FakeRunner` and `ScriptedRunner` answer from a script, for tests with no model.
- `ClaudeCodeRunner` runs each call through the Claude Code command line in
  headless mode (`claude --print`), on whatever account the local `claude` is
  logged in with. **This is the one in use: you are on a subscription, not the
  API.** A call is a fresh session with `--tools ""`, `--strict-mcp-config`, no
  user or project settings and its own system prompt, so the model sees only
  what it is handed; `--json-schema` gives structured answers. Measured: about
  three seconds of overhead and under a thousand tokens of fixed context.
- `ApiRunner`, a plain Messages API tool loop, stays possible behind the same
  interface if you ever want per-token billing and finer control of caching.
- `ExternalRunner` parks the wake until an outside MCP client collects it, for
  a human seat or a Claude Code session sitting in.

Agents that need tools (players; the auditor when it wants to search the
Comprehensive Rules) get them as an MCP server passed with `--mcp-config`, with
every built-in tool still off. So MCP is how seats get their tools after all,
as well as the outside interface for starting and watching games.

Models are set per role. Default `claude-opus-5` throughout; §13 shows what
cheaper choices would save.

## 8. Rules integrity

Magic has more subtle interactions than four players concentrating on winning
will reliably catch, and one wrong board state quietly spoils everything after
it. Rules are therefore checked in three layers: every action is audited as it
happens, contested calls go to a judge, and the auditor itself is measured.

### 8.1 The auditor

A non-playing agent that checks each action against the public record. Its
scope is what a digital client enforces, and nothing more:

- **Was it legal?** Timing, costs and the mana announced to pay them, targets,
  summoning sickness, land drops, commander tax, once-per-turn limits.
- **Was it carried out correctly?** Did the resolution do what the oracle text
  says, including replacement effects, copy and control-change effects, and
  power and toughness that depend on layers.
- **What follows from it?** Which abilities now trigger, for any seat, and
  which state-based actions apply.

It never comments on strategy. It sees public information only, which is enough
because the server already guarantees hidden-zone integrity — nobody can cast a
card they do not hold — and it means findings can go in the public log without
leaking anything.

**Unit of audit.** Everything that changed the table since the last audit:
what one seat did between taking the pen and giving it up, plus any mechanical
steps the server performed. "Played a land, tapped three, cast Rhystic Study"
is one audit, not five. Passes, table talk and notes are not audited. That is
about one audit per wake that changed the table.

**Compiling cards into hooks.** When a card becomes relevant — it is cast, it
enters, it changes controller — the auditor reads it once, carefully, and
writes hooks into the ledger (§8.2) for everything about it the server will
need to know at a moment when no agent is looking. This is where card knowledge
gets turned into something a card-agnostic server can check mechanically, and
it is cheaper and more reliable than hoping each later audit notices the same
trigger again.

**Materials.** The public view before and after, the events, oracle text and
Scryfall rulings for every card involved, any lints, the ledger (§8.2), this
game's earlier rulings, and Comprehensive Rules search as a tool. Most audits
are a single request. The auditor takes a slower, tool-using look when it is
unsure: a stack more than one deep, a replacement or control-change effect, a
lint. The Scryfall rulings carry a lot of the weight, since most card-specific
traps are written down there.

**Verdict.** Either `ok` or a list of findings. Every finding must quote the
oracle text or cite the rule number it rests on.

| Finding | What happens |
| --- | --- |
| `illegal`, `misresolved` | Comes with a proposed fix: a rewind to the event, or a patch. The actor gets a `correction` wake and either accepts, and the server applies the fix, or contests, and it goes to the judge |
| `trigger` | "Blood Artist (seat 2) triggers." A backstop for anything the hooks missed. It goes on the stack at the priority checkpoint. Optional triggers are included, the way a client prompts for them |
| `sba` | A state-based action nobody applied. The patch is applied at the priority checkpoint, before anyone receives priority, unless a seat contests it |
| `note` | Low confidence. Logged; nothing blocks |

**Timing.** The verdict gates the priority checkpoint (§4.3): nobody receives
priority on an unaudited board. An audit takes seconds and a wake takes most of
a minute, so the next seats' wakes start speculatively while it runs. Most
verdicts are `ok` and the speculation stands. On a finding, or a new trigger,
those wakes are cancelled, their tokens die (§5), and the correction is handled
first.

**Stateless.** The auditor is rebuilt for every call from the table, so it has
no growing context. Its memory is the ledger.

`audit: handoff` is the default. `audit: off` exists for cheap casual runs.

### 8.2 The ledger

A public list, kept current by the auditor as part of each verdict and visible
to every seat. Each entry has a source and an expiry, and the server drops
entries when they expire or their source leaves. It holds two kinds of thing.

**Facts that live on no card.** Continuous effects with a duration ("creatures
seat 1 controls get +2/+2 until end of turn"), what has been used this turn,
choices made as a card entered ("Pithing Needle names Sol Ring").

**Hooks**, which the server matches mechanically, with no model call:

| Hook | Example | When it matches |
| --- | --- | --- |
| Hold on a mechanical step | "seat 2's draws are replaced (Notion Thief)"; "Mana Vault does not untap" | The server hands the step to the seat concerned instead of performing it |
| Step trigger | "at seat 1's upkeep: Phyrexian Arena"; a delayed "at the next end step, sacrifice the token" | The controller is woken as the step begins |
| Event trigger | "whenever a creature dies: Blood Artist, seat 2" — a filter over logged event kinds, like a watch | The trigger is queued for the next priority checkpoint |

Hooks cover public objects. A card that triggers from a hidden zone is its
owner's business, through a watch. A missed or wrong hook is an auditor error
like any other: the next audit is the backstop, and the regression suite (§8.4)
measures how often it happens.

### 8.3 Disputes and the judge

The judge is the appeal: contested corrections, and disputes a player raises
about something the auditor passed.

1. In any wake a seat may call `dispute(event, claim, proposed_remedy)`. The
   table freezes.
2. The seat that acted gets a `dispute_response` wake. If it concedes, the
   remedy is applied and play continues with no judge.
3. If it contests, a fresh judge agent receives both statements (or the
   auditor's finding and the actor's objection), the relevant stretch of log,
   the public view, the ledger, and oracle text and Scryfall rulings for the
   cards involved, with Comprehensive Rules search as a tool. It returns a
   decision, a remedy, and a short explanation.
4. The ruling goes into the public log and into `rulings[]`, which every later
   wake and audit includes, so the same interaction is not argued twice and an
   overruled auditor does not repeat itself.

Remedies are **rewind** to just before an event, or **patch** the state going
forward. The log is event-sourced, so a rewind is a replay to event N. After a
rewind the unknown part of every library is reshuffled, because the rewound
seats may have seen cards they would now draw again. The judge is told to
prefer a patch when hidden information has been revealed since the error or
more than a turn has passed, which is what human judges do. Because the
auditor catches most errors one handoff after they happen, rewinds are
normally short.

`ask_judge(question)` gets a private answer before acting. The log records only
that a question was asked.

Guards: three failed disputes or contests per seat per game, after which
further ones from that seat are declined. The judge's ruling is final.

### 8.4 Measuring the auditor

An LLM auditor reduces rules errors. It does not eliminate them, and if it runs
on the same model as the players it shares some of their blind spots. It has
two advantages over them — better materials and a single job — and beyond that
the only honest answer is to measure it.

The **rules regression suite** is a set of scripted scenarios, run through
`ScriptedRunner` with no players, each containing either a planted error or a
play that looks wrong and is legal. Ordinary interactions come first, because
they are what games are made of and where most errors will be: a replaced
draw, a land that enters tapped, a death trigger, a sacrifice choice, a
counterspell exchange with priority held, a summoning-sick creature tapped for
mana, commander tax, a board wipe with several simultaneous deaths. The exotic
cases come later: layers (Humility with Opalescence), replacement-effect
ordering, the commander zone-change replacement against "dies" triggers, copy
effects, split second. Each scenario also checks that the right hooks were
written. The suite reports catch rate, false-alarm rate, hook accuracy, cost
and time, per model and prompt. False alarms count as much as misses, since each one
costs a correction wake and possibly a judge. Every real error you later find
while reading a game log becomes a new scenario.

## 9. Politics

`say(text, to?)` is public table talk, logged for everyone. A message with an
addressee wakes that seat for a reply only. Limits: two exchanges per decision,
six talk wakes per turn. Deals are not binding, as in real Commander. There are
no private whispers by default; a physical table has none either, and a public
channel is much easier to read back afterwards.

## 10. Decks

**Parsing.** MTGO text format: `N Card Name` lines, with the commander in the
block after the blank line (or `Sideboard` header). Tolerate Moxfield and
Archidekt exports (`1x`, set codes, foil markers). `commander:` in the game
config overrides detection; partners, backgrounds and companions are handled
explicitly.

**Card data.** Scryfall's `oracle_cards` bulk file, cached locally and refreshed
weekly, indexed by normalized name including both halves of split and
double-faced cards. Agents never rely on recalled card text: every card they
can see comes with oracle text from this file. Rulings are fetched per card on
first use and cached.

**Validation.** 100 cards including commanders, singleton except basics and
"any number" cards, color identity, Commander legality, commander eligibility.
A failure is a warning for your deck, since you may be testing something
deliberately illegal, and a rejection for a generated one.

**Opponents**, by how much you specify:

| You give | The opponent deck starts from |
| --- | --- |
| A deck name | That deck's current version, from the gauntlet or your own decks |
| Full list | The list |
| Commander only | EDHREC's average deck for that commander; if unavailable, an LLM deckbuilder with Scryfall search, looped against the validator |
| Nothing | A deck sampled from the gauntlet; or, to grow the gauntlet, a commander sampled by EDHREC popularity — not yours, and preferably a different color identity from the other seats — then as above |

Whatever its origin, a deck that has played becomes a deck directory (§12.2)
with a version history, so a pod is reviewable, editable and reusable, and the
seed list is only version one. Power sits around mid-to-high bracket 3 as a
rough guide. The validator enforces legality only; what makes a deck right for
these pods is how its games score on the rubric (§12.1).

To verify in milestone 6: EDHREC's JSON endpoints are unofficial, so they need
aggressive caching and the fallback must work alone; and whether per-bracket
average decks are exposed there.

## 11. What a game produces

Every game gets its own directory, named to be browsable:
`games/2026-09-20_1405_<your-commander>_<short-id>/`.

```
config.json            seats, seed, models, options
decks/seat-N.txt       the exact lists played, generated ones included
cards.json             oracle text of every card in the game, as of that day
events.jsonl           authoritative log, full information
state.json             latest full snapshot (derived)
public.json            what everyone sees
seat-N.private.json    exactly what seat N sees
log.md                 play-by-play with table talk, lints and rulings
audits.jsonl           every auditor verdict, with what it was shown
disputes/NNN.json      claim, response, ruling
notes/seat-N.md        each agent's private notes
transcripts/seat-N.jsonl   agent reasoning, for "why did it do that"
usage.json             tokens and cost by seat and wake kind
report.md              metrics and post-mortems (below)
narrative.md           the story of the game (below)
```

These are the state files from the original sketch. Agents cannot read them;
you can, live, and `fcsim resume <game_dir>` rebuilds a crashed game from
`events.jsonl`. The directory is self-contained: decklists and card text are
copied in, so a game stays readable after errata and after you have edited the
deck, and every later tool takes nothing but the directory.

### 11.1 Narrative

`fcsim summarize <game_dir>` writes `narrative.md`, a readable account of the
game. It is a separate command that needs only the directory, and `fcsim play`
calls it when a game ends unless given `--no-summary`. So you get a narrative by
default, can skip it, and can regenerate it later with a different model or
`--style recap|story|analysis`. It also works on a game that hit the turn cap,
adjourned or crashed, as the story so far.

`log.md` is the public play-by-play as the table saw it. The narrative is
written afterwards with **full information** — every hand, every seat's notes
and reasoning — which is what makes it worth reading: "seat 3 sat on Cyclonic
Rift for three turns; its notes say it was waiting for seat 1 to overextend."
Contents: the pod and the result; the game told turn cycle by turn cycle; the
turning points; the politics, including deals made and broken; misplays visible
in hindsight; and a closing section on what the game says about your deck.

A long game is a couple of thousand events, too much to narrate well in one
pass. The summarizer first condenses each turn cycle into
`narrative/cycle-NN.md`, then writes the narrative from those. The per-cycle
files are kept, so regenerating in another style is cheap. The whole thing is a
few dozen requests against the several hundred a game costs.

### 11.2 Report

**Computed from the log:** result, elimination order and cause;
mulligans; lands and available mana by turn; missed land drops; turn each
commander was cast; cards cast against cards that sat in hand three or more
turns; the final hand; tutor targets; removal the deck absorbed.

**Written by agents:** each seat's post-mortem. From your deck's pilot:
what worked, which cards over- and underperformed, what it wanted and never
drew. From the opponents: when your deck looked threatening and what they held
back for it. Opponents' private notes are often the most useful part, since
they record how the deck reads from across the table.

`fcsim report games/*` aggregates across games: per-card times drawn, times
cast, turns stuck in hand, presence in wins against losses. The report states
the sample size next to every number.

Every report also scores the game against the table-quality rubric (§12.1):
length, participation, interaction, threat rotation, how it ended.

**Rules health.** Corrections accepted, findings contested and how the judge
ruled, auditor notes left unresolved, forced passes, and the cards involved in
each. A reader, human or reviewer, should know how far to trust a game before
drawing conclusions from it.

## 12. Improving decks

Playtesting is the means. The end is decks that produce good games, for you and
for the other three seats, at mid-to-high bracket 3. The EDHREC lists in §10
are seeds. Every deck, yours and the opponents', then evolves from the games it
plays.

### 12.1 What "better" means

Asked to improve a deck, an agent left to itself adds power: out go the pet
cards, in come tutors and fast mana. For the games you want, that is the wrong
direction as often as the right one. Two things pin "better" down: the deck's
brief, and a rubric for what a good game looks like.

**The brief** (`brief.md`, one per deck, written by you or drafted by an agent
for you to edit). What the deck is trying to do and what a good game with it
looks like; the power target; locked cards that stay however they perform;
excluded cards; a budget or a "cards I own" list. Every proposed change is
argued against the brief, never against win rate alone.

**Brackets are a guide, not a constraint.** Nothing is rejected for breaking a
bracket-3 guideline. `fcsim deck check` reports the usual signals for
information — Game Changers, fast mana, tutors, known combos, mass land denial,
extra turns — because they often explain why a deck's games score badly on the
rubric. The rubric is what decides.

**The table-quality rubric** (`rubric.md`, shared across decks, yours to edit),
scored for every game. Part is computed from the log:

| Measure | Good | Bad |
| --- | --- | --- |
| Length | 4–10 turns per player; judged over a deck's games, where most should land in the band | a deck whose games are routinely over before turn 4, or drag past 12 |
| Spotlight | every seat has a stretch where its deck does its thing and the table has to deal with it | a seat that never mattered; one seat holding the spotlight all game |
| Participation | every seat making plays most turns | a seat with many do-nothing turns: screwed, flooded or locked out |
| Sitting out | eliminations close to the end | a seat eliminated many turns before the game ends |
| Interaction | removal, counterspells, blocks and tricks every cycle | four games of solitaire |
| Threat rotation | the seat the table worries about changes | one archenemy all game, or nobody ever threatening |
| Turn weight | turns of similar length | one seat repeatedly taking turns many times longer than anyone else's |
| Ending | a win the table saw coming and could have answered | a win from nowhere; a hard lock and concessions |

Part is judged by the reviewer, quoting the log as evidence:

- Did every player get to shine: did each deck do its thing, as its brief
  describes it, and did it matter to the game?
- Were the turning points decisions, or draws?
- Salt: moments that would feel bad at a real table, such as repeated resource
  denial aimed at one player, a stolen commander, or a long loop with no
  certain outcome.
- Was there a story: swings, a comeback, a deal betrayed?

Your own reaction outranks all of it. After reading a narrative, drop a
`feedback.md` into the game directory ("miserable: seat 2 did nothing but wipe
the board") and the reviewer treats it as the strongest evidence it has.

In play nothing changes: agents play to win. Fun is designed into the lists. A
pilot that pulled punches to keep the game pleasant would make the test
meaningless.

### 12.2 A deck is a directory

```
decks/<name>/
  brief.md            what the deck is for, its limits, its locked cards
  primer.md           how to pilot it; goes into the pilot's prompt
  current.txt         the list, MTGO format
  versions/v001.txt   every version ever played
  CHANGELOG.md        per version: swaps, reasons, evidence, who approved
  track-record.json   per-card history across all games and versions
  reviews/            one per review
  proposals/          pending changes
```

Each game directory records the deck name and version in every seat.

### 12.3 Evidence, cheapest first

A pod game is expensive and four of them are a small sample. The tuner uses the
cheapest evidence that can answer the question, and every proposal says which
kind it rests on.

| Tier | Cost | Answers |
| --- | --- | --- |
| Static analysis | free | curve; land and color-source counts against the pips; counts of ramp, draw, removal and wipes; bracket signals, for information: Game Changers, fast mana, tutor density, known combos |
| Shuffle simulation | free, thousands of trials | keepable opening hands, land drops by turn, colors available by turn. Real statistics, but only as good as two inputs: what each source actually produces (Scryfall's `produced_mana`, plus a hand-kept table for fetches, conditional and tapped lands, and modal cards), and a stated mulligan policy. Results are reported as conditional on that policy |
| Solo goldfish | one agent | how fast and how reliably the gameplan comes together unopposed. Also a pace check against the rest of the gauntlet: a deck that goldfishes a win several turns before the others will make short, one-sided games |
| Pod games | full price (§13) | interaction, politics, how the deck reads from across the table, table quality |

Per-card history accumulates in `track-record.json` across versions — drawn,
cast, turns stuck in hand, present in wins and in losses, named in
post-mortems — so evidence about a card builds up instead of starting again
each round.

### 12.4 The loop

**`fcsim review <deck> [game_dirs…]`.** A reviewer agent with full information
and hindsight reads each game's report, its per-cycle narrative, the pilot's
post-mortem, what the opponents' notes say about this deck, and any
`feedback.md`. Its review separates four causes: the **deck** (structure, dead
cards, a gameplan that does not come together), the **pilot** (misplays, missed
lines, poor threat assessment), the **pod** (a bad matchup) and **variance**.
Evaluating the play matters because a misplayed deck looks like a bad deck.
The same goes for a misruled one: the review reads each game's rules-health
section (§11.2) and discounts evidence about any card caught up in a
correction, a judge ruling or an unresolved auditor note, rather than
recommending a cut because of an interaction the table got wrong.
Pilot findings go into `primer.md` — what to mulligan, the key lines, what to
hold and for what — and the pilot gets the primer in its prompt from then on.
The deck is played better over time, and the primer is worth reading yourself.

**`fcsim propose <deck>`.** A few swaps, five at most by default, so the deck
keeps its identity and a change in results can be traced to a cause. Each swap
gives the cut, the addition, the reason, the evidence (tier, game, turn) and
the expected effect on the rubric. Swaps are validated against legality, color
identity and the brief. The tuner reads the changelog, so it
does not bring back what was cut two versions ago without saying why.
Proposals may lower power: if a deck dominates its pods or produces non-games,
cutting fast mana or a tutor is an improvement.

**`fcsim apply <deck>`.** A proposal is a markdown file with a checkbox per
swap. You tick, edit or reject; `apply` writes the next version and the
changelog entry. For your decks this step is always yours. For gauntlet decks,
`--auto` applies without asking.

**`fcsim tune <deck> --games 4 --rounds 3`** chains play, review, propose and
apply, stopping at each proposal unless `--auto`.

**`fcsim compare <deck> v3 v4`** answers "did that change help?", which needs
everything else held still. A comparison pins the opponents' deck versions,
every primer including this deck's (both sides play with the newer one, so the
list change is judged on its own), the models and prompts, and the pods. It
rotates seat order across games, since going first matters, and reuses the
same shuffle seeds for the opponents on both sides. Only one deck is tuned at a
time; the rest of the gauntlet is frozen for the duration. Even so, a handful
of games is weak evidence, and `compare` says so next to every number.

### 12.5 The gauntlet

Opponents are first-class decks in the same format: seeded from an EDHREC
average or a list you supply, given an agent-drafted brief, then tuned by the
same loop with automatic approval. The aim is eight to ten decks across
archetypes — tokens, voltron, aristocrats, spellslinger, control, big mana,
graveyard, group hug — so that pods vary. A pod is sampled from the gauntlet
and any of your own decks.

Three guards against an arms race. First, a **reference set**: three or four
decks that are never tuned, kept as a fixed yardstick. Balanced win rates
inside the gauntlet prove nothing if every deck got stronger together, but
drift shows up against decks that did not move: every gauntlet deck's goldfish
speed, and its results against the reference pod, are tracked over versions.
Second, a power band within the gauntlet: over enough games each deck in a
four-player pod should win about a quarter of the time, and a deck above 40% or
below 10% is flagged. Third, when tuning one deck the tuner sees the others
only as they appeared in the games, never their full lists, so it cannot build
against the gauntlet.

## 13. Cost and time

Rough, and to be replaced with measurements from milestones 2 and 3. On a
subscription these are estimates at list prices of the plan usage consumed, not
charges. The figures
assume a game of about 35 turns. At the 4–10 cycle lengths the rubric aims for,
expect about two-thirds of them.

A 4-player game of about 35 turns comes to roughly 300–600 wakes: about four
for the active seat each turn, plus up to three opponents for every spell and
combat. With one-session-per-turn context, a wake is about four requests over
an 18k-token context — roughly 68k tokens read from cache, 7k written, 2k
output.

| Model | Per wake | 400-wake game | With triage (≈250 wakes + 450 triage calls) |
| --- | --- | --- | --- |
| `claude-opus-5` | ≈ $0.13 | ≈ $50 | ≈ $40 |
| `claude-sonnet-5` | ≈ $0.05 | ≈ $21 | ≈ $16 |
| `claude-haiku-4-5` | ≈ $0.03 | ≈ $10 | ≈ $8 |

**Auditing** adds about one request per wake that changed the table, roughly
400 a game. Each reads a cached 8k-token prefix plus about 7k tokens of fresh
board, events and card text, and writes about 1k. One audit in ten takes the
slower look at about three times the price.

| Auditor model | Per audit | Per game |
| --- | --- | --- |
| `claude-opus-5` | ≈ $0.06 | ≈ $30 |
| `claude-sonnet-5` | ≈ $0.03 | ≈ $12 |

So auditing adds a bit over half to the cost of a game. It is the last place to
save on model strength: a weak player makes a game less realistic, while a weak
auditor makes it wrong. The regression suite (§8.4) is what tells you whether a
cheaper auditor is good enough.

Wall clock is the other constraint. At 30–60 seconds a wake, a sequential game
takes two to four hours. Parallel triage and parallel mulligans help within a
game, and audits overlap the next wake rather than adding to it; beyond that,
run several games at once as separate processes.

**Tuning** multiplies all of this. One round of `fcsim tune` with four audited
pod games is about $300 on `claude-opus-5` or $120 on `claude-sonnet-5`; the
review and proposal themselves are a rounding error next to the games. That is
why §12.3 puts the free and cheap evidence first: static analysis and shuffle
simulation cost nothing, a solo goldfish is about a tenth of a pod game, and
pod games are kept for what only they can show.

Levers, in the order I would reach for them: watches narrowed by the agents,
triage, a turn cap, a cheaper model for triage and talk wakes only, a cheaper
model for opponents than for the deck under test.

## 14. Guards

- **Length.** A cap of 14 turn cycles, after which the judge assesses standings.
- **Budget.** Hard limits from the first prototype that calls a model, not
  added later: per-wake tool-call and time limits, and a per-game and per-run
  dollar ceiling, on which the game adjourns with state saved and can be
  resumed. `fcsim tune` refuses to start without a ceiling.
- **Loops.** `declare_loop` is the rules' shortcut mechanism: state the loop,
  the iteration count and the net result; opponents get priority to respond or
  dispute.
- **Stalls.** The scheduler always names a seat or declares the game over, and
  asserts as much. A seat that will not act is nudged, then passed for. A wake
  that ends in a refusal or a truncated response counts as a stall.
- **Crashes.** The event log is authoritative and sessions are disposable, so
  resuming is a replay plus fresh agents reading their notes.
- **Elimination.** When a seat leaves, the server removes everything it owns
  and the remaining agents handle control effects ending.

## 15. Stack and layout

Python 3.12 with `uv`, and so far the standard library only: state and events
are dataclasses, matching the rules reference that was already in the
repository. Tool schemas will be written once and exported both as MCP tools
and as API tool definitions. `anthropic` for the default runner, the official `mcp` package for
the HTTP server, `claude-agent-sdk` only if `SdkRunner` is wanted, `httpx` for
Scryfall and EDHREC, `pytest` plus property tests.

```
src/fcsim/
  cards/    scryfall.py  decklist.py  validate.py  edhrec.py  deckgen.py
  table/    model.py  events.py  mutations.py  visibility.py  turn.py  lints.py  replay.py
  tools/    registry.py  player.py  judge.py  host.py
  sched/    scheduler.py  wakes.py  watches.py  triage.py  audit.py
  agents/   runner.py  scripted.py  api_runner.py  sdk_runner.py  external.py  prompts/
  mcp/      host.py (start, status, report)  http_server.py  inproc.py
  report/   metrics.py  quality.py  render.py  aggregate.py  narrative.py
  workshop/ static.py  shuffle.py  review.py  propose.py  apply.py  track_record.py
  cli.py    fcsim deck check|gen · play · resume · summarize · report · serve
            fcsim review · propose · apply · tune · compare
decks/<name>/ (§12.2)  rubric.md  data/ (cache, gitignored)
games/  tests/  tests/rules/ (regression scenarios)
```

The test that matters most is a non-interference property test. Take a random
game state and a seat, build a second state that differs only in what is hidden
from that seat (swap two unknown cards in an opponent's hand, permute the
unknown part of a library), apply the same random sequence of valid calls to
both, and assert that everything the seat can observe is byte-identical: views,
handles, logs, tool results, error messages, wake prompts. Checking only that
hidden card names never appear would miss leaks through identifiers, ordering
and error text.

## 16. Milestones

| | Deliverable | Proves |
| --- | --- | --- |
| M0 | Scryfall cache, decklist parser, validator, static analysis with bracket signals, shuffle simulation: `fcsim deck check` | Your lists load cleanly, and you already get a free report on mana, curve and power signals |
| M1 | Table: state, handles and visibility, mutations, RNG, event log and replay, turn skeleton and priority checkpoint, resolutions, call contract, lints; `ScriptedRunner`; the non-interference test | Hidden information holds and the protocol is sound; no model needed yet |
| M2 | Auditor with hooks and ledger, run against a **small rules suite of ordinary interactions**; hard spending limits | The riskiest assumption, tested first and cheaply: catch rate, false alarms, hook accuracy, cost and time per audit |
| M3 | Tool registry as an MCP server, `ClaudeCodeRunner` seats, game directory, report. **An audited ten-turn solo goldfish of one supplied deck**, with measured cost. A minimal MCP host interface: `start`, `status`, `report` | Tool ergonomics, prompts, real cost per wake and per audit. The first useful artifact, usable from Claude Code |
| M4 | Scheduler, all wake kinds, default watches, combat, choices, corrections, disputes and judge, elimination, table talk. **One fixed pod of supplied lists**, audited; one run with `audit: off` for comparison | The core loop; whether agents play to win; how much the auditor is catching |
| M5 | Metrics, table-quality rubric, rules health, post-mortems, `fcsim summarize` narrative, `--games N --parallel K`, aggregate report | Games you can read, compare, and know how far to trust |
| M6 | EDHREC seeds, random commanders, LLM deckbuilder fallback, deck directories, reference set | Pods from partial input |
| M7 | Workshop: briefs, primers, track record, `review`, `propose`, `apply`, `tune`, `compare`; first gauntlet decks | The point of the project: decks get better from their games |
| M8 | Narrowed watches, triage, model tiers; the wider rules suite | Affordable enough to tune routinely |
| M9 | Full HTTP MCP server, external seats, human seat, simple board viewer | Play a seat yourself |

## 17. Open questions

Each has a default I will proceed with unless you say otherwise.

1. **How agents are billed.** Settled: your Claude subscription, through
   headless Claude Code (§7.4). The dollar figures in §13 are then estimates at
   list prices of how much plan usage a run consumes, not a bill, and every run
   has a hard limit on calls and on that estimate.
2. **Models.** Default `claude-opus-5` for every role. A cheaper model for
   opponents or for triage is your call, given §13.
3. **Budget and volume.** How many games per tuning round, and what is a game
   worth? This decides whether the cost work in M8 moves ahead of M5–M7.
4. **The rubric.** §12.1 now has your priorities: fun and interactive, every
   player gets to shine, most games within 4–10 turns. The remaining measures
   are my guesses. Default: as written, revised after you have read a few
   narratives.
5. **Card pool.** Should proposals be limited to cards you own, or to a price
   per card? Default: no limit unless the deck's brief sets one.
6. **Approval.** Changes to your decks always wait for you; gauntlet decks tune
   themselves. Say if you want to approve those too.
7. **Table talk.** Public only by default.
8. **A human seat.** If you want to pilot your own deck against three agents,
   M9 moves up.
9. **Language.** Python, because both agent SDKs and the MCP SDK are
   first-class there.
10. **Forgotten optional triggers.** By default the auditor prompts for every
    trigger, including "may" abilities, as MTGO does, on the view that a
    forgotten trigger says something about the pilot and nothing about the
    deck. If you would rather a missed "may" stayed missed, it is a flag.

## 18. Alternative considered: an existing engine

Forge and XMage implement nearly every card, and Forge can run AI-against-AI
Commander games headlessly. Wrapping one would make rules errors impossible,
where the auditor (§8) only makes them rarer.
I did not choose it because the point here is the play — politics, threat
assessment, sequencing — and Forge's player interface is well over a hundred
callbacks that an LLM adapter would need to answer, with no natural place for
table talk. It is still worth knowing about for the one thing this design
cannot afford: played-out games in the thousands, for instance a win rate with
a real confidence interval. Shuffle simulation (§12.3) covers the statistics
that need no play.
