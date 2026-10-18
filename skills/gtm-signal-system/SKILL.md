---
name: gtm-signal-system
description: Run the GTM signal system (champion moves, weekly GTM brief, pipeline surface in Slack) and answer questions from its outputs. Use when asked to run the weekly brief, post the pipeline digest, explain why an account is on the list, check the champion review queue, or replay seller corrections.
---

# GTM signal system

Repo root holds `src/`, `contracts/`, `fixtures/`, `tests/`. Always run from the repo root with `PYTHONPATH=src`. Versions: rules 1.2, lexicon 2.4, observer prompt 1.1, surface 1.3, plays 1.2.

## Run
- Demo (no keys): `PYTHONPATH=src python3 -m pg_system --out /tmp/gtm-demo`. Never run the demo or a dry run into the live `out/`. Expected demo result: Card 2 · Review 1 · Hold 6 · Watch 3 · No action 2, 10 cards (7 Ready), 21 dry-run Slack requests.
- Preflight before the first live run (reads only): `PYTHONPATH=src python3 -m pg_system --check`; report every source that fails and why (exit 1 when any fails), and every warning. With `config.json -> preflight_person` set it runs one real people lookup; the champion SQL is checked with `EXPLAIN`, never run.
- Live, dry run to files: `PYTHONPATH=src python3 -m pg_system --mode live --out /tmp/gtm-dry`. It leaves `replay/LATEST` alone, so the buttons already in Slack keep working.
- Live and post to Slack: in production the Slack app runs it (`/pipeline` in the pipeline channel, or the weekly schedule). Outside that, `PYTHONPATH=src python3 -m pg_system --mode live --post-slack --channel <channel id>`. Ask the user to confirm the channel before posting. Never post to a channel the user did not name. Without `--mode live` the command refuses to post (the demo invents accounts); `--allow-demo-post` exists only for a test channel the user names.
- If it prints "Not run: another pipeline run is using ..." (exit 3), a run already holds `out/.run.lock`: wait for it, do not delete the lock.
- If it prints "Not run: Live mode needs: ...", tell the user exactly which variables are missing (`.env.example`).
- Another competitor list: `LEXICON_PATH=contracts/lexicon.travel-expense.json` selects the real travel-and-expense list (no precision or recall figure for it).

## Read the outputs (in the `--out` folder, default `out/`)
- `brief.md`: what changed this period against the last, every item with speaker and source link; "Held" lists what did not count and why.
- `surface.json` / `canvas.md`: ranked accounts with why now, contact, play (P1 to P12 in `contracts/plays.json`, first match wins), the account context line, ICP fit (when `icp` is set), status and checks.
- `review_queue.csv` / `ops.md`: champion rows a person must look at (Hold and Review) with the reason and candidate profiles ranked by score; parked rows (Can't resolve); run health, old moves, metrics and the weekly QA sample.
- `summary.json`: counts, `alerts` (run health) and `metrics`. Report alerts first.
When asked "why is X on the list", quote the card's `headline`, `why_now` lines, `context`, `rank_reason` and links from `surface.json`; never add reasons the card does not contain. If an account is missing, check `canvas.md` -> "Held back by a seller this week" (snoozed or dismissed) and the old-moves table in `ops.md` before saying it had no signal.
When asked about a Hold, read its reason as written: "Search hit the 50-result cap" means the list was cut off and the full same-name pull did not run or did not finish; "A full search of N same-name profiles found none tied" means every same-name profile was read. Neither means "N people share this name". "N profiles are tied to X and disagree" means different LinkedIn members: two copies of one member (the same member id under an old and a new URL slug) are already merged, the freshest copy winning.

## Corrections
- `PYTHONPATH=src python3 -m pg_system --check-regressions` replays every saved correction against today's rules. Cases are read from `feedback/regression_cases.jsonl` (or `--regression-file <path>`, `$REGRESSION_FILE`, or `config.json -> regression_file`). It exits 1 when a case marked `fixed` fails again, else 0. A case that names the right play or decision passes only on that answer. When an open case now passes, tell the user and suggest flipping its `status` to `fixed`.
- Rule changes go in `src/` and `contracts/` together, then the full test line must pass (306 checks in 9 files):
  `python3 tests/test_rules.py && python3 tests/test_system.py && python3 tests/test_brief.py && python3 tests/test_brief_eval.py && python3 tests/test_surface.py && python3 tests/test_connectors.py && python3 tests/test_regressions.py && python3 tests/test_slack_app.py && python3 tests/test_ops.py`

## Do not
- Do not edit `out/` by hand or change a decision in the output; change the rule and rerun.
- Do not put keys in any file. They live in environment variables.
- Do not describe the brief's numbers as more than they are: they come from synthetic text, and competitor 1.00 is exact matching of three invented names. The observer's real-API numbers ran on `openai/gpt-5.6-luna` through OpenRouter, not on Claude Haiku.
