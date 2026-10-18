# Runbook

How to run the GTM Signal System in production, what to watch, and what to do when something breaks.

## What runs where

| Piece | What it is | How it starts |
|---|---|---|
| The Slack app | **One process.** Socket Mode (an outbound WebSocket, no public URL). It handles the buttons and forms, `/pipeline`, and a scheduler thread that ticks once a minute. | `PYTHONPATH=src python3 slack_app/app.py` from the checkout, or the Docker image (`CMD`) |
| Each pipeline run | **A child process** the app starts: `python3 -m pg_system --mode live --post-slack --out $PIPELINE_OUT [--channel C]`, run from the checkout with `PYTHONPATH=src`. One run at a time: `/pipeline` and the schedule share a lock in the app, and every run also holds `out/.run.lock`, so a run started by hand on the same `out/` stops at once ("Not run: another pipeline run is using ...", exit code 3). A run that goes past `PIPELINE_TIMEOUT_S` (default 3600) is stopped. | `/pipeline` in the pipeline channel, or the weekly `schedule` in `config.json` |
| Preflight, dry runs, regression replay | One-off commands you run by hand | See below |

**Why a child process:** a run reads `contracts/plays.json`, `contracts/lexicon.json` and the pipeline code fresh each time, so a rule change counts from the next run with no restart. A crash inside a run stays in the child, so the buttons keep working.

| You changed | Takes effect |
|---|---|
| `config.json` (admins, channel, schedule, `write_back`, owners) | The next click, command or scheduler tick. No restart. |
| `contracts/*`, pipeline code in `src/` | The next run. No restart. |
| `slack_app/app.py`, or code the clicks use (`pg_system/feedback_loop.py`, `pg_system/state.py`, `gtm_connectors/salesforce.py`, `gtm_connectors/slack_post.py`) | After an app restart (clicks are handled inside the app process). |
| Environment variables (tokens, `PIPELINE_MODE`) | After an app restart. |

**It runs from a checkout.** The code finds `config.json`, `contracts/` and `sql/` (and `fixtures/` for the demo) relative to the checkout it is in. `pip install ".[live]"` installs the packages, the live dependencies and the `gtm-signals` command; run them from the checkout (working directory = the repo, `PYTHONPATH=src`). A wheel does not contain `config.json`, `contracts/`, `sql/` or `slack_app/`: an installed copy somewhere else finds the first three through `GTM_HOME` (the folder that holds them), and the Slack app still runs from a checkout. The Docker image is a checkout at `/app`. The wheel (`gtm_signal_system-2.0.0-py3-none-any.whl`) was built in a fresh venv with setuptools 82 and installed with `pip install "<wheel>[live]"`; the real slack_bolt 1.30.0 / slack_sdk 3.45.0 and snowflake-connector-python 4.5.0 (Python 3.9) and 5.0.0 (Python 3.12) accept every call and keyword the code uses, and the Slack app imports and registers its listeners. `snowflake-connector-python` must be 3.8.1 or later (older 3.x silently ignored key-pair sign-in). The Docker image was **not built** (no Docker on the build machine; its installs were resolved offline for linux x86_64 and arm64): build and start it once on the server before relying on it.

### Environment

| Variable | Needed | Notes |
|---|---|---|
| `PIPELINE_MODE=live` | Yes, to post anything | Without it, `/pipeline` says no and the weekly run is skipped, with a DM to the review owner. Demo mode invents accounts and its buttons work, so it never goes to Slack. |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | Yes | The app-level token needs `connections:write`. |
| Source keys (`.env.example`) | For the sources switched on in `config.json -> sources` | The child runs inherit the app's environment. |
| `SF_INTEGRATION_USER_ID` | Recommended | The run-as user's Id (005...): every Task that user owns or created is kept out of the rep notes, on top of the "[GTM Signal]" subject the system's own Tasks carry. The preflight warns when it is missing. |
| `PEOPLE_RATE_PER_SEC`, `PEOPLE_DEEP_LIMIT`, `PEOPLE_EXPORT_URL`, `PEOPLE_PAST_COMPANY_FIELD` | No | People-data tuning: calls a second for every worker together (default 5); the most same-name profiles pulled when a 50-deep search comes back cut off (default 5000); a bulk export endpoint for that pull; a work-history filter field, only if your provider has one. |
| `PIPELINE_OUT` | Recommended | Where state lives (default `<checkout>/out`). Set it to a dedicated path on the server, for example `/srv/gtm/out`. |
| `PIPELINE_TIMEOUT_S` | No | Stop a run after this many seconds (default 3600). |
| `GTM_HOME` | Only for an installed wheel | The folder holding `config.json`, `contracts/` and `sql/` when the code runs from an installed wheel rather than a checkout. Not needed in a checkout or the Docker image. |

