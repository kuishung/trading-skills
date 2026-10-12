"""The Massive Flat Files downloader (``app/services/flat_files.py``, v4.138).

No network and no boto3: every test hands ``FlatFiles`` a fake S3 client with the
boto3 shapes (``list_objects_v2`` / ``head_object`` / ``get_object`` / ``download_file``)
and a fake clock whose ``sleep`` only records, so the back-off is checked to the second
without waiting. The keys below are made-up test values.
"""
from __future__ import annotations

import datetime as _dt
import gzip
import json
import logging
import sys
import types

import pytest

from app.services import flat_files, massive
from app.services.flat_files import FlatFiles, FlatFilesError

KEY_ID = "UNITTESTKEYID0123456789"                      # not a real key
SECRET = "unit-test-secret-Zq9vX2/abcdefghijk+0123"     # not a real secret
CSV_HEAD = b"ticker,volume,open,close,high,low,window_start,transactions\n"


def gz(day: str, rows: int = 3, salt: str = "") -> bytes:
    body = CSV_HEAD + b"".join(b"O:AAPL261016C%08d,%d,1,1.1,1.2,0.9,1760000000000000000,3\n" % (150000 + i, i + 1)
                               for i in range(rows))
    return gzip.compress(body + salt.encode(), mtime=0)


def key(day: str) -> str:
    return "us_options_opra/day_aggs_v1/%s/%s/%s.csv.gz" % (day[:4], day[5:7], day)


class ClientError(Exception):
    """botocore's ClientError shape: ``.response`` with Error + ResponseMetadata."""

    def __init__(self, code: str, status: int, message: str = ""):
        super().__init__("An error occurred (%s) when calling the operation: %s" % (code, message))
        self.response = {"Error": {"Code": code, "Message": message},
                         "ResponseMetadata": {"HTTPStatusCode": status}}


class EndpointConnectionError(Exception):
    """botocore's connection error, by name (the module classifies by class name)."""


class Body:
    def __init__(self, data: bytes, cut: int | None = None, fail: Exception | None = None):
        self.data, self.pos, self.cut, self.fail = data, 0, cut, fail
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        if self.fail is not None and self.pos > 0:
            raise self.fail
        end = len(self.data) if n is None or n < 0 else min(len(self.data), self.pos + n)
        if self.cut is not None:
            end = min(end, self.cut)
        if self.fail is not None and end >= len(self.data):
            end = max(1, len(self.data) // 2)
        out = self.data[self.pos:end]
        self.pos = end
        return out

    def close(self):
        self.closed = True


class FakeS3:
    """Objects in memory; ``page`` keys per list page (pagination is exercised);
    ``fail[op]`` = a list of exceptions raised by the next calls of that op (in order);
    ``refuse`` = month prefixes whose listing answers 403."""

    def __init__(self, objects: dict[str, bytes] | None = None, page: int = 2):
        self.objects: dict[str, dict] = {}
        for k, v in (objects or {}).items():
            self.put(k, v)
        self.page = page
        self.calls: list[tuple] = []
        self.fail: dict[str, list] = {}
        self.refuse: set[str] = set()
        self.cut: dict[str, int] = {}
        self.stream_fail: dict[str, Exception] = {}

    def put(self, k: str, data: bytes, etag: str | None = None):
        self.objects[k] = {"data": data, "etag": '"%s"' % (etag or "e%x" % (hash(data) & 0xFFFFFFF))}

    def _maybe_fail(self, op):
        q = self.fail.get(op)
        if q:
            raise q.pop(0)

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=1000, ContinuationToken=None):
        self.calls.append(("list", Prefix, ContinuationToken))
        self._maybe_fail("list_objects_v2")
        if any(Prefix.endswith(m) for m in self.refuse):
            raise ClientError("AccessDenied", 403, "Access Denied")
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        chunk = keys[start:start + self.page]
        resp = {"Contents": [{"Key": k, "Size": len(self.objects[k]["data"]), "ETag": self.objects[k]["etag"],
                              "LastModified": _dt.datetime(2026, 10, 1)} for k in chunk],
                "IsTruncated": start + self.page < len(keys), "KeyCount": len(chunk)}
        if resp["IsTruncated"]:
            resp["NextContinuationToken"] = str(start + self.page)
        return resp

    def head_object(self, Bucket, Key):
        self.calls.append(("head", Key))
        self._maybe_fail("head_object")
        if Key not in self.objects:
            raise ClientError("404", 404)
        o = self.objects[Key]
        return {"ContentLength": len(o["data"]), "ETag": o["etag"]}

    def get_object(self, Bucket, Key, Range=None):
        self.calls.append(("get", Key))
        self._maybe_fail("get_object")
        if Key not in self.objects:
            raise ClientError("NoSuchKey", 404, "The specified key does not exist.")
        o = self.objects[Key]
        return {"Body": Body(o["data"], self.cut.get(Key), self.stream_fail.get(Key)),
                "ContentLength": len(o["data"]), "ETag": o["etag"]}

    def download_file(self, Bucket, Key, Filename):   # the boto3 shape (not used by the module)
        self.calls.append(("download", Key))
        with open(Filename, "wb") as fh:
            fh.write(self.objects[Key]["data"])

    def ops(self, name):
        return [c for c in self.calls if c[0] == name]


