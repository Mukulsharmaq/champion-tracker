# Architecture

```
 SOURCES (gtm_connectors read each API's response shape)   DECISIONS (code, versioned, tested)          DELIVERY (Slack)
 ────────────────────────────────────────────────────────   ──────────────────────────────────          ─────────────────────────
 Snowflake: won-deal role + product use, active admins ──► champion seed ─┐
   (no test / internal workspaces, no lost deals)                         ├─► champion_tracker.rules ──► Card / Review / Hold / Watch
 People data (API) or a Clay CSV export ──── candidates ──────────────────┘     identity → move → account → stop rule
   name + company first, then 50 deep; a full 50 with nobody tied               (rules 1.2; review-owner overrides applied)
   = every same-name profile pulled with work history, tied locally
                                                                                         │
 Gong: /v2/calls/extensive + /transcript + trackers ─┐                                   │
 Salesforce: Task notes (SOQL) ──────────────────────┼─► signals ─► gtm_briefs.extract ─► findings ─► brief ──────────────► sellers' canvas
 Slack: field-notes history + thread replies ────────┘   (one per      (lexicon 2.4;       (theme, who,   (this period          (brief + next steps)
                                                          speaker turn) trackers; optional  quote, link,   vs last)
                                                                        observer, checked)  confidence)
 Salesforce: Accounts (SOQL, CRM ids, owner ids, industry, size, country) ─────────┐
                                                                                    ▼
                                                     pg_surface.build_cards ◄──── champion moves in / out
                                                     which account · why now · who · context line ·
                                                     play (plays 1.2: P1-P12, first match wins) ──────► digest + thread replies (buttons)
                                                     · ICP fit · Ready / Check first · owner or routed  List (next steps)
                                                                                                        Clay table (same play rule)
 Hold / Review rows (candidates scored), old moves, QA sample, run health, metrics ─────────────────► review owner: queue + review canvas

 Slack click ──► pg_system.feedback_loop ──► log (versions, locked) ──► allowed? card owner / review owner ──► CRM write-back by record id
                                         └─► wrong person / play / account, QA Wrong ──► test case with the card's inputs
                                                                                      └─► replayed on every rule change
```

## Pieces

| Piece | What it does |
|---|---|
| `champion_tracker` | Identity, movement and account rules (rules 1.2); people-data adapters (REST API with the full same-name pull, a shared rate limit and retries; Clay CSV export; fixtures); the champion card; the feedback record and its lock |
| `gtm_briefs` | Lexicon 2.4 loaded from `contracts/lexicon.json` (or the file `LEXICON_PATH` names, such as `contracts/lexicon.travel-expense.json`); extraction per speaker turn with confidence; the brief (this period against the last); the optional observer (prompt 1.1, stamped on the findings only it found; its real API path has run on the evaluation sets, on `openai/gpt-5.6-luna` through OpenRouter) |
| `pg_surface` | Plays 1.2 loaded from `contracts/plays.json`; account cards (surface 1.3) with a context line and the optional ICP fit; Slack digest, thread replies, owner DMs, List rows, canvas, review queue |
| `gtm_connectors` | Gong, Salesforce, Slack field notes, Snowflake (read and run history), Slack delivery. Written against the official API docs and tested with fixtures the author generated in each API's response shape; none has run against a live tenant, though the real `slack_bolt` / `slack_sdk` and `snowflake-connector-python` libraries accept every call and keyword the code uses. Writes that are not idempotent are retried only on 429 |
| `pg_system` | The one-command pipeline (one run per `out/` folder, under a lock), preflight (`--check`), click handling, click memory (`state.py`), run health, metrics, the regression replay |
| `slack_app` | One Socket Mode process: buttons and forms, `/pipeline`, the weekly scheduler. Each pipeline run is a child process |

