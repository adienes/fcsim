# Playtest findings

What real games have shown about the harness: its speed, its flow, and the quality of play and
rulings. Each item says what was seen, the evidence, and where it stands. `fcsim status <game_dir>`
produces the numbers quoted here.

Games so far: a 3-turn Talrand goldfish, a 5-turn Zuko goldfish, and four four-player pods, all
with Fire Lord Zuko in seat 0. Pods 1 to 3 (2026-09-20) used the same three EDHREC average lists
(Lathril, Miirym, Wilhelt): Miirym won in round 6, Lathril in round 9, Miirym in round 10. Pod 4 drew
its opponents at random from EDHREC's hundred most-built commanders (Ashling, Aragorn, The Wise
Mothman) and is the first on the table's own rules layer. A running game is stopped and resumed on
new code whenever a fix lands, so each pod's later turns test more than its early ones did.

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
| S13 | "Auditing 211 events" was mostly not events. | Third pod, turn 30: 106 passes, 34 checkpoint releases, 10 notes and policies, and 60 real actions. The table stopped at every mid-turn checkpoint only for the driver to release it at once, journalling each stop and copying the game state each time | **Fixed**: with one audit a turn the table stops only between turns (`gated="turn"`), and the progress line counts actions |
| S14 | Models compose payments by hand, from handles they have to read off the board. | Third pod: 17 calls rejected for a stale or unknown handle, and one {2}{G}{G} paid with one green source | **Built, not yet run with models**: the table plans a payment for every card the player can afford, keeping back the lands that make any color, and the briefing gives each as a call ready to use: `cast {"card": "c41", "payment": {"tap": ["c3", "c9"]}}` |
| S15 | The auditor was asked about turns the table had already verified. | With timing, zones, payments and combat checked by the table, a turn of "land, a creature with only keywords, attack, damage as worked out" leaves nothing to find or to write down | **Built, not yet run with models**: such a stretch is not audited. A permanent counts only if its card does nothing but tap for mana or carry keywords, or is in the hook library with every trigger a pattern; anything with a `because`, a warning, a target, an X, a payment note or a hand-made effect is audited as before |
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
| R11 | The auditor sometimes remembers Commander's old rules. | Suite rerun after the disputes change: 40 of 41, the miss a false alarm claiming a dying commander "never touches the graveyard" (the pre-2020 replacement rule), so Blood Artist should not have triggered. The same scenario had passed before: variance, not the new prompt | **Fixed**: the brief now states 903.9a and 903.9b, commander tax, 21 commander damage and 40 life; the scenario passes again. This is also what the judge is for |
| R12 | A card that comes back has no hooks until the next audit. | Second pod, turn 29: Wilhelt died, was recast in the main phase, and his end-step trigger was missed because the ledger entry had gone with the old object | **Fixed**: the driver remembers what the auditor has written for each card, relative to its controller, and writes it again the moment a permanent of that name arrives: a recast commander, a second copy, a token copy |
| R13 | A creature was cast in the end of combat step. | Second pod, turn 29: Death Baron, caught by the auditor a turn later | **Fixed**: the table is told a card's types and keywords, so a card that is not an instant and has no flash is refused outside its controller's main phase or over a non-empty stack until `because` names what allows it |
| R14 | A trigger on the permanent itself was offered whenever anything entered. | Fourth pod, turn 11: "The Wise Mothman does not put a trigger on the stack: Whenever The Wise Mothman enters ... (because Fire Nation Palace entered)". The hook vocabulary could not say "this permanent itself", so "enters" meant anything entering; Mothman's attack trigger had the same hole, and hooks the auditor wrote with no `on` at all (Laelia's attack trigger, Bojuka Bog) were never queued | **Fixed**: `on.self` for abilities that trigger only on their own permanent, and the auditor is told to give `on` whenever the event is one of the five. A card known from the library gets its own "when this enters" queued as it arrives, since the table had matched that event before the hooks existed. `rewrite_hooks` replaces what the ledger holds for a permanent, and a resumed game is re-hooked from the library where the library has learned better. Nine library entries were marked |
| R15 | The table's combat arithmetic served the first combat of a game and refused every later one. | Fourth pod: `as_proposed` was tried at every combat and rejected 7 times with `no_proposal`, so players went back to doing the sums. The count of damage steps was never reset between combats; attackers and blockers were not reset for an additional combat either | **Fixed**: every combat starts clean. My scripted test had only ever exercised one combat per game; it now plays a second |
| R16 | Players write a mutation's name as a key beside its fields. | Fourth pod: 22 calls rejected as `unknown_op`, such as `{"mill": {}, "seat": 0, "count": 1}` and `{"move": true, "from": ..., "to": ...}` | **Fixed**: where exactly one key is a mutation's name, that is the mutation |
| R17 | Trigger conditions were being added to the table one at a time. | After the fourth pod I added `milled` and `deals_combat_damage` as two more named events, each a line of code; the question "we're not hardcoding these, right?" was the right one. Magic has thousands of trigger conditions | **Fixed**: the table can only see a handful of things happen, because it has only a handful of mutations: an object moves between zones, damage, life, counters, tap and untap, attack, block, cast. Every mutation records one such fact, and a hook's `on` is a pattern over them, all data: `{event: moved, from: hand, to: graveyard, who: opponent}` is "whenever an opponent discards". enters, dies, milled, drawn, gains_life and the rest are names for patterns in a table, not code. Filters: who, whom, type, not_type, kind, sign, min, combat, nontoken, nonland, self, other, once. A condition no pattern states ("your second spell each turn") stays text for the auditor to check. A card moving between hidden zones is never named in a trigger's "because" |
| R8 | The auditor judged the big turn correctly. | Turn 19: cost reductions and 56 damage accepted; only Miirym's own missed triggers flagged | Good sign; the suite should gain a long-chain scenario |

