# Evaluation (2026-10-18)

Two parts, measured differently:

| Part | Measured on | Who checked |
|---|---|---|
| Champion tracker | Real public data: 126 published case studies of one B2B software company | Claude agents and Claude in the session; no person has checked it yet |
| GTM brief | Synthetic text only: two sets written and labelled by LLM agents | The code scores itself against the agents' labels |

The pipeline surface and the connectors are not evaluated here: they run on demo data, and the connectors were written against the official API docs and tested with fixtures the author generated in each API's response shape. None has run against a live tenant. The Slack output (a canvas from `canvas.md`, a List with the 10 next-steps rows, the digest) was posted once to the candidate's own Slack as private files through a Slack connector, not through the app's bot token.

# Part 1. The champion tracker on real public data

The real rows are not in this repo (they name real people). This section reports what happened and how it was checked.

## Input
- 126 published customer case studies from one B2B software company (all of its listing pages, de-duplicated).
- People found: **195** named people: **181** at customers, **14** excluded (vendor staff, consulting partners, a chatbot).

## Extraction (who is named and what they did)
| Check | Result |
|---|---|
| Method | 6 Claude agents. They copy names, titles and one evidence sentence verbatim and tag the action; the code decides the level from the tag. |
| Answer key | Made by Claude reading 15 stories (27 customer people). Not a human key. |
| Recall against the key | **27/27** |
| Champion / User agreement | **26/27**. The one disagreement is an assistant-level role that administers expenses: the key says user, the agent says owns the program. |
| Evidence verbatim | **195/195** sentences appear word for word in the source page |

**Tagging caveat.** The real seed's tags were made under the old contract wording. About 19 of the 38 `owns_program` rows would need re-tagging under contract v2 (`contracts/champion_definition.yaml`: a title alone never makes a champion).

## Lookup (current job)
- First a match by name + company. Then, for misses only, a name search 50 deep that keeps every same-name profile with its work history.
- A first version searched only 10 deep and held common names where the right person sat outside the top 10. The 50-deep search replaced it.
- On 2026-10-18 the 50-deep search still came back full, with nobody tied to the customer, for 37 people (34 of them had been labelled "N people share this name"). The list was cut off, so that label was false.
- GetLeads cannot filter on past employers: a company filter means the current employer, which a mover has left. So the fix is a full same-name pull with work history, tied locally by the rules. The live adapter does the same (`PEOPLE_DEEP_LIMIT`, optional `PEOPLE_EXPORT_URL`; `PEOPLE_PAST_COMPANY_FIELD` only for providers that filter on work history).

## Decisions (rules 1.2, with the pipeline's aliases and its customer logo wall as known customers)

| Decision | With the stored lookups | After the full same-name pull |
|---|---:|---:|
| Card | 0 | 0 |
| Review | 19 | 26 |
| Hold | 56 | 32 |
| Watch | 70 | 81 |
| No action | 36 | 42 |
| Excluded | 14 | 14 |

With the stored lookups, 37 of the 56 holds were cut-off searches ("Search hit the 50-result cap") and 19 had other reasons. After the pull the hold rate is 32/181 (18 %). One row moved from Watch to Hold since the previous write-up: a profile that still names the customer although that job ended in 2024 now holds (the rules now check an ended customer job before "still at the customer").

## The full same-name pull on the 37 cut-off searches

