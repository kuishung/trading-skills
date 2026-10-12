"""Massive Flat Files - the options day files that feed the Options Screener's IV history
(v4.138; the plan: one file per trading day of every US option contract that traded).

What it reads
-------------
Massive's S3-compatible file store (``https://files.massive.com``, bucket ``flatfiles``),
dataset ``us_options_opra/day_aggs_v1/YYYY/MM/YYYY-MM-DD.csv.gz``: one gzip CSV per
trading day with the columns ``ticker,volume,open,close,high,low,window_start,transactions``
(``ticker`` like ``O:AAPL261016C00150000``, ``window_start`` in nanoseconds). Options
Starter includes it with 2 years of history; a day's file is published about 11:00 New York
time the next day. ``scr_flativ`` turns a file into IV values; this module only lists,
downloads and keeps the files.

The keys
--------
``TST_MASSIVE_S3_KEY_ID`` and ``TST_MASSIVE_S3_SECRET`` (``app/.env`` on Hermes,
gitignored) - a SEPARATE pair from ``TST_MASSIVE_API_KEY`` (Dashboard -> Keys -> Flat
Files). They go only into the S3 client; never into a log line, an exception text, a
``repr`` or a status file (every message passes ``scrub``, and ``massive._scrub`` - the
collectors' scrubber - masks them too). ``TST_MASSIVE_S3_ENDPOINT`` (default
``https://files.massive.com``) and ``TST_MASSIVE_S3_BUCKET`` (default ``flatfiles``)
are optional overrides.

The client
----------
``boto3`` (S3 version-4 signatures), imported LAZILY - only when a real client is built
at the first request - so the module imports on a PC without boto3 and the tests inject
a fake client (``list_objects_v2`` / ``head_object`` / ``get_object``). botocore's own
retries are turned off; this module retries a dropped connection, a timeout, a 5xx and
``SlowDown`` itself (``retries`` times, waiting 2, 4, 8, 16 s ...).

The local store
---------------
``dashboard_tst/state/flatfiles/<same key path>`` plus ``state/flatfiles/manifest.json``
= ``{key: {"day", "size", "etag", "downloaded_at"}}``. Files are KEPT: on Starter a day
older than 2 years can never be downloaded again. A file is written to a temp name beside
it and renamed into place only once its size matches what Massive sent and the whole
file decompresses (gzip CRC and length checked - a damaged copy never replaces a good
one); the manifest is written the same way (temp + rename), after every file. A file that
later cannot be read is set aside as ``<name>.bad`` by ``discard(day)`` (never deleted),
so the next ``ensure`` downloads a fresh copy.

Errors
------
Everything that goes wrong is a ``FlatFilesError`` with a ``kind``:
``config`` (keys not set on this PC, boto3 not installed, the bucket not found, a file
LIST answered 404 / 400 / 405 - the endpoint or the bucket is wrong, a local write that
failed), ``auth`` (401, ``InvalidAccessKeyId``, ``SignatureDoesNotMatch`` -
the keys are wrong), ``plan`` (403 - the plan does not include the files, or the day is
outside its 2 years), ``missing`` (``NoSuchKey`` / 404 on one day's file - ``ensure``
turns it into None: a day not published yet, a weekend, a holiday), ``network`` (a dropped
connection, a timeout, a 5xx or ``SlowDown`` that did not clear after the retries),
``http`` (anything else, and a downloaded file that is not a whole gzip file). ``.status``
is the HTTP status and ``.code`` the S3 error code when there was one.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import logging
import os
import re
import threading
import time
import zlib
from pathlib import Path

try:   # app/.env into the environment (no-op when absent) - the settings module does it
    from .. import config as _config  # noqa: F401
except Exception:  # noqa: BLE001 - the module also works outside the app package
    _config = None

ENV_KEY_ID = "TST_MASSIVE_S3_KEY_ID"
ENV_SECRET = "TST_MASSIVE_S3_SECRET"
ENV_ENDPOINT = "TST_MASSIVE_S3_ENDPOINT"
ENV_BUCKET = "TST_MASSIVE_S3_BUCKET"
DEFAULT_ENDPOINT = "https://files.massive.com"
DEFAULT_BUCKET = "flatfiles"
PREFIX = "us_options_opra/day_aggs_v1/"

DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "state" / "flatfiles"
MANIFEST_NAME = "manifest.json"

RETRIES = 4                  # a network error / 5xx / SlowDown retried 4 times ...
BACKOFF_S = 2.0              # ... waiting 2, 4, 8, 16 s ...
BACKOFF_MAX_S = 60.0         # ... never more than a minute
CHUNK = 1 << 20              # download in 1 MB pieces
LIST_PAGE = 1000             # list_objects_v2's largest page (a month holds ~23 files)
LIST_MAX_PAGES = 50          # a runaway continuation token stops here
LIST_CACHE_S = 6 * 3600.0    # a listing's size / etag is trusted this long by ensure()
REPLACE_TRIES = 5            # os.replace retried on a Windows sharing violation ...
REPLACE_WAIT_S = 0.5         # ... every half second
GZIP_MAGIC = b"\x1f\x8b"

KINDS = ("config", "auth", "plan", "missing", "network", "http")

_KEY_RE = re.compile(r"^" + re.escape(PREFIX) + r"(\d{4})/(\d{2})/(\d{4})-(\d{2})-(\d{2})\.csv\.gz$")

_AUTH_CODES = {"invalidaccesskeyid", "signaturedoesnotmatch", "invalidtoken", "expiredtoken",
               "tokenrefreshrequired", "authorizationheadermalformed", "invalidsecurity",
               "missingsecurityheader", "unauthorized", "unauthorizedaccess", "401"}
_MISSING_CODES = {"nosuchkey", "notfound", "404"}
_CONFIG_CODES = {"nosuchbucket", "permanentredirect", "invalidbucketname"}
_RETRY_CODES = {"slowdown", "internalerror", "serviceunavailable", "requesttimeout", "throttling",
                "throttlingexception", "toomanyrequests", "requestlimitexceeded", "bandwidthlimitexceeded",
                "500", "502", "503", "504"}
_NET_NAMES = {"EndpointConnectionError", "ConnectionClosedError", "ReadTimeoutError",
              "ConnectTimeoutError", "ProxyConnectionError", "SSLError", "IncompleteReadError",
              "ResponseStreamingError", "ProtocolError", "HTTPClientError", "AWSConnectionError",
              "ConnectionError", "ReadTimeout", "ConnectTimeout", "ChunkedEncodingError"}
_CRED_NAMES = {"NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError"}


class FlatFilesError(RuntimeError):
    """A Flat Files operation that failed. ``kind`` is one of ``KINDS``; ``status`` the
    HTTP status and ``code`` the S3 error code when the server answered. The text is
    scrubbed - it never carries the keys."""

    def __init__(self, kind: str, message: str, status: int | None = None, code: str | None = None):
        super().__init__(scrub(message))
        self.kind = kind if kind in KINDS else "http"
        self.status = status
        self.code = code

    def __repr__(self) -> str:
        return "FlatFilesError(%r, %r)" % (self.kind, str(self))


class _LocalWriteError(Exception):
    """A write on this PC failed (disk full, no permission) - never retried as a network error."""


# ────────────────────────────────── settings ──────────────────────────────────

def _env(name: str) -> str | None:
    v = (os.environ.get(name) or "").strip()
    return v or None


def configured() -> bool:
    """True when both ``TST_MASSIVE_S3_KEY_ID`` and ``TST_MASSIVE_S3_SECRET`` are set
    (non-blank) in this process's environment (``app/.env`` is loaded on import)."""
    return bool(_env(ENV_KEY_ID) and _env(ENV_SECRET))


