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
| S3 | A fresh session per wake costs about 9 s of start-up, and a busy turn has many wakes. | Turn 17: 20 wakes; turn 19 (Miirym's dragon chain): 39 wakes, 900 s of model time | **Next**: keep the active seat's session open while responses are checked, so `pass_priority` returns "nobody responded, carry on" instead of ending the wake |
| S4 | Auditing at every mid-turn checkpoint stopped the game constantly. | 22 audits in 12 turns | **Fixed**: one audit per turn by default (`--audit-every checkpoint` for the strict mode) |
| S5 | Audit time grows with the record. | 128 s for turn 17, 104 s for the 218-event turn 19 | Partly fixed: runs of passes are one line, notes left out. **Open**: summarise long trigger chains; audit concurrently with the next turn's first wake |
| S6 | Nobody ever responds, yet everyone is asked. | 148 respond-or-pass questions in the game, 0 answered "respond" | **Open**: let a seat set a standing policy ("pass for me until my turn unless I am targeted or attacked"); skip the question for a seat with no untapped mana and no free spells it has declared |
| S7 | Trigger chains ask the table once per trigger. | Turn 19: each Lozhan, Ganax and Miirym trigger got its own triage round and its own resolution wake | **Open**: when one player stacks several triggers at once, ask about them together; S3 removes the resolution wakes |
| S8 | Agents make one more call after their wake has ended. | 31 `stale_wake` rejections, each a wasted round trip | **Open**: say so more forcefully in the last result, or end the session from the driver side |
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
| R5 | Missed triggers are the commonest error by far. | 11 of 16 findings: Norin, Isochron Scepter, Wilhelt, Ganax, Miirym's copy trigger | Caught and queued at end of turn. **Open**: match game events against `event_trigger` hooks so the table queues a trigger when it happens, not a turn later |
| R6 | State-based losses were applied a few passes late. | Zuko and Wilhelt sat at -1 life while priority was passed | **Open**: the table knows life totals; it could stop and ask for the loss to be carried out before anyone gets priority |
| R7 | A pilot repeated commander damage the table had already counted, and could not undo it. | Zuko goldfish: 16 recorded, 8 dealt | **Fixed**: tool text says the table counts it; `commander_damage` takes `set` |
| R8 | The auditor judged the big turn correctly. | Turn 19: cost reductions and 56 damage accepted; only Miirym's own missed triggers flagged | Good sign; the suite should gain a long-chain scenario |

## Flow and play

| # | Finding | Evidence | Status |
| --- | --- | --- | --- |
| F1 | "Table turn 9" reads as turn nine; it was each player's third. | User confusion mid-game | **Open**: reports and prompts should say "round 3, Zuko's turn" |
| F2 | No way to see a running game except reading files. | Asked twice "still going?" | **Fixed**: `fcsim status <game_dir>` |
| F3 | A game could not be continued. | First run stopped at its round cap | **Fixed**: `fcsim pod --resume` replays the journal; verified on 506 real events |
| F4 | News was capped at 40 lines, mostly passes, so real plays fell out of a briefing. | Found by a test after the upkeep wake was removed | **Fixed**: passes are left out of news |
| F5 | Politics appears by itself once there are threats. | No talk in rounds 1-2; by round 3 all four seats argued about who the threat was, and Zuko changed its removal target | Good. **Open**: reply wakes, so a direct question gets an answer before the decision |
| F6 | The table read the threat correctly and still lost to it. | All three opponents named Miirym's engine in round 3; it killed two of them on turn 5 | A deck-balance result, not a harness one: see below |
| F7 | Round caps count eliminated seats. | `--cycles` multiplies by the number of seats at the start | **Open**: count rounds by living players |
| F8 | No narrative or rubric score at the end. | Only `report.md` standings and each player's own account | **Open**: PLAN §11.1 and §12.1 |

## What the pod says about the decks

EDHREC's average Miirym list killed two players from 27 and 37 life in one main phase on its fifth
turn: Temur Battlecrier's discount, Earthquake Dragon for {G}, and two copies of Lozhan turning each
dragon's mana value into damage. Legal, and a known line. Against the rubric (most games 4-10
turns, every player gets to shine, interaction) this pod scores badly: two seats never got going
(Lathril sat on two lands all game), nobody held interaction for the engine everyone had named, and
the game was decided by one untouched turn. An average list is a seed, not a tuned opponent; this is
what the workshop is for.