class Clock:
    def __init__(self):
        self.t = _dt.datetime(2026, 10, 12, 3, 0, 0)
        self.sleeps: list[float] = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(round(s, 3))


@pytest.fixture
def keys_env(monkeypatch):
    monkeypatch.setenv("TST_MASSIVE_S3_KEY_ID", KEY_ID)
    monkeypatch.setenv("TST_MASSIVE_S3_SECRET", SECRET)
    monkeypatch.delenv("TST_MASSIVE_S3_ENDPOINT", raising=False)
    monkeypatch.delenv("TST_MASSIVE_S3_BUCKET", raising=False)


def make(tmp_path, s3=None, **kw):
    clock = Clock()
    ff = FlatFiles(client=s3 if s3 is not None else FakeS3(), root=tmp_path / "flatfiles",
                   now=clock.now, sleep=clock.sleep, **kw)
    return ff, clock


def no_parts(root) -> bool:
    return not [p for p in root.rglob("*") if p.name.endswith((".part", ".tmp"))]


# ────────────────────────────────── settings ──────────────────────────────────

def test_configured_needs_both_keys(monkeypatch):
    monkeypatch.setenv("TST_MASSIVE_S3_KEY_ID", KEY_ID)
    monkeypatch.setenv("TST_MASSIVE_S3_SECRET", SECRET)
    assert flat_files.configured() is True
    monkeypatch.setenv("TST_MASSIVE_S3_SECRET", "   ")
    assert flat_files.configured() is False
    monkeypatch.delenv("TST_MASSIVE_S3_SECRET")
    assert flat_files.configured() is False
    monkeypatch.setenv("TST_MASSIVE_S3_SECRET", SECRET)
    monkeypatch.delenv("TST_MASSIVE_S3_KEY_ID")
    assert flat_files.configured() is False


def test_endpoint_bucket_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("TST_MASSIVE_S3_ENDPOINT", raising=False)
    monkeypatch.delenv("TST_MASSIVE_S3_BUCKET", raising=False)
    assert flat_files.endpoint() == "https://files.massive.com"
    assert flat_files.bucket() == "flatfiles"
    monkeypatch.setenv("TST_MASSIVE_S3_ENDPOINT", "files.example.test/")
    monkeypatch.setenv("TST_MASSIVE_S3_BUCKET", "other")
    assert flat_files.endpoint() == "https://files.example.test"
    assert flat_files.bucket() == "other"


def test_key_layout_and_local_path(tmp_path):
    assert flat_files.key_for(_dt.date(2026, 10, 9)) == "us_options_opra/day_aggs_v1/2026/10/2026-10-09.csv.gz"
    assert flat_files.key_for("2025-01-02") == "us_options_opra/day_aggs_v1/2025/01/2025-01-02.csv.gz"
    assert flat_files.day_of_key(key("2026-10-09")) == _dt.date(2026, 10, 9)
    assert flat_files.day_of_key("us_options_opra/day_aggs_v1/2026/09/2026-10-09.csv.gz") is None  # wrong folder
    assert flat_files.day_of_key("us_options_opra/minute_aggs_v1/2026/10/2026-10-09.csv.gz") is None
    ff, _ = make(tmp_path)
    p = ff.local_path("2026-10-09")
    assert p == tmp_path / "flatfiles" / "us_options_opra" / "day_aggs_v1" / "2026" / "10" / "2026-10-09.csv.gz"


def test_no_keys_is_a_config_error(tmp_path, monkeypatch):
    monkeypatch.delenv("TST_MASSIVE_S3_KEY_ID", raising=False)
    monkeypatch.delenv("TST_MASSIVE_S3_SECRET", raising=False)
    ff = FlatFiles(root=tmp_path / "ff")
    assert "keys=missing" in repr(ff)
    with pytest.raises(FlatFilesError) as ei:
        ff.list_days("2026-10-01", "2026-10-10")
    assert ei.value.kind == "config"
    assert "TST_MASSIVE_S3_KEY_ID" in str(ei.value) and "TST_MASSIVE_S3_SECRET" in str(ei.value)


