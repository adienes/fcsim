# Playtest findings

What real games have shown about the harness: its speed, its flow, and the quality of play and
rulings. Each item says what was seen, the evidence, and where it stands. `fcsim status <game_dir>`
produces the numbers quoted here.

Games so far: a 3-turn Talrand goldfish, a 5-turn Zuko goldfish, and two four-player pods of the
same decks (Zuko, Lathril, Miirym, Wilhelt; EDHREC average lists for the last three), both started
2026-09-20. The first was resumed three times as the driver changed under it; the second ran on
the driver as it stood after every item marked "built, not yet run with models" below.

## Speed

| # | Finding | Evidence | Status |
| --- | --- | --- | --- |
| S1 | Most wakes decided nothing. A seat was woken to pass on every spell and in its own upkeep and end step. | First three rounds: 59 of 98 wakes only looked and passed, 48% of model time | **Fixed**: parallel tool-less triage; own upkeep/draw passed for; `until "cleanup"`; certain no-ops skipped |
| S2 | Agents spent round trips fetching context. | 55% of tool calls were `get_view`, `get_notes`, `lookup_card`, about 5 s each | **Fixed**: every wake opens with a briefing (board with handles, hand and cards in play with text, notes) |
| S3 | A fresh session per wake costs about 9 s of start-up, and a busy turn has many wakes. | Turn 17: 20 wakes; turn 19 (Miirym's dragon chain): 39 wakes, 900 s of model time | **Built, not yet run with models**: when the pen leaves a seat, the driver first does everything that needs no other full wake (mechanical passes, the parallel respond-or-pass round). If the pen comes straight back, the call returns `carry_on` with the table as it now stands and the same session continues. In the scripted test a whole turn, commander cast included, is one wake |
| S4 | Auditing at every mid-turn checkpoint stopped the game constantly. | 22 audits in 12 turns | **Fixed**: one audit per turn by default (`--audit-every checkpoint` for the strict mode) |
| S5 | Audit time grows with the record. | 128 s for turn 17, 104 s for the 218-event turn 19 | Partly fixed: runs of passes are one line, notes left out. **Open**: summarise long trigger chains; audit concurrently with the next turn's first wake |
| S6 | Nobody ever responds, yet everyone is asked. | 148 respond-or-pass questions in the game, 0 answered "respond" | **Built, not yet run with models**: `set_response_policy` (`ask`, `pass_unless_targeted`, `pass_always`), private and reset at the seat's own turn; the driver only asks seats whose policy wants asking. The prompt tells players to set it when tapped out. It also showed that a viewer's log listed the tool name of events with no public text, which leaked that a policy had been set: fixed |
| S7 | Trigger chains ask the table once per trigger. | Turn 19: each Lozhan, Ganax and Miirym trigger got its own triage round and its own resolution wake | **Built, not yet run with models**: the respond-or-pass question lists the whole stack, marks what is new since that seat was last asked, and takes `ask_again`: false means "I will let everything now on the stack resolve", and the seat is asked again only when something new is added. In the scripted test a commander with a trigger above it costs two questions instead of four. The same answer can carry `policy`, which sets the seat's standing response policy without a wake (S6). S3 removes the resolution wakes |
| S8 | Agents make one more call after their wake has ended. | 31 `stale_wake` rejections: 19 were `write_notes`, 5 `get_view`, 5 `add_trigger`, 1 `cast`, 1 `act` | **Fixed**: the rejections were mostly lost work, not wasted work. A player's last act is to save its notes, and the wake had often ended by then, so its only memory was thrown away. Notes, the response policy and looking are now accepted on an ended wake, since none of them touches the game. Session continuity (S3) removes most of the rest: the wake no longer ends when the pen comes straight back |
| S10 | Opening hands are decided one seat after another. | Four sequential wakes of 30 to 60 s before turn 1, though the decisions are independent | **Built, not yet run with models**: all seats are asked at once, with one question and no tools: keep or mulligan, and which handles go on the bottom. Seats that mulligan are asked again together. An answer the table refuses falls back to an ordinary wake, and if that fails too the driver keeps for the seat, so a game always starts |
| S11 | A turn that is only a basic land still costs an audit. | Second pod: about 12 s on each of the quiet early turns | **Fixed**: a stretch with nothing but talk, passes and a basic land the table accepted without a warning is not sent to the auditor. Any other land is, since it may enter tapped or trigger |
| S12 | The machine went to sleep under a running game. | Second pod: turns 13 to 24 took 25 minutes of model time and almost two hours of wall clock. `pmset -g log` shows a laptop on battery cycling through maintenance sleep; calls are timed with a monotonic clock, which stops during sleep, so `status` showed nothing wrong | **Fixed**: on macOS the long-running commands hold a `caffeinate -i` assertion for as long as they live (`FCSIM_ALLOW_SLEEP=1` turns it off). A closed lid still sleeps |
| S9 | Opus at full effort deliberates over passing. | Players now run at medium effort, triage at low | Done for this game; per-role effort is a command-line option |

Measured effect so far: about five minutes a table turn on the first driver; quiet turns now take
one to three minutes. Busy turns are still six to fifteen, which is what S3 and S7 are for.

## Rules quality

| # | Finding | Evidence | Status |
| --- | --- | --- | --- |
| R1 | The auditor faulted legal mulligans, and both players complied and lost a card. | Turn 1: two `illegal` findings against the free multiplayer mulligan | **Fixed**: what the table enforces (mulligans, turn order, card conservation) never reaches the auditor |
| R2 | Players defer to the auditor even when it is wrong. | Neither disputed R1, though the prompt allowed it | **Built; the judge checked with a model, the flow with scripted seats**: a seat asked to correct the table can `contest` instead, and nobody holds the pen until a judge rules: uphold (back to the seat, no second appeal), overrule (nothing changes) or amend (the judge's own fix). The judge gets both sides, the stretch of play, the board, the cards with rulings, and the text of every rule number either side cited. A ruling is kept in the game and shown in every later briefing and audit. Any player can `dispute` another's play; the auditor must answer it in public at its next audit. Three lost contests or disputes and a seat may raise no more. Replaying the first game's mulligan false alarm, the judge overruled in 16 s and pointed out that the rule the auditor quoted (103.4) is about life totals; given the correct Lord of the Undead finding and a weak objection, it upheld in 37 s with exact repair steps. Not built: rewinds as a remedy, `ask_judge`, an appeal when the auditor rejects a dispute |
| R3 | A fresh session repeats what it just did. | Wilhelt's end-step trigger put on the stack and resolved five times in one step | **Fixed**: the briefing opens with "what you have already done this turn" |
| R4 | Each player re-derives the board, and gets it wrong. | Combat resolved with Wilhelt's printed 3/3 while Lord of the Undead made it 4/4 | **Fixed**: the auditor writes continuous effects and current power/toughness onto the board. They were only as fresh as the last audit, so **built, not yet run with models**: before a player who could block is asked to, the driver compares the battlefield (permanents, controllers, counters, notes, attachments, continuous effects) with what it was when the numbers were last worked out. If it differs, the auditor gets a short characteristics-only question (the board, this turn's log, the cards in play; layer order spelled out) and the stamps are replaced. No call when nobody can block or nothing has changed |
| R5 | Missed triggers are the commonest error by far. | 11 of 16 findings: Norin, Isochron Scepter, Wilhelt, Ganax, Miirym's copy trigger | **Built, not yet run with models**: the auditor gives an event trigger a small structured `on` (event: cast/enters/dies/leaves/attacks; who; a type-line word; nontoken; other). The table records what happens as facts, matches them, and puts the trigger in front of its controller before anyone receives priority. A loose match is declined by the player. A trigger put on the stack by hand consumes its queued twin. Hooks exist from the turn after a permanent arrives, since audits are per turn |
| R6 | State-based losses were applied a few passes late. | Zuko and Wilhelt sat at -1 life while priority was passed | **Built, not yet run with models**: before anyone gets priority the driver removes a player at 0 life, 10 poison, 21 commander damage or a draw from an empty library, as a logged correction. A `hold` hook with step `loss` (Platinum Angel) stops it |
| R7 | A pilot repeated commander damage the table had already counted, and could not undo it. | Zuko goldfish: 16 recorded, 8 dealt | **Fixed**: tool text says the table counts it; `commander_damage` takes `set` |
| R9 | The driver itself led a player into an illegal play. | Second pod, turn 1: the last player to keep was told by `carry_on` that it had priority, in its own upkeep, and played a land there. The auditor caught it and the land went back, at the cost of that turn's land drop | **Fixed**: the driver passes through a quiet upkeep, draw step or another player's empty-stack priority for the waking seat as it does for everyone else; and the table now refuses a land play outside one's own main phase with an empty stack (rule 305.1), since it knows the turn. Journals written before the check still replay: a replay never refuses what was once accepted |
| R10 | A slip of a handle cast another player's card. | Second pod, turn 20: Miirym's pilot cast Laelia from Zuko's exile by mistake and undid it by hand | **Fixed**: casting from anywhere but one's own hand or command zone is refused until the player names, in `because`, the effect that allows it; the log then shows the claim for the auditor to check. A real permission (flashback, a card exiled "you may play it") costs one extra field |
| R8 | The auditor judged the big turn correctly. | Turn 19: cost reductions and 56 damage accepted; only Miirym's own missed triggers flagged | Good sign; the suite should gain a long-chain scenario |

## Flow and play

| # | Finding | Evidence | Status |
| --- | --- | --- | --- |
| F1 | "Table turn 9" reads as turn nine; it was each player's third. | User confusion mid-game | **Fixed**: seats count their own turns; prompts, the board text, reports and `status` say "round 5, Miirym's turn (table turn 19)" |
| F2 | No way to see a running game except reading files. | Asked twice "still going?" | **Fixed**: `fcsim status <game_dir>` |
| F3 | A game could not be continued. | First run stopped at its round cap | **Fixed**: `fcsim pod --resume` replays the journal; verified on 506 real events |
| F4 | News was capped at 40 lines, mostly passes, so real plays fell out of a briefing. | Found by a test after the upkeep wake was removed | **Fixed**: passes are left out of news |
| F5 | Politics appears by itself once there are threats. | No talk in rounds 1-2; by round 3 all four seats argued about who the threat was, and Zuko changed its removal target | Good. **Open**: reply wakes, so a direct question gets an answer before the decision |
| F6 | The table read the threat correctly and still lost to it. | All three opponents named Miirym's engine in round 3; it killed two of them on turn 5 | A deck-balance result, not a harness one: see below |
| F7 | Round caps count eliminated seats. | `--cycles` multiplies by the number of seats at the start | **Fixed**: a game is capped in rounds; on a resume the turn in progress is the first of the new rounds |
| F9 | Nobody can act at instant speed on an empty stack in another player's turn. | The driver passes for every seat there (S1), so "at the end of your turn, I cast" cannot happen: flash creatures and instant-speed draw are weaker than they should be | Worse than first noted: the same pass meant nobody could kill an attacker, or play a trick after blocks, because the stack is empty at both moments. The first pod's rubric shows it: 0 plays at instant speed all game. **Built, not yet run with models**: when attackers have been declared, and again when blocks have, every seat that could act (cards in hand or something untapped, and no standing "pass always") is asked at once, with no tools, whether it acts; those that say yes are woken with priority. The end step before one's own turn is opt-in: `set_response_policy` takes `hold_for_end_step`, private and reset on one's own turn. Other empty-stack moments are still passed |
| F8 | No narrative or rubric score at the end. | Only `report.md` standings and each player's own account | **Partly built**: `fcsim summarize <game_dir>` condenses each round with full information (the log, private draws, each player's own account, audit findings), then writes one file, `narrative.md`: the story, with a closing section that judges the game against the group's rubric, followed by the round accounts it was written from. Finished rounds are read back from that file, so another style costs two calls. Building it showed that the turn's own draw was recorded nowhere: every draw is now told privately to its seat. **Rubric scores built**: `fcsim rubric <game_dir>` replays the journal and scores length, participation, sitting out, interaction, threat rotation, turn weight and the ending, each with its threshold stated, plus a row per seat. The scores go into `report.md` and `rubric.json` at the end of a game and into the narrative's prompt. On the first pod they say what a reader said: Lathril idle on 3 of 4 turns, a 50-action turn against a typical 4, and three players killed in one turn from 27, 37 and 28 life. Spotlight and salt stay with the reader |