## Flow and play

| # | Finding | Evidence | Status |
| --- | --- | --- | --- |
| F1 | "Table turn 9" reads as turn nine; it was each player's third. | User confusion mid-game | **Fixed**: seats count their own turns; prompts, the board text, reports and `status` say "round 5, Miirym's turn (table turn 19)" |
| F2 | No way to see a running game except reading files. | Asked twice "still going?" | **Fixed**: `fcsim status <game_dir>` |
| F3 | A game could not be continued. | First run stopped at its round cap | **Fixed**: `fcsim pod --resume` replays the journal; verified on 506 real events |
| F4 | News was capped at 40 lines, mostly passes, so real plays fell out of a briefing. | Found by a test after the upkeep wake was removed | **Fixed**: passes are left out of news |
| F5 | Politics appears by itself once there are threats. | No talk in rounds 1-2; by round 3 all four seats argued about who the threat was, and Zuko changed its removal target | Good. **Built, not yet run with models**: a `say` that names players in `to` asks each of them at once, with no tools, what it says back; the answers are said at the table on their behalf and returned to the speaker in the same call, so a deal can be struck before the speaker acts. Three such exchanges a wake at most |
| F6 | The table read the threat correctly and still lost to it. | All three opponents named Miirym's engine in round 3; it killed two of them on turn 5 | A deck-balance result, not a harness one: see below |
| F7 | Round caps count eliminated seats. | `--cycles` multiplies by the number of seats at the start | **Fixed**: a game is capped in rounds; on a resume the turn in progress is the first of the new rounds |
| F9 | Nobody can act at instant speed on an empty stack in another player's turn. | The driver passes for every seat there (S1), so "at the end of your turn, I cast" cannot happen: flash creatures and instant-speed draw are weaker than they should be | Worse than first noted: the same pass meant nobody could kill an attacker, or play a trick after blocks, because the stack is empty at both moments. The first pod's rubric shows it: 0 plays at instant speed all game. **Built, not yet run with models**: when attackers have been declared, and again when blocks have, every seat that could act (cards in hand or something untapped, and no standing "pass always") is asked at once, with no tools, whether it acts; those that say yes are woken with priority. The end step before one's own turn is opt-in: `set_response_policy` takes `hold_for_end_step`, private and reset on one's own turn. Other empty-stack moments are still passed |
| F10 | The rubric could not replay a finished game. | Second pod: the game ended, the report was written, and the scoring step then failed with a table error. The replay was given no card data, so triggers keyed on card types were not queued and the rebuilt game left the path its journal records. The first pod had no such hooks, which is why it had worked | **Fixed**: every replay for reading (rubric, card record, narrative, review, report) is handed what the table was told at the time, from the `cards.json` pinned in the game directory |
| F11 | The card record ignored cards exiled from the library, and the reviewer's "stuck on lands" was half wrong. | Asked how an exile deck gets stuck on lands, the journal says: in the first pod Zuko made every land drop (the reviewer invented a stall, and I repeated it); in the second it saw 23 cards, 7 of them exiled face up, and 4 were lands, none among the exiled ones. Six of the seven exiled cards were never played, an exiled Arcane Signet among them while the pilot sat on four lands | **Fixed**: a card exiled face up from the library counts as seen, and the record says whether it was played from exile. `play_land` now says a land can be played from exile or wherever an effect allows, given `because`; it used to read "from your hand". **Fixed**: the reviewer is given each of the seat's own turns as the log has them (lands held, land played, spells cast), the list in numbers (counts by type and mana value, the cheap instants and the top end by name), and is told these outrank the round accounts; a second pass then checks every statement of fact in the draft against the record, and the review is written again if any does not hold, with the corrections listed at its foot |
| F12 | The readable log was three quarters "passes priority". | Third pod: 1,466 of 1,928 lines. Players never saw them (their news leaves passes out, the auditor's record collapses them, the narrative skips them), but a person reading `log.md` did, and so did a player calling `get_log` | **Fixed**: the log and `get_log` tell a run of passes in one line ("`1782-1790` turn 31 upkeep to main1: Zuko, Lathril, Miirym passed priority (9 passes)"); a pass that carries a warning or a private draw stays its own entry. The journal keeps every pass, since a replay needs them. `GameDirectory.rewrite_log()` redoes an old game's log: the three pods' logs went from 1,105, 1,856 and 1,943 lines to 499, 771 and 785 |
| F13 | A journal that no longer rebuilt its game was found by whichever tool tripped on it first. | Second pod: the rubric crashed after the game; my own tests had replayed only games the scripted seats could play | **Fixed**: every game ends by replaying its own journal and comparing the result with the table it played on; `report.md` says "The journal replays to the same game", or says loudly that it does not |
| F8 | No narrative or rubric score at the end. | Only `report.md` standings and each player's own account | **Partly built**: `fcsim summarize <game_dir>` condenses each round with full information (the log, private draws, each player's own account, audit findings), then writes one file, `narrative.md`: the story, with a closing section that judges the game against the group's rubric, followed by the round accounts it was written from. Finished rounds are read back from that file, so another style costs two calls. Building it showed that the turn's own draw was recorded nowhere: every draw is now told privately to its seat. **Rubric scores built**: `fcsim rubric <game_dir>` replays the journal and scores length, participation, sitting out, interaction, threat rotation, turn weight and the ending, each with its threshold stated, plus a row per seat. The scores go into `report.md` and `rubric.json` at the end of a game and into the narrative's prompt. On the first pod they say what a reader said: Lathril idle on 3 of 4 turns, a 50-action turn against a typical 4, and three players killed in one turn from 27, 37 and 28 life. Spotlight and salt stay with the reader |