def endpoint() -> str:
    """``TST_MASSIVE_S3_ENDPOINT`` (no trailing slash; ``https://`` added when it has no
    scheme) or ``https://files.massive.com``."""
    v = (_env(ENV_ENDPOINT) or DEFAULT_ENDPOINT).rstrip("/")
    return v if "://" in v else "https://" + v


def bucket() -> str:
    """``TST_MASSIVE_S3_BUCKET`` or ``flatfiles``."""
    return _env(ENV_BUCKET) or DEFAULT_BUCKET


def key_for(day) -> str:
    """The object key of one day's options file:
    ``us_options_opra/day_aggs_v1/2026/10/2026-10-09.csv.gz``."""
    d = _to_date(day)
    if d is None:
        raise ValueError("key_for: a date is needed (YYYY-MM-DD), got %r" % (day,))
    return "%s%04d/%02d/%s.csv.gz" % (PREFIX, d.year, d.month, d.isoformat())


def day_of_key(key) -> _dt.date | None:
    """The day of an options day-file key, or None when the key is not one (another
    dataset, a stray object, a file filed under the wrong month folder)."""
    m = _KEY_RE.match(str(key or ""))
    if not m:
        return None
    try:
        d = _dt.date(int(m.group(3)), int(m.group(4)), int(m.group(5)))
    except ValueError:
        return None
    if (d.year, d.month) != (int(m.group(1)), int(m.group(2))):
        return None
    return d


