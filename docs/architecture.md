# Architecture

```
 Warehouse (facts + history)            Clay / people data (context)          GitHub (definitions, policies, tests)
 ─────────────────────────────          ────────────────────────────          ─────────────────────────────────────
 closed-won contact roles  ─┐           job-change signal ─┐                  contracts/champion_definition.yaml
 active admins (no test /   ├─► champions ──────────────────┼─► candidates ──► rules.py  (identity → move → account → stop rule)
 internal workspaces)      ─┘   (sql/champion_definition)   │   (every same-  │            │
 call evidence (links)          + evidence sentence         │    name profile │            ▼
                                                            │    + history)   │   Card / Review / Hold / Watch / No action
                                                            └─────────────────┘            │
                                                                                            ▼
 Slack (delivery, review, correction)  ◄──────────────────── slack_card.py (who · why now + evidence · play · buttons)
   accept / snooze / dismiss / wrong person / wrong play ──► feedback.py ─► CRM write-back + new regression test
```

## Design choices
- **The model observes, code decides.** An LLM (or agent) only extracts names, titles and a verbatim evidence sentence. Every decision is deterministic code with a written reason.
- **Hold is a feature.** A wrong card costs the seller's trust. Uncertain identity or moves go to Hold or Review with the exact reason, never to a card.
- **Versioned truth.** Thresholds, aliases and plays live in the repo and are tested (`test_config_matches_the_written_contract`).
- **Feedback closes the loop.** Seller corrections are logged with the rule version and turned into regression cases, so a fix can't silently undo an old one.
- **No keys or personal data in the repo.** Live adapters read keys from environment variables. Fixtures are synthetic.