**Never point a demo at the live `out/`.** `python3 -m pg_system` with no `--out` writes to `./out`, the app's default too. A demo or dry run there moves `replay/LATEST`, so every button on the digest already in Slack starts saying "older run". Run demos and dry runs with `--out /tmp/<something>`. The Docker image ships without `fixtures/`, so the demo cannot run there at all.

**Use `/pipeline`, not `--post-slack` by hand, while the app runs.** The run lock stops a hand-started run on the same `out/` folder, but a hand-started run on another folder would post a second digest whose card buttons the app refuses as an older run. The command line also refuses `--post-slack` in demo mode unless `--allow-demo-post` is given (for a test channel only).

## Safety switches

| Switch | Where | Effect |
|---|---|---|
| `PIPELINE_MODE` | Environment | Anything except `live` = the app posts nothing (see above). |
| `--allow-demo-post` | Command line | Without it, `python3 -m pg_system --post-slack` in demo mode is refused (exit 2): the demo invents accounts and its buttons work. |
| Run lock | Built in | `out/.run.lock`: one run per `out/` folder. The file names the process that holds it. |
| `pipeline_admins` | `config.json` | Empty = `/pipeline` is off for everyone. Only the people listed can run it, and only in `config.json -> channel`. Whoever runs the server edits it (normally the review owner); it counts from the next command. |
| `write_back` | `config.json` | `dry_run` (default) = CRM updates and Tasks only go to `feedback/crm_write_back.jsonl`. `live` = Salesforce PATCH by record id plus a Task. |
| Slack Connect guard | Built in | A channel shared with another organisation is refused before anything is posted (`conversations.info`, which needs `channels:read` / `groups:read`). |

## What to watch

| Signal | Where | Healthy | Act when |
|---|---|---|---|
| Heartbeat | `out/heartbeat.json`: `at`, `at_epoch`, `ok`, `error`, `last_tick`, `last_run_date`, `last_result` | Rewritten every minute, `ok: true` | Older than 5 minutes = the app is down or stuck: restart it. `ok: false` = the scheduler cannot read `config.json`, or the Slack token failed its check at start-up (the app then exits with code 2); `error` says why. |
| Weekly run | `out/schedule_state.json`: `last_run_date`, `result`, `started_at`, `finished_at`, `refused_on` | `result` starts with `ok:` on the scheduled day | `failed: …` → next section. `running` for longer than the timeout = the app died mid-run. `refused_on` = today → `PIPELINE_MODE` is not `live`. |
| Run health | `out/summary.json` → `alerts` (and `out/history/<date>.json` per run) | No alerts, or ones you understand | A silent source, failed or capped lookups, calls with no CRM account, an observer that failed, a Slack step that failed after the digest, a jump in a hold reason: read the review owner's ops message. |
| Review queue | The review owner's ops message (DM): the queue, candidates ranked by score, and run health | The queue is cleared every week | The queue grows week after week. A row nobody can resolve: Can't resolve parks it (it stays on the review canvas, without buttons). |
| CRM writes | Dry run: `feedback/crm_write_back.jsonl`. Live: the click lines in `feedback/feedback.jsonl` (the app does not log live PATCHes yet) | One line per allowed click | A click message in Slack says "CRM not updated: …". |
| Docker | `docker inspect --format '{{.State.Health.Status}}' gtm-signals` | `healthy` | `unhealthy` = the heartbeat is stale or not ok. |

An outside monitor (cron on another box, an uptime tool) can run the same check as the Docker health check:

```bash
python3 -c "import json,sys,time; h=json.load(open('/srv/gtm/out/heartbeat.json')); sys.exit(0 if h.get('ok') and time.time()-h['at_epoch']<300 else 1)"
```

## When the weekly run fails