def test_no_boto3_is_a_config_error_without_the_secret(tmp_path, keys_env, monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)        # import boto3 -> ImportError, even where it exists
    ff = FlatFiles(root=tmp_path / "ff")
    with pytest.raises(FlatFilesError) as ei:
        ff.ensure("2026-10-09")
    assert ei.value.kind == "config"
    assert "boto3" in str(ei.value)
    assert SECRET not in str(ei.value) and SECRET not in repr(ei.value)


def test_real_client_is_built_lazily_with_s3v4(tmp_path, keys_env, monkeypatch):
    """A stand-in boto3 records how the client is built - nothing is sent anywhere."""
    seen = {}
    fake_s3 = FakeS3({key("2026-10-09"): gz("2026-10-09")})

    class Session:
        def __init__(self, **kw):
            seen["session"] = kw

        def client(self, service, **kw):
            seen["client"] = (service, kw)
            return fake_s3

    class Config:
        def __init__(self, **kw):
            self.kw = kw

    boto3 = types.ModuleType("boto3")
    boto3.session = types.SimpleNamespace(Session=Session)
    botocore = types.ModuleType("botocore")
    botocore_config = types.ModuleType("botocore.config")
    botocore_config.Config = Config
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", botocore_config)

    ff = FlatFiles(root=tmp_path / "ff", sleep=lambda s: None)
    assert "session" not in seen                       # nothing built until the first request
    assert ff.ensure("2026-10-09") is not None
    assert seen["session"] == {"aws_access_key_id": KEY_ID, "aws_secret_access_key": SECRET}
    service, kw = seen["client"]
    assert service == "s3" and kw["endpoint_url"] == "https://files.massive.com"
    assert kw["config"].kw["signature_version"] == "s3v4"
    assert kw["config"].kw["retries"]["total_max_attempts"] == 1   # the module retries, not botocore
    assert SECRET not in repr(ff) and KEY_ID not in repr(ff)

    # new keys / endpoint in app/.env: reset_client builds a fresh client from them
    monkeypatch.setenv("TST_MASSIVE_S3_ENDPOINT", "https://files.example.test")
    monkeypatch.setenv("TST_MASSIVE_S3_SECRET", SECRET + "-new")
    ff.reset_client()
    assert ff.ensure("2026-10-09", refresh=True) is not None
    assert seen["client"][1]["endpoint_url"] == "https://files.example.test"
    assert seen["session"]["aws_secret_access_key"] == SECRET + "-new"
    assert SECRET not in ff.scrub("old " + SECRET)       # the old pair stays masked too


# ────────────────────────────────── listing ──────────────────────────────────

def test_list_days_walks_each_month_across_a_year_boundary(tmp_path):
    days = ["2025-11-28", "2025-12-01", "2025-12-15", "2025-12-31", "2026-01-02", "2026-01-05",
            "2026-01-12", "2026-02-02"]
    s3 = FakeS3({key(d): gz(d) for d in days}, page=2)
    s3.objects["us_options_opra/day_aggs_v1/2025/12/README.txt"] = {"data": b"x", "etag": '"r"'}
    ff, _ = make(tmp_path, s3)
    got = ff.list_days(_dt.date(2025, 12, 10), _dt.date(2026, 1, 6))
    assert [g["day"] for g in got] == [_dt.date(2025, 12, 15), _dt.date(2025, 12, 31),
                                      _dt.date(2026, 1, 2), _dt.date(2026, 1, 5)]
    assert got[0]["key"] == key("2025-12-15")
    assert got[0]["size"] == len(gz("2025-12-15")) and got[0]["etag"]
    prefixes = sorted({c[1] for c in s3.ops("list")})
    assert prefixes == ["us_options_opra/day_aggs_v1/2025/12/", "us_options_opra/day_aggs_v1/2026/01/"]
    assert any(c[2] for c in s3.ops("list"))           # a second page was asked for (pagination)
    assert ff.list_days("2026-01-10", "2026-01-01") == []
    assert not s3.ops("get") and not s3.ops("head")    # listing never downloads


def test_list_days_skips_months_outside_the_plan(tmp_path):
    s3 = FakeS3({key("2024-10-01"): gz("a"), key("2024-11-01"): gz("b"), key("2024-12-02"): gz("c")})
    s3.refuse = {"2024/10/"}
    ff, _ = make(tmp_path, s3)
    got = ff.list_days("2024-10-01", "2024-12-31")
    assert [g["day"].isoformat() for g in got] == ["2024-11-01", "2024-12-02"]
    assert ff.refused_months == ["2024-10"]
    s3.refuse = {"2024/10/", "2024/11/", "2024/12/"}
    with pytest.raises(FlatFilesError) as ei:
        ff.list_days("2024-10-01", "2024-12-31")
    assert ei.value.kind == "plan" and ei.value.status == 403