## Direction: the table takes over whatever depends on no card's text

After two pods the ask was less hallucination, more robustness and more speed, by baking rules
into the table so that a model's context is structured and it does no work the table can do. The
project stays self-contained: no outside rules engine. What the table now owns, with `because` as
the way past it when a card says otherwise:

| Rule | Status |
| --- | --- |
| Land plays only in one's own main phase with an empty stack | refused (R9) |
| Non-instant, non-flash cards only at sorcery timing | refused (R13) |
| Casting or playing from an unusual zone | needs the effect named (R10, F11) |
| State-based losses: 0 life, 10 poison, 21 commander damage, empty library | applied by the driver (R6) |
| Attacking: tapped, defender, arrived this turn without haste | refused |
| Blocking: tapped blocker; a flier blocked without flying or reach | refused |
| Combat damage: first strike and double strike steps, trample, deathtouch, lifelink, indestructible, lethal damage, a commander's move to the command zone | worked out by the table from printed numbers, counters and the auditor's stamps (which now carry keywords); offered to the attacker as `as_proposed`, and applied without a wake when nothing has happened since the attack that could change it |
| Paying for a spell: the permanents tapped must be able to make the cost, colors, X and commander tax included | refused, unless the payment's `note` says what else pays. The third pod paid {2}{G}{G} with one green source; the auditor found it a turn later |
| What a player can afford | the briefing lists the cards its untapped mana sources can pay for, so no model counts lands |
| Triggers and holds of a card any auditor has described before | on the ledger from the moment the card arrives, from a hook library kept across games (R12) |
| The rad counter trigger (rule 728.1), an ability with no source that no permanent's hook can carry | queued by the table as the player's first main phase begins. Rules the table takes over are numbered (`mechanics` in a game's config), so a journal replays under the rules it was played with |
| Still the agents' and the auditor's: everything a card's text says | ability costs, menace, the legend rule and zero toughness are next |

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

The whole game: 9 rounds, 34 table turns, 2,100 events, won by Lathril. 65 wakes took 50 minutes of
model time (46 s each; the longest, a 31-call Elf turn, 4 minutes), 33 audits took 23 minutes, 5
power-and-toughness checks before blocks 98 s, and 13 respond-or-pass questions 46 s: about 75
minutes of model time in all, against most of a day for the first pod's six rounds. No wake only
looked and passed, and the driver never had to finish one. The auditor raised five findings, all of
them right (R9, R12, R13 and a missed dies-trigger among them), and nobody needed the judge. The game
ran on the driver as it stood when it was resumed, so the combat windows (F9), the replies to table
talk (F5), opening hands decided together (S10) and the hook memory (R12) have still run with
scripted seats only. Next time, stop a running game at a turn boundary and resume it on new code:
`--resume` exists for exactly that.

## What the pod says about the decks

EDHREC's average Miirym list killed two players from 27 and 37 life in one main phase on its fifth
turn: Temur Battlecrier's discount, Earthquake Dragon for {G}, and two copies of Lozhan turning each
dragon's mana value into damage. Legal, and a known line. Against the rubric (most games 4-10
turns, every player gets to shine, interaction) this pod scores badly: two seats never got going
(Lathril sat on two lands all game), nobody held interaction for the engine everyone had named, and
the game was decided by one untouched turn. An average list is a seed, not a tuned opponent; this is
what the workshop is for.

The second pod, same lists, went the other way round: Lathril, idle on three of four turns in the
first game, won in round 9 with 38 attackers and 112 damage, killing Miirym from 28 and Wilhelt
from 22 in one damage step after Kindred Dominance had cleared the other boards. Participation was
good for every seat this time. Over both games (`fcsim report`): length good twice, turn weight
and ending bad twice (a 50-action and a 46-action turn; every loser went out in one turn from
half its life or more), interaction mixed twice, and not one play at instant speed on another
player's turn in either game, which is what F9 was written to fix. `fcsim review` on the Zuko
list over both games is in the second game's directory: it blames the pod and the draws first,
then a top-heavy hand and a suite of combat tricks that are blank on an empty board.
