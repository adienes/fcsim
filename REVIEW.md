# Repository review

Reviewed on 2026-09-20 through commit `0a79236`.

The package boundaries are sensible, but I found several correctness issues worth fixing before trusting the playtest results. **All 114 existing tests pass**, and the issues below came from additional targeted reproductions.

1. **[P1] Goldfish can loop indefinitely on an opponent-turn trigger.**  
   When the pilot owns a pending trigger during Goldfish’s turn, `settle()` cannot handle it, while the outer loop keeps calling `settle()` without advancing state or checking the budget. I reproduced 500 consecutive iterations at the same decision. Dispatch required decisions by seat and decision type, independently of whose turn it is. [goldfish.py:155](src/fcsim/play/goldfish.py#L155)

2. **[P1] Public logs reveal face-down card identities.**  
   Adding a counter to a face-down permanent correctly conceals its name in `get_view`, then exposes it in everyone’s `get_log`. Combat and payment formatting also use raw object names. Visibility needs to govern every output path, including warnings and logs. [ops.py:102](src/fcsim/table/ops.py#L102), [table.py:390](src/fcsim/table/table.py#L390)

3. **[P1] The auditor loses information necessary to judge resolved spells.**  
   Audit materials include human-readable event summaries, which omit declared X, modes, and spell text. I produced **identical audit prompts** for Fireball cast with X=1 versus X=5, both resolving for one damage. Once the spell leaves the stack, the distinguishing information disappears. Give the auditor structured, visibility-filtered action records containing those declarations. [auditor.py:200](src/fcsim/audit/auditor.py#L200), [table.py:296](src/fcsim/table/table.py#L296)

4. **[P1] Failed model invocations bypass the call budget.**  
   Timeouts and malformed JSON raise before `budget.record()`. With `max_calls=1`, I successfully launched two mocked invocations that timed out; accounting still showed zero calls. The audit suite continues after these failures, so the advertised hard limit is ineffective on that path. Count attempts before launching and explicitly handle unknown usage after failures. [runner.py:90](src/fcsim/agents/runner.py#L90)

5. **[P2] Randomness is outside the transaction and replay state.**  
   A rejected `[roll, invalid_operation]` batch consumes randomness despite leaving the game and event log unchanged. Separately, replay reconstructs the board but produces different subsequent rolls because recorded outcomes never advance the generator. RNG state needs rollback and restoration alongside game state. [table.py:164](src/fcsim/table/table.py#L164), [model.py:40](src/fcsim/table/model.py#L40)

6. **[P2] Eliminating the active player deletes other players’ spells.**  
   In a three-player reproduction, Ann conceded during her turn while Bob’s Lightning Bolt was on the stack. The stack was cleared, the turn advanced, and Bolt remained an orphaned object in the stack zone. The departing player’s turn should continue under [CR 800.4j](https://media.wizards.com/2026/downloads/MagicCompRules%2020260807.pdf#page=232). [turn.py:293](src/fcsim/table/turn.py#L293)

7. **[P2] Commander castability counts mana already spent on ramp.**  
   After deploying mana sources, the simulation checks commander affordability with `spent=0`. My deterministic opening reported a three-mana commander castable on turn one after using the only land to cast Sol Ring—only two mana remained. This biases the reported castability curve beyond the documented color-matching approximation. [shuffle.py:188](src/fcsim/workshop/shuffle.py#L188)

8. **[P2] Hooks bind to card names rather than specific permanents.**  
   With Ann and Bob each controlling Howling Mine, a hook specifying Ann as controller attached to Bob’s newer copy. Destroying Bob’s copy then removed Ann’s hook. Have the auditor return an unambiguous public object handle and validate it when applying the verdict. [table.py:461](src/fcsim/table/table.py#L461)

9. **[P2] The advertised vigilance argument fails.**  
   `declare_attackers(..., tap=False)` returns `unknown_handle`: the generic recursive resolver interprets every `tap` value as a card handle because payment also uses that key. Resolve handles according to the command schema and field location. [table.py:126](src/fcsim/table/table.py#L126)

10. **[P2] Commander quantities are silently discarded.**  
    `99 Island` plus `2 Talrand, Sky Summoner` resolves to a 100-card deck with no validation issues. Resolution merges quantities and then drops the commander count. Preserve quantities through validation or reject invalid counts explicitly. [validate.py:70](src/fcsim/cards/validate.py#L70)

11. **[P2] Hook scoring ignores false positives.**  
    A clean scenario with an entirely bogus “hold untap” hook still passes. Matching also doesn’t require the correct seat or timing fields. Consequently, `hook_accuracy` measures missing expected hooks without measuring harmful extra hooks. Compare hooks in both directions and check their behavioral fields. [suite.py:115](src/fcsim/audit/suite.py#L115)

Beyond those bugs, I’d focus the design work here:

- **Make audit boundaries explicit.** Hooks are installed after an entire pilot turn, but advancing that turn can already execute the next player’s untap/upkeep. The driver needs a checkpoint before automatic steps that depend on the verdict. The auditor also currently receives no rules-lookup tool. [goldfish.py:189](src/fcsim/play/goldfish.py#L189)
- **Separate persistence from reporting.** Events reach disk only after a wake and its audit. An interruption can lose already-accepted actions. Append the authoritative journal at commit boundaries, write snapshots atomically, and pin rules/rulings with each game. [goldfish.py:98](src/fcsim/play/goldfish.py#L98)
- **Keep the current package layout; strengthen the contracts.** `cards`, `rules`, `table`, `audit`, and `play` are useful divisions. The recurring code smell is loosely specified dictionaries interpreted differently across schemas, dispatch, logging, and replay. Typed commands and structured public events would address several findings together. `goldfish.py` is the first place I’d separate orchestration from game-directory storage.
- **CPU optimization can wait.** Rules search rebuilds its FTS index per query, but measured about 13 ms locally. State-copy commits measured about 0.9 ms with 123 objects. Caching the search index is reasonable; the runaway loop and failed-call accounting are much larger performance footguns.
- **Refresh the README’s opening status.** It still says the HTTP MCP server is unimplemented, contradicting the implemented goldfish section. [README.md:49](README.md#L49)

The review did not modify implementation files or make live model calls.
