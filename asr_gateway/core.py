"""Durable queue, one dispatcher, and replaceable wake/backend adapters."""
import hashlib
import json
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
        for name in ("ready_timeout", "inference_timeout", "sync_timeout", "job_timeout", "retention", "poll_seconds", "max_attempts", "max_upload", "max_storage", "max_jobs"):
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
            return job_id

    def get(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def recover(self):
        # Must be called only by the process holding the exclusive dispatcher lock.
        with self.connect() as db:
            db.execute("UPDATE jobs SET state='queued', attempt=NULL WHERE state IN ('waking','transcribing')")

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            db.execute("UPDATE jobs SET state='failed', error='job_deadline_or_attempt_limit', body=NULL, updated=? WHERE state='queued' AND (created < ? OR attempts >= ?)", (now, now-self.config.job_timeout, self.config.max_attempts))
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            attempt = uuid.uuid4().hex
            db.execute("UPDATE jobs SET state='waking', error=NULL, backend_status=NULL, attempt=?, attempts=attempts+1, updated=? WHERE id=?", (attempt, now, row["id"]))
            job = dict(row)
            job.update(attempt=attempt, attempts=row["attempts"]+1)
            return job

    def transcribing(self, job):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state='transcribing', updated=? WHERE id=? AND attempt=?", (time.time(), job["id"], job["attempt"]))

    def finish(self, job, result, content_type, status):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Results replace audio; cap total retained payload even under concurrent submits.
            used = db.execute("SELECT COALESCE(SUM(COALESCE(LENGTH(body),0)+COALESCE(LENGTH(result),0)),0) FROM jobs WHERE id!=?", (job["id"],)).fetchone()[0]
            if used + len(result) > self.config.max_storage:
                raise CapacityError()
            db.execute("UPDATE jobs SET state='succeeded', body=NULL, result=?, result_type=?, backend_status=?, updated=? WHERE id=? AND attempt=? AND state='transcribing'", (result, content_type, status, time.time(), job["id"], job["attempt"]))

    def fail(self, job, reason, retry=False, status=None):
        retry = retry and job["attempts"] < self.config.max_attempts and time.time()-job["created"] < self.config.job_timeout
        with self.connect() as db:
            db.execute("UPDATE jobs SET state=?, error=?, backend_status=?, body=CASE WHEN ? THEN body ELSE NULL END, updated=? WHERE id=? AND attempt=? AND state IN ('waking','transcribing')", ("queued" if retry else "failed", reason, status, retry, time.time(), job["id"], job["attempt"]))

    def cleanup(self):
        with self.connect() as db:
            db.execute("DELETE FROM jobs WHERE state IN ('succeeded','failed') AND updated < ?", (time.time()-self.config.retention,))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Backend:
    def __init__(self, config):
        self.config = config
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def headers(self):
        return {"Authorization": "Bearer " + self.config.backend_key} if self.config.backend_key else {}

    def ready(self):
        try:
            with self.opener.open(Request(self.config.health_url, headers=self.headers()), timeout=3) as response:
                return response.status == 200
        except (OSError, URLError):
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
        try:
            if not self.backend.ready():
                self.backend.wake()
                deadline = min(time.monotonic()+c.ready_timeout, time.monotonic()+max(0, c.job_timeout-(time.time()-job["created"])))
                while not self.stop.is_set() and time.monotonic() < deadline:
                    if self.backend.ready():
                        break
                    self.stop.wait(c.poll_seconds)
                else:
                    self.store.fail(job, "backend_not_ready", retry=True)
                    return
            self.store.transcribing(job)
            body, content_type, status = self.backend.transcribe(job)
            self.store.finish(job, body, content_type, status)
        except HTTPError as error:
            self.store.fail(job, "backend_http_error", retry=error.code in (408, 429, 500, 502, 503, 504), status=error.code)
        except (URLError, OSError, TimeoutError):
            self.store.fail(job, "backend_connection_error", retry=True)
        except (ValueError, CapacityError):
            self.store.fail(job, "backend_response_rejected")

    def run(self):
        self.store.recover()
        while not self.stop.is_set():
            self.store.cleanup()
            job = self.store.claim()
            if job:
                self.process(job)
            self.stop.wait(self.store.config.poll_seconds)
