# Test fixtures

`sample_mistakes.srt` is a fake caption file. Each deliberate mistake copies one from
`data/examples.csv` (the "Caption mistakes (59)" sheet), so later phases can reuse the file.

| # | Caption | Real example it copies | Expected (default config) |
|---|---|---|---|
| 1 | So the market picked up | E2CS 00:00:07:50, "picked up" should be "peaked up" | Phase 4 (audio) |
| 2 | and number one is, | E2CS 00:00:08:09, "one is" should be "one" | Phase 4 |
| 3 | i should be careful here | E2CS 00:00:38:45, "i should be" should be "it should be" | **capitalization** ("i"; not for E8D). The missing "t" needs Phase 4 |
| 4 | the (0.13 s) | E2CS 00:00:14:32, "the" came for a few frames | **too_short** |
| 6 | same | E2CS 00:00:37:02, only "same" shown for "in the same direction" | **too_short** |
| 8 | This is where we turn the | E2CS Ep 317 06:10, "the" left at the end of the line | **phrase_break** |
| 10-11 | hormonal / development (0.3 s each) | E8C 00;00;15;07, should come together, passing too fast | **too_short** x2 |
| 13 | Wow. | whitelisted one-word caption | no issue |
| 14 | so you even | E8D 00:00:16:12, make it a double line with the next line | phrase_break only when `fragment_max_words: 3` |
| 15 | know what I mean | followed by a 2 s gap (end of segment) | no issue |
| 16 | Okay. | after a 2 s pause (isolated) | no issue |
| 17 | We work twenty hourse a week | E2CI 00:02:55:16, "hourse" should be "hours" | **spelling** |
| 18 | Are you just want to to rest | E3JB "Or" not "Are"; duplicate "to" | **repeated_word**. "Are" needs Phase 4 |
| 19 | in the realM of august, leo. | E8D "realM"; E8C capitalize "august", "leo" | **capitalization** x2 ("realM", "august"; only "realM" for E8D). "leo" needs `=Leo` in the glossary |
| 20 | Thanks. | last caption of the file | no issue |

`clean_sample.srt` has no mistakes. It is full of things that trip spell checkers: names, brands,
contractions, numbers, a link and a handle, quotes, title-case names. The only issues it should
produce are two `low` "is this a name?" questions (Buffett, McGwire), which go away once they are in the glossary.

`punctuation_mistakes.srt` holds punctuation mistakes from `data/examples.csv` for the Phase 3
check. The tests answer for the model with a fake (no API calls); the fake gives the correction
the reviewer asked for:

| # | Caption | Real example | Correction |
|---|---|---|---|
| 1 | and there it was. | E3RK 0:03, no full stop after "there it was" | and there it was |
| 3 | it was pretty bad. That | E3RK 0:25, no full stop after "pretty bad" and no capital after it | it was pretty bad that |
| 4 | was when it started | (none: the fake answers with confidence 0.4 to test `min_confidence`) | dropped |
| 5 | it was recreational then it got worse | E3RK 0:11, comma after "recreational" | recreational, then |
| 6 | his speech was slow slurred and heavy | E3JB 00:01:55:09, comma before "slurred" | slow, slurred |
| 7 | all that weight on his shoulders back | E2CS 00:00:35:12, "shoulder's" not "shoulders" | shoulder's |
| 8 | I'm bringing sexy, back | E8D 0:13, no comma after "sexy" | sexy back |
| 9 | Why does that matter. | E3ME 00:01:03:11, question mark | matter? |
| 10 | that's where we come from | E8D 0:27, full stop after "come from" | come from. |