def scrub(text, extra=()) -> str:
    """Text safe to show or log: the Flat Files secret and key id (from the environment,
    plus any in ``extra``) masked, anything shaped like a credential or a signature
    masked; capped at 500 characters."""
    s = str(text or "")
    for v in (_env(ENV_SECRET), _env(ENV_KEY_ID), *tuple(extra or ())):
        if isinstance(v, str) and len(v) >= 4:
            s = s.replace(v, "***")
    s = re.sub(r"(?i)(aws_secret_access_key|secret_access_key|secret_key|secretaccesskey)(\s*[=:]\s*)[^\s,;&\"']+",
               r"\1\2***", s)
    s = re.sub(r"(?i)(aws_access_key_id|awsaccesskeyid|access_key_id)(\s*[=:>]\s*)[^\s,;&\"'<]+", r"\1\2***", s)
    s = re.sub(r"(?i)(credential=)[^/\s,;&\"']+", r"\1***", s)
    s = re.sub(r"(?i)(signature=)[0-9a-f]+", r"\1***", s)
    return s[:500]


# ────────────────────────────────── small helpers ──────────────────────────────────

def _to_date(x) -> _dt.date | None:
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    s = str(x or "").strip()
    try:
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return _dt.date.fromisoformat(s[:10])
        if len(s) == 8 and s.isdigit():
            return _dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None
    return None


def _need_date(x, what: str) -> _dt.date:
    d = _to_date(x)
    if d is None:
        raise ValueError("%s: a date is needed (YYYY-MM-DD), got %r" % (what, x))
    return d


def _etag(v) -> str | None:
    s = str(v or "").strip().strip('"').strip().lower()
    return s or None


