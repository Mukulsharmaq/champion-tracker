"""Source connectors. Each one reads the exact JSON shape its API returns, so the demo fixtures and the live API go
through the same parser:

  gong.py         POST /v2/calls/extensive + POST /v2/calls/transcript      -> call signals (one per speaker turn)
  salesforce.py   GET /services/data/vXX.X/query (SOQL)                       -> accounts + CRM-note signals
  slack_notes.py  conversations.history + users.list                          -> field-note signals
  warehouse.py    champion definition SQL (SQLite demo, Snowflake live)       -> champion seed rows; run history
  slack_post.py   chat.postMessage + canvases + Lists (+ chat.update)         -> delivery

Every signal carries "account_id" (the Salesforce Account Id) next to the account name, so the three sources join
on the CRM id, not on a spelling.

Keys come from environment variables only (see .env.example). Nothing here writes to a source system except
slack_post (Slack) and salesforce.write_back (CRM), and both have a dry-run mode.

Retries (http_json): a read is retried on 429, 5xx and network errors. A write that is not idempotent (a POST that
creates something: chat.postMessage, a Salesforce Task) is retried only on 429, which means the server did not
process it; a timeout or a 5xx may have created the record already, so it fails at once instead of posting twice.
"""
import json
import os
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}     # idempotent by HTTP semantics


class MissingConfig(RuntimeError):
    """A live source was asked for but its environment variables are not set."""


class AuthError(RuntimeError):
    """401/403 from a source. `code` lets a caller refresh an expired token on 401 without retrying a 403 (scopes)."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def need(*names: str) -> Dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise MissingConfig("Set these environment variables for live mode: " + ", ".join(missing))
    return {n: os.environ[n] for n in names}


def http_json(method: str, url: str, headers: Dict[str, str], body: Optional[dict] = None, tries: int = 4,
              form: bool = False, idempotent: Optional[bool] = None, timeout: float = 60) -> dict:
    """JSON over HTTPS. Auth errors fail at once. idempotent (default: True for GET/HEAD/OPTIONS/PUT/DELETE, False for
    POST/PATCH) decides the retries: idempotent calls retry 429, 5xx and network errors (honouring Retry-After);
    non-idempotent calls retry only 429 (not processed) and fail at once on a 5xx or a timeout, which may have been
    processed: retrying could create a second Task or post a second digest."""
    if idempotent is None:
        idempotent = method.upper() in SAFE_METHODS
    data = None
    if body is not None:
        data = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
        headers = dict(headers, **{"Content-Type": "application/x-www-form-urlencoded" if form else "application/json; charset=utf-8"})
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError(f"{url}: auth failed ({e.code}) - check the token and its scopes", e.code) from e
            if e.code == 429 or (e.code >= 500 and idempotent):
                last = e
                if i < tries - 1:
                    time.sleep(float(e.headers.get("Retry-After") or 2 * (i + 1)))
                continue
            if e.code >= 500:
                raise RuntimeError(f"{url}: HTTP {e.code} on a {method} that is not retried (it may have been "
                                   f"processed): {e.read()[:300]!r}") from e
            raise RuntimeError(f"{url}: HTTP {e.code}: {e.read()[:300]!r}") from e
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            if not idempotent:
                raise RuntimeError(f"{url}: {method} failed and is not retried (it may have been processed): {e}") from e
            last = e
            if i < tries - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url}: failed after {tries} tries: {last}")


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_atomic(path: str, text: str):
    """Write a whole file so a reader (another process) sees the old file or the new one, never half of one: a unique
    temp file in the same folder, flushed, then os.replace."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def dump_json_atomic(path: str, obj, indent: Optional[int] = 2):
    write_atomic(path, json.dumps(obj, indent=indent, ensure_ascii=False, default=str))