## Design choices
- **LLMs extract and propose; code decides.** On the public data, LLM agents extract names, titles and verbatim evidence and tag the action; the code decides the level from the tag (Champion for decision and ownership tags, User otherwise) and makes every later decision. In the brief, the optional observer proposes themes with quoted words, and the code keeps a theme only when it can verify the words. Identity, move, theme, play and priority are deterministic code with a written reason.
- **Unsure rows are held on purpose.** A wrong card costs the seller's trust. Uncertain identity, cut-off searches, accounts not in the CRM and rep-only reports carry an explicit check instead of a confident card, and Review moves older than a year go to the review owner instead of a seller.
- **Same parser for demo and live.** The demo data is stored in each API's response shape, so `--mode demo` runs the parsers `--mode live` runs. A round-trip test shows the API-shaped fixtures and the readable CSVs give the same answers; it does not show a real tenant behaves the same.
- **Contracts are the code.** `contracts/plays.json` (ordered rules) and `contracts/lexicon.json` are read at runtime; the Clay play formula is generated from the same rules; tests check the code and `contracts/*.yaml` agree.
- **Clicks are memory.** `pg_system/state.py` turns the feedback log into what the next run shows (snooze through the next weekly run, not now through the next two, dismiss and wrong account until new evidence, working, parked queue rows) and into identity overrides the rules apply (`rules.run(overrides=...)`). Only allowed clicks count.
- **Two owners.** Account-card clicks count from the card owner, decided by the Salesforce owner id (a name only when no other owner has it); routed cards go to `default_owner` or the review owner. Identity decisions (This is them, None of these, Paste the right profile, Can't resolve, Wrong person, QA labels) count only from the review owner; a seller's Wrong person is saved as a test case and waits for the review owner. Review-queue and QA buttons stay valid across runs while the row is in the latest run.
- **Delivery in place.** One canvas tab and one shared List per channel, edited every run; the review queue, the review canvas (shared through the review owner's DM channel) and the QA sample go to the review owner, not the sellers. Once the digest is posted, each later step is tried on its own, so a failed thread reply never breaks the digest's buttons.
- **Runs are child processes.** The Slack app starts each run as `python3 -m pg_system --mode live --post-slack`, so a contract or pipeline-code change counts from the next run without a restart, and a crash stays in the child.
- **Corrections become tests.** A wrong-person, wrong-play, wrong-account or QA "Wrong" click is stored with the inputs behind the card and replayed against every rule change (`--check-regressions`). A correction that names the right play or decision passes only when the rules give exactly that answer.
- **State survives a crash.** Every state file is written whole (temp file + rename); clicks and the retention purge share a lock, so no click is lost.
- **No keys or personal data in the repo.** Keys come from environment variables. Demo data is invented. Corrections with real names go to the git-ignored `feedback/`; a filled `config.json` is git-ignored too.

## Pipeline steps (`python3 -m pg_system`)
0. Take `out/.run.lock` (a second run exits); blank notes older than `retention_days` in the feedback log, the saved corrections and the write-back log.
1. Champions from the warehouse (`gtm_connectors/warehouse.py`; the demo runs the SQLite copy of the SQL), candidates from people data (`champion_tracker/adapters`: parallel, cached 8 days, rate-limited; a cut-off search gets the full same-name pull; a Clay export gives one profile per person).
2. Identity overrides from the feedback log (review owner only); pasted profiles join the lookup (fetched by URL and cached when `PEOPLE_PROFILE_URL` is set); parked rows are marked. Champion decisions (`champion_tracker.rules.run`); known customers come from the CRM's customer accounts.
3. Accounts and signals: Salesforce accounts (with owner id, industry, employees, country) and notes (never the system's own Tasks), Gong calls, Slack field notes, each signal with the Salesforce Account Id.
4. Gong tracker themes and the optional observer (checked by code; if it fails, the run goes on without it), then findings and the brief for this period against the last (`gtm_briefs`).
5. Account cards (`pg_surface.surface`): blocked companies get no card, Review moves older than `old_move_days` make no card, every card gets its context line and the optional ICP fit, cards with no owner are routed, earlier clicks applied (`state.apply`). Review queue (Review and Hold rows, minus old moves, candidates scored), QA sample, owner Slack ids by Salesforce owner id.
6. Run health against the previous run and metrics (`pg_system.monitor`, `pipeline.metrics`).
7. Outputs, replay inputs for every card and queue row, then delivery: a dry run writes `slack_requests.json`; `--post-slack` posts (refused in demo mode without `--allow-demo-post`). `replay/LATEST` moves when the digest reached Slack, even if a later step failed (or in a demo run without `--post-slack`). Old replay folders past `keep_runs` are removed; with `write_to_warehouse`, the run is written to Snowflake.
