"""Durable queue, one dispatcher, and replaceable wake/backend adapters."""
import hashlib
import json
import logging
import os
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


logger = logging.getLogger("asr_gateway")


def event(name, **fields):
    # Callers supply only allowlisted operational metadata, never exception messages,
    # URLs, headers, form fields, audio, or transcripts.
    logger.info(json.dumps({"event": name, **fields}, sort_keys=True))


def connection_detail(error):
    cause = error.reason if isinstance(error, URLError) else error
    fields = {"exception_type": type(cause).__name__}
    number = getattr(cause, "errno", None)
    if isinstance(number, int):
        fields["errno"] = number
    return fields


@dataclass
class Config:
    database: str = "/data/jobs.sqlite3"
    api_key: str = ""
    backend_url: str = ""
    health_url: str = ""
    backend_key: str = ""
    model: str = ""
    wake_mode: str = "none"
    wake_host: str = ""
    wake_port: int = 9
    wake_url: str = ""
    wake_key: str = ""
    wake_mac: str = ""
    ready_timeout: float = 180
    inference_timeout: float = 600
    sync_timeout: float = 180
    job_timeout: float = 1800
    retention: float = 3600
    poll_seconds: float = 2
    wake_retry_seconds: float = 10
    session_seconds: int = 30 * 24 * 3600
    browser_username: str = "owner"
    browser_password: str = ""
    max_attempts: int = 2
    max_upload: int = 25 * 1024 * 1024
    max_storage: int = 256 * 1024 * 1024
    max_jobs: int = 128

    @classmethod
    def from_env(cls):
        defaults = cls()
        return cls(**{name: type(getattr(defaults, name))(os.environ.get(
            "ASR_" + name.upper(), getattr(defaults, name))) for name in cls.__dataclass_fields__})

    def validate(self):
        if len(self.api_key) < 24:
            raise ValueError("ASR_API_KEY must contain at least 24 characters")
        if not self.model:
            raise ValueError("ASR_MODEL is required")
        if not self.browser_username or len(self.browser_username) > 128:
            raise ValueError("ASR_BROWSER_USERNAME must contain 1-128 characters")
        if self.browser_password and len(self.browser_password) < 16:
            raise ValueError("ASR_BROWSER_PASSWORD must contain at least 16 characters")
        for url in [self.backend_url, self.health_url] + ([self.wake_url] if self.wake_mode == "http" else []):
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.fragment:
                raise ValueError("Configure explicit HTTP(S) URLs without embedded credentials")
        if self.wake_mode not in ("none", "tcp", "http", "wol"):
            raise ValueError("Unsupported wake mode")
        if self.wake_mode in ("tcp", "wol") and not self.wake_host:
            raise ValueError("ASR_WAKE_HOST is required")
        if self.wake_mode == "wol" and len(bytes.fromhex(self.wake_mac.replace(":", "").replace("-", ""))) != 6:
            raise ValueError("Invalid Wake-on-LAN MAC")
        for name in ("ready_timeout", "inference_timeout", "sync_timeout", "job_timeout", "retention", "poll_seconds", "wake_retry_seconds", "session_seconds", "max_attempts", "max_upload", "max_storage", "max_jobs"):
            if getattr(self, name) <= 0:
                raise ValueError(name + " must be positive")


class CapacityError(Exception):
    pass


class ConflictError(Exception):
    pass