# ────────────────────────────────── ensure ──────────────────────────────────

def test_ensure_downloads_writes_the_manifest_and_skips_present_files(tmp_path):
    data = gz("2026-10-09")
    s3 = FakeS3({key("2026-10-09"): data})
    ff, clock = make(tmp_path, s3)
    p = ff.ensure("2026-10-09")
    assert p == ff.local_path("2026-10-09") and p.read_bytes() == data
    assert ff.last_action == "downloaded" and ff.downloads == 1 and ff.downloaded_bytes == len(data)
    entry = ff.manifest()[key("2026-10-09")]
    assert entry == {"day": "2026-10-09", "size": len(data), "etag": s3.objects[key("2026-10-09")]["etag"].strip('"'),
                     "downloaded_at": "2026-10-12T03:00:00Z"}
    on_disk = json.loads((tmp_path / "flatfiles" / "manifest.json").read_text())
    assert on_disk == ff.manifest()
    assert no_parts(tmp_path)

    # present: no request at all
    n = len(s3.calls)
    assert ff.ensure(_dt.date(2026, 10, 9)) == p and ff.last_action == "present"
    assert len(s3.calls) == n

    # a new process reads the manifest back and skips the file too
    ff2, _ = make(tmp_path, s3)
    assert ff2.manifest() == ff.manifest()
    assert ff2.ensure("2026-10-09") == p and ff2.last_action == "present"
    assert len(s3.calls) == n


def test_ensure_returns_none_for_a_day_not_published_yet(tmp_path):
    s3 = FakeS3({key("2026-10-08"): gz("x")})
    ff, _ = make(tmp_path, s3)
    assert ff.ensure("2026-10-09") is None and ff.last_action == "missing"
    assert not ff.local_path("2026-10-09").exists() and ff.manifest() == {}
    # NoSuchKey on the GET itself (the listing said it was there, then it was gone)
    s3.put(key("2026-10-10"), gz("y"))
    ff.list_days("2026-10-01", "2026-10-31")
    del s3.objects[key("2026-10-10")]
    assert ff.ensure("2026-10-10") is None
    assert no_parts(tmp_path)


def test_ensure_range_downloads_newest_first_and_skips_present(tmp_path):
    days = ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]
    s3 = FakeS3({key(d): gz(d) for d in days})
    ff, _ = make(tmp_path, s3)
    ff.ensure("2026-09-30")                              # already here
    s3.calls.clear()
    seen = []
    out = ff.ensure_range("2026-09-28", "2026-10-02", on_day=lambda n, t, d, a: seen.append((n, t, d.isoformat(), a)))
    assert [c[1] for c in s3.ops("get")] == [key("2026-10-02"), key("2026-10-01"), key("2026-09-29")]
    assert not s3.ops("head")                            # the listing answered for every day
    assert out["listed"] == 4 and out["downloaded"] == 3 and out["present"] == 1 and out["missing"] == 0
    assert out["bytes"] == sum(len(gz(d)) for d in ("2026-09-29", "2026-10-01", "2026-10-02"))
    assert seen[0] == (1, 4, "2026-10-02", "downloaded") and seen[2] == (3, 4, "2026-09-30", "present")
    # a stop request ends the walk
    out2 = ff.ensure_range("2026-09-28", "2026-10-02", stop=lambda: True)
    assert out2["stopped"] is True and out2["downloaded"] == 0


def test_ensure_redownloads_when_the_etag_or_size_changes(tmp_path):
    s3 = FakeS3({key("2025-10-15"): gz("old")}, page=50)
    ff, _ = make(tmp_path, s3)
    ff.ensure("2025-10-15")
    old_etag = ff.manifest()[key("2025-10-15")]["etag"]

    # Massive re-publishes the day: same size, new etag
    s3.objects[key("2025-10-15")]["etag"] = '"republished-1"'
    assert ff.changed(["2025-10-15", "2025-10-16"]) == [_dt.date(2025, 10, 15)]
    s3.calls.clear()
    assert ff.ensure("2025-10-15") is not None and ff.last_action == "downloaded"
    assert len(s3.ops("get")) == 1
    assert ff.manifest()[key("2025-10-15")]["etag"] == "republished-1" != old_etag
    assert ff.changed(["2025-10-15"]) == []

    # a bigger file (new size), seen by a refresh=True check (one HEAD)
    new = gz("2025-10-15", rows=40, salt="v2")
    s3.put(key("2025-10-15"), new, etag="republished-2")
    ff2, _ = make(tmp_path, s3)                          # a fresh process: no listing cache
    assert ff2.ensure("2025-10-15") is not None and ff2.last_action == "present"   # no check by default
    assert ff2.ensure("2025-10-15", refresh=True).read_bytes() == new
    assert ff2.last_action == "downloaded"
    assert ff2.manifest()[key("2025-10-15")]["size"] == len(new)


