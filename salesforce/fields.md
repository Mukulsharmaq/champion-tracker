# Salesforce fields the write-back uses

Create these once (Setup → Object Manager), or ask your admin. `python3 -m pg_system --check` reports any missing **Account** field (and the renewal field); it does not check the Contact fields yet.
Until `config.json` says `"write_back": "live"`, nothing is written: every update and Task goes to `feedback/crm_write_back.jsonl`.
None of this has run against a live Salesforce org yet: the connector was written against the official API docs and tested with fixtures the author generated.

## Account (surface cards)
| API name | Type | Values / length |
|---|---|---|
| `Pipeline_Play__c` | Text | 10 (the play code, P1 to P12) |
| `Pipeline_Play_Status__c` | Picklist | Working, Snoozed, Dismissed, Account rejected, Play rejected |
| `Pipeline_Play_Note__c` | Long text area | 1,000 |
| `Pipeline_Surface_Version__c` | Text | 10 |

## Contact (champion rows: review-queue decisions and Wrong person)
| API name | Type | Values / length |
|---|---|---|
| `Champion_Move_Status__c` | Picklist | Working, Snoozed, Dismissed, Identity rejected, Identity confirmed, Play rejected |
| `Champion_Move_Note__c` | Long text area | 1,000 |
| `Champion_Move_Rule_Version__c` | Text | 10 |

"Identity rejected" and "Identity confirmed" exist only on the Contact picklist: identity is about the person, never the account. The Contact write-back matches on the row id, which is the CRM contact id when the champions come from the warehouse SQL; it never matches a contact by name.

## What each action writes
Only allowed clicks write (the card owner for account cards, the review owner for identity); anyone else's click is logged as a comment and writes nothing. Every write is by record id.

| Click | Who can make it count | Object | Fields written |
|---|---|---|---|
| Run this play | Card owner | Account | `Pipeline_Play__c` = the play code, `Pipeline_Play_Status__c` = Working, `Pipeline_Play_Note__c` = the note, `Pipeline_Surface_Version__c` = the card's surface version (1.3); and a Task (below) |
| Snooze 1 week, Not now | Card owner | Account | The same four fields, status Snoozed |
| Wrong account | Card owner | Account | The same four fields, status Account rejected (the note is what the form said) |
| Wrong play | Card owner | Account | The same four fields, status Play rejected |
| Wrong person (on an account card whose contact is a matched champion) | Review owner | Contact | `Champion_Move_Status__c` = Identity rejected, `Champion_Move_Note__c` = the note, `Champion_Move_Rule_Version__c` = the row's rule version (1.2) |
| This is #n (This is them) | Review owner | Contact | Status Identity confirmed |
| None of these | Review owner | Contact | Status Identity rejected |
| Paste the right profile | Review owner | Contact | Status Identity confirmed; the note is the pasted profile URL |
| Can't resolve, QA Looks right, QA Wrong | Review owner | Nothing | About the queue and the rules, not the contact |
| Champion card buttons: Work this account, Snooze, Dismiss, Wrong play (`champion_cards.json`, which the pipeline does not post) | Review owner | Contact | Status Working, Snoozed, Dismissed or Play rejected |

No account-card button writes Dismissed today; the value stays on the Account picklist for a team that adds a Dismiss button.

## Task (created by "Run this play")
Standard fields only: `WhatId` (the account), `Subject` ("[GTM Signal] " + the play code and name), `Description` (why now, what to do, the sources), `ActivityDate` (today + 7), `Status` (Not Started), `Priority` (High for High-tier cards, else Normal).
The system never reads its own Tasks back as rep notes: the notes query skips every Task whose subject starts with "[GTM Signal]", and, with `SF_INTEGRATION_USER_ID` set, every Task that user owns or created. Only Tasks dated up to today are read.

## Connected app (or external client app) for the client-credentials flow
OAuth scopes `api`; enable Client Credentials Flow; Run As = an API-only integration user with the permission set below.
Then set `SF_INSTANCE_URL` (your My Domain URL), `SF_CLIENT_ID`, `SF_CLIENT_SECRET`, and `SF_INTEGRATION_USER_ID` (that user's Id, 005...).
The token is cached by the long-running app and shared by its click threads under a lock; when Salesforce expires it (401) the connector drops only the token that failed, fetches a new one and retries once, so the session timeout policy needs no change. A 403 is a missing permission and is never retried. A PATCH or a Task create that times out or gets a 5xx is not retried (it may have gone through); a 429 is.

## Permission set for the integration user
Create one permission set (for example "GTM Signal System") and assign it to the Run As user. Least access the code needs:

| Object | Object access | Field access |
|---|---|---|
| Account | Read, Edit | Read: `Name`, `Website`, `Type`, `ParentId`, `OwnerId`, `Industry`, `NumberOfEmployees`, `BillingCountry`, your renewal field (`SF_RENEWAL_FIELD`). Read + Edit: the 4 `Pipeline_*` fields above |
| Contact | Read, Edit | Read + Edit: the 3 `Champion_Move_*` fields above |
| Opportunity | Read | `StageName`, `Type`, `CloseDate`, `IsClosed` (the open opportunity is read as a sub-query on Account) |
| Task | Read, Create | `Description`, `ActivityDate`, `AccountId`, `WhatId`, `TaskSubtype`, `Subject`, `Status`, `Priority`, `OwnerId`, `CreatedById` |
| User | Read | `Name`, `Email`, `Title` (account and Task owners; the owner's email finds their Slack id, and the owner's Id decides whose clicks count) |

System permission: **API Enabled**. Nothing else: the code never deletes, never creates accounts or contacts, and never changes owners.
If the org narrows the accounts read with `SF_ACCOUNT_WHERE` (for example on `Owner.UserRole.Name`), the user also needs read on the fields that condition uses.

## Reading only part of the org
`SF_ACCOUNT_WHERE` (optional) adds a SOQL condition to the accounts read, e.g. `Owner.UserRole.Name LIKE 'NA%'`.
The preflight (`--check`) reports how many accounts that reads with one `SELECT COUNT()`; it never pulls them. A signal whose Account Id names an account this filter left out is never joined to another account by name: it is listed in run health, and a champion row that carries such an id gets a card with the check "was not among the accounts read".
