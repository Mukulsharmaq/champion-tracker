# Champion review queue (7 rows, owner: RevOps owner)

7 rows: 1 Review, 6 Hold · employer unknown 1 · identity only likely 1 · move over 90 days 1 · no profile 1 · records disagree 1 · rename or acquisition 1 · same name 1

Clear a row from the review message: This is #n, None of these, Paste the right profile, or Can't resolve (parks the row so it stops coming back).

| # | Decision | Level | Person | Was | Champion at | Moved to | Move age (days) | Reason | Candidates | Row id |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Review | Champion | [Leo Park](https://example.my.salesforce.com/lightning/r/Contact/003W002/view) | Director of Spend Management | Northwind Foods | Fabrikam | 159 | Confirmed champion move; check: move is 159 days old (over 90). | 1. Leo Park · VP Procurement @ Fabrikam · score 0.6 | 003W002 |
| 2 | Hold | Champion | [Morgan Ellis](https://example.my.salesforce.com/lightning/r/Contact/003W009/view) | AP Specialist | Pied Piper | Bachmanity | 641 | Moved, but identity is only Likely: the only Morgan Ellis on file, no record ties them to Pied Piper. | 1. Morgan Ellis · Accounts Payable Lead @ Bachmanity · score 0 | 003W009 |
| 3 | Hold | Champion | [Elena Cruz](https://example.my.salesforce.com/lightning/r/Contact/003W007/view) | Expense Systems Lead | Wayne Logistics |  |  | 'Gotham Group' job starts the same month as the Wayne Logistics job (2015-07): looks like a rename or acquisition, not a move. | 1. Elena Cruz · Expense Systems Lead @ Gotham Group · score 0.6 | 003W007 |
| 4 | Hold | Champion | [Grace Holt](https://example.my.salesforce.com/lightning/r/Contact/003W005/view) | Head of Travel | Umbrella Health |  |  | Records disagree: evidence says 'Head of Travel', profile says 'Office Coordinator', refreshed 981 days ago. | 1. Grace Holt · Office Coordinator @ Umbrella Health · score 0.4 | 003W005 |
| 5 | Hold | Champion | [Hugo Lind](https://example.my.salesforce.com/lightning/r/Contact/003W008/view) | Procurement Director | Hooli |  |  | Profile's latest job (Raviga) ended 2026-05-01; current employer unknown. | 1. Hugo Lind · Procurement Director @ Raviga · score 0.6 | 003W008 |
| 6 | Hold | Champion | [Sam Lee](https://example.my.salesforce.com/lightning/r/Contact/003W003/view) | VP Finance | Globex Media |  |  | 4 people share this name; none is tied to Globex Media. | 1. Samuel Lee · VP Finance @ Soylent · score 0.2; 2. Sam Lee · Chef @ Bistro 9 · score 0; 3. Sam Lee · Engineer @ Massive Dynamic · score 0; 4. Sam Lee · Teacher @ Springfield High · score 0 | 003W003 |
| 7 | Hold | Champion | [Zed Coe](https://example.my.salesforce.com/lightning/r/Contact/003W010/view) | Controller | Initrode |  |  | No profile found under this name. | none | 003W010 |

## Run health
- 1 field note(s) named no account and were skipped (listed in skipped_notes.json).
- 4 card owner(s) have no Slack id, so their clicks cannot change anything: Alex Kim, Jordan Blake, Riley Shaw, Taylor Brooks. Add them to config.json -> owner_slack_ids (by name or Salesforce owner id).

## Old moves (0): confirmed moves over 365 days old

Not sent one by one and on no seller's card: a move this old is rarely news. Clear them here when there is time.

None this run.

## Metrics
0 seller clicks · accepted n/a · wrong person n/a · wrong play n/a · wrong account n/a · snoozed n/a · queue age median 0 days (max 0) · time to detect median 47 days

## Weekly QA sample (5)
- Elena Cruz (Wayne Logistics): Hold · 'Gotham Group' job starts the same month as the Wayne Logistics job (2015-07): looks like a rename or acquisition, not a move.
- Hugo Lind (Hooli): Hold · Profile's latest job (Raviga) ended 2026-05-01; current employer unknown.
- Sam Lee (Globex Media): Hold · 4 people share this name; none is tied to Globex Media.
- Priya Nair (Initech): Watch · New 'current' role is a side seat (Advisory Board Member); Initech job still open. Not a move.
- Rita Shah (Stark Freight): Watch · Still at Stark Freight.