def test_changed_ignores_days_not_downloaded_and_refused_months(tmp_path):
    s3 = FakeS3({key("2024-10-01"): gz("a"), key("2026-10-01"): gz("b"), key("2026-10-02"): gz("c")})
    ff, _ = make(tmp_path, s3)
    ff.ensure("2024-10-01")
    ff.ensure("2026-10-01")
    s3.refuse = {"2024/10/"}                             # now outside the plan
    s3.objects[key("2024-10-01")]["etag"] = '"zzz"'
    assert ff.changed(["2024-10-01", "2026-10-01", "2026-10-02", "junk"]) == []
    # the kept old file still answers (a 403 on the HEAD keeps it)
    s3.fail["head_object"] = [ClientError("403", 403)]
    assert ff.ensure("2024-10-01", refresh=True) == ff.local_path("2024-10-01")
    assert ff.last_action == "kept"


def test_a_file_on_disk_without_a_manifest_line_is_adopted_not_downloaded(tmp_path):
    data = gz("2026-10-05")
    s3 = FakeS3({key("2026-10-05"): data})
    ff, _ = make(tmp_path, s3)
    p = ff.local_path("2026-10-05")
    p.parent.mkdir(parents=True)
    p.write_bytes(data)
    assert ff.ensure("2026-10-05") == p and ff.last_action == "adopted"
    assert not s3.ops("get") and len(s3.ops("head")) == 1
    assert ff.manifest()[key("2026-10-05")]["size"] == len(data)


def test_a_broken_manifest_is_set_aside(tmp_path, caplog):
    root = tmp_path / "flatfiles"
    root.mkdir()
    (root / "manifest.json").write_text("{not json")
    ff, _ = make(tmp_path)
    assert ff.manifest() == {}
    assert (root / "manifest.json.bad").exists() and not (root / "manifest.json").exists()


# ────────────────────────────────── atomic writes ──────────────────────────────────

def test_a_download_that_breaks_leaves_no_file_and_no_manifest_line(tmp_path):
    data = gz("2026-10-09", rows=200)
    s3 = FakeS3({key("2026-10-09"): data})
    s3.stream_fail[key("2026-10-09")] = EndpointConnectionError("Could not connect to the endpoint URL")
    ff, clock = make(tmp_path, s3)
    with pytest.raises(FlatFilesError) as ei:
        ff.ensure("2026-10-09")
    assert ei.value.kind == "network"
    assert clock.sleeps == [2.0, 4.0, 8.0, 16.0]
    assert len(s3.ops("get")) == 5
    assert not ff.local_path("2026-10-09").exists()
    assert ff.manifest() == {} and not (tmp_path / "flatfiles" / "manifest.json").exists()
    assert no_parts(tmp_path)


def test_a_short_download_is_retried_and_an_old_copy_survives_a_failed_redownload(tmp_path):
    old = gz("2026-10-09")
    s3 = FakeS3({key("2026-10-09"): old})
    ff, clock = make(tmp_path, s3)
    p = ff.ensure("2026-10-09")
    new = gz("2026-10-09", rows=60, salt="new")
    s3.put(key("2026-10-09"), new, etag="v2")
    s3.cut[key("2026-10-09")] = len(new) - 10           # every GET stops 10 bytes short
    with pytest.raises(FlatFilesError) as ei:
        ff.ensure("2026-10-09", refresh=True)
    assert ei.value.kind == "network" and "stopped early" in str(ei.value)
    assert p.read_bytes() == old                         # the kept copy is untouched
    assert ff.manifest()[key("2026-10-09")]["size"] == len(old)
    assert no_parts(tmp_path)
    # once the stream is whole again the new copy replaces it
    s3.cut.clear()
    clock.sleeps.clear()
    s3.fail["get_object"] = [EndpointConnectionError("reset")]
    assert ff.ensure("2026-10-09", refresh=True).read_bytes() == new
    assert clock.sleeps == [2.0]


def test_a_reply_that_is_not_gzip_is_refused(tmp_path):
    s3 = FakeS3({key("2026-10-09"): b"<html>maintenance</html>"})
    ff, _ = make(tmp_path, s3)
    with pytest.raises(FlatFilesError) as ei:
        ff.ensure("2026-10-09")
    assert ei.value.kind == "http" and "gzip" in str(ei.value)
    assert not ff.local_path("2026-10-09").exists() and no_parts(tmp_path)


