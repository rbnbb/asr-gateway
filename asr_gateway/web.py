"""Small WSGI adapter; multipart bodies are stored and forwarded without rewriting."""
import hmac
import json
import re
import time
from http import HTTPStatus
from pathlib import Path

from .core import CapacityError, ConflictError


class App:
    def __init__(self, store, healthy=lambda: True):
        self.store = store
        self.config = store.config
        self.healthy = healthy

    def __call__(self, env, start_response):
        def respond(status, value, content_type="application/json", extra=()):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            start_response(f"{status} {HTTPStatus(status).phrase}", [
                ("Content-Type", content_type), ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "no-referrer"), *extra])
            return [body]

        path, method = env.get("PATH_INFO", ""), env.get("REQUEST_METHOD", "GET")
        if method == "GET" and path == "/health":
            ok = self.healthy()
            return respond(200 if ok else 503, {"status": "ok" if ok else "dispatcher_unavailable"})
        if method == "GET" and path == "/":
            return respond(200, Path(__file__).with_name("recorder.html").read_bytes(), "text/html; charset=utf-8", [("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")])
        auth = env.get("HTTP_AUTHORIZATION", "")
        if not hmac.compare_digest(auth.encode(), ("Bearer " + self.config.api_key).encode()):
            return respond(401, {"error": "unauthorized"}, extra=[("WWW-Authenticate", "Bearer")])
        if method == "GET" and path == "/v1/models":
            return respond(200, {"object": "list", "data": [{"id": self.config.model, "object": "model", "owned_by": "configured-backend"}]})
        if method == "POST" and path in ("/jobs", "/v1/audio/transcriptions"):
            try:
                size = int(env.get("CONTENT_LENGTH") or "0")
            except ValueError:
                return respond(400, {"error": "invalid_content_length"})
            if size <= 0:
                return respond(411, {"error": "content_length_required"})
            if size > self.config.max_upload:
                return respond(413, {"error": "upload_too_large"})
            content_type = env.get("CONTENT_TYPE", "")
            if not content_type.lower().startswith("multipart/form-data;") or "boundary=" not in content_type.lower():
                return respond(415, {"error": "multipart_form_required"})
            key = env.get("HTTP_IDEMPOTENCY_KEY")
            if key and len(key) > 256:
                return respond(400, {"error": "idempotency_key_too_long"})
            body = env["wsgi.input"].read(size)
            if len(body) != size:
                return respond(400, {"error": "incomplete_upload"})
            try:
                job_id = self.store.submit(body, content_type, key)
            except CapacityError:
                return respond(503, {"error": "queue_full"}, extra=[("Retry-After", "30")])
            except ConflictError:
                return respond(409, {"error": "idempotency_key_content_mismatch"})
            location = "/jobs/" + job_id
            if path == "/jobs":
                return respond(202, {"id": job_id, "status_url": location, "result_url": location+"/result"}, extra=[("Location", location)])
            deadline = time.monotonic() + self.config.sync_timeout
            while time.monotonic() < deadline:
                job = self.store.get(job_id)
                if not job:
                    return respond(410, {"error": "job_expired", "id": job_id})
                if job["state"] == "succeeded":
                    return respond(job["backend_status"], job["result"], job["result_type"], [("X-Job-ID", job_id)])
                if job["state"] == "failed":
                    return respond(502, {"error": job["error"], "id": job_id, "backend_status": job["backend_status"]})
                time.sleep(0.2)
            return respond(504, {"error": "wait_timeout_job_retained", "id": job_id, "status_url": location}, extra=[("X-Job-ID", job_id), ("Location", location)])
        match = re.fullmatch(r"/jobs/([0-9a-f]{32})(/result)?", path)
        if method == "GET" and match:
            job = self.store.get(match[1])
            if not job:
                return respond(404, {"error": "job_not_found_or_expired"})
            if match[2]:
                if job["state"] != "succeeded":
                    return respond(409, {"error": "result_not_available", "state": job["state"]})
                return respond(job["backend_status"], job["result"], job["result_type"])
            return respond(200, {k: job[k] for k in ("id", "state", "created", "updated", "attempts", "error", "backend_status")})
        return respond(404, {"error": "not_found"})