def _size(v) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _months(start: _dt.date, end: _dt.date):
    """(year, month) from ``start``'s month to ``end``'s, oldest first (across years)."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            y, m = y + 1, 1


def _same_remote(entry: dict | None, info: dict | None) -> bool:
    """Does the manifest ``entry`` describe the remote object ``info``? Sizes must agree;
    etags must agree when both are known."""
    if not entry or not info:
        return False
    if _size(entry.get("size")) != _size(info.get("size")):
        return False
    a, b = _etag(entry.get("etag")), _etag(info.get("etag"))
    return a is None or b is None or a == b


def _error_parts(exc) -> tuple[str, int | None, str]:
    """(S3 error code as sent, HTTP status, server message) of a botocore ClientError-shaped
    exception (``.response = {"Error": {...}, "ResponseMetadata": {...}}``)."""
    resp = getattr(exc, "response", None)
    if not isinstance(resp, dict):
        return "", None, ""
    err = resp.get("Error") if isinstance(resp.get("Error"), dict) else {}
    meta = resp.get("ResponseMetadata") if isinstance(resp.get("ResponseMetadata"), dict) else {}
    code = str(err.get("Code") or "").strip()
    status = _size(meta.get("HTTPStatusCode"))
    if status is None and code.isdigit():
        status = int(code)
    return code, status, str(err.get("Message") or "").strip()


def _is_network(exc) -> bool:
    names = {c.__name__ for c in type(exc).__mro__}
    if names & _NET_NAMES:
        return True
    return isinstance(exc, (ConnectionError, TimeoutError))


def _is_aws(exc) -> bool:
    mod = type(exc).__module__ or ""
    return mod.split(".", 1)[0] in {"botocore", "boto3", "urllib3", "s3transfer"}


# ────────────────────────────────── the store ──────────────────────────────────

class FlatFiles:
    """The options day files: list them on Massive, download the missing / changed ones,
    keep them under ``root`` with a manifest. Thread-safe (one lock around the manifest
    and the listing cache). ``client`` - an S3 client (boto3-shaped); None builds a real
    one from the environment at the first request. ``now`` - a clock returning a
    datetime (UTC; default the system clock); ``sleep`` - the back-off sleep."""

    def __init__(self, client=None, root=None, now=None, sleep=None, *, log=None,
                 bucket_name: str | None = None, retries: int = RETRIES, backoff_s: float = BACKOFF_S):
        self._client = client
        self._own_client = False
        self.root = Path(root) if root is not None else DEFAULT_ROOT
        self._now_fn = now
        self._sleep = sleep or time.sleep
        self.log = log or logging.getLogger(__name__)
        self._bucket_fixed = bucket_name is not None
        self.bucket = str(bucket_name or bucket())
        self.endpoint = endpoint()
        self.retries = max(0, int(retries))
        self.backoff_s = float(backoff_s) if backoff_s and backoff_s > 0 else BACKOFF_S
        self._lock = threading.RLock()
        self._remote: dict[str, tuple[dict, _dt.datetime]] = {}
        self._masks: tuple[str, ...] = ()
        self._manifest: dict[str, dict] = self._load_manifest()
        self.requests = 0                    # S3 calls sent (retries included)
        self.downloads = 0                   # files written this process
        self.downloaded_bytes = 0
        self.refused_months: list[str] = []  # "YYYY-MM" months the last list_days was refused (plan)
        self.last_action: str | None = None  # what the last ensure() did

    def __repr__(self) -> str:   # never the keys
        keys = "set" if (self._client is not None or configured()) else "missing"
        return "<FlatFiles %s bucket=%s keys=%s root=%s>" % (self.endpoint, self.bucket, keys, self.root)

    # ── clock + text ──

    def _now(self) -> _dt.datetime:
        t = self._now_fn() if self._now_fn is not None else _dt.datetime.now(_dt.timezone.utc)
        if isinstance(t, _dt.datetime) and t.tzinfo is not None:
            t = t.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return t

    def _stamp(self) -> str:
        return self._now().replace(microsecond=0).isoformat() + "Z"

    def scrub(self, text) -> str:
        return scrub(text, self._masks)

    def _log(self, level: int, msg: str) -> None:
        try:
            self.log.log(level, self.scrub(msg))
        except Exception:  # noqa: BLE001 - logging never breaks a download
            pass

    # ── the S3 client ──

    def _s3(self):
        if self._client is None:
            self._client = self._build_client()
            self._own_client = True
        return self._client

    def _build_client(self):
        kid, sec = _env(ENV_KEY_ID), _env(ENV_SECRET)
        if not (kid and sec):
            raise FlatFilesError("config", "the Flat Files keys are not set on this PC: add %s and %s "
                                 "to app\\.env (Massive Dashboard -> Keys -> Flat Files)" % (ENV_KEY_ID, ENV_SECRET))
        self._masks = tuple(dict.fromkeys(self._masks + (sec, kid)))   # an older pair stays masked too
        self.endpoint = endpoint()                    # app/.env may have been re-read since __init__
        if not self._bucket_fixed:
            self.bucket = bucket()
        try:
            import boto3
            from botocore.config import Config
        except Exception:  # noqa: BLE001 - ImportError, or a broken install
            raise FlatFilesError("config", "boto3 is not installed on this PC - run: "
                                 "pip install -r app\\requirements.txt") from None
        try:
            session = boto3.session.Session(aws_access_key_id=kid, aws_secret_access_key=sec)
            return session.client(
                "s3", endpoint_url=self.endpoint,
                config=Config(signature_version="s3v4", connect_timeout=15, read_timeout=60,
                              retries={"total_max_attempts": 1, "mode": "standard"}))
        except Exception as exc:  # noqa: BLE001
            raise FlatFilesError("config", "could not set up the Flat Files client (%s: %s)" % (
                type(exc).__name__, scrub(exc, (sec, kid)))) from None

    def reset_client(self) -> None:
        """Drop a client this object built, so the next request builds a new one from the
        environment (after ``app/.env`` was re-read with new keys). An injected client is kept."""
        if self._own_client and self._client is not None:
            try:
                close = getattr(self._client, "close", None)
                if callable(close):
                    close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
            self._own_client = False

    close = reset_client

    def _call(self, what: str, op: str, **kw):
        """One S3 call (``op`` on the client) with the retries; returns its response."""
        return self._retry(what, lambda: self._send(op, **kw))

    def _send(self, op: str, **kw):
        client = self._s3()
        with self._lock:
            self.requests += 1
        return getattr(client, op)(**kw)

    def _retry(self, what: str, fn):
        """Run ``fn`` - retried on a network error, a 5xx, SlowDown - and turn whatever
        goes wrong into a ``FlatFilesError`` (``_LocalWriteError`` -> ``config``, no retry)."""
        n = 0
        while True:
            try:
                return fn()
            except FlatFilesError:
                raise
            except _LocalWriteError as exc:
                raise FlatFilesError("config", "could not write %s on this PC (%s) - check the free disk "
                                     "space and the folder %s" % (what, self.scrub(exc), self.root)) from None
            except Exception as exc:  # noqa: BLE001 - classified below
                kind, status, code, text = self._classify(exc)
                if kind is None:
                    raise
                if kind == "retry":
                    if n < self.retries:
                        n += 1
                        wait = min(BACKOFF_MAX_S, self.backoff_s * 2 ** (n - 1))
                        self._log(logging.WARNING, "Flat Files: %s failed (%s) - try %d of %d in %.0f s"
                                  % (what, text, n + 1, self.retries + 1, wait))
                        self._sleep(wait)
                        continue
                    raise FlatFilesError("network", "could not reach Massive Flat Files for %s after %d tries (%s)"
                                         % (what, n + 1, text), status, code or None) from None
                raise self._error(kind, what, status, code, text) from None

    def _classify(self, exc) -> tuple[str | None, int | None, str, str]:
        """(kind, status, code, text) of an exception; kind ``retry`` = try again, None =
        not an S3 / network error (re-raised as it is)."""
        code, status, message = _error_parts(exc)
        lc = code.lower()
        name = type(exc).__name__
        if code or status is not None:
            text = "HTTP %s%s%s" % (status if status is not None else "?",
                                    (", " + code) if code and code != str(status) else "",
                                    (": " + self.scrub(message)[:200]) if message else "")
            if lc in _AUTH_CODES or status == 401:
                return "auth", status, code, text
            if lc in _CONFIG_CODES or lc == "requesttimetooskewed":
                return "config", status, code, text
            if lc in _MISSING_CODES or status == 404:
                return "missing", status, code, text
            if lc in _RETRY_CODES or status == 429 or (status is not None and status >= 500):
                return "retry", status, code, text
            if status == 403 or lc in {"accessdenied", "403", "accountproblem", "allaccessdisabled"}:
                return "plan", status, code, text
            return "http", status, code, text
        if name in _CRED_NAMES:
            return "config", None, "", "%s: %s" % (name, self.scrub(exc))
        if _is_network(exc):
            return "retry", None, "", "%s: %s" % (name, self.scrub(exc))
        if _is_aws(exc):
            return "http", None, "", "%s: %s" % (name, self.scrub(exc))
        return None, None, "", ""

    def _error(self, kind: str, what: str, status, code: str, text: str) -> FlatFilesError:
        code_out = code or None
        lc = str(code or "").lower()
        # (a config message names the fix FIRST: the page and the tray may cut the server's own text)
        if lc == "requesttimetooskewed":
            msg = ("the clock on this PC is wrong - set the Windows time to sync automatically (Massive refused "
                   "%s: %s)" % (what, text))
            return FlatFilesError("config", msg, status, code_out)
        if kind == "config":
            if lc in ("nosuchbucket", "invalidbucketname"):
                msg = ("Massive Flat Files has no bucket %r - check %s in app\\.env, default %s (%s)"
                       % (self.bucket, ENV_BUCKET, DEFAULT_BUCKET, text))
            elif lc == "permanentredirect":
                msg = ("Massive Flat Files sent %s elsewhere - check %s in app\\.env, default %s (%s)"
                       % (what, ENV_ENDPOINT, DEFAULT_ENDPOINT, text))
            else:
                msg = ("Massive Flat Files could not be used for %s - check the %s and %s lines in app\\.env (%s)"
                       % (what, ENV_KEY_ID, ENV_SECRET, text))
            return FlatFilesError("config", msg, status, code_out)
        if kind == "auth":
            msg = ("Massive did not accept the Flat Files keys for %s (%s) - copy the Access Key ID and the "
                   "Secret Access Key again from massive.com/dashboard/keys into app\\.env" % (what, text))
        elif kind == "plan":
            msg = ("Massive refused %s (%s) - the plan does not include these files, the day is older than "
                   "the plan's history, or the keys in app\\.env are not the Flat Files pair" % (what, text))
        elif kind == "missing":
            msg = "%s is not on Massive (not published yet, or no trading that day) (%s)" % (what, text)
        else:
            msg = "Massive Flat Files answered %s for %s" % (text, what)
        return FlatFilesError(kind, msg, status, code_out)

    # ── listing ──

    def _list_month(self, year: int, month: int) -> list[dict]:
        """Every day file in one month folder (all pages): ``[{day, key, size, etag}]``
        oldest first. Fills the listing cache."""
        prefix = "%s%04d/%02d/" % (PREFIX, year, month)
        what = "the file list for %04d-%02d" % (year, month)
        out: dict[str, dict] = {}
        token = None
        for _ in range(LIST_MAX_PAGES):
            kw = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": LIST_PAGE}
            if token:
                kw["ContinuationToken"] = token
            try:
                resp = self._call(what, "list_objects_v2", **kw) or {}
            except FlatFilesError as exc:
                raise self._list_config(exc) or exc from None
            for obj in resp.get("Contents") or ():
                if not isinstance(obj, dict):
                    continue
                key = str(obj.get("Key") or "")
                d = day_of_key(key)
                size = _size(obj.get("Size"))
                if d is None or size is None:
                    continue
                out[key] = {"day": d, "key": key, "size": size, "etag": _etag(obj.get("ETag"))}
            token = resp.get("NextContinuationToken") if resp.get("IsTruncated") else None
            if not token:
                break
        else:
            raise FlatFilesError("http", "Massive Flat Files kept paging past %d pages for %s" % (LIST_MAX_PAGES, what))
        items = sorted(out.values(), key=lambda i: i["day"])
        stamp = self._now()
        with self._lock:
            for i in items:
                self._remote[i["key"]] = ({"size": i["size"], "etag": i["etag"]}, stamp)
        return items

    def _list_config(self, exc: FlatFilesError) -> FlatFilesError | None:
        """A file LIST answered 404 (``missing``) or 400 / 405 (``http``): an S3 list never
        says "not found" for a month with no files (it answers an empty list), so the
        endpoint or the bucket is wrong - a ``config`` error naming the two settings (a 404
        on one day's file stays ``missing``: not published yet). None for anything else."""
        if not (exc.kind == "missing" or (exc.kind == "http" and exc.status in (400, 405))):
            return None
        code = str(exc.code or "")
        http = "HTTP %s%s" % (exc.status if exc.status is not None else "?",
                              (", " + code) if code and code != str(exc.status) else "")
        return FlatFilesError("config", "Massive Flat Files has no file list at %s, bucket %r - check %s and %s in "
                              "app\\.env, defaults %s and %s (%s)" % (self.endpoint, self.bucket, ENV_ENDPOINT,
                                                                       ENV_BUCKET, DEFAULT_ENDPOINT, DEFAULT_BUCKET,
                                                                       http),
                              exc.status, exc.code)

    def list_days(self, start, end) -> list[dict]:
        """The day files on Massive from ``start`` to ``end`` (inclusive):
        ``[{"day": date, "key", "size", "etag"}]`` OLDEST first. Lists each month folder
        (never guesses file names - holidays and weekends simply have none). A month the
        plan refuses (403 - older than its history) is skipped and named in
        ``self.refused_months``; when EVERY month is refused the ``plan`` error is raised."""
        s, e = _need_date(start, "list_days"), _need_date(end, "list_days")
        if s > e:
            self.refused_months = []
            return []
        out: list[dict] = []
        refused: list[str] = []
        listed = 0
        last_plan: FlatFilesError | None = None
        for y, m in _months(s, e):
            try:
                items = self._list_month(y, m)
            except FlatFilesError as exc:
                if exc.kind == "plan":
                    refused.append("%04d-%02d" % (y, m))
                    last_plan = exc
                    continue
                raise
            listed += 1
            out.extend(i for i in items if s <= i["day"] <= e)
        self.refused_months = refused
        if listed == 0 and last_plan is not None:
            raise last_plan
        if refused:
            self._log(logging.INFO, "Flat Files: months outside the plan (refused): %s" % ", ".join(refused))
        return sorted(out, key=lambda i: i["day"])

    def _cached(self, key: str) -> dict | None:
        with self._lock:
            hit = self._remote.get(key)
        if not hit:
            return None
        info, at = hit
        try:
            age = (self._now() - at).total_seconds()
        except Exception:  # noqa: BLE001
            return None
        return dict(info) if 0 <= age <= LIST_CACHE_S else None

    def _head(self, key: str, what: str) -> dict:
        resp = self._call(what, "head_object", Bucket=self.bucket, Key=key) or {}
        info = {"size": _size(resp.get("ContentLength")), "etag": _etag(resp.get("ETag"))}
        with self._lock:
            self._remote[key] = (dict(info), self._now())
        return info

    # ── the files ──

    def local_path(self, day) -> Path:
        """Where one day's file lives on this PC (whether or not it is there)."""
        return self.root.joinpath(*key_for(day).split("/"))

    def manifest(self) -> dict:
        """A copy of the manifest: ``{key: {"day", "size", "etag", "downloaded_at"}}``."""
        with self._lock:
            return copy.deepcopy(self._manifest)

    def local_days(self) -> list[_dt.date]:
        """The days whose file is on this PC (in the manifest and on disk), oldest first."""
        with self._lock:
            keys = list(self._manifest)
        out = []
        for k in keys:
            d = day_of_key(k)
            if d is not None and self.local_path(d).is_file():
                out.append(d)
        return sorted(out)

    def ensure(self, day, *, refresh: bool = False) -> Path | None:
        """The local path of ``day``'s file, downloading it first when it is not on this PC
        or Massive's copy changed (size / etag differ from the manifest). None when Massive
        has no file for that day (not published yet - it lands about 11:00 New York time the
        next day - a weekend, a holiday). A file already here is not checked again unless a
        recent ``list_days`` / ``changed`` saw a different copy or ``refresh=True`` (one
        HEAD request). A file kept from a day the plan no longer serves stays as it is.
        ``self.last_action``: downloaded / present / adopted / kept / missing."""
        d = _need_date(day, "ensure")
        key = key_for(d)
        path = self.local_path(d)
        what = "the day file %s" % d.isoformat()
        with self._lock:
            entry = copy.deepcopy(self._manifest.get(key))
        local_size = path.stat().st_size if path.is_file() else None
        intact = entry is not None and local_size is not None and local_size == _size(entry.get("size"))
        info = None if refresh else self._cached(key)
        if intact and info is None and not refresh:
            self.last_action = "present"
            return path
        if info is None:
            try:
                info = self._head(key, what)
            except FlatFilesError as exc:
                if exc.kind in ("missing", "plan") and local_size is not None:
                    if entry is None:   # a file with no manifest line: recorded as it is
                        self._record(key, d, local_size, None)
                    self.last_action = "kept"
                    return path
                if exc.kind == "missing":
                    self.last_action = "missing"
                    return None
                raise
        if intact and _same_remote(entry, info):
            self.last_action = "present"
            return path
        if entry is None and local_size is not None and local_size == info.get("size"):
            self._record(key, d, local_size, info.get("etag"))     # a file here without its manifest line
            self.last_action = "adopted"
            return path
        got = self._download(key, path, what)
        if got is None:
            if local_size is not None:
                self.last_action = "kept"
                return path
            self.last_action = "missing"
            return None
        self._record(key, d, got["size"], got["etag"])
        with self._lock:
            self._remote[key] = ({"size": got["size"], "etag": got["etag"]}, self._now())
            self.downloads += 1
            self.downloaded_bytes += got["size"]
        self.last_action = "downloaded"
        self._log(logging.INFO, "Flat Files: downloaded %s (%.1f MB)" % (d.isoformat(), got["size"] / 1e6))
        return path

    def ensure_range(self, start, end, *, on_day=None, stop=None) -> dict:
        """``ensure`` every day file Massive lists from ``start`` to ``end``, NEWEST FIRST
        (the days that matter most land first). ``on_day(done, total, day, action)`` after
        each day (an error in it is ignored); ``stop()`` -> True ends the walk early. A
        key / plan / network error stops the walk and is raised. Returns
        ``{"listed", "downloaded", "present", "missing", "bytes", "stopped", "refused_months"}``."""
        items = self.list_days(start, end)
        out = {"listed": len(items), "downloaded": 0, "present": 0, "missing": 0, "bytes": 0,
               "stopped": False, "refused_months": list(self.refused_months)}
        total = len(items)
        for n, item in enumerate(reversed(items), 1):
            if stop is not None and stop():
                out["stopped"] = True
                break
            before = self.downloaded_bytes
            path = self.ensure(item["day"])
            action = self.last_action or ("missing" if path is None else "present")
            if action == "downloaded":
                out["downloaded"] += 1
                out["bytes"] += self.downloaded_bytes - before
            elif action == "missing":
                out["missing"] += 1
            else:
                out["present"] += 1
            if on_day is not None:
                try:
                    on_day(n, total, item["day"], action)
                except Exception:  # noqa: BLE001 - a progress hook never stops the walk
                    pass
        return out

    def changed(self, days) -> list[_dt.date]:
        """The days among ``days`` whose file on Massive differs from the manifest (size or
        etag) - Massive re-publishes old days now and then. Lists each month once (days
        not in the manifest, days no longer listed and months the plan refuses are left
        out). The listing is cached, so ``ensure(day)`` then downloads the new copy."""
        ds = sorted({d for d in (_to_date(x) for x in (days or ())) if d is not None})
        if not ds:
            return []
        remote: dict[str, dict] = {}
        for y, m in sorted({(d.year, d.month) for d in ds}):
            try:
                for item in self._list_month(y, m):
                    remote[item["key"]] = item
            except FlatFilesError as exc:
                if exc.kind == "plan":
                    continue
                raise
        with self._lock:
            man = copy.deepcopy(self._manifest)
        out = []
        for d in ds:
            key = key_for(d)
            entry, info = man.get(key), remote.get(key)
            if entry is None or info is None:
                continue
            if not _same_remote(entry, info):
                out.append(d)
        return out

    # ── download + manifest (atomic writes) ──

    def _download(self, key: str, path: Path, what: str) -> dict | None:
        """Download ``key`` to ``path`` through a temp file beside it (renamed into place
        only when complete). ``{size, etag}`` of what was written; None when the object is
        not there (NoSuchKey)."""
        tmp = path.with_name("%s.%d.part" % (path.name, os.getpid()))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise FlatFilesError("config", "could not create the folder %s on this PC (%s)" % (
                path.parent, self.scrub(exc))) from None

        def attempt():
            resp = self._send("get_object", Bucket=self.bucket, Key=key) or {}
            body = resp.get("Body")
            if body is None:
                raise FlatFilesError("http", "Massive Flat Files sent no file body for %s" % what)
            want = _size(resp.get("ContentLength"))
            n = 0
            head = b""
            try:
                try:
                    fh = open(tmp, "wb")
                except OSError as exc:
                    raise _LocalWriteError(exc) from None
                with fh:
                    while True:
                        chunk = body.read(CHUNK)
                        if not chunk:
                            break
                        if len(head) < 2:
                            head = (head + chunk[:2])[:2]
                        try:
                            fh.write(chunk)
                        except OSError as exc:
                            raise _LocalWriteError(exc) from None
                        n += len(chunk)
                    try:
                        fh.flush()
                        os.fsync(fh.fileno())
                    except OSError as exc:
                        raise _LocalWriteError(exc) from None
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
            if want is not None and n != want:
                raise ConnectionError("the download of %s stopped early (%d of %d bytes)" % (what, n, want))
            if n < 2 or head != GZIP_MAGIC:
                raise FlatFilesError("http", "Massive Flat Files sent %s but it is not a gzip file (%d bytes)"
                                     % (what, n))
            try:
                _check_gzip(tmp)
            except (OSError, EOFError, zlib.error) as exc:   # not retried here: asked for again later
                raise FlatFilesError("http", "Massive Flat Files sent %s but the file is damaged (%s: %s)"
                                     % (what, type(exc).__name__, self.scrub(exc))) from None
            return {"size": n, "etag": _etag(resp.get("ETag"))}

        try:
            got = self._retry(what, attempt)
        except FlatFilesError as exc:
            _remove(tmp)
            if exc.kind == "missing":
                return None
            raise
        except BaseException:
            _remove(tmp)
            raise
        try:
            _replace(tmp, path, self._sleep)
        except OSError as exc:
            _remove(tmp)
            raise FlatFilesError("config", "could not save %s on this PC (%s)" % (what, self.scrub(exc))) from None
        return got

    def discard(self, day) -> Path | None:
        """Set ``day``'s local file aside because it cannot be read (a damaged copy whose
        size still matches): renamed to ``<name>.bad`` (an older ``.bad`` is replaced) -
        NEVER deleted, a day older than the plan's 2 years cannot be downloaded again - and
        its manifest line and cached listing dropped, so the next ``ensure`` downloads a
        fresh copy (or, for a day the plan no longer serves, reports it refused). Returns
        the ``.bad`` path, None when no file was here."""
        d = _need_date(day, "discard")
        key = key_for(d)
        path = self.local_path(d)
        bad = path.with_name(path.name + ".bad")
        moved = None
        with self._lock:
            if path.is_file():
                try:
                    _replace(path, bad, self._sleep)
                except OSError as exc:
                    raise FlatFilesError("config", "could not set aside the damaged day file %s on this PC (%s)"
                                         % (d.isoformat(), self.scrub(exc))) from None
                moved = bad
            self._remote.pop(key, None)
            if self._manifest.pop(key, None) is not None:
                self._save_manifest()
        self._log(logging.WARNING, "Flat Files: the day file %s could not be read - %s; downloaded again at the "
                  "next try" % (d.isoformat(), ("set aside as " + bad.name) if moved else "no file here"))
        return moved

    def _record(self, key: str, day: _dt.date, size: int, etag: str | None) -> None:
        with self._lock:
            self._manifest[key] = {"day": day.isoformat(), "size": int(size), "etag": etag,
                                   "downloaded_at": self._stamp()}
            self._save_manifest()

    def _manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    def _load_manifest(self) -> dict:
        p = self._manifest_path()
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except Exception as exc:  # noqa: BLE001 - a broken manifest is set aside, never fatal
            bad = p.with_name(MANIFEST_NAME + ".bad")
            try:
                os.replace(p, bad)
            except OSError:
                pass
            self._log(logging.WARNING, "Flat Files: the manifest could not be read (%s: %s) - set aside as %s; "
                      "the files on disk are re-checked one by one" % (type(exc).__name__, exc, bad.name))
            return {}
        out = {}
        for k, v in data.items():
            if day_of_key(k) is None or not isinstance(v, dict):
                continue
            out[k] = {"day": str(v.get("day") or day_of_key(k).isoformat()), "size": _size(v.get("size")),
                      "etag": _etag(v.get("etag")), "downloaded_at": v.get("downloaded_at")}
        return out

    def _save_manifest(self) -> None:
        """Write the manifest through a temp file + rename (a crash leaves the old one whole)."""
        p = self._manifest_path()
        tmp = p.with_name("%s.%d.tmp" % (MANIFEST_NAME, os.getpid()))
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._manifest, fh, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            _replace(tmp, p, self._sleep)
        except Exception as exc:  # noqa: BLE001
            _remove(tmp)
            raise FlatFilesError("config", "could not save the Flat Files manifest on this PC (%s: %s)" % (
                type(exc).__name__, self.scrub(exc))) from None


def _remove(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _check_gzip(path: Path) -> None:
    """Read the whole gzip file at ``path`` through zlib - each member's CRC and length are
    checked - and raise ``zlib.error`` / ``EOFError`` / ``OSError`` when it is damaged or
    cut short. Nothing is kept in memory but one piece at a time."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    fresh = True                              # no byte of the current member read yet
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            while chunk:
                if fresh and not chunk.strip(b"\x00"):
                    chunk = b""               # zero padding after the last member (gzip allows it)
                    break
                fresh = False
                d.decompress(chunk)
                if not d.eof:
                    break
                chunk = d.unused_data         # another member follows
                d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                fresh = True
    if not fresh:
        raise EOFError("the file ends before its end-of-stream marker (cut short)")


def _replace(src: Path, dst: Path, sleep) -> None:
    """``os.replace`` retried on a Windows sharing violation (a virus scanner or a reader
    holding the old file for a moment)."""
    for n in range(REPLACE_TRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if n + 1 >= REPLACE_TRIES:
                raise
            sleep(REPLACE_WAIT_S)
