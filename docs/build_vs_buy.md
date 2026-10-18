# Build or buy champion tracking?

The fair test: run **the same champion list** through this build and a vendor, then compare accuracy, missed moves, speed, upkeep and cost. Neither option removes the need to check the results.

| Measure | This build (measured on 195 people named in public case studies; 181 at customers, 129 Champion-level) | Vendor | How to measure |
|---|---|---|---|
| Identity accuracy (confirmed = right person) | 19/19 on hand-checked moves (small n); 20 Confirmed identities hand-checked in the page-1 test (earlier rules) | to measure | Hand-check every confirmed move from both, on the same list |
| Hold rate (honest "not sure") | 55/181 held (30 %), each with a reason; a Hold that turns out to be a real move counts as a miss | to measure | Count holds and how many each later confirms |
| Missed moves | Not yet measured (needs a ground truth) | to measure | Union of moves found by either; check each by hand; count what each missed |
| Freshness | Depends on the people-data refresh (profiles here: median 87 days old; 96 % within 120 days) | to measure | Days from a known move date to detection |
| Champion quality | Evidence sentence + level per person (Champion vs User) | usually "was a contact" | Share of alerts with proof of influence |
| Upkeep | Rules in code with 38 tests; aliases and thresholds in config | vendor-managed | Hours a month spent fixing matches |
| Cost | $0 marginal on the demo (lookup plan + free formulas) | license | Total cost per confirmed, worked move |

## When to buy
- You need broad coverage and real-time move detection across many contacts, and can accept the vendor's definition of a "champion".

## When to build
- "Champion" must mean **your** definition (closed-won contact role, active admin, positive call evidence), and the seller needs the evidence and the reason, not just an alert.
- Owning the rules (versioned, tested, explainable) matters more than coverage.

## Hybrid (likely best)
- Buy move detection as an input. Keep identity, champion definition, stop rules and the feedback loop in your own code. The vendor improves one input; it does not define the truth.