1. **The review owner gets a DM** with the error and the last lines the run printed. If the DM says the run was *skipped*, `PIPELINE_MODE` is not `live`: set it and restart the app, and it catches up once that day.
2. **Preflight on the app server, with the same environment:** `PYTHONPATH=src python3 -m pg_system --check` (Docker: `docker exec gtm-signals python3 -m pg_system --check`). It tests every switched-on source with one small read and writes nothing. Fix what it names.
3. **Look at the channel first.** Once the digest is posted, each later step (thread replies, owner DMs, the review canvas, the queue, the QA sample) is tried on its own: a failure there does not fail the run, it shows in run health, and the digest's buttons work. A run that failed before the digest posted nothing new. A re-run posts a new digest; the canvas and the List are edited in place (their ids are in `out/slack_state.json`), so they are never duplicated.
4. **Re-run with `/pipeline`** in the pipeline channel (people in `pipeline_admins`). The schedule does not retry the same day: it records the date before it starts, so a restart can never post twice.
5. **The app was down all of the scheduled day?** There is no catch-up the next day (a digest on the wrong day surprises sellers). The stale heartbeat should have told you; run `/pipeline`.

## Where state lives

| Path (under `PIPELINE_OUT` unless shown) | What | If it is lost |
|---|---|---|
| `replay/LATEST`, `replay/<run id>/` | The inputs behind every button. Only the latest run's buttons are accepted. | Every button in Slack says "older run" until the next run. |
| `slack_state.json` | Canvas id, List id, the List row id per account, the review canvas id and whether it is shared yet | The next run creates a second canvas and a second List. |
| `.run.lock` | Held while a run is going (the pid and start time are written in it) | Nothing: it is taken again by the next run. |
| `history/<date>.json`, `summary.json` | Counts, alerts and metrics per run; run health compares with the last one | One run's alerts compare against nothing. |
| `history/cards-<run id>.json`, `history/first_seen.json`, `history/queue_first_seen.json` | Every run's cards (never pruned: `scripts/backtest.py` reads them); first sightings for time to detect and queue age | The backtest loses those runs; time to detect and queue age start again. |
| `people_cache.jsonl` | Cached people-data lookups and pasted profiles fetched by URL (live mode, API people source; entries older than `people_cache_days`, default 8, are dropped on each run; failed lookups are never cached) | The next run asks the people API again (cost and time). |
| `owner_slack_ids.json` | CRM owner → Slack id, found by email | Found again on the next live run. |
| `schedule_state.json`, `heartbeat.json` | The app's scheduler memory and heartbeat | Losing `schedule_state.json` on a scheduled day after the hour = one extra run. |
| `feedback/feedback.jsonl` (checkout) | Every click. The next run reads it: snoozes, dismissals, "working since", identity decisions, parked rows. Appends and the purge take `feedback.jsonl.lock`. | **The system forgets every decision people made.** Back it up with `out/`. |
| `feedback/regression_cases.jsonl` (checkout) | Corrections saved as test cases, with real names (git-ignored) | The corrections stop being replayed on rule changes. |
| `feedback/crm_write_back.jsonl` (checkout) | Every CRM update and Task the dry run would have made, with the record id. Live writes are not logged here yet. | The record of what would have been written. |
| `config.json` (checkout) | Names, ids and switches (no keys); not in git | Rebuild from `config.example.json`. |
| Keys | Environment / your secret store. Never in the repo or the image. | |

## Retention

| Setting (`config.json`) | What it does | Default |
|---|---|---|
| `keep_runs` | `replay/<run id>/` folders kept; older ones are deleted at the end of each run (never the run whose buttons are in Slack). Corrections carry their own copy of the inputs, so nothing a correction needs is lost. | 52 |
| `retention_days` | `pg_system.state.purge()` blanks the free-text notes on entries older than this in the feedback log, the saved corrections and the CRM write-back log, at the start of every pipeline run, so a seller's words about a named person are not kept for ever. The action itself stays (overrides and metrics need it). Each file is rewritten whole under its lock, so a click saved meanwhile waits and is kept. | 365 |

The pipeline calls `purge()` at the start of every run. To blank old notes between runs, run it by hand:

```bash
PYTHONPATH=src python3 -c "from pg_system.state import purge; print(purge('feedback/feedback.jsonl', 365, also=['feedback/regression_cases.jsonl', 'feedback/crm_write_back.jsonl']), 'notes blanked')"
```

