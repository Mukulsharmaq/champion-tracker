# Build or buy champion tracking?

The fair test: run **the same champion list** through this build and a vendor, then compare accuracy, missed moves, speed, upkeep and cost. That vendor run has not been done yet. Neither option removes the need to check the results.

| Measure | This build (195 people named in public case studies; 181 at customers) | Vendor | How to measure |
|---|---|---|---|
| Identity accuracy (confirmed = right person) | 19/19 Review rows the right person with a real move, and 24 rows resolved by the full same-name pull checked against the role in the case study. Both checked by Claude in the session, not by a person (small n) | to measure | A person checks every confirmed move from both, on the same list |
| Hold rate (honest "not sure") | 32/181 held (18 %) after the full same-name pull, each with a reason; 56/181 (31 %) before it, when 37 searches came back cut off. A second retry of the 13 holds left from the pull found nothing more. A Hold that turns out to be a real move counts as a miss | to measure | Count holds and how many each later confirms |
| Missed moves | Not yet measured (needs a ground truth) | to measure | Union of moves found by either; check each by hand; count what each missed |
| Freshness | Depends on the people-data refresh; not measured here. Card 0 on the real rows: no confirmed champion moved within 90 days | to measure | Days from a known move date to detection (the build reports time to detect per run) |
| Champion quality | Evidence sentence or warehouse record + level per person (Champion vs User); about 19 of 38 real `owns_program` tags need re-tagging under contract v2 | to measure | Share of alerts with proof of influence |
| Upkeep | Rules in code with 71 rule tests (306 checks in all); aliases and thresholds in config and contracts | vendor-managed | Hours a month spent fixing matches |
| Cost | People-data plan + hosting. The full same-name pull for 37 people cost $0 cash on an unlimited plan (87,474 fair-use credits), and the second retry of the 13 rows still held used 8 more fair-use credits; the Clay formula columns used 0 credits; the optional observer is paid per call (12 calls on `openai/gpt-5.6-luna` cost $0.024) | license | Total cost per confirmed, worked move |

## When to buy
- You need broad coverage and fast move detection across many contacts, and can accept the vendor's definition of a "champion".

## When to build
- "Champion" must mean **your** definition (a won-deal role with recent product use, or an active admin who also holds such a role), and the seller needs the evidence and the reason, not just an alert.
- Owning the rules (versioned, tested, explainable) matters more than coverage.

## Hybrid (likely best)
- Buy move detection as an input. Keep identity, the champion definition, the stop rules and the feedback loop in your own code. The vendor improves one input; it does not define the truth.
- Note: a Clay export as the input gives one profile per person, so this build's namesake check cannot run on that path. A vendor feed that returns one "best match" has the same limit. A people API that filters only on the current employer cannot find a champion who left by searching for the old company; this build pulls every same-name profile with its work history instead.
