# The same rule in Clay (free formula columns, 0 credits)

Built and checked in a real Clay table on 2026-10-18: 195 rows imported from CSV, 5 formula columns, `Rule check` = 195/195 match, credits used 0. The table holds the pipeline's snapshot from before the review fixes; the columns are the same as `champion_tracker`'s decisions CSV, minus `Row id` and `Rule version`.

## Build it without spending credits
1. New workbook → **Import from CSV** (the decisions CSV from `champion_tracker`).
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

**Route (Clay rule)**: the decision, re-computed inside Clay
```js
{{Level}} == "n/a" ? "Excluded" : ({{Same person?}} == "Hold" || !{{Same person?}}) ? "Hold" : {{Moved?}} == "No" ? ({{Level}} == "Champion" ? "Watch" : "No action") : {{Moved?}} != "Yes" ? "Hold" : {{Same person?}} == "Likely" ? "Hold" : {{Level}} != "Champion" ? "No action" : ({{Joined new company}} && moment().diff(moment({{Joined new company}}), "days") <= 90 && !(({{New account}} || "").includes("size unknown") || ({{New account}} || "").includes("; 1 to 10 staff") || ({{New account}} || "").includes("; 2 to 10 staff") || ({{New account}} || "").includes("; 11 to 50 staff") || ({{New account}} || "").includes("; 51 to 200 staff") || ({{New account}} || "").includes("no domain") || ({{Reason}} || "").includes("still open"))) ? "Card" : "Review"
```
Size bands are matched with their "; … staff" wrapper so that "5001 to 10000" never matches "1 to 10".

**Rule check**
```js
{{Route (Clay rule)}} == {{Decision}} ? "match" : "MISMATCH: pipeline " + {{Decision}} + " vs Clay " + {{Route (Clay rule)}}
```

**Rep card (preview)**: text only; nothing is sent
```js
({{Decision}} == "Card" || {{Decision}} == "Review") ? "Champion moved: " + {{Person}} + " (" + {{Title on page}} + ", " + {{Case study}} + ") is now " + {{Now}} + ", joined " + {{Joined new company}} + ". Evidence: " + {{Evidence (verbatim)}} + " | Play: " + {{Play}} + " | Check: " + {{Reason}} : ""
```

## In production
Clay's job-change tracking and company checks feed the candidate lists. These formulas (or the Python rules) decide. Under Clay's Actions & Data Credits pricing (2026), sending data to a third-party tool such as Slack counts as an action per record, so send only Card rows.