## Second pod: what the new driver did

Three rounds (12 table turns) of the same four decks, every player at medium effort:

| | First pod, rounds 1-3 | Second pod, rounds 1-3 |
| --- | --- | --- |
| Player wakes | 98 | 16 (one a turn, and four to keep hands) |
| Model time in wakes | about 50 min | 5 min 51 s (22 s a wake) |
| Wakes that only looked and passed | 59 | 0 |
| Respond-or-pass questions | 148 over the game, none answered yes | 5: players set a standing policy 8 times |
| Audits | 22, one per checkpoint | 12, one per turn, 14 s each |
| Wall time per table turn | about 5 min | about 45 s |

The session-continuity change (S3) did what it was for: a turn is one session. It also caused the
one rules error of the three rounds (R9), now fixed.

## What the pod says about the decks

EDHREC's average Miirym list killed two players from 27 and 37 life in one main phase on its fifth
turn: Temur Battlecrier's discount, Earthquake Dragon for {G}, and two copies of Lozhan turning each
dragon's mana value into damage. Legal, and a known line. Against the rubric (most games 4-10
turns, every player gets to shine, interaction) this pod scores badly: two seats never got going
(Lathril sat on two lands all game), nobody held interaction for the engine everyone had named, and
the game was decided by one untouched turn. An average list is a seed, not a tuned opponent; this is
what the workshop is for.
