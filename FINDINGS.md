# Playtest findings

What real games have shown about the harness: its speed, its flow, and the quality of play and
rulings. Each item says what was seen, the evidence, and where it stands. `fcsim status <game_dir>`
produces the numbers quoted here.

Games so far: a 3-turn Talrand goldfish, a 5-turn Zuko goldfish, and one four-player pod (Zuko,
Lathril, Miirym, Wilhelt; EDHREC average lists for the last three), started 2026-09-20 and resumed
three times as the driver changed under it.

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
| S9 | Opus at full effort deliberates over passing. | Players now run at medium effort, triage at low | Done for this game; per-role effort is a command-line option |

Measured effect so far: about five minutes a table turn on the first driver; quiet turns now take
one to three minutes. Busy turns are still six to fifteen, which is what S3 and S7 are for.

## Rules quality

| # | Finding | Evidence | Status |
| --- | --- | --- | --- |
| R1 | The auditor faulted legal mulligans, and both players complied and lost a card. | Turn 1: two `illegal` findings against the free multiplayer mulligan | **Fixed**: what the table enforces (mulligans, turn order, card conservation) never reaches the auditor |
| R2 | Players defer to the auditor even when it is wrong. | Neither disputed R1, though the prompt allowed it | **Open**: disputes and a judge (PLAN §8.3); until then a false alarm costs real game state |
| R3 | A fresh session repeats what it just did. | Wilhelt's end-step trigger put on the stack and resolved five times in one step | **Fixed**: the briefing opens with "what you have already done this turn" |
| R4 | Each player re-derives the board, and gets it wrong. | Combat resolved with Wilhelt's printed 3/3 while Lord of the Undead made it 4/4 | **Fixed**: the auditor writes continuous effects and current power/toughness onto the board. **Open**: they are only as fresh as the last audit; recompute before blocks when the board changed this turn |
| R5 | Missed triggers are the commonest error by far. | 11 of 16 findings: Norin, Isochron Scepter, Wilhelt, Ganax, Miirym's copy trigger | **Built, not yet run with models**: the auditor gives an event trigger a small structured `on` (event: cast/enters/dies/leaves/attacks; who; a type-line word; nontoken; other). The table records what happens as facts, matches them, and puts the trigger in front of its controller before anyone receives priority. A loose match is declined by the player. A trigger put on the stack by hand consumes its queued twin. Hooks exist from the turn after a permanent arrives, since audits are per turn |
| R6 | State-based losses were applied a few passes late. | Zuko and Wilhelt sat at -1 life while priority was passed | **Built, not yet run with models**: before anyone gets priority the driver removes a player at 0 life, 10 poison, 21 commander damage or a draw from an empty library, as a logged correction. A `hold` hook with step `loss` (Platinum Angel) stops it |
| R7 | A pilot repeated commander damage the table had already counted, and could not undo it. | Zuko goldfish: 16 recorded, 8 dealt | **Fixed**: tool text says the table counts it; `commander_damage` takes `set` |
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
| F8 | No narrative or rubric score at the end. | Only `report.md` standings and each player's own account | **Partly built**: `fcsim summarize <game_dir>` condenses each round with full information (the log, private draws, each player's own account, audit findings), then writes one file, `narrative.md`: the story, with a closing section that judges the game against the group's rubric, followed by the round accounts it was written from. Finished rounds are read back from that file, so another style costs two calls. Building it showed that the turn's own draw was recorded nowhere: every draw is now told privately to its seat. **Open**: computed rubric scores |

## What the pod says about the decks

EDHREC's average Miirym list killed two players from 27 and 37 life in one main phase on its fifth
turn: Temur Battlecrier's discount, Earthquake Dragon for {G}, and two copies of Lozhan turning each
dragon's mana value into damage. Legal, and a known line. Against the rubric (most games 4-10
turns, every player gets to shine, interaction) this pod scores badly: two seats never got going
(Lathril sat on two lands all game), nobody held interaction for the engine everyone had named, and
the game was decided by one untouched turn. An average list is a seed, not a tuned opponent; this is
what the workshop is for.
