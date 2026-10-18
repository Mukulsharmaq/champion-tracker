# The champion routing in Clay (free formula columns, 0 credits)

## What this check proves, and what it does not
- **Routing only.** The formula columns below re-compute the final routing (Card / Review / Hold / Watch / No action / Excluded) from columns the Python had already decided: `Same person?`, `Moved?`, `New account`, `Reason` (plus `Level` and `Joined new company`). The `Rule check` column compares Clay's routing with the code's row by row. That proves routing only. It does not show that identity matching is right: no Clay column matches names, compares companies, reads work history or detects side seats. That all happens in Python (`src/champion_tracker/rules.py`, `matching.py`).
- **Where it lives.** The 195-row table (CSV import + 5 formula columns, 0 credits used) was built on 2026-10-18 in a Clay workspace owned by another company, so it cannot be shared and its name is never shown. Rebuilding it in the candidate's own workspace is still open.
- **Which rules it holds.** The table holds a pre-review snapshot, made by the rules before 1.1. Today's rules (1.2) give, on the same 195 rows: Card 0 · Review 19 · Hold 56 · Watch 70 · No action 36 · Excluded 14 with the stored lookups, and Card 0 · Review 26 · Hold 32 · Watch 81 · No action 42 · Excluded 14 after the full same-name pull ([`docs/eval.md`](../docs/eval.md)). A rebuilt table should import a fresh `decisions.csv`.
- **In the live system** Clay is only a people-data source through a CSV export (`src/champion_tracker/adapters/clay_export.py`), one profile per person, so the namesake check cannot run on that path.

### Mirrors that are out of date against rules 1.2
| Area | Status in Clay | Rules 1.2 |
|---|---|---|
| Company matching | Not in Clay. The snapshot's `Same person?`, `Tied by` and `New account` columns were made by the old company matching | Only legal suffixes and trailing geography are dropped, and geography stays when dropping it would leave a generic or short head ("Air Canada" is not "Air India"); customer domains are learned from jobs under the customer's own name, and a website named for it counts only when it is the customer's own known domain |
| Side seats | Not in Clay. The snapshot's `Moved?` was made by the old side-seat rule | Judged on the title only; trustee, volunteer, mentor and ambassador only as the role itself; self-employment only when the company or title IS self-employment |
| Fractional or interim roles | Not mirrored: the Route formula sends a fresh, resolved move to Card | Review, with the check "fractional or interim role" |
| Cut-off searches | Not in Clay: the snapshot's `Same person?` came from the stored lookups | A cut-off search gets the full same-name pull; nobody tied after it = Hold ("A full search of N same-name profiles found none tied") |
| Two profiles of one LinkedIn member (the same member id under an old and a new URL slug) | Not in Clay: the snapshot's `Same person?` came from the stored lookups | One person; the freshest copy wins. Different members that disagree still hold |
| A profile that still names the customer after that job ended | The snapshot has `Moved?` = No, so the Route formula says Watch | Hold, current employer unknown (the code now gives `Moved?` = Unknown, which the Route formula already holds) |
| Size bands | The Route formula lists 1 to 10, 2 to 10, 11 to 50 and 51 to 200, and "size unknown" | Also "0 to 1"; every provider spelling ("11-50", "11 - 50 employees") is normalised before the check |
| Month-only dates | `moment()` reads "2026-07" as 1 July and counts days to the day the formula runs, not the run date | Aged from the latest possible day, counted to the run date; "fresh only because of the rounding" goes to Review |
| Holds added in 1.1 and 1.2 | Not mirrored: rows held for a future start date, product use more than 60 days after leaving, or a missing evidence sentence or link have `Same person?` = Confirmed and `Moved?` = Yes, so the Route formula would send them to Card or Review | Hold (product use within 60 days of leaving does not hold: a last expense report) |

Because `moment()` uses the day the formula runs, a row that was a Card on the run date turns into "Older move" in Clay once 90 days have passed; compare on the run date.

## Build it without spending credits
1. New workbook → **Import from CSV**: the decisions CSV from `python3 -m champion_tracker` (it has a `Case study` column; the pipeline's `decisions.csv` calls the same column `Champion at`, so rename it or edit the formulas).
2. Set table **Auto-run** to off. Formula columns still compute.
3. Add column → **Formula**. Skip the AI "formula generator" box and expand the manual **Formula** section underneath. Type the expressions below. Columns are referenced as `{{Column Name}}`.
4. Never add enrichment, AI, HTTP, signal or Slack/CRM send columns for this. Those cost actions or credits per row.

## Columns

**Days since joined (live)**
```js
{{Joined new company}} ? moment().diff(moment({{Joined new company}}), "days") : ""
```

**Fresh move? (90 days)**
```js
{{Moved?}} == "Yes" ? ({{Joined new company}} && moment().diff(moment({{Joined new company}}), "days") <= 90 ? "Fresh (90 days or less)" : "Older move") : ""
```

**Route (Clay rule)**: the decision, re-computed inside Clay from the Python's columns
```js
{{Level}} == "n/a" ? "Excluded" : ({{Same person?}} == "Hold" || !{{Same person?}}) ? "Hold" : {{Moved?}} == "No" ? ({{Level}} == "Champion" ? "Watch" : "No action") : {{Moved?}} != "Yes" ? "Hold" : {{Same person?}} == "Likely" ? "Hold" : {{Level}} != "Champion" ? "No action" : ({{Joined new company}} && moment().diff(moment({{Joined new company}}), "days") <= 90 && !(({{New account}} || "").includes("size unknown") || ({{New account}} || "").includes("; 1 to 10 staff") || ({{New account}} || "").includes("; 2 to 10 staff") || ({{New account}} || "").includes("; 11 to 50 staff") || ({{New account}} || "").includes("; 51 to 200 staff") || ({{New account}} || "").includes("no domain") || ({{Reason}} || "").includes("still open"))) ? "Card" : "Review"
```
Size bands are matched with their "; … staff" wrapper so that "5001 to 10000" never matches "1 to 10". To mirror rules 1.2, add `|| ({{New account}} || "").includes("; 0 to 1 staff")` and `|| ({{Reason}} || "").includes("fractional or interim")`.

**Rule check**
```js
{{Route (Clay rule)}} == {{Decision}} ? "match" : "MISMATCH: pipeline " + {{Decision}} + " vs Clay " + {{Route (Clay rule)}}
```

**Rep card (preview)**: text only; nothing is sent
```js
({{Decision}} == "Card" || {{Decision}} == "Review") ? "Champion moved: " + {{Person}} + " (" + {{Title on page}} + ", " + {{Case study}} + ") is now " + {{Now}} + ", joined " + {{Joined new company}} + ". Evidence: " + {{Evidence (verbatim)}} + " | Play: " + {{Play}} + " | Check: " + {{Reason}} : ""
```

## In production
A Clay enrichment table can feed the candidates as a CSV export (`people_source: clay_csv`), one profile per person. The Python rules decide; these columns only show the same routing where RevOps already works. Check Clay's current pricing before adding any send column: sending data to another tool can cost an action or credit per row.
