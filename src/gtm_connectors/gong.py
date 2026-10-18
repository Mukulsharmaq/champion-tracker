"""Gong calls -> signals.

Reads the two shapes Gong's API returns:
  POST /v2/calls/extensive   calls[].metaData (id, url, started), calls[].parties (speakerId, name, title,
                             affiliation Internal/External), calls[].context (the CRM account linked to the call)
  POST /v2/calls/transcript  callTranscripts[].transcript[] = speaker turns, each with sentences (start ms, text)

One signal per speaker turn, so every finding keeps who said it, on which call, and when in the call. The call's
CRM account is kept by name and by its Salesforce Id ("account_id" = the context objectId), so calls join the CRM
on the id even when the account is renamed.
Demo:  load("fixtures/gong/calls_extensive.json", "fixtures/gong/calls_transcript.json")
Live:  fetch(from_iso, to_iso)   needs GONG_ACCESS_KEY, GONG_ACCESS_KEY_SECRET, GONG_BASE_URL (your tenant's API host
       from Gong > Settings > API; toDateTime is exclusive)
       count_calls(from_iso, to_iso)   the preflight's cheap read: pages the call list only (no context, parties or
       transcripts) and counts calls
Speakers Gong marks Unknown (often dial-ins) are placed by email domain: the account's domain -> customer, one of
OUR_DOMAINS (comma list) -> internal, otherwise 'unknown' (counts, labelled 'speaker not identified').
Both Gong endpoints are POSTs that only read, so they are retried on 5xx and timeouts (idempotent=True).
"""
import base64
import os
from typing import Dict, List, Tuple

from . import http_json, load_json, need


def _account(call: Dict) -> Tuple[str, str, str]:
    """(name, website, Salesforce Account Id) of the CRM account Gong linked to the call."""
    for ctx in call.get("context") or []:
        for obj in ctx.get("objects") or []:
            if obj.get("objectType") == "Account":
                fields = {f.get("name"): f.get("value") for f in obj.get("fields") or []}
                return fields.get("Name") or "", fields.get("Website") or "", str(obj.get("objectId") or "")
    return "", "", ""


OUR_DOMAINS = {d.strip().lower() for d in os.environ.get("OUR_DOMAINS", "").split(",") if d.strip()}


def _side(party: Dict, account_site: str) -> str:
    side = {"Internal": "internal", "External": "customer"}.get(party.get("affiliation") or "", "unknown")
    if side == "unknown":
        mail = (party.get("emailAddress") or "").lower().rsplit("@", 1)[-1]
        site = (account_site or "").lower().replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
        if mail and site and (mail == site or mail.endswith("." + site)):
            return "customer"
        if mail and mail in OUR_DOMAINS:
            return "internal"
    return side


def to_signals(extensive: Dict, transcripts: Dict) -> List[Dict]:
    calls = {str(c["metaData"]["id"]): c for c in extensive.get("calls", [])}
    out = []
    for t in transcripts.get("callTranscripts", []):
        call = calls.get(str(t.get("callId")))
        if not call:
            continue
        meta = call["metaData"]
        parties = {p.get("speakerId"): p for p in call.get("parties") or [] if p.get("speakerId")}
        account, site, account_id = _account(call)
        hits = [(o.get("startTime"), o.get("speakerId"), tr.get("name")) for tr in (call.get("content") or {}).get("trackers") or []
                for o in tr.get("occurrences") or []]
        for n, turn in enumerate(t.get("transcript") or [], 1):
            sentences = turn.get("sentences") or []
            text = " ".join((s.get("text") or "").strip() for s in sentences).strip()
            if not text:
                continue
            p = parties.get(turn.get("speakerId"), {})
            start_s = int((sentences[0].get("start") or 0) / 1000)
            lo, hi = (sentences[0].get("start") or 0) / 1000, (sentences[-1].get("end") or 0) / 1000
            trackers = sorted({name for at, spk, name in hits if at is not None and lo <= at <= hi + 1
                               and (not spk or spk == turn.get("speakerId"))})
            out.append({"source": "call", "id": f"GC-{meta['id']}-{n}", "date": (meta.get("started") or "")[:10],
                        "account": account, "account_id": account_id,
                        "speaker": p.get("name") or "Unknown speaker", "speaker_title": p.get("title") or "",
                        "side": _side(p, site), "text": text,
                        "url": f"{meta.get('url', '')}", "at": f"{start_s // 60:02d}:{start_s % 60:02d}", "trackers": trackers})
    return out


def load(extensive_path: str, transcript_path: str) -> List[Dict]:
    return to_signals(load_json(extensive_path), load_json(transcript_path))


def _client() -> Tuple[str, Dict[str, str]]:
    env = need("GONG_ACCESS_KEY", "GONG_ACCESS_KEY_SECRET", "GONG_BASE_URL")
    auth = base64.b64encode(f"{env['GONG_ACCESS_KEY']}:{env['GONG_ACCESS_KEY_SECRET']}".encode()).decode()
    return env["GONG_BASE_URL"].rstrip("/"), {"Authorization": f"Basic {auth}"}


def count_calls(from_iso: str, to_iso: str) -> int:
    """How many calls Gong has in the window, for the preflight. Asks for no context, parties or transcripts, so it
    stays cheap on a busy tenant. Gong answers 404 when the window has no calls: that is 0, not an error."""
    base, h = _client()
    n, cursor = 0, None
    while True:
        body = {"filter": {"fromDateTime": from_iso, "toDateTime": to_iso}}
        if cursor:
            body["cursor"] = cursor
        try:
            r = http_json("POST", f"{base}/v2/calls/extensive", h, body, idempotent=True)
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                return n
            raise
        n += len(r.get("calls") or [])
        cursor = (r.get("records") or {}).get("cursor")
        if not cursor:
            return n


def fetch(from_iso: str, to_iso: str) -> List[Dict]:
    base, h = _client()
    calls, cursor = [], None
    while True:
        body = {"filter": {"fromDateTime": from_iso, "toDateTime": to_iso},
                "contentSelector": {"context": "Extended", "exposedFields": {
                    "parties": True, "content": {"trackers": True, "trackerOccurrences": True}}}}
        if cursor:
            body["cursor"] = cursor
        try:
            r = http_json("POST", f"{base}/v2/calls/extensive", h, body, idempotent=True)
        except RuntimeError as e:
            if "HTTP 404" in str(e):          # Gong: "No calls found for the specified period"
                return []
            raise
        calls += r.get("calls", [])
        cursor = (r.get("records") or {}).get("cursor")
        if not cursor:
            break
    transcripts = []
    ids = [str(c["metaData"]["id"]) for c in calls]
    for i in range(0, len(ids), 100):
        cursor = None
        while True:
            body = {"filter": {"callIds": ids[i:i + 100]}}
            if cursor:
                body["cursor"] = cursor
            r = http_json("POST", f"{base}/v2/calls/transcript", h, body, idempotent=True)
            transcripts += r.get("callTranscripts", [])
            cursor = (r.get("records") or {}).get("cursor")
            if not cursor:
                break
    return to_signals({"calls": calls}, {"callTranscripts": transcripts})