| | |
|---|---|
| Tool and cost | GetLeads, $0 cash on the unlimited plan; 87,474 fair-use credits |
| A rule it brought out | Two profiles of one LinkedIn member, the same member id under an old and a new URL slug, are one person; the freshest copy wins (the old slug's copy had stopped being refreshed and still named the customer). Rules 1.2; `test_one_linkedin_member_under_an_old_and_a_new_url_is_one_person` |
| Resolved | 24 of 37: 7 Review, 11 Watch, 6 No action. Each checked by Claude against the role in the case study, not by a person |
| Still Hold | 13: 11 "a full search of N same-name profiles found none tied" (N from 110 to 60,892); 1 with two tied profiles that disagree (two different people, one at the customer and one who left; the story has no date to tell them apart); 1 profile that still names the customer though that job ended |
| Second retry of the 13 | A web search for each person's profile, then a GetLeads lookup by profile URL (8 fair-use credits). Nothing GetLeads holds, so the 13 stay held. Two leads are written down for a person to check (one appears still at the customer in a recent press release; one may have moved); one row has initials only |
| Scripts | `capped_retry.py` (the pull; `--offline` reproduces it from the saved exports) and `held_retry.py` (the second retry), kept outside the repo with the real rows and a write-up of each run |

## Card 0
No confirmed champion moved within 90 days. The freshest move has a start date known only to the month: it is 79 to 109 days old, so it stays Review with the check "start date known only to the month". Review moves older than `old_move_days` (365) do not become seller cards; they go to the review owner's old-moves table.

## Checks
- **Review rows:** each of the 19 Review rows (stored lookups) was checked against its profile and work history: 19/19 are the right person with a real move. Each of the 24 rows the pull resolved was checked against the role in the case study. Both checks were done by Claude in the session, not by a person. No human spot-check has happened yet.
- **Clay:** a 195-row table (CSV import + 5 free formula columns, 0 credits) re-computes the final routing from columns the Python had already decided (Same person?, Moved?, New account, Reason). It holds a pre-review snapshot, and its formula check proves routing only, not identity. It lives in a workspace owned by another company, so it cannot be shared and its name is never shown. Details: `clay/formulas.md`.
- **Tests:** `python3 tests/test_rules.py` runs 71 checks on the champion rules and the live people adapter, with a stubbed provider for the full pull.

# Part 2. The GTM brief on synthetic text

Gong, CRM and Slack content is private and no real call text was available, so the extractor is measured on text written by LLM agents. All numbers are precision / recall. `python3 tests/test_brief_eval.py` prints them; `--errors` lists every false hit and miss on the tuning half.

## Protocol
| Step | What was done |
|---|---|
| 1. Old set | 160 texts (`tests/data/brief_eval_set.csv`) written and labelled by one LLM agent from a spec the rule author wrote. The spec listed phrases and traps the rules already handled, so the set was written knowing the rules. E081-E160 is the holdout. |
| 2. Independent set | 200 texts (140 call turns, 35 CRM notes, 25 field notes) written by one agent and labelled by a different agent; neither was shown the rules. Measured once, untuned, on all 200 (lexicon 2.2; observer prompt 1.0, three runs). This is a record: it cannot be re-run with today's rules. |
| 3. Tuning | Error analysis on N001-N100 only (`indep_dev_*.csv/json`): lexicon 2.3, observer check 1.1, observer prompt 1.1. The tuning-half numbers (0.78-1.00; lexicon alone 0.88-1.00) are high by construction: tuning, not a result. Lexicon 2.4 then added fixes from red-team probes, not from evaluation rows. |
| 4. Held-out half | N101-N200 (`indep_test_*.csv/json`) measured **once** after tuning. The lexicon numbers are re-scored by the test on every change (floors; lexicon 2.4 gives the same figures as the recorded 2.3 measurement); the observer numbers are recorded in `indep_test_measurement_2026-10-18.json`. |
| 5. Real API path | The code's own observer call (`observer.call_openai` through OpenRouter, 2026-10-18) on the held-out half, three runs, plus the old-set holdout, the tuning half N001-N100 (it held its floors) and a 10-row pilot once each: 12 calls, $0.024. The OpenRouter account's provider settings block Anthropic models, so it ran on `openai/gpt-5.6-luna`. The held-out replies are recorded in `indep_test_observer_real_api_luna_run{1,2,3}.json` and re-scored by `tests/test_brief_eval.py` through today's code check. |

The observer outputs `observer_haiku_output.json`, `indep_dev_observer_haiku_run{1,2,3}.json` and `indep_test_observer_v1.1_run{1,2,3}.json` were produced by Claude Haiku agents given the exact prompt text. The real API path has run since (step 5), on Luna; `call_anthropic`, and Claude Haiku through the real path, have not (that needs Anthropic models allowed in the OpenRouter account, or an `ANTHROPIC_API_KEY`).

## (1) Old set, holdout E081-E160, lexicon only

| Theme | Lexicon only |
|---|---|
| Pricing friction | 1.00 / 0.67 |
| Competitor | 0.94 / 1.00 |
| Expansion | 1.00 / 0.80 |
| Timing | 1.00 / 0.92 |
| People change | 1.00 / 0.70 |
| Engagement | 1.00 / 0.60 |

E088 was seen during a grep for false-hit risk while writing lexicon 2.3, so the people-change figure is not a clean holdout number.

## (2) Independent set, first measurement, untuned, all 200

| Theme | Lexicon 2.2 | Lexicon + observer (prompt 1.0, three runs) |
|---|---|---|
| Pricing friction | 0.75 / 0.33 | 0.65-0.88 / 0.67-0.78 |
| Competitor | 1.00 / 1.00 | 1.00 / 1.00 |
| Expansion | 0.75 / 0.60 | 0.50-0.54 / 0.70 |
| Timing | 0.56 / 0.50 | 0.38-0.40 / 1.00 |
| People change | 1.00 / 0.45 | 1.00 / 0.91 |
| Engagement | 1.00 / 0.22 | 0.80 / 0.89 |

## (3) Independent set, held-out half N101-N200, measured once

| Theme (positives) | Lexicon 2.4 only | + observer prompt 1.1, Claude Haiku agents (three runs) | + observer prompt 1.1, real API path on Luna (three runs) |
|---|---|---|---|
| Pricing friction (11) | 0.67 / 0.18 | 0.73-0.89 / 0.73 | 0.89-0.90 / 0.73-0.82 |
| Competitor (9) | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| Expansion (5) | 0.60 / 0.60 | 0.50-0.57 / 0.80 | 0.57-0.67 / 0.80 |
| Timing (3) | 0.67 / 0.67 | 0.33 / 0.67 | 0.29 / 0.67 |
| People change (4) | 1.00 / 0.75 | 1.00 / 1.00 | 1.00 / 1.00 |
| Engagement (4) | 1.00 / 0.75 | 1.00 / 1.00 | 1.00 / 1.00 |

The real-API runs made one more timing false hit than the agent runs ("get three quotes, a procurement thing", N113). On the old-set holdout, run once through the real API, they scored the same as or better than the Haiku agents' run on that set, except engagement: recall 0.70 (three misses, E089, E104 and E145, against one for the Haiku agents' run, which used prompt 1.0).

