# Failures found and fixed while building this

Each one broke a version of this on real data or in review, in three rounds: the first build (#1-#52), the red-team round and round 3 (both 2026-10-18). Most are now tests (`tests/test_rules.py` for the champion rules, the other test files for the rest; 306 checks in 9 files). Two are not: #13 (extraction recall) is a scored check outside the repo, and #43 (Snowflake key-pair sign-in) is a code path with no test that has not run against Snowflake (the real snowflake-connector-python 4.5.0 and 5.0.0 accepted its calls and keywords when the package was built). The "re-check confirmed people" part of #12 is a process step, not code. People's names in the examples below are invented.

"Review agents" below are Claude agents run in the same session as the build, not outside reviewers.

## Found on real data
| # | What broke | How it showed up | Fix (rule) | Caught by |
|---|---|---|---|---|
| 1 | Same name, different person | One name returned 5 profiles: a paralegal, a retail worker, a teacher… Only one worked at the customer. | A profile counts only if tied to the old company (employer, work history, headline). Several namesakes with no tie → Hold. | Page-1 test |
| 2 | Name search matched on substrings | A short name (like "Jo de Vries") returned a different, longer one (like "Joanna Devrieson") | Exact last name + first name equal or a 3+ letter prefix (Rob matches Robert). Added later (rules 1.1): listed nicknames, family-name-first order, honorifics and the seed's name variants; rules 1.2 add accents, compound surnames and transliterations (round 3) | Full-run row check |
| 3 | A board seat recorded as the new job | "Chair, regional board" (freelance) became the current employer while the real job was still open | A side seat while the old job is open is not a move | Page-1 test |
| 4 | Stale profile contradicts the evidence | Profile showed a junior admin title, last refreshed 2+ years ago; the evidence said head of the program | A stale profile (> 365 days) with a contradicting title → Hold: records disagree | Page-1 test |
| 5 | Acquired company's domain | Email on an acquired brand's domain; the person is still at the parent | Parent/acquired names are listed as aliases in config, never guessed | Page-1 test |
| 6 | Company matched by name to the wrong firm | A lookup by a short company name returned a different company with a longer name | Companies are compared as whole normalised names (since rules 1.1, only legal suffixes and trailing geography are dropped; since 1.2, never down to a generic or short head); domains are preferred | Page-1 test |
| 7 | A lagging field kept the old employer | The org/email domain still showed the old company after the person moved | Fields that lag behind a move are never used for identity or movement | Full run |
| 8 | A job that had already ended was treated as current | The "current" employer's job had an end date | If the latest job ended → current employer unknown → Hold | Full-run row check |
| 9 | An acquisition was treated as a move | The "new" employer's start date equals the old job's start month (renamed or acquired company) | Same start month → Hold: looks like a rename or acquisition | Full-run row check |
| 10 | A self-employed side gig was treated as a move | The current entry was "self-employed content creator" while the job at the customer (under its parent's name) was still open | A self-employed/freelance seat while the customer job is open is not a move; parent names count as the customer. Since rules 1.1, self-employment only when the company or title IS self-employment ("Independent Bank" is a bank) | Full-run row check |
| 11 | Move age measured from the wrong date | Left the customer 4 years ago but joined the current company 9 months ago | Move age = days since joining the new company (the "why now") | Full run |
| 12 | Search depth silently held common names | A 10-deep name search missed the right person for common names, including people already confirmed | Match by name + company first, then a 50-deep search for misses; re-check confirmed people after any lookup change (a process step). The 50-deep search was itself cut off for most of these: see the red-team round (capped search) and round 3 (the full same-name pull) | Full run |
| 13 | Extractor recall | Regex extraction found 13/27 named people | Agents read every story; scored against an answer key Claude made by reading 15 stories (27/27) before the full run. A scored check outside the repo, not a test | Page-1 test |

## Found by a four-lens review (review agents in the same session; each reproduced, then fixed)
| # | What broke | Fix |
|---|---|---|
| 14 | A board seat or freelance gig became a Card once the old job had an end date | A side seat is never a real move: Hold (current employer unknown) |
| 15 | A common word in a headline ("Head of Travel at …") tied a stranger to a customer named "Travel …" | A headline ties only on the whole company name, or "ex-/formerly/@" + the name |
| 16 | A name match beat a different domain ("Hooli Realty" counted as customer "Hooli") | Domain decides when there is one; the name is used only without a domain |
| 17 | Two profiles of one person on the same domain (renamed company) were held as "disagreeing" | Duplicates are grouped by domain first |
| 18 | A returning employee was aged from their first stint years ago; then an internal promotion looked like a fresh move | Age = start of continuous tenure: a promotion extends it, a real gap resets it |
| 19 | One malformed date ("2026-09", "unknown") or a null list crashed the whole run | Tolerant date parsing; missing lists treated as empty |
| 20 | A champion row without its evidence sentence or source link could still reach a seller | Hold until both exist |
| 21 | Feedback and CRM write-back were keyed on the person's name | Every row has a stable row id; write-back matches on it, never on a name |
| 22 | Live mode returned provider field names the rules don't read, and a bad token looked like "no profile" | Provider keys are mapped once; auth/provider failures become "Lookup failed; retry" |

Bugs 2, 8, 9 and 10 were caught by reading every row that would reach a person, **after** the unit tests passed. Bugs 14-22 came from review agents in the same session, and every one was reproduced before it was fixed. Lesson: tests lock in the failures you already know about. Reading real rows and adversarial review find the next ones.

## Found while building the brief and the pipeline surface (each is a test in `tests/test_system.py` or `tests/test_connectors.py`)
| # | What broke | Fix |
|---|---|---|
| 23 | "We spend a lot of time on approvals" counted as a mention of the competitor Spendwise | Competitors match whole words only |
| 24 | "We export everything to the general ledger" counted as Ledgerly | Same whole-word rule; the trap is in the demo data |
| 25 | "Honestly, the price was never the issue" counted as pricing friction | A negated mention is held, with the reason |
| 26 | A rep's guess ("I think they'll say our price is too high") counted as a customer signal | Our own speculation is excluded; rep reports count only when they report what the customer said, and are labelled |
| 27 | One quote that touched two themes appeared twice on the same card | Why-now lines are grouped per source signal, with all its themes on one line |
| 28 | A champion who left 159 days ago showed up as "champion left" news at the old account | Only fresh moves count at the old account; old moves surface at the new account with a check |
| 29 | A field note naming two companies ("Contoso: Dana Reyes (ex-Acme) liked our post") could land on the wrong account | The account prefix wins; otherwise the longest whole-name match (never a name that is an ordinary word); no match = skipped and listed |
| 30 | A rep's CRM note ranked above the customer's own words on the card | The customer's own words come first, then our team's reports |
| 31 | Call small talk ("Thanks for making the time today") could leak into the brief | Every turn is parsed with its speaker side; turns with no theme never count (the demo calls carry filler on purpose) |
| 32 | Test-workspace admins, the vendor's own staff and contacts on lost deals would count as champions | The champion SQL drops test workspaces, internal users and lost deals; the demo warehouse carries all three traps |

## Found by three review agents in the same session (code, live APIs, the hiring manager's eye); each reproduced, fixed and tested except #43
| # | What broke | Fix |
|---|---|---|
| 33 | The brief's lexicon matched the demo's own sentences ("sharper renewal number", "same setup") and would miss how real buyers talk | Rewritten as general patterns in `contracts/lexicon.json`. Measured on a synthetic set of 160 texts written and labelled by one LLM agent from a spec the rule author wrote, and later on a 200-text set written by one agent and labelled by another (`docs/eval.md`), plus an optional model observer whose proposals count only when the code can verify the quoted words |
| 34 | The "hardest failure" fix (name + company first, then 50 deep) existed in the demo run but not in the live adapter | `people_data.lookup_record` now matches on name + company first and searches 50 deep only for misses (and, since round 3, runs the full same-name pull when that search comes back cut off); tested |
| 35 | Two CRM records on one domain produced two cards with one id: Slack rejects the message and a click could write to the wrong record | Card id and grouping come from the CRM record id |
| 36 | "Acme Robotics Germany" (prospect) landed on "Acme Robotics" (customer, renewing) and fired a renewal save | Exact name first; a looser match counts only when exactly one account fits |
| 37 | A click on last week's digest still wrote last week's play to the CRM | Every button carries a run id; only the latest run's cards act |
| 38 | Anyone's click removed the buttons for everyone, so the owner could no longer act | Only an allowed click closes the card; others get a private note |
| 39 | Snooze, Not now and Dismiss changed nothing on the next run | The feedback log is the memory: snooze 7 days, not now 14 (round 3 added a day so each lasts through the weekly run), dismiss and wrong account until new evidence, working cards marked |
| 40 | "Wrong person" never reached the rules | The review owner's identity decision is applied on every later run (`rules.run(overrides=...)`), and the review owner can confirm a namesake in one click. A seller's Wrong person is saved as a test case and waits for the review owner |
| 41 | The live Salesforce notes query filtered on `Description`, a long text field Salesforce cannot filter | Filter removed; empty notes skipped in code; logged emails excluded by `TaskSubtype` |
| 42 | A fixed Salesforce session token would expire before the weekly run | Client-credentials flow: each run (a child process) fetches a token; the long-running app refreshes it on a 401 and retries once |
| 43 | Snowflake password sign-in is being retired for service users | Key-pair sign-in supported (and the passphrase parameter name follows the connector version); the live extra needs snowflake-connector-python 3.8.1 or later, since older 3.x silently ignored key-pair sign-in. No test; not run against Snowflake |
| 44 | A contact on two won deals came back twice from the champion SQL | One row per contact (latest won deal); a NULL test/internal flag counts as real usage |
| 45 | Our rep's own words on a call ("our pricing is per seat") counted as a reported signal | On calls, only the customer's words count, or our rep repeating them ("to recap, your CFO wants…") |
| 46 | A curly apostrophe ("They’ll say…") turned our own guess into evidence | Quotes normalised before any rule runs |
| 47 | Gong "Unknown" speakers (dial-ins) were treated as our team | Placed by email domain against the account's domain; otherwise counted and labelled "speaker not identified" |
| 48 | The canvas and the List were created as the bot's private files, a new pair every week | The canvas is a tab in the channel and is replaced in place; the List is shared to the channel and its rows are updated in place |
| 49 | A rep's CRM note repeating a customer's quote was counted twice in the brief | Counted once; shown as corroboration |
| 50 | Cards led with whatever came first ("Champion left") even when the play was triggered by something else | The signal that triggered the play comes first, and each card says what it was ranked on |
| 51 | A card with no one to contact was marked Ready | "Check first: no one to contact yet" |
| 52 | The sellers' canvas carried the identity review queue and run health | Sellers get the brief and next steps; the queue and health go to the review owner |

## The red-team round (2026-10-18, a 40-item critique by review agents in the same session)

Each item was reproduced, then fixed in code. The test that covers it is named where there is one.

### Champion tracker (rules 1.1 at the time)
| What broke | Fix | Test |
|---|---|---|
| The 50-deep search was still cut off for 34 of the 40 "N people share this name" holds, so that reason was a false statement of fact | A search that returns exactly 50 is `capped`; an untied result is held as "Search hit the 50-result cap"; run health counts capped searches; the reviewer can paste the right profile. This round also merged in one company-filtered search, which could only find people still at the customer; round 3 replaced it with the full same-name pull | `test_capped_search_runs_one_full_same_name_pull_and_keeps_only_tied_profiles`, `test_capped_lookup_with_no_tie_is_held_with_the_cap_reason`, `test_short_search_is_not_capped` |
| Company names matched too loosely or too strictly ("Acme USA, LLC" vs "Acme North America Inc"; "Delta" vs "Delta Dental") | Only legal suffixes and trailing geography are dropped; generic descriptors only after a name of two or more words; a website named for the company ties it | `test_different_companies_never_merge`, `test_still_at_the_customer_under_its_legal_name_by_its_own_domain` |
| A namesake at a lookalike company taught the customer's domain | Customer domains are learned only from jobs whose company IS the customer (or a listed alias), or a website named for it | `test_a_namesake_at_a_lookalike_company_never_teaches_the_customers_domain` |
| A namesake's old internship at the customer confirmed a recent champion | With an Evidence date on the seed row, a work-history tie counts only if that job overlaps it (365-day grace) | `test_evidence_date_rules_out_a_namesakes_old_internship` |
| Names: nicknames, family-name-first order, honorifics, maiden names, non-Latin scripts, and full names split on the first space | Nickname table, both orders for two-word names, honorifics and suffixes ignored, the seed's Name variants, exact match for non-Latin names; the seed uses CRM first and last names | `test_nicknames_reversed_order_honorifics_variants_and_non_latin_names`, `test_seed_uses_crm_first_and_last_names_and_parses_full_names_safely`, `test_review_queue_candidates_use_the_seed_name_variants` |
| Side seats: "Director, Advisory Services" or "Financial Advisor" read as side seats; "Independent Bank" as self-employment | Side seats judged on the title only (board, advisory board, advisor without a job word, trustee, volunteer, mentor, ambassador); self-employment only when the company or title IS self-employment | `test_real_jobs_are_not_side_seats_but_board_and_advisor_seats_are` |
| Month-only start dates ("2026-07", or a provider padding it to the 1st) were aged from the 1st | Aged from the latest possible day; fresh only by that rounding = Review with the check "start date known only to the month"; the provider's padding is undone; a future start = Hold | `test_month_only_start_is_aged_from_the_end_of_its_month`, `test_live_adapter_restores_month_precision`, `test_future_start_date_is_held` |
| Product use at the old customer after the profile's leaving date was ignored | Records disagree: Hold | `test_product_activity_after_leaving_is_held` |
| Size bands in other spellings ("11-50", "11 - 50 employees") escaped the small-company check | One spelling for every band; the small bands are 0 to 1, 1 to 10, 2 to 10, 11 to 50 and 51 to 200 | `test_clay_style_size_bands_count_as_small` |
| The champion SQL treated any won-deal role as a champion | A won-deal role (deal closed in the last 3 years) counts only with product use in the last 365 days; otherwise a user ("role only, no product use"). The demo carries Owen Marsh, a won-deal role with no product use: No action | `test_a_won_deal_role_is_a_champion_only_with_recent_product_use`, `test_both_champion_sql_files_say_the_same_thing`, `test_owen_the_role_only_mover_gets_no_card` |
| The contract said two things about `owns_program` (title or sentence) | A title alone never makes a champion, for any tag | `test_owns_program_contract_says_one_thing` |
| Fixture contradictions: demo movers kept using the product after they left | Fixtures rebuilt so nobody's product use is dated after they left | `test_demo_movers_stop_using_the_product_before_they_leave_and_levels_hold` |

### GTM brief (lexicon 2.2 and 2.3 at the time, observer prompt 1.1)
| What broke | Fix | Test |
|---|---|---|
| The customer's own travel spend ("cut travel spend", "hotel rates in London") counted as pricing friction | Pricing needs a price word and a friction cue within 6 words in one sentence, and is never about their own travel spend unless it points at us | `test_their_travel_spend_is_not_pricing_friction`, `test_travel_words_still_count_when_the_pushback_points_at_us`, `test_price_word_and_friction_cue_must_be_close_together` |
| Timing and expansion false hits: a month alone, an expense deadline, a hiring freeze, past expansion | Timing needs a purchase word next to the date; past expansion with "no plans for more" is history | `test_a_month_alone_is_not_a_timing_window`, `test_timing_needs_a_purchase_word_next_to_the_date`, `test_past_expansion_with_no_plans_for_more_is_not_a_request` |
| Competitor names that are everyday words ("ramp up", "I concur") | `competitor_rules`: case-sensitive names and excluded phrases | `test_competitor_names_that_are_everyday_words_follow_competitor_rules` |
| The observer could add a theme from evidence that did not name it ("the product" as pricing evidence), and was sent turns the code would never count | Evidence must hold a cue word for its theme and not match its refusal pattern; only counted turns are sent; turns past the cap and unreadable replies are reported | `test_observer_evidence_must_contain_a_cue_for_that_theme`, `test_observer_sends_only_turns_the_code_would_count_and_reports_stats`, `test_observer_counts_turns_past_the_cap_and_malformed_replies` |
| A quote containing `<!channel>` or `<@U…>` would ping people from the digest, canvas or queue | Every source text is neutralised to plain words | `test_neutralize_turns_slack_mentions_into_plain_words`, `test_quotes_cannot_ping_from_the_canvas_the_digest_or_the_ops_queue` |

### Pipeline surface and clicks
| What broke | Fix | Test |
|---|---|---|
| Sources joined accounts by spelling | CRM ids end to end: every signal carries the Salesforce Account Id (15- and 18-character forms compared); cards, write-back and the backtest key on the record id | `test_every_source_names_the_account_by_its_salesforce_id`, `test_match_account_order_is_crm_id_then_domain_then_name` |
| A champion who joined a competitor or partner got a "congratulate them" card | `blocked` domains and names: no card, and run health says so | `test_champion_who_joined_a_competitor_gets_no_card_and_a_note`, `test_blocked_moves_get_no_card_and_reach_run_health` |
| A champion who joined a customer's subsidiary read as a new logo; a sister domain merged into the customer | Parent is a customer → expansion side (P2); a sister domain not in the CRM is flagged with a check, not merged | `test_champion_who_joined_a_customer_subsidiary_is_expansion_side`, `test_sister_domain_not_in_the_crm_is_flagged_not_merged_into_the_customer` |
| Open deals were ignored: a seller could cold-pitch a champion inside a live deal, or open a second opportunity | P11 (champion inside an open deal: brief the deal owner) and P12 (expansion already in the pipeline) sit before the plays they refine | `test_open_deal_changes_the_play_and_shows_as_evidence`, `test_new_plays_sit_before_the_plays_they_refine_and_old_order_is_kept` |
| Cards with no account owner had nobody whose click counted | Routed to `default_owner` (or the review owner), with a check; owners matched by name or Salesforce owner id | `test_card_with_no_account_owner_is_routed_with_a_check`, `test_a_routed_card_belongs_to_the_person_it_was_routed_to` |
| Only the top 6 cards had buttons | Every card past the top 6 comes as a thread reply with its own buttons | `test_cards_below_the_top_six_come_as_thread_replies_with_their_buttons` |
| A reviewer who found the right profile could not use it | Paste the right profile on every queue row; the URL is checked and normalised, joins the lookup and is confirmed on every later run | `test_pasted_profile_urls_are_checked_and_normalised`, `test_paste_the_right_profile_is_kept_on_the_next_run`, `test_a_fetched_profile_joins_the_lookup_once_and_is_confirmed` |

### Slack, delivery and the app
| What broke | Fix | Test |
|---|---|---|
| A Slack Connect channel would show customer names to another organisation | `conversations.info` first; a channel shared with another organisation is refused unless `allow_shared_channel` | `test_a_slack_connect_channel_is_refused_unless_allowed` |
| A failure after creating the canvas or List made next week create a second one | Slack state saved right after each create | `test_state_is_saved_right_after_each_create_so_a_failed_post_never_duplicates` |
| The app could post the demo's invented accounts, with working buttons, to the sellers' channel | The app posts only when `PIPELINE_MODE=live`; `/pipeline` says no and the weekly run is skipped with a DM to the review owner | `test_demo_mode_refuses_to_post_from_the_slash_command`, `test_demo_mode_skips_the_weekly_run_logs_it_and_tells_the_review_owner_once` |
| Runs inside the app process needed a restart for contract edits, and a crash took the buttons down | Each run is a child process; a failure comes back as its last lines | `test_each_run_is_a_fresh_live_process_and_a_failure_brings_back_only_the_last_lines`, `test_a_real_child_process_failure_comes_back_as_a_message_and_the_app_lives_on` |
| A restart on the scheduled day missed or doubled the weekly run, and nobody knew the app was down | Scheduler state saved before the run (same-day catch-up, never twice); a heartbeat file every minute | `test_the_scheduler_catches_up_once_and_the_state_file_stops_a_second_run`, `test_a_broken_config_keeps_the_heartbeat_going_and_marks_it_not_ok` |
| The manifest missed scopes the code calls | `channels:read`, `groups:read`, `im:history` and the rest added; a test compares them | `test_manifest_names_the_app_and_asks_for_every_scope_the_code_calls` |
| No packaging | `pyproject.toml` (live extra, `gtm-signals` command), `requirements-live.txt`, a Dockerfile with no secrets. Tests read these files. The wheel was later built and installed with the real live libraries (round 3); the image was not built | `test_pip_install_builds_a_named_package_with_the_live_extra_and_a_command`, `test_the_image_installs_the_live_extra_runs_the_app_and_bakes_in_no_secrets` |
| A live dry run moved `replay/LATEST`, so every button in Slack said "older run" | A dry run writes its replay folder and leaves LATEST alone | `test_a_live_dry_run_leaves_the_live_buttons_working` |

### Connectors and the warehouse
| What broke | Fix | Test |
|---|---|---|
| A lost or partial local feedback log could erase the click history in the warehouse | Per-run tables are replaced by run id; `pg_feedback` is append-only; one transaction, batched inserts | `test_feedback_history_is_append_only_and_survives_a_lost_local_log`, `test_write_run_batches_inserts_and_is_all_or_nothing` |
| A cached Salesforce token expired inside the long-running app | A 401 drops the token, fetches a new one and retries once; a 403 or a supplied token fails at once | `test_salesforce_401_drops_the_cached_token_and_retries_once`, `test_salesforce_401_with_a_supplied_token_fails_at_once` |
| Preflight read more than it needed to check a source | Salesforce: one describe call and one `COUNT()`; Gong: a count of the call list only | `test_salesforce_account_filter_and_preflight_never_pull_all_accounts`, `test_gong_count_calls_pages_the_list_only_and_404_means_zero`, `test_preflight_counts_instead_of_pulling` |

### Operations
| What broke | Fix | Test |
|---|---|---|
| One corrupt line in the feedback log stopped the weekly run | Skipped, counted in run health; the rest still applies | `test_a_corrupt_feedback_line_is_skipped_counted_and_the_rest_still_applies` |
| A CI could not tell a broken fix from a pass | `--check-regressions` exits 1 when a case marked fixed fails again; each test file exits non-zero on a failure | `test_check_regressions_exits_one_when_a_fixed_case_breaks` |
| People-data lookups had no cache and ran one at a time | Parallel (`people_workers`) and cached (`people_cache_days`); failed lookups are never cached | `test_people_lookups_run_in_parallel_and_reuse_a_fresh_cache` |
| Years-old moves flooded the review queue | Review rows older than `old_move_days` (365) go to a table in the review canvas | `test_old_moves_go_to_the_canvas_not_one_by_one` |
| Nobody could see whether the cards were used or right | Metrics: clicks by action, acceptance, wrong person / play / account rates, snooze rate, queue age, time to detect | `test_metrics_rates_queue_age_and_time_to_detect` |
| Rows nobody was sent were never checked | Weekly QA sample of 5 Watch / No action / Hold rows to the review owner; Wrong saves a test case | `test_weekly_qa_sample_goes_to_the_review_owner_and_wrong_becomes_a_test` |
| The backtest could see only the last 12 replay folders, and renewal opportunities made P4 look perfect | Every run keeps `history/cards-<run id>.json` (never pruned); the join uses the account id; renewals are excluded by default | `test_every_run_keeps_its_cards_for_the_backtest_which_joins_on_account_id_without_renewals` |
| Sellers' notes about named people were kept for ever | Notes older than `retention_days` (365) are blanked at the start of every run, atomically; the action stays | `test_old_notes_are_blanked_and_the_log_is_rewritten_atomically` |

## Round 3 (2026-10-18): a second red team and a code review

Review agents in the same session again. Each item was reproduced, then fixed in code; each test failed before its fix. Rules 1.2, lexicon 2.4, surface 1.3.

### Champion tracker and people data
| What broke | Fix | Test |
|---|---|---|
| The red-team fix for cut-off searches ran one more search "filtered by the company". GetLeads (like most people APIs) reads that filter as the current employer, which a mover has left, so it could only find people still at the customer | The full same-name pull: when the 50-deep search comes back full with nobody tied, every same-name profile is pulled with its work history (`PEOPLE_DEEP_LIMIT`, default 5,000; optional `PEOPLE_EXPORT_URL`) and tied locally by the rules. `PEOPLE_PAST_COMPANY_FIELD` adds a work-history filter only for providers that have one. Every lookup record carries `depth` and `capped`, and a successful one `full_search` and `searched` | `test_a_capped_search_pulls_every_namesake_and_ties_locally`, `test_capped_search_runs_one_full_same_name_pull_and_keeps_only_tied_profiles`, `test_a_work_history_filter_is_used_only_when_the_provider_has_one`, `test_every_lookup_record_carries_depth_and_capped` |
| After a finished full pull, the hold still said the right profile "may be further down" | "A full search of N same-name profiles found none tied: check by hand, or paste the right profile" | `test_after_a_full_search_the_hold_says_nothing_is_further_down` |
| On the real rows | 37 cut-off searches pulled in full with GetLeads ($0 cash, 87,474 fair-use credits), with the member-id rule below: 24 resolved (7 Review, 11 Watch, 6 No action), each checked by Claude against the role in the case study; 13 still Hold (11 nobody tied in a full search of 110 to 60,892 profiles; 1 with two tied profiles that disagree, two different people, one at the customer and one who left, with no date in the story to tell them apart; 1 profile that still names the customer though that job ended). Decisions went from Review 19 · Hold 56 · Watch 70 · No action 36 to Review 26 · Hold 32 · Watch 81 · No action 42; Card stays 0. A second retry of the 13 (a web search for each profile, then a GetLeads lookup by profile URL, 8 fair-use credits) found nothing GetLeads holds; two leads are written down for a person to check, and one row has initials only | Outside the repo (`capped_retry.py --offline`, `held_retry.py`) |
| Two profiles of one LinkedIn member (the same member id under an old and a new URL slug) were held as "2 profiles are tied to the customer and disagree": the old slug's copy had stopped being refreshed and still named the customer | One member id = one person, and the freshest copy wins. Different member ids that disagree still hold. On the real rows one of the two "disagree" holds resolved | `test_one_linkedin_member_under_an_old_and_a_new_url_is_one_person` |
| A profile that still named the customer as the current employer while every customer job on it had ended was read as "Still at the customer" (Watch) | The ended-job check runs first: Hold, current employer unknown. On the real rows one row moved from Watch to Hold | `test_a_profile_that_still_names_the_customer_after_the_job_ended_is_held` |
| Dropping trailing geography could leave a generic or short head: "Air Canada" and "Air India" both became "Air" | Geography stays when dropping it would leave a generic or short head; two geography tails must name the same region ("Acme USA" = "Acme North America", "Canon USA" is not "Canon Europe") | `test_geography_never_leaves_a_generic_or_short_head` |
| A website merely named for the customer taught the customer a lookalike domain (a move from ramp.com to ramp.network) | A website named for the customer counts only when it is the customer's own known domain, or when nothing else gives the customer a domain | `test_a_lookalike_website_is_never_learned_as_the_customers_domain` |
| A last expense report filed after the last day held a real move as "records disagree" | Product use up to 60 days after leaving is normal (`activity_after_leaving_grace_days`); later use still holds | `test_a_last_expense_report_after_the_last_day_keeps_the_card` |
| An admin seat alone made a champion in the warehouse SQL | An admin counts only with a won-deal role at the contact's own account; an admin seat alone is a user | `test_an_admin_seat_never_makes_a_champion_without_a_won_deal_role`, `test_won_deal_roles_count_only_at_the_contacts_own_account_and_ties_break_by_id` |
| Names failed to match across spellings: "Pérez Rodríguez" vs "Pérez", "O'Brien" vs "OBrien", "Mc Donald" vs "McDonald", "Mohammed" vs "Mohamed", "Søren" vs "Soren" | Accents folded (with the letters NFKD does not split), apostrophes, hyphens and inner spaces ignored, one part of a compound surname matches, common first-name transliterations count. "Maria De" is still not "Maria De la Cruz", and "Jo de Vries" is still not "Joanna Devrieson" | `test_compound_surnames_apostrophes_spaces_accents_and_transliterations` |
| Staff jobs named after side roles ("Volunteer Coordinator Manager", "Brand Ambassador Program Manager", "Trustee Services Officer") read as side seats; a fractional or interim seat became a Card with no word | Trustee, volunteer, mentor and ambassador are side seats only as the role itself; a fractional or interim seat is a real move that goes to Review with the check "fractional or interim role" | `test_staff_jobs_named_after_side_roles_are_real_moves`, `test_fractional_or_interim_seat_is_a_real_move_sent_to_review` |
| A reject stored as the provider's raw profile URL never met the same profile pasted as a clean link | One normalised key per profile (`https://www.linkedin.com/in/<slug>`, slug unquoted and lower case) | `test_profile_urls_are_normalised_so_a_raw_reject_meets_a_pasted_link` |
| Worker threads could burst past the provider's rate limit; any error was retried and `Retry-After` was ignored | One token bucket for every worker (`PEOPLE_RATE_PER_SEC`, default 5); only 408, 429, 5xx and network errors are retried, waiting what `Retry-After` says; other 4xx fail at once | `test_one_rate_limiter_is_shared_by_every_lookup`, `test_people_api_honours_retry_after_and_never_retries_other_4xx` |
| Move rows did not carry the new company's size, industry or country, so the surface could not say what the account is | `New company size` (one band spelling), `New company industry`, `New company country` on every move row | `test_move_rows_carry_the_new_accounts_icp_fields` |

### Pipeline surface
| What broke | Fix | Test |
|---|---|---|
| Review moves years old became "Check first" cards for sellers | A Review move older than `old_move_days` (365) makes no card; it goes to the old-moves table in the review canvas | `test_review_moves_older_than_old_move_days_make_no_card_and_are_handed_back`, `test_accounts_carry_industry_size_country_and_the_pipeline_passes_icp_and_old_moves` |
| A CRM id that matched no account read this run (another region's record, or filtered out) fell back to a match by name or domain | Such an id is unmatched and listed; a champion row with an unread id gets the check "was not among the accounts read" | `test_a_crm_id_that_matches_no_account_is_unmatched_not_joined_by_name` |
| A seller had to open the CRM to learn the account's type, owner, renewal or open deal | One context line on every card: type, owner, renewal and days to it, open opportunity, industry, size and country when known (Salesforce `Industry`, `NumberOfEmployees`, `BillingCountry`, else the champion's profile) | `test_every_card_carries_a_short_account_context_line` |
| Nothing said whether the account was worth working | Optional `icp` in `config.json`: outside it, half the priority and a check saying why; an unknown field never fails; a company not in the CRM is checked on the champion's profile | `test_icp_fit_halves_priority_and_says_why_and_unknowns_never_fail`, `test_icp_uses_the_champion_profile_for_a_company_not_in_the_crm` |
| An owner's DM digest promised "more in the thread below", but no thread is posted in a DM | The DM says the rest are in the channel digest and the List | `test_an_owner_dm_digest_points_to_the_channel_digest_and_the_list` |
| A URL with `<`, `>`, `|` or a space could break out of Slack's link form (`https://x/><!channel`) | A link is written only when its URL is safe, and escaped | `test_links_are_escaped_and_unsafe_urls_are_dropped` |
| A queue row nobody could resolve came back every week; candidates came in the provider's order | Can't resolve parks the row (listed, no buttons, until a later identity decision); candidates are ordered by a likelihood score that never decides | `test_every_queue_row_can_be_parked_candidates_rank_by_score_and_parked_rows_are_listed`, `test_review_candidates_carry_a_score_and_the_likeliest_comes_first` |

### Slack, clicks and the app
| What broke | Fix | Test |
|---|---|---|
| A failure after the digest was posted (a rate-limited thread reply) stopped the rest and left `replay/LATEST` on the old run, so the posted digest's buttons said "older run" | Each later step is tried on its own; `LATEST` moves when the digest posted; errors go to run health | `test_a_failure_after_the_digest_is_posted_still_moves_latest_and_keeps_the_buttons_working` |
| Two reps with the same name could act on each other's accounts (owners matched by name) | Card owners are decided by the Salesforce owner id; a name counts only when no other owner has it | `test_two_owners_with_the_same_name_never_act_on_each_other_s_accounts` |
| A form opened from a card in a thread reply read back the digest and overwrote the reply with it | The app reads the reply with `conversations.replies` and updates only the message with that exact ts | `test_a_form_opened_from_a_thread_reply_card_updates_that_reply_not_the_digest` |
| A profile pasted after "None of these" stayed rejected | A paste removes that profile from the rejects and confirms it | `test_a_profile_pasted_after_none_of_these_is_confirmed_not_still_rejected` |
| The review canvas could stay unshared with the review owner, with nobody told | Shared through the review owner's DM channel (`conversations.open`, then `canvases.access.set`), retried every run until it works; a failure is said in the review queue message | `test_the_review_canvas_is_shared_through_the_dm_channel_and_retried_every_run_until_it_works` |
| A pasted "profile" could be a company page, a search URL or carry Slack markup | Only a LinkedIn `/in/` profile or another https URL, at most 300 characters, with no whitespace, `<`, `>` or `|` | `test_profile_links_must_be_a_profile_and_carry_no_slack_markup` |
| A pasted profile was fetched (a paid call) on every run, and the lookup cache rewrite dropped it | Cached by URL in `people_cache.jsonl` for `people_cache_days` | `test_pasted_profiles_are_cached_by_url_and_survive_the_lookup_cache_rewrite` |
| A snooze clicked on run day ended on the next weekly run, so the card came back a week early | Hidden until click day + 7 + 1 (Not now: + 14 + 1) | `test_snooze_and_not_now_hide_the_card_on_the_next_weekly_runs` |
| A correction passed as soon as the answer changed, even to another wrong answer | The form can name the right play (or no card) or the right decision; the case then passes only on that answer | `test_a_correction_with_the_right_answer_passes_only_on_that_answer`, `test_qa_wrong_opens_the_form_and_the_right_decision_reaches_the_test_case`, `test_a_case_with_the_right_answer_passes_only_on_that_answer` |
| The review owner's buttons on last week's queue message said "older run" although the row was still in the queue | Review-queue and QA buttons apply to the latest run's inputs while the row is in the latest run | `test_queue_buttons_work_on_last_weeks_message_and_a_parked_row_leaves_the_buttons` |
| `python3 -m pg_system --post-slack` without `--mode live` posted the demo | Refused without `--allow-demo-post` (exit 2) | `test_the_cli_refuses_to_post_the_demo_without_allow_demo_post` |

### Operations and connectors
| What broke | Fix | Test |
|---|---|---|
| A click appended while the weekly purge rewrote the feedback log was lost | Clicks and the purge share one lock file | `test_a_click_appended_during_a_purge_waits_for_the_lock_and_is_kept` |
| The purge blanked notes in the feedback log only | It also blanks old notes in the saved corrections and the CRM write-back log | `test_the_purge_also_blanks_old_notes_in_the_test_cases_and_the_crm_log` |
| An observer error stopped the whole run | The run goes on with the lexicon and Gong trackers; run health says the observer failed; the prompt version is in the summary | `test_an_observer_failure_runs_on_without_it_and_the_prompt_version_is_on_the_summary` |
| A crash mid-write could leave half a state file; a manual run could overlap the weekly run | Every state file is written whole (temp file + rename) and a corrupt one says how to fix it; one run per `out/` folder (`out/.run.lock`, the second exits with code 3) | `test_state_files_are_replaced_whole_and_a_corrupt_one_says_how_to_fix_it`, `test_only_one_run_at_a_time_per_out_folder` |
| Two click threads refreshing the Salesforce token at once could crash a call or drop a fresh token | One lock around the token; a 401 drops only the token that failed | `test_a_token_cleared_by_another_thread_never_crashes_a_call_and_only_the_failed_token_is_dropped` |
| `.gitignore` hid `tests/data/` (the suites' data) and did not ignore a filled `config.json` | `!tests/data/` and `config.json` added | `test_gitignore_keeps_the_test_data_and_ignores_a_filled_config` |
| The Task "Run this play" creates came back next week as a rep's note | The Task subject starts with "[GTM Signal]" and the notes query skips it; `SF_INTEGRATION_USER_ID`'s Tasks are skipped; only Tasks dated up to today are read | `test_the_system_never_reads_its_own_tasks_as_rep_notes` |
| At a weekly cadence the people cache could miss last week's lookups, so every lookup ran again | `people_cache_days` 8 (a day to spare): last week's lookups are reused, the week before's are looked up again | `test_last_weeks_people_lookups_are_reused_on_this_weeks_run` |
| The preflight never exercised the people source and ran a count of the champion SQL over the product events | One real lookup of `preflight_person`; the champion SQL is checked with `EXPLAIN`, never run | `test_preflight_runs_one_real_people_lookup_and_explains_the_sql_instead_of_running_it` |
| A post that timed out was retried and could post a second digest or create a second Task | Writes that are not idempotent (posts, creates, PATCHes) retry only on 429, which means the server did not process them; reads, edits and shares also retry on 5xx and network errors | `test_posts_are_not_retried_on_timeouts_or_5xx_but_reads_and_429s_are` |
| Warehouse clicks were keyed by time, user and action: two clicks in one second on different cards collided, and a purged note never reached the warehouse | One row per click key (its own id, else card or row, time, user, action, profile), merged in Snowflake; rows under the old key move to the new one | `test_two_clicks_in_one_second_on_different_cards_both_land_and_a_purge_reaches_the_warehouse`, `test_feedback_written_under_the_old_key_moves_to_the_new_key`, `test_snowflake_feedback_is_merged_on_its_key` |

### GTM brief (lexicon 2.4, observer prompt 1.1)
| What broke | Fix | Test |
|---|---|---|
| A finding that existed only because of the observer looked like a lexicon finding | Stamped `observer_prompt` with the prompt version | `test_findings_from_observer_themes_carry_the_observer_prompt_version` |
| A comparison that puts our price above another offer ("the FX fees on your card are way higher than Amex") was missed; "left a comment" read as a people change | Comparisons anchored to us are pricing friction; a role that left, quit or moved on is a people change, a comment is not | `test_a_price_comparison_against_another_offer_is_pricing_friction`, `test_a_named_role_who_left_quit_or_moved_on_is_a_people_change` |
| The only competitor list was three invented names | `contracts/lexicon.travel-expense.json`: a real travel-and-expense list with rules for everyday-word names, chosen with `LEXICON_PATH`; no precision or recall figure | `test_travel_expense_lexicon_is_the_demo_lexicon_with_only_the_competitors_changed`, `test_travel_expense_lexicon_fires_only_where_a_real_competitor_is_written` |

### Found when the observer, the package and the Slack output first ran for real (2026-10-18)

Found by running the real API, the real libraries and the real rows, not by review agents.

| What broke | Fix | Test |
|---|---|---|
| Running the real path (`observer.call_openai` through OpenRouter) needed three changes to the OpenAI-compatible call: a token cap, a reasoning-effort setting, and a way past models that reject `temperature` | `max_tokens` 8000; optional `OBSERVER_REASONING_EFFORT` (`reasoning.effort` on OpenRouter, `reasoning_effort` elsewhere); `temperature` from `OBSERVER_TEMPERATURE` (default 0, empty sends none), asked once more without it when a model rejects it | `test_openai_path_sends_a_token_cap_reasoning_effort_and_retries_without_temperature` |
| One failed observer batch dropped the observer for the whole run | The failed batch is counted (`failed_batches`) and the run goes on with the next batch | `test_a_failed_observer_batch_costs_its_turns_not_the_run` |
| The observer numbers came only from agents given the prompt | The real path ran on `openai/gpt-5.6-luna` (the OpenRouter account blocks Anthropic models): held-out half three times, plus the old-set holdout, the tuning half and a 10-row pilot once each, 12 calls, $0.024. The held-out replies are recorded and re-scored, with floors as measured, not as targets ([`eval.md`](eval.md)) | `test_real_api_runs_on_the_heldout_half_hold_their_measured_floors` |
| The live extra allowed snowflake-connector-python 3.x releases that silently ignore key-pair sign-in | `snowflake-connector-python>=3.8.1` in `pyproject.toml` and `requirements-live.txt`; 4.5.0 (Python 3.9) and 5.0.0 (Python 3.12) accept every call and keyword the code uses | `test_pip_install_builds_a_named_package_with_the_live_extra_and_a_command` |
| With the real `slack_bolt`, creating the app at import called `auth.test`, so a bad token crashed the app before any heartbeat was written: a restart loop with no reason on file | The app is created without token verification and checks the token once at start-up (`auth.test`, not at import): a bad one writes `heartbeat.json` with `ok: false` and the reason, then the app exits with code 2 | `test_a_bad_token_at_start_up_leaves_a_heartbeat_that_says_why` |
| A wheel holds no `config.json`, `contracts/` or `sql/`, so an installed package stopped at import | `GTM_HOME` names the folder that holds them. The wheel `gtm_signal_system-2.0.0-py3-none-any.whl` builds in a fresh venv (setuptools 82), `pip install "<wheel>[live]"` works, and the Slack app imports and registers its listeners with the real slack_bolt 1.30.0 / slack_sdk 3.45.0 | `test_gtm_home_points_an_installed_package_at_its_contracts` |

Slack: the output was posted once to the candidate's own Slack, private files only, through a Slack connector: a canvas from `canvas.md` (accepted as written: headings, tables, links), a List with the 10 next-steps rows, and the digest as a self-DM. The app's own API calls, with its bot token, have still not run against Slack.

## What stays open
| Open item | Why it is open |
|---|---|
| No connector has run against a live tenant | Written against the official API docs and tested with fixtures the author generated; a real tenant may behave differently. Salesforce, Snowflake and Gong need trial accounts the candidate must create (Gong has no sandbox). In Slack, the output went through a Slack connector once, not through the app's bot token |
| The brief is measured on synthetic text only | The observer over-fires on timing and expansion; the samples are small; about 200 real labelled call turns are needed. The travel-expense list has no figure at all |
| The observer on Claude Haiku through the real path | `call_openai` ran on Luna, because the OpenRouter account blocks Anthropic models; `call_anthropic` has not run. Needs Anthropic models allowed in that account, or an `ANTHROPIC_API_KEY` |
| 13 real rows still Hold after the full same-name pull and a second retry | 11 with nobody tied, 1 with two tied profiles that disagree (two different people; the story has no date to tell them apart), 1 profile that names the customer though the job ended: they need a person (review queue or Paste the right profile). Two leads from the second retry are written down for that person; one row has initials only |
| A person's spot-check of the real-data decisions | The 19/19 Review check and the 24 resolved rows were checked by Claude in the session |
| About 19 of 38 real `owns_program` tags rest on a title next to a quote | They were made under the old contract wording and need re-tagging under contract v2 |
| The Clay export path cannot run the namesake check | One profile per person |
| The Clay table lives in another company's workspace | It cannot be shared; rebuild it in the candidate's own workspace |
| When the full pull itself stops at `PEOPLE_DEEP_LIMIT`, the hold reason says the pull "did not run" | It ran but did not finish; the wording is in `rules.py` and was not changed in this pass |
| Live CRM PATCHes are not logged by the app | Only dry-run writes go to `feedback/crm_write_back.jsonl` |
| Preflight checks the Account fields only | The Contact fields in `salesforce/fields.md` are not checked |
| The Docker image was not built | Docker is not available on the build machine; the Dockerfile's installs were resolved offline for linux x86_64 and arm64. The wheel was built and installed with the real live libraries |
| The plays are examples | The candidate's examples, to be replaced by the team's own; nobody at the target company has approved them |