def test_the_manifest_write_is_atomic(tmp_path, monkeypatch):
    s3 = FakeS3({key("2026-10-08"): gz("a"), key("2026-10-09"): gz("b")})
    ff, _ = make(tmp_path, s3)
    ff.ensure("2026-10-08")
    man = tmp_path / "flatfiles" / "manifest.json"
    before = man.read_text()

    replaced = []
    real_replace = flat_files.os.replace

    def spy(src, dst):
        replaced.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(flat_files.os, "replace", spy)
    ff.ensure("2026-10-09")
    man_moves = [r for r in replaced if r[1] == str(man)]
    assert man_moves and man_moves[0][0].endswith(".tmp")
    assert str(tmp_path / "flatfiles") in man_moves[0][0]  # the temp file sits beside it (same volume)
    monkeypatch.setattr(flat_files.os, "replace", real_replace)

    # a write that dies half-way leaves the old manifest whole
    def boom(*a, **k):
        raise OSError("disk full")

    s3.put(key("2026-10-07"), gz("c"))
    good = man.read_text()
    monkeypatch.setattr(flat_files.json, "dump", boom)
    with pytest.raises(FlatFilesError) as ei:
        ff.ensure("2026-10-07")
    assert ei.value.kind == "config" and "manifest" in str(ei.value)
    assert man.read_text() == good and good != before
    assert no_parts(tmp_path)


# ────────────────────────────────── errors ──────────────────────────────────

@pytest.mark.parametrize("op,exc,kind", [
    ("list_objects_v2", ClientError("AccessDenied", 403, "Access Denied"), "plan"),
    ("list_objects_v2", ClientError("InvalidAccessKeyId", 403, "The AWS Access Key Id you provided does not exist"), "auth"),
    ("list_objects_v2", ClientError("SignatureDoesNotMatch", 403, "The request signature we calculated does not match"), "auth"),
    ("list_objects_v2", ClientError("Unauthorized", 401), "auth"),
    ("list_objects_v2", ClientError("NoSuchBucket", 404, "The specified bucket does not exist"), "config"),
    ("list_objects_v2", ClientError("RequestTimeTooSkewed", 403, "skewed"), "config"),
    # a LIST that answers 400 / 405 / a plain 404: the endpoint or the bucket is wrong (setup)
    ("list_objects_v2", ClientError("InvalidArgument", 400, "bad"), "config"),
    ("list_objects_v2", ClientError("MethodNotAllowed", 405, "nope"), "config"),
    ("list_objects_v2", ClientError("404", 404, "Not Found"), "config"),
    ("list_objects_v2", ClientError("NoSuchKey", 404, "The specified key does not exist."), "config"),
    ("list_objects_v2", ClientError("Conflict", 409, "odd"), "http"),
])
def test_s3_errors_map_to_kinds(tmp_path, op, exc, kind):
    s3 = FakeS3()
    s3.fail[op] = [exc] * 10
    ff, clock = make(tmp_path, s3)
    with pytest.raises(FlatFilesError) as ei:
        ff.list_days("2026-10-01", "2026-10-10")
    assert ei.value.kind == kind
    assert ei.value.code == exc.response["Error"]["Code"]  # the S3 code as sent, also in the text
    assert exc.response["Error"]["Code"] in str(ei.value)
    assert clock.sleeps == []                            # never retried
    assert len(s3.ops("list")) == 1


def test_missing_credentials_in_botocore_is_a_config_error(tmp_path):
    class NoCredentialsError(Exception):
        pass

    s3 = FakeS3()
    s3.fail["list_objects_v2"] = [NoCredentialsError("Unable to locate credentials")]
    ff, _ = make(tmp_path, s3)
    with pytest.raises(FlatFilesError) as ei:
        ff.list_days("2026-10-01", "2026-10-02")
    assert ei.value.kind == "config" and "TST_MASSIVE_S3_KEY_ID" in str(ei.value)


def test_head_403_is_plan_and_head_404_is_missing(tmp_path):
    s3 = FakeS3()
    ff, _ = make(tmp_path, s3)
    s3.fail["head_object"] = [ClientError("403", 403)]
    with pytest.raises(FlatFilesError) as ei:
        ff.ensure("2024-01-02")
    assert ei.value.kind == "plan" and ei.value.status == 403
    assert ff.ensure("2026-10-11") is None               # a Sunday: no file, not an error