## Plain reading
- The lexicon alone misses most pricing pushback on independent text (recall 0.18 on the held-out half).
- The observer lifts pricing, people-change and engagement recall, but over-fires on timing and expansion (precision 0.33 and 0.50-0.57 on the held-out half with the Haiku agents; 0.29 and 0.57-0.67 through the real API on Luna).
- Competitor 1.00 means exact matching of a configured list of three invented names. It says nothing about how real competitor names behave.
- The samples are small: 3 to 11 positives per theme on the held-out half.

## The travel-expense lexicon
`contracts/lexicon.travel-expense.json` (2.4-travel-expense) is the same lexicon with a real travel-and-expense competitor list (Concur, Expensify, Ramp, Brex, Perk, Amex GBT / Egencia, Spotnana, Emburse, Coupa, Center, Airbase, Pleo, Spendesk, Rippling Spend) and rules for names that are everyday words. Select it with `LEXICON_PATH`. It has no precision or recall figure: the evaluation sets were written with the demo names. On all 360 evaluation rows it fires only on the two rows that write a real name (both Concur), and never on "cost center" or "Amex" rows.

## Limits
- Every text is synthetic, written to sound like call turns and notes, not taken from real calls.
- The old set was written knowing the rules; only the independent set's held-out half says how the rules carry over to unseen phrasing.
- The Haiku columns come from three runs of Claude Haiku agents given the prompt, not from the code's own API calls. The code's own calls ran on Luna, not Haiku, because the OpenRouter account blocks Anthropic models.

## Next step
In the first live weeks, label about 200 real call turns (people, not a model), add them as a third set, and re-run `tests/test_brief_eval.py` on them before trusting the brief's recall.
