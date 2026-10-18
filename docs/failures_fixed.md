# Failures found and fixed while building this

Each one broke a version of this on real data or in review. Each rule failure is now a test in `tests/test_rules.py`. Names in the examples below are invented.

## Found on real data
| # | What broke | How it showed up | Fix (rule) | Caught by |
|---|---|---|---|---|
| 1 | Same name, different person | One name returned 5 profiles: a paralegal, a retail worker, a teacher… Only one worked at the customer. | A profile counts only if tied to the old company (employer, work history, headline). Several namesakes with no tie → Hold. | Page-1 test |
| 2 | Name search matched on substrings | A short name (like "Jo de Vries") returned a different, longer one (like "Joanna Devrieson") | Exact last name + first name equal or a 3+ letter prefix (Rob ~ Robert) | Full-run hand-check |
| 3 | A board seat recorded as the new job | "Chair, regional board" (freelance) became the current employer while the real job was still open | A side seat while the old job is open is not a move | Page-1 test |
| 4 | Stale profile contradicts the evidence | Profile showed a junior admin title, last refreshed 2+ years ago; the evidence said head of the program | A stale profile (> 365 days) with a contradicting title → Hold: records disagree | Page-1 test |
| 5 | Acquired company's domain | Email on an acquired brand's domain; the person is still at the parent | Parent/acquired names are listed as aliases in config, never guessed | Page-1 test |
| 6 | Company matched by name to the wrong firm | A lookup by a short company name returned a different company with a longer name | Companies are compared as whole normalised names; domains are preferred | Page-1 test |
| 7 | A lagging field kept the old employer | The org/email domain still showed the old company after the person moved | Fields that lag behind a move are never used for identity or movement | Full run |
| 8 | A job that had already ended was treated as current | The "current" employer's job had an end date | If the latest job ended → current employer unknown → Hold | Full-run hand-check |
| 9 | An acquisition was treated as a move | The "new" employer's start date equals the old job's start month (renamed or acquired company) | Same start month → Hold: looks like a rename or acquisition | Full-run hand-check |
| 10 | A self-employed side gig was treated as a move | The current entry was "self-employed content creator" while the job at the customer (under its parent's name) was still open | A self-employed/freelance seat while the customer job is open is not a move; parent names count as the customer | Full-run hand-check |
| 11 | Move age measured from the wrong date | Left the customer 4 years ago but joined the current company 9 months ago | Move age = days since joining the new company (the "why now") | Full run |
| 12 | Search depth silently held common names | A 10-deep name search missed the right person for common names, including people already confirmed | Match by name + company first, then a 50-deep search for misses; re-check confirmed people after any lookup change | Full run |
| 13 | Extractor recall | Regex extraction found 13/27 named people | Agents read every story; scored against a hand-read key (27/27) before the full run | Page-1 test |

## Found by a four-lens review of this repo (each reproduced, then fixed)
| # | What broke | Fix |
|---|---|---|
| 14 | A board seat or freelance gig became a Card once the old job had an end date | A side seat is never a real move: Hold (current employer unknown) |
| 15 | A common word in a headline ("Head of Travel at …") tied a stranger to a customer named "Travel …" | A headline ties only on the whole company name, or "ex-/formerly/@" + the name |
| 16 | A name match beat a different domain ("Hooli Realty" counted as customer "Hooli") | Domain decides when there is one; the name is used only without a domain |
| 17 | Two profiles of one person on the same domain (renamed company) were held as "disagreeing" | Duplicates are grouped by domain first |
| 18 | A returning employee was aged from their first stint years ago; then an internal promotion looked like a fresh move | Age = start of continuous tenure: a promotion extends it, a real gap resets it |
| 19 | One malformed date ("2026-09", "unknown") or a null list crashed the whole run | Tolerant date parsing; missing lists treated as empty |
| 20 | A champion row without its evidence sentence or source link could still reach a seller | Hold until both exist |
| 21 | Feedback and CRM write-back were keyed on the person's name | Every row has a stable row id; write-back matches on it, never on a name |
| 22 | Live mode returned provider field names the rules don't read, and a bad token looked like "no profile" | Provider keys are mapped once; auth/provider failures become "Lookup failed; retry" |

Bugs 2, 8, 9 and 10 were caught by hand-checking every row that would reach a person, **after** the unit tests passed. Bugs 14–22 came from an independent review, and every one was reproduced before it was fixed. Lesson: tests lock in the failures you already know about. Reading real rows and adversarial review find the next ones.