Not pruned automatically (only their notes are blanked): `history/` (two small files per run; the backtest needs the `cards-` files), `feedback/regression_cases.jsonl` (mark fixed cases `"status": "fixed"` and archive them), `feedback/crm_write_back.jsonl`.

## Moving state to a new machine

1. **Stop the old app and keep it stopped.** Two app processes means two schedulers (two digests) and Slack splitting the clicks between them.
2. Check out **the same commit** on the new machine and run `pip install ".[live]"`.
3. Copy the state, keeping the paths: `rsync -a out/ new-host:/srv/gtm/out/` and `rsync -a feedback/ config.json new-host:/srv/gtm-signal-system/`. Keys go through your secret store into the new machine's environment, never by chat or email. With Docker, copy the `gtm-out` and `gtm-feedback` volumes (or bind-mount host folders and rsync those).
4. On the new machine: `PYTHONPATH=src python3 -m pg_system --check` passes, then start the app.
5. Confirm: `heartbeat.json` updates within a minute; `replay/LATEST` matches the old machine; a click on the latest digest is accepted (not "older run").

## Rolling back a rule change

Definitions, plays, the lexicons and the code are versioned in git, and the versions are stamped: rules (1.2) on every decision row and champion click, surface (1.3) and plays (1.2) on every card, surface (1.3) on every card click, the lexicon (2.4) on every finding, and the observer prompt (1.1) on findings only the observer found (`feedback/feedback.jsonl`; and in Snowflake by run id when `write_to_warehouse` is on). So you can see which version made a card and which clicks it got.

1. Revert on the server checkout: `git revert <commit>` (or `git checkout <good tag> -- contracts/plays.json`).
2. Contracts and pipeline code count from the next run. If this week's digest was wrong, re-run with `/pipeline`. If the change touched click handling (`slack_app/`, `feedback_loop.py`, `state.py`), restart the app.
3. Replay the saved corrections on the reverted rules: `PYTHONPATH=src python3 -m pg_system --check-regressions`, and run `python3 tests/test_regressions.py`.
4. Wrong CRM writes: in live mode the app does not keep a log of the PATCHes yet. Find the clicks in `feedback/feedback.jsonl` (account, play, action, version, time) and undo the fields (`salesforce/fields.md`) and Tasks by hand or with a data loader. In dry run, `feedback/crm_write_back.jsonl` has the exact fields.

## First-week checklist

| # | Step | Done when |
|---|---|---|
| 1 | Create the Slack app from `slack_app/manifest.yaml`; add an app-level token with `connections:write`; invite the bot to the digest channel and the field-notes channel. | The bot is in both channels. |
| 2 | `config.json`: `channel`, `field_notes_channel`, `review_owner.slack_id`, `pipeline_admins` (two people), `schedule`, `"write_back": "dry_run"`, `preflight_person` (someone you know is in the people provider), `owner_slack_ids` keyed by Salesforce owner id, and `icp` if you want the fit check. | |
| 3 | Create the Salesforce fields and the permission set (`salesforce/fields.md`); set `SF_INTEGRATION_USER_ID`. Then preflight: `PYTHONPATH=src python3 -m pg_system --check` (it checks the Account fields, runs one real lookup of `preflight_person` and checks the champion SQL with `EXPLAIN` without running it) | `"ok": true`, and the warnings read |
| 4 | Dry run into a separate folder: `PYTHONPATH=src python3 -m pg_system --mode live --out /tmp/gtm-dry` (files only). Read `canvas.md`, `ops.md`, `summary.json` → alerts, and `slack_requests.json`. | The cards and the queue make sense to the review owner. |
| 5 | Start the app with `PIPELINE_MODE=live`. | `out/heartbeat.json` appears within a minute. |
| 6 | `/pipeline` from an admin in the channel. | Digest, canvas tab, List and the ops DM all arrive. |
| 7 | The review owner clears the queue (This is #n / None of these / Paste the right profile / Can't resolve). A seller clicks Run this play on their own account; someone else's click gets a private note. | The cards close with who did what. |
| 8 | Read `feedback/crm_write_back.jsonl`. | The fields and Tasks are what you would want in Salesforce. |
| 9 | Watch the first scheduled run. | `schedule_state.json` → `result: ok…`; the channel got exactly one digest. |
| 10 | **Last:** set `"write_back": "live"`. It counts from the next click. | After a week of dry-run logs that look right. |
