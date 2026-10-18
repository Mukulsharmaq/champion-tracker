# Evaluation on real public data (2026-10-18)

The real rows are not in this repo (they name real people). This page reports what happened and how it was checked.

## Input
- 126 published customer case studies from one B2B software company (all of its listing pages, de-duplicated; each page repeats 6 featured stories).
- People found: **195** named people → **181** at customers, **14** excluded (vendor staff, consulting partners, a chatbot).

## Extraction (who is named and what they did)
| Check | Result |
|---|---|
| Method | 6 Claude agents, 21 stories each. They copy names, titles and one evidence sentence verbatim and tag the action. |
| Answer key | 15 stories read by hand (27 customer people) |
| Recall vs key | **27/27** |
| Champion vs User agreement | **26/27**. The one disagreement is an assistant-level role that administers expenses: the key says user, the agent says owns the program. |
| Evidence verbatim | **195/195** sentences appear word for word in the source page |
| Regex baseline (for comparison) | 13/27 (48 %), not usable alone |

## Lookup (current job)
- First, a match by name + company (batches of 100). Then, for misses only, a name search 50 deep that keeps every same-name profile with its work history.
- A first version searched only 10 deep. It held common names where the right person sat outside the top 10. Fixed by the 50-deep retry (see failures_fixed.md).
- Cost: about 3.5k lookup credits on an unlimited plan, so **$0**.

## Decisions (this repo's rules v1.0, after the review fixes)
| Decision | Rows |
|---|---:|
| Card | 0 |
| Review | 19 |
| Hold | 55 (40 same name with none tied · 8 no profile · 4 latest job ended · 2 records disagree · 1 moved but identity only Likely) |
| Watch | 71 |
| No action | 36 |
| Excluded | 14 |

Level split among customer people: 129 Champion / 52 User. Freshest confirmed champion move: 109 days (so no Card under the 90-day rule).

## Checks
- **Hand-check:** every Review row was checked against its profile and work history. 19/19 are the right person with a real move (after the fixes in failures_fixed.md). A sample of 8 Watch rows was also correct.
- **Clay parity:** the full rule was rebuilt as free Clay formula columns. A `Rule check` column comparing Clay to the pipeline: **195/195 match**. Clay credits before and after: unchanged (**0 used**).
- **Repo parity:** this package re-run on the same 195 rows, with the same customer aliases and known-customer domains as the pipeline: **1 decision difference**, a fix from the review (two profiles on one domain after a company rename, now merged → Watch instead of Hold). Move ages: 0 differences.
- **Clay snapshot:** the Clay table holds the pipeline's 195 rows from before that fix (Hold 56 / Watch 70).
- **Profile freshness:** 2,410 candidate profiles had a refresh date. Median 87 days before the run, and 96 % within 120 days.
- **Tests:** 38 pass (`python3 tests/test_rules.py`): the broken-record cases, plus controls, an end-to-end run on the fixtures, and card, feedback and contract checks.