def test_a_list_that_cannot_exist_names_the_endpoint_and_bucket_settings(tmp_path, monkeypatch):
    """An S3 list answers an empty list for a month with no files - never 404: a list that
    answers 404 (or 400 / 405) means the endpoint or the bucket is wrong, a setup error that
    names the two settings first (not "not published yet"); one day's file answering 404
    is still "not published yet"."""
    monkeypatch.setenv("TST_MASSIVE_S3_ENDPOINT", "https://api.massive.com")
    monkeypatch.delenv("TST_MASSIVE_S3_BUCKET", raising=False)
    s3 = FakeS3({key("2026-10-09"): gz("a")})
    ff, clock = make(tmp_path, s3)
    assert ff.ensure("2026-10-09") is not None           # (HEAD + GET: no list)
    for err in (ClientError("404", 404, "Not Found"), ClientError("MethodNotAllowed", 405, "nope")):
        s3.fail["list_objects_v2"] = [err, err]
        for call in (lambda: ff.list_days("2026-10-01", "2026-10-10"), lambda: ff.changed(["2026-10-09"])):
            with pytest.raises(FlatFilesError) as ei:
                call()
            e = ei.value
            assert e.kind == "config" and e.status == err.response["ResponseMetadata"]["HTTPStatusCode"]
            assert str(e).startswith("Massive Flat Files has no file list at https://api.massive.com, bucket "
                                     "'flatfiles' - check TST_MASSIVE_S3_ENDPOINT and TST_MASSIVE_S3_BUCKET in "
                                     "app\\.env, defaults https://files.massive.com and flatfiles (HTTP ")
            assert "not published yet" not in str(e)
    assert clock.sleeps == []
    assert ff.ensure("2026-10-10") is None and ff.last_action == "missing"


def test_a_damaged_download_is_refused_and_never_replaces_a_good_copy(tmp_path):
    """The whole file is decompressed (CRC + length) before it is renamed into place: a
    damaged copy is refused as ``http`` (asked for again later, not retried at once) and a
    good copy already here survives - a day older than 2 years cannot be fetched again."""
    good = gz("2026-10-09", rows=200)
    s3 = FakeS3({key("2026-10-09"): good})
    ff, clock = make(tmp_path, s3)
    p = ff.ensure("2026-10-09")
    broken = bytearray(gz("2026-10-09", rows=200, salt="v2"))
    broken[20] ^= 0xFF                                    # the gzip header is fine, the data is not
    s3.put(key("2026-10-09"), bytes(broken), etag="v2")   # Massive re-published a broken copy
    with pytest.raises(FlatFilesError) as ei:
        ff.ensure("2026-10-09", refresh=True)
    assert ei.value.kind == "http" and "damaged" in str(ei.value)
    assert clock.sleeps == [] and len(s3.ops("get")) == 2
    assert p.read_bytes() == good and ff.manifest()[key("2026-10-09")]["size"] == len(good)
    # a new day whose file ends early inside its gzip stream (the size Massive sent, no end)
    s3.put(key("2026-10-08"), gz("2026-10-08", rows=200)[:-12])
    with pytest.raises(FlatFilesError) as ei:
        ff.ensure("2026-10-08")
    assert ei.value.kind == "http" and "damaged" in str(ei.value) and "EOFError" in str(ei.value)
    assert not ff.local_path("2026-10-08").exists() and key("2026-10-08") not in ff.manifest()
    assert no_parts(tmp_path)
    # two gzip members, and zero padding after the last one, are whole files
    s3.put(key("2026-10-07"), gz("a") + gz("b") + b"\x00" * 8)
    assert ff.ensure("2026-10-07").read_bytes() == gz("a") + gz("b") + b"\x00" * 8


def test_discard_sets_a_damaged_file_aside_and_the_next_ensure_downloads_it_again(tmp_path):
    data = gz("2026-10-09", rows=200)
    s3 = FakeS3({key("2026-10-09"): data, key("2024-01-02"): gz("2024-01-02")})
    ff, _ = make(tmp_path, s3)
    p = ff.ensure("2026-10-09")
    broken = bytearray(data)
    broken[20] ^= 0xFF
    p.write_bytes(bytes(broken))                          # damaged on this PC, the size unchanged
    assert ff.ensure("2026-10-09") == p and ff.last_action == "present"     # (only the size is checked)
    bad = ff.discard("2026-10-09")
    assert bad == p.with_name(p.name + ".bad") and bad.read_bytes() == bytes(broken)   # kept, never deleted
    assert not p.exists() and key("2026-10-09") not in ff.manifest()
    assert key("2026-10-09") not in json.loads((tmp_path / "flatfiles" / "manifest.json").read_text())
    assert ff.ensure("2026-10-09") == p and ff.last_action == "downloaded" and p.read_bytes() == data
    assert len(s3.ops("get")) == 2
    assert ff.discard("2026-10-08") is None               # no file here: nothing set aside
    # a day the plan no longer serves: set aside, then reported refused - the .bad copy stays
    old = ff.ensure("2024-01-02")
    ff.discard("2024-01-02")
    s3.fail["head_object"] = [ClientError("AccessDenied", 403, "Access Denied")]
    with pytest.raises(FlatFilesError) as ei:
        ff.ensure("2024-01-02")
    assert ei.value.kind == "plan" and old.with_name(old.name + ".bad").is_file()
    assert no_parts(tmp_path)


