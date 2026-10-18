"""Live preflight: test every switched-on source with a few small reads, and report what is missing, before the
first real run. Writes nothing anywhere.   PYTHONPATH=src python3 -m pg_system --check

  Salesforce  one describe call and one COUNT() (salesforce.preflight, never the accounts themselves)
  Gong        a count of the last two days' calls (gong.count_calls, no transcripts)
  Slack       auth.test, and conversations.info for each configured channel (the bot must be a member)
  warehouse   EXPLAIN on the champion SQL (Snowflake: EXPLAIN USING TEXT): the SQL compiles against your tables and
              the plan is built, without scanning the product events
  people      one real lookup of config.json -> preflight_person {"first", "last", "company"} (a person you know is
              in the provider, ideally someone still at a customer), the same call the run makes. Without it the
              report says the people source was not exercised. PEOPLE_MATCH_URL unset is a warning: the name +
              company step is skipped and every person goes to the 50-deep same-name search.
  observer    the provider's variables

"ok" is false when a variable is missing or a source fails; "warnings" never change "ok" but should be read."""
import os
from datetime import date, timedelta
from typing import Dict

from gtm_connectors import MissingConfig
from .pipeline import DEFAULT_SOURCES, required_env

NOT_EXERCISED = ("people source not exercised: set preflight_person {\"first\", \"last\", \"company\"} in config.json "
                 "to run one real lookup")
NO_MATCH_URL = ("PEOPLE_MATCH_URL is not set: the name + company step is skipped, so every person goes to the 50-deep "
                "same-name search (slower, and people with common names are held more often)")


def _try(fn):
    try:
        return {"ok": True, **(fn() or {})}
    except (MissingConfig, RuntimeError, KeyError, ValueError, OSError) as e:
        return {"ok": False, "error": str(e)[:300]}


def explain_sql(sql: str, dialect: str = "snowflake") -> str:
    """The statement that validates `sql` without running it: Snowflake EXPLAIN USING TEXT, SQLite EXPLAIN QUERY PLAN."""
    body = sql.strip().rstrip(";").strip()
    return ("EXPLAIN QUERY PLAN " if dialect == "sqlite" else "EXPLAIN USING TEXT ") + body


def people_check(cfg: Dict, lookup=None) -> Dict:
    """One real lookup of preflight_person, or a note that the source was not exercised."""
    person = cfg.get("preflight_person") or {}
    if not (person.get("first") and person.get("last") and person.get("company")):
        return {"exercised": False, "note": NOT_EXERCISED, "match_url_set": bool(os.environ.get("PEOPLE_MATCH_URL")),
                "profile_url_set": bool(os.environ.get("PEOPLE_PROFILE_URL"))}
    if lookup is None:
        from champion_tracker.adapters import people_data
        lookup = people_data.lookup_record
    rec = lookup({"Name": f"{person['first']} {person['last']}", "First": person["first"], "Last": person["last"],
                  "Customer": person["company"], "Slug": "preflight"})
    if not rec.get("ok"):
        raise RuntimeError(f"people lookup failed: {rec.get('error') or 'no detail'}")
    return {"exercised": True, "person": f"{person['first']} {person['last']} ({person['company']})",
            "candidates": len(rec.get("candidates") or []), "source": rec.get("source", ""), "capped": bool(rec.get("capped")),
            "match_url_set": bool(os.environ.get("PEOPLE_MATCH_URL")),
            "profile_url_set": bool(os.environ.get("PEOPLE_PROFILE_URL"))}


def preflight(cfg: Dict) -> Dict:
    src = {**DEFAULT_SOURCES, **(cfg.get("sources") or {})}
    report = {"missing": required_env(cfg, post_slack=bool(cfg.get("channel"))), "sources": {}, "warnings": []}

    if src["salesforce"]:
        from gtm_connectors import salesforce
        def sf():
            info = salesforce.preflight()
            if info["missing_account_fields"]:
                raise ValueError("Account fields missing (see salesforce/fields.md): " + ", ".join(info["missing_account_fields"]))
            if not info["customer_types_found"]:
                raise ValueError(f"None of SF_CUSTOMER_TYPES are Account.Type values in this org: {info['account_types_in_org']}")
            return info                              # includes "accounts": the COUNT() the run would read
        report["sources"]["salesforce"] = _try(sf)
        if not os.environ.get("SF_INTEGRATION_USER_ID"):
            report["warnings"].append("SF_INTEGRATION_USER_ID is not set: Tasks the integration user writes are kept out "
                                      "of the notes by their '[GTM Signal]' subject only")
    if src["gong"]:
        from gtm_connectors import gong
        day = date.today()
        report["sources"]["gong"] = _try(lambda: {"calls_last_2_days": gong.count_calls(
            f"{(day - timedelta(days=2)).isoformat()}T00:00:00Z", f"{(day + timedelta(days=1)).isoformat()}T00:00:00Z")})
    if src["field_notes"] or cfg.get("channel"):
        from gtm_connectors import slack_post
        def sl():
            who = slack_post._call("auth.test", {}, True)
            out = {"team": who.get("team"), "bot": who.get("user")}
            for key in ("channel", "field_notes_channel"):
                if cfg.get(key):
                    ch = slack_post._call("conversations.info", {"channel": cfg[key]}, True)["channel"]
                    if not ch.get("is_member"):
                        raise ValueError(f"the bot is not in {key} {cfg[key]}: /invite it")
                    out[key] = ch.get("name")
            return out
        report["sources"]["slack"] = _try(sl)
    if src["warehouse"]:
        from gtm_connectors import warehouse
        def wh():
            mod, con = warehouse.connect()
            try:
                cur = con.cursor()
                cur.execute(explain_sql(warehouse._sql("champion_definition.sql")))
                plan = cur.fetchall() or []
                return {"sql_valid": True, "plan_rows": len(plan), "check": "EXPLAIN USING TEXT (the query is not run)"}
            finally:
                con.close()
        report["sources"]["warehouse"] = _try(wh)
    if src["people"] and cfg.get("people_source", "api") == "api":
        report["sources"]["people"] = _try(lambda: people_check(cfg))
        if not report["sources"]["people"].get("exercised", True) and report["sources"]["people"]["ok"]:
            report["warnings"].append(NOT_EXERCISED)
        if not os.environ.get("PEOPLE_MATCH_URL"):
            report["warnings"].append(NO_MATCH_URL)
    if (cfg.get("observer") or {}).get("enabled"):
        from gtm_briefs import observer
        def obs():
            missing = observer.required_env()          # ValueError for an unknown OBSERVER_PROVIDER
            if missing:
                raise ValueError("observer needs " + ", ".join(missing))
            return {"provider": observer.provider_name()}
        report["sources"]["observer"] = _try(obs)
    report["ok"] = not report["missing"] and all(v["ok"] for v in report["sources"].values())
    return report