class Store:
    def __init__(self, config):
        self.config = config
        os.makedirs(os.path.dirname(os.path.abspath(config.database)), mode=0o700, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, key TEXT UNIQUE, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                body BLOB, content_type TEXT NOT NULL, attempt TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, result BLOB, result_type TEXT,
                error TEXT, backend_status INTEGER)""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.config.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, body, content_type, key=None):
        fingerprint = hashlib.sha256(content_type.encode() + b"\0" + body).hexdigest()
        # Idempotency is deliberately exact-body: reuse the same multipart boundary.
        key = hashlib.sha256(key.encode()).hexdigest() if key else None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT id, fingerprint FROM jobs WHERE key=?", (key,)).fetchone() if key else None
            if old:
                if old["fingerprint"] != fingerprint:
                    raise ConflictError()
                return old["id"]
            count, size = db.execute("SELECT COUNT(*), COALESCE(SUM(COALESCE(LENGTH(body),0)+COALESCE(LENGTH(result),0)),0) FROM jobs").fetchone()
            if count >= self.config.max_jobs or size + len(body) > self.config.max_storage:
                raise CapacityError()
            job_id = uuid.uuid4().hex
            now = time.time()
            db.execute("INSERT INTO jobs(id,key,fingerprint,state,created,updated,body,content_type) VALUES(?,?,?,'queued',?,?,?,?)", (job_id, key, fingerprint, now, now, body, content_type))
        event("job_accepted", job_id=job_id, bytes=len(body))
        return job_id

    def retry(self, job_id, key):
        job = self.get(job_id)
        if not job:
            raise KeyError(job_id)
        if job["state"] != "failed" or job["body"] is None:
            raise ConflictError()
        # A retry is a new job, preserving the failed attempt's history. Namespace
        # the caller's key so retries do not collide with original submissions.
        new_id = self.submit(job["body"], job["content_type"], "retry:" + job_id + ":" + key)
        event("job_retry_requested", job_id=new_id, previous_job_id=job_id)
        return new_id

    def get(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def recover(self):
        # Must be called only by the process holding the exclusive dispatcher lock.
        with self.connect() as db:
            count = db.execute("UPDATE jobs SET state='queued', attempt=NULL WHERE state IN ('waking','transcribing')").rowcount
        event("dispatcher_recovered", jobs=count)

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            expired = db.execute("UPDATE jobs SET state='failed', error='job_deadline_or_attempt_limit', updated=? WHERE state='queued' AND (created < ? OR attempts >= ?)", (now, now-self.config.job_timeout, self.config.max_attempts)).rowcount
            if expired:
                event("jobs_exhausted", jobs=expired)
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            attempt = uuid.uuid4().hex
            db.execute("UPDATE jobs SET state='waking', error=NULL, backend_status=NULL, attempt=?, attempts=attempts+1, updated=? WHERE id=?", (attempt, now, row["id"]))
            job = dict(row)
            job.update(attempt=attempt, attempts=row["attempts"]+1)
        event("job_claimed", job_id=job["id"], attempt=job["attempts"], state="waking")
        return job

    def transcribing(self, job):
        with self.connect() as db:
            changed = db.execute("UPDATE jobs SET state='transcribing', updated=? WHERE id=? AND attempt=?", (time.time(), job["id"], job["attempt"])).rowcount
        if changed:
            event("job_transcribing", job_id=job["id"], attempt=job["attempts"])

    def finish(self, job, result, content_type, status):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Results replace audio; cap total retained payload even under concurrent submits.
            used = db.execute("SELECT COALESCE(SUM(COALESCE(LENGTH(body),0)+COALESCE(LENGTH(result),0)),0) FROM jobs WHERE id!=?", (job["id"],)).fetchone()[0]
            if used + len(result) > self.config.max_storage:
                raise CapacityError()
            changed = db.execute("UPDATE jobs SET state='succeeded', body=NULL, result=?, result_type=?, backend_status=?, updated=? WHERE id=? AND attempt=? AND state='transcribing'", (result, content_type, status, time.time(), job["id"], job["attempt"])).rowcount
        if changed:
            event("job_succeeded", job_id=job["id"], attempt=job["attempts"], elapsed_seconds=round(time.time()-job["created"], 3), backend_status=status)

    def fail(self, job, reason, retry=False, status=None, stage=None, detail=None):
        retry = retry and job["attempts"] < self.config.max_attempts and time.time()-job["created"] < self.config.job_timeout
        with self.connect() as db:
            changed = db.execute("UPDATE jobs SET state=?, error=?, backend_status=?, updated=? WHERE id=? AND attempt=? AND state IN ('waking','transcribing')", ("queued" if retry else "failed", reason, status, time.time(), job["id"], job["attempt"])).rowcount
        if changed:
            event("job_requeued" if retry else "job_failed", job_id=job["id"], attempt=job["attempts"], reason=reason, stage=stage, backend_status=status, elapsed_seconds=round(time.time()-job["created"], 3), **(detail or {}))

    def cleanup(self):
        with self.connect() as db:
            count = db.execute("DELETE FROM jobs WHERE state IN ('succeeded','failed') AND updated < ?", (time.time()-self.config.retention,)).rowcount
        if count:
            event("jobs_expired", jobs=count)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Backend:
    def __init__(self, config):
        self.config = config
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.readiness_detail = {}

    def headers(self):
        return {"Authorization": "Bearer " + self.config.backend_key} if self.config.backend_key else {}

    def ready(self):
        try:
            with self.opener.open(Request(self.config.health_url, headers=self.headers()), timeout=3) as response:
                self.readiness_detail = {"health_status": response.status}
                return response.status == 200
        except HTTPError as error:
            self.readiness_detail = {"health_status": error.code}
            error.close()
            return False
        except (OSError, URLError) as error:
            self.readiness_detail = connection_detail(error)
            return False

    def wake(self):
        c = self.config
        if c.wake_mode == "tcp":
            with socket.create_connection((c.wake_host, c.wake_port), timeout=3):
                pass  # Connection-only trigger; no guessed protocol bytes.
        elif c.wake_mode == "http":
            headers = {"Authorization": "Bearer " + c.wake_key} if c.wake_key else {}
            with self.opener.open(Request(c.wake_url, data=b"", headers=headers, method="POST"), timeout=5):
                pass
        elif c.wake_mode == "wol":
            mac = bytes.fromhex(c.wake_mac.replace(":", "").replace("-", ""))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.sendto(b"\xff"*6 + mac*16, (c.wake_host, c.wake_port))

    def transcribe(self, job):
        headers = self.headers()
        headers["Content-Type"] = job["content_type"]
        request = Request(self.config.backend_url.rstrip("/") + "/audio/transcriptions", data=job["body"], headers=headers, method="POST")
        remaining = max(0.1, self.config.job_timeout - (time.time()-job["created"]))
        with self.opener.open(request, timeout=min(self.config.inference_timeout, remaining)) as response:
            body = response.read(self.config.max_upload+1)
            if len(body) > self.config.max_upload:
                raise ValueError("backend_response_too_large")
            content_type = response.headers.get("Content-Type", "application/json")
            if content_type.split(";")[0] not in ("application/json", "text/plain", "text/vtt", "application/x-subrip"):
                raise ValueError("unsupported_backend_response_type")
            return body, content_type, response.status


class Dispatcher:
    def __init__(self, store, backend):
        self.store, self.backend = store, backend
        self.stop = threading.Event()

    def process(self, job):
        c = self.store.config
        stage = "readiness"
        try:
            if not self.backend.ready():
                deadline = min(time.monotonic()+c.ready_timeout, time.monotonic()+max(0, c.job_timeout-(time.time()-job["created"])))
                wake_sent, next_wake = False, 0
                last_detail = None
                while not self.stop.is_set() and time.monotonic() < deadline:
                    detail = getattr(self.backend, "readiness_detail", {})
                    if detail != last_detail:
                        event("backend_waiting", job_id=job["id"], attempt=job["attempts"], **detail)
                        last_detail = dict(detail)
                    if not wake_sent and time.monotonic() >= next_wake:
                        try:
                            self.backend.wake()
                            wake_sent = True
                            event("wake_sent", job_id=job["id"], mode=c.wake_mode)
                        except (OSError, URLError) as error:
                            # Wake delivery can be uncertain. Keep polling readiness
                            # and retry wake inside this attempt's startup window.
                            extra = {"wake_status": error.code} if isinstance(error, HTTPError) else connection_detail(error)
                            event("wake_failed", job_id=job["id"], attempt=job["attempts"], **extra)
                            if isinstance(error, HTTPError):
                                error.close()
                            next_wake = time.monotonic() + c.wake_retry_seconds
                    if self.backend.ready():
                        break
                    self.stop.wait(c.poll_seconds)
                else:
                    if self.stop.is_set():
                        return  # Leave the claim for startup recovery.
                    self.store.fail(job, "backend_not_ready", retry=True, stage=stage, detail=getattr(self.backend, "readiness_detail", {}))
                    return
            stage = "transcription"
            self.store.transcribing(job)
            body, content_type, status = self.backend.transcribe(job)
            stage = "result_storage"
            self.store.finish(job, body, content_type, status)
        except HTTPError as error:
            self.store.fail(job, "backend_http_error", retry=error.code in (408, 429, 500, 502, 503, 504), status=error.code, stage=stage)
            error.close()
        except (URLError, OSError, TimeoutError) as error:
            self.store.fail(job, "backend_connection_error", retry=True, stage=stage, detail=connection_detail(error))
        except (ValueError, CapacityError):
            self.store.fail(job, "backend_response_rejected", stage=stage)

    def run(self):
        self.store.recover()
        while not self.stop.is_set():
            self.store.cleanup()
            job = self.store.claim()
            if job:
                self.process(job)
            self.stop.wait(self.store.config.poll_seconds)