def test_network_and_5xx_are_retried_with_back_off(tmp_path):
    s3 = FakeS3({key("2026-10-01"): gz("a")})
    ff, clock = make(tmp_path, s3)
    s3.fail["list_objects_v2"] = [EndpointConnectionError("no route"), ClientError("SlowDown", 503, "Please reduce")]
    assert len(ff.list_days("2026-10-01", "2026-10-31")) == 1
    assert clock.sleeps == [2.0, 4.0]

    clock.sleeps.clear()
    s3.fail["list_objects_v2"] = [ClientError("InternalError", 500, "We encountered an internal error")] * 9
    with pytest.raises(FlatFilesError) as ei:
        ff.list_days("2026-10-01", "2026-10-31")
    assert ei.value.kind == "network" and ei.value.status == 500
    assert clock.sleeps == [2.0, 4.0, 8.0, 16.0]
    assert "5 tries" in str(ei.value)

    clock.sleeps.clear()
    s3.fail["list_objects_v2"] = [TimeoutError("read timed out")] * 9
    with pytest.raises(FlatFilesError) as ei:
        ff.list_days("2026-10-01", "2026-10-31")
    assert ei.value.kind == "network" and ei.value.status is None


def test_a_programming_error_is_not_swallowed(tmp_path):
    s3 = FakeS3()
    s3.fail["list_objects_v2"] = [TypeError("bug")]
    ff, _ = make(tmp_path, s3)
    with pytest.raises(TypeError):
        ff.list_days("2026-10-01", "2026-10-02")


# ────────────────────────────────── the secret ──────────────────────────────────

def test_the_secret_never_appears_in_errors_reprs_or_logs(tmp_path, keys_env, caplog):
    """Worst case: the server / the transport echoes the secret and the key id back."""
    leak = "secret_access_key=%s Credential=%s/20261012/us-east-1/s3/aws4_request %s" % (SECRET, KEY_ID, SECRET)
    s3 = FakeS3({key("2026-10-01"): gz("a")})
    ff, clock = make(tmp_path, s3)
    caplog.set_level(logging.DEBUG)
    errors = []
    for op, exc in [("list_objects_v2", ClientError("SignatureDoesNotMatch", 403, leak)),
                    ("list_objects_v2", ClientError("AccessDenied", 403, leak)),
                    ("list_objects_v2", ClientError("Weird", 418, leak))]:
        s3.fail[op] = [exc]
        with pytest.raises(FlatFilesError) as ei:
            ff.list_days("2026-10-01", "2026-10-02")
        errors.append(ei.value)
    s3.fail["list_objects_v2"] = [EndpointConnectionError(leak)] * 9
    with pytest.raises(FlatFilesError) as ei:
        ff.list_days("2026-10-01", "2026-10-02")
    errors.append(ei.value)
    s3.fail["get_object"] = [ConnectionError(leak)] * 9
    with pytest.raises(FlatFilesError) as ei:
        ff.ensure("2026-10-01")
    errors.append(ei.value)
    assert caplog.records                                # the retries were logged ...
    for e in errors:
        for text in (str(e), repr(e), " ".join(map(str, e.args))):
            assert SECRET not in text and KEY_ID not in text
        assert e.__cause__ is None and e.__suppress_context__  # the raw exception is not chained on
    for text in (caplog.text, repr(ff), str(ff.manifest())):
        assert SECRET not in text and KEY_ID not in text   # ... and every line is masked
    # the module's scrubber and the collectors' (massive._scrub) both mask the pair
    for scrubbed in (flat_files.scrub(leak), massive._scrub(leak)):   # noqa: SLF001
        assert SECRET not in scrubbed and KEY_ID not in scrubbed
    # the masks of a built client stay even after the environment changes
    ff._masks = (SECRET, KEY_ID)                          # noqa: SLF001 - as _build_client sets them
    assert SECRET not in ff.scrub("x " + SECRET)


def test_scrub_masks_credential_shapes_without_the_env(monkeypatch):
    monkeypatch.delenv("TST_MASSIVE_S3_KEY_ID", raising=False)
    monkeypatch.delenv("TST_MASSIVE_S3_SECRET", raising=False)
    s = flat_files.scrub("aws_secret_access_key=abc123XYZ Credential=AKIAFAKE/20261012/x Signature=deadbeef")
    assert "abc123XYZ" not in s and "AKIAFAKE" not in s and "deadbeef" not in s
