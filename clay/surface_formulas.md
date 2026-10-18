# The pipeline surface in Clay (free formula columns, 0 credits)

`python3 -m pg_system` writes `out/clay_surface.csv`: one row per account with the play inputs, the play the code
chose, the contact, why now and the sources. Import it into Clay the same way as the champion table
([formulas.md](formulas.md)): **Import from CSV**, table **Auto-run off**, manual **Formula** columns only. No
enrichment, AI, HTTP or send columns, so no credits are used.

## Columns

`Open deal` is Yes when the CRM account has an open opportunity. `Type` is `customer` for a customer, and also for an
account whose parent is a customer when a champion joined it (that card is on the expansion side).

`In ICP` says whether the account fits `config.json -> icp` (industries to include or exclude, minimum and maximum
employees, countries): `Yes`, `No` (outside it: the code halved the card's priority and added a check that says why),
`Unknown` (a field the ICP uses is unknown for this account; unknown never fails the check) or `Not set` (no ICP
given). It moves `Priority` and `Tier`, never the play, so the play formula below does not read it. Industry, size
and country come from Salesforce `Industry`, `NumberOfEmployees` and `BillingCountry`, else from the moved champion's
profile for a company not in the CRM. To filter in Clay: `{{In ICP}} == "No"` lists the cards outside it.

**Play (Clay rule)**: generated from `contracts/plays.json` by `scripts/clay_formula.py --write` (first match wins; no match = no card)
```js
{{Type}} == "customer" && {{Near renewal}} == "Yes" && (Number({{Pricing}}) > 0 || Number({{Competitor risk}}) > 0) ? "P4" : {{Type}} == "customer" && Number({{Competitor risk}}) > 0 ? "P5" : {{Type}} == "customer" && Number({{Champion in}}) > 0 ? "P2" : {{Type}} == "customer" && Number({{Expansion}}) > 0 && {{Open deal}} == "Yes" ? "P12" : {{Type}} == "customer" && Number({{Expansion}}) > 0 ? "P6" : {{Type}} == "customer" && Number({{Champion out}}) > 0 ? "P9" : {{Type}} != "customer" && Number({{Champion in}}) > 0 && {{Open deal}} == "Yes" ? "P11" : {{Type}} != "customer" && Number({{Champion in}}) > 0 ? "P1" : {{Type}} != "customer" && Number({{Competitor risk}}) > 0 ? "P7" : {{Type}} != "customer" && Number({{Pricing}}) > 0 ? "P8" : Number({{Timing}}) > 0 ? "P10" : "no card"
```

**Play check**
```js
{{Play (Clay rule)}} == {{Play}} ? "match" : "MISMATCH: code says " + {{Play}}
```

**Renewal window**
```js
{{Renewal days}} === "" ? "" : Number({{Renewal days}}) <= 30 ? "Under 30 days" : Number({{Renewal days}}) <= 120 ? "Under 120 days" : "Later"
```

The Python rules stay the source of truth (tested); the Clay columns let RevOps see and filter the same decision
where they already work, and the check column proves the two agree row by row.
