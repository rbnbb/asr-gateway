import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError

from asr_gateway.core import Backend, CapacityError, Config, ConflictError, Dispatcher, Store
from asr_gateway.web import App

KEY = "a-test-key-with-at-least-24-characters"
TYPE = "multipart/form-data; boundary=test-boundary"
AUDIO = (b"--test-boundary\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\ntest-model\r\n"
         b"--test-boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"test.wav\"\r\n"
         b"Content-Type: audio/wav\r\n\r\nsynthetic-audio\r\n--test-boundary--\r\n")


class FakeBackend:
    def __init__(self):
        self.awake = False
        self.wakes = 0
        self.calls = 0

    def ready(self):
        return self.awake

    def wake(self):
        self.wakes += 1
        self.awake = True

    def transcribe(self, job):
        self.calls += 1
        return b'{"text":"hello"}', "application/json", 200


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(database=os.path.join(self.tmp.name, "jobs.sqlite3"), api_key=KEY,
                             model="test-model", sync_timeout=0.02, poll_seconds=0.005)
        self.store = Store(self.config)
        self.app = App(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, path, method="GET", body=b"", auth=KEY, **headers):
        env = {"PATH_INFO": path, "REQUEST_METHOD": method, "CONTENT_LENGTH": str(len(body)),
               "CONTENT_TYPE": TYPE, "wsgi.input": io.BytesIO(body), "HTTP_AUTHORIZATION": "Bearer " + auth, **headers}
        response = {}
        def start(status, fields):
            response.update(status=int(status.split()[0]), headers=dict(fields))
        result = b"".join(self.app(env, start))
        return response["status"], result, response["headers"]

    def test_authenticated_async_round_trip(self):
        status, body, headers = self.call("/jobs", "POST", AUDIO)
        self.assertEqual(status, 202)
        job_id = json.loads(body)["id"]
        backend = FakeBackend()
        Dispatcher(self.store, backend).process(self.store.claim())
        status, result, headers = self.call("/jobs/"+job_id+"/result")
        self.assertEqual((status, json.loads(result)["text"], backend.wakes), (200, "hello", 1))
        self.assertIsNone(self.store.get(job_id)["body"])

    def test_unauthorized_cannot_submit_or_read(self):
        self.assertEqual(self.call("/jobs", "POST", AUDIO, auth="wrong")[0], 401)
        job_id = self.store.submit(AUDIO, TYPE)
        self.assertEqual(self.call("/jobs/"+job_id, auth="wrong")[0], 401)
        self.assertEqual(self.call("/jobs/"+job_id+"/result", auth="wrong")[0], 401)

    def test_sync_timeout_retains_upload(self):
        status, body, headers = self.call("/v1/audio/transcriptions", "POST", AUDIO)
        self.assertEqual(status, 504)
        self.assertEqual(self.store.get(json.loads(body)["id"])["body"], AUDIO)
        self.assertIn("X-Job-ID", headers)

    def test_sync_returns_backend_text_format(self):
        self.config.sync_timeout = 2
        backend = FakeBackend()
        backend.transcribe = lambda job: (b"hello", "text/plain", 200)
        dispatcher = Dispatcher(self.store, backend)
        thread = threading.Thread(target=dispatcher.run)
        thread.start()
        try:
            status, body, headers = self.call("/v1/audio/transcriptions", "POST", AUDIO)
            self.assertEqual((status, body, headers["Content-Type"]), (200, b"hello", "text/plain"))
        finally:
            dispatcher.stop.set()
            thread.join(2)

    def test_exact_body_idempotency_and_conflict(self):
        first = self.store.submit(AUDIO, TYPE, "one")
        self.assertEqual(first, self.store.submit(AUDIO, TYPE, "one"))
        with self.assertRaises(ConflictError):
            self.store.submit(AUDIO+b"x", TYPE, "one")

    def test_concurrent_claims_are_unique(self):
        for _ in range(8):
            self.store.submit(AUDIO, TYPE)
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(lambda _: self.store.claim(), range(8)))
        self.assertEqual(len({j["id"] for j in jobs}), 8)

    def test_restart_and_stale_attempt_cannot_overwrite(self):
        job_id = self.store.submit(AUDIO, TYPE)
        old = self.store.claim()
        self.store.transcribing(old)
        second = Store(self.config)
        second.recover()
        new = second.claim()
        second.transcribing(new)
        self.store.finish(old, b"stale", "text/plain", 200)
        self.assertEqual(second.get(job_id)["state"], "transcribing")
        second.finish(new, b"correct", "text/plain", 200)
        self.store.finish(old, b"stale", "text/plain", 200)
        self.assertEqual(second.get(job_id)["result"], b"correct")

    def test_actual_process_kill_preserves_committed_audio(self):
        script = """import sys, time
from asr_gateway.core import Config, Store
s=Store(Config(database=sys.argv[1]))
j=s.submit(b'durable audio', 'multipart/form-data; boundary=test')
s.transcribing(s.claim())
print(j, flush=True)
time.sleep(60)
"""
        process = subprocess.Popen([sys.executable, "-c", script, self.config.database], stdout=subprocess.PIPE, text=True)
        try:
            job_id = process.stdout.readline().strip()
            self.assertEqual(len(job_id), 32)
        finally:
            process.kill()
            process.wait(timeout=5)
            process.stdout.close()
        recovered = Store(self.config)
        recovered.recover()
        job = recovered.claim()
        self.assertEqual((job["id"], job["body"]), (job_id, b"durable audio"))
        Dispatcher(recovered, FakeBackend()).process(job)
        self.assertEqual(recovered.get(job_id)["state"], "succeeded")

    def test_bounded_queue_and_upload(self):
        self.config.max_jobs = 1
        self.store.submit(AUDIO, TYPE)
        self.assertEqual(self.call("/jobs", "POST", AUDIO)[0], 503)
        self.config.max_upload = 1
        self.assertEqual(self.call("/jobs", "POST", AUDIO)[0], 413)

    def test_storage_limit(self):
        self.config.max_storage = len(AUDIO)-1
        with self.assertRaises(CapacityError):
            self.store.submit(AUDIO, TYPE)

    def test_retry_exhaustion(self):
        job_id = self.store.submit(AUDIO, TYPE)
        backend = FakeBackend()
        def fail(job):
            raise OSError("synthetic disconnect")
        backend.transcribe = fail
        dispatcher = Dispatcher(self.store, backend)
        dispatcher.process(self.store.claim())
        self.assertEqual(self.store.get(job_id)["state"], "queued")
        dispatcher.process(self.store.claim())
        self.assertEqual(self.store.get(job_id)["state"], "failed")
        self.assertIsNone(self.store.claim())

    def test_permanent_backend_error_is_not_retried(self):
        job_id = self.store.submit(AUDIO, TYPE)
        backend = FakeBackend()
        def fail(job):
            raise HTTPError("http://example.test", 400, "invalid audio", {}, None)
        backend.transcribe = fail
        Dispatcher(self.store, backend).process(self.store.claim())
        self.assertEqual(self.store.get(job_id)["state"], "failed")

    def test_expiry_and_attempt_limit(self):
        job_id = self.store.submit(AUDIO, TYPE)
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET created=0 WHERE id=?", (job_id,))
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.get(job_id)["state"], "failed")
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET updated=0 WHERE id=?", (job_id,))
        self.store.cleanup()
        self.assertIsNone(self.store.get(job_id))

    def test_models_available_without_waking_backend(self):
        status, body, _ = self.call("/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"][0]["id"], "test-model")

    @unittest.skipUnless(os.environ.get("ASR_NETWORK_TESTS") == "1", "Set ASR_NETWORK_TESTS=1 where loopback sockets are permitted")
    def test_tcp_wake_and_real_http_forwarding(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        received = {}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
            def do_POST(self):
                received.update(path=self.path, body=self.rfile.read(int(self.headers["Content-Length"])), type=self.headers["Content-Type"])
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"text":"from mock server"}')
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            self.config.wake_mode = "tcp"
            self.config.wake_host, self.config.wake_port = listener.getsockname()
            self.config.backend_url = "http://127.0.0.1:"+str(server.server_port)+"/v1"
            self.config.health_url = self.config.backend_url+"/health"
            backend = Backend(self.config)
            backend.wake()
            connection, _ = listener.accept()
            self.assertEqual(connection.recv(1), b"")
            connection.close()
            self.assertTrue(backend.ready())
            body, _, code = backend.transcribe({"body": AUDIO, "content_type": TYPE, "created": time.time()})
            self.assertEqual((code, json.loads(body)["text"]), (200, "from mock server"))
            self.assertEqual(received, {"path": "/v1/audio/transcriptions", "body": AUDIO, "type": TYPE})
        finally:
            listener.close()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_delayed_readiness_wakes_only_once(self):
        job_id = self.store.submit(AUDIO, TYPE)
        backend = FakeBackend()
        polls = []
        def ready():
            polls.append(True)
            return len(polls) >= 4
        backend.ready = ready
        Dispatcher(self.store, backend).process(self.store.claim())
        self.assertEqual((backend.wakes, backend.calls), (1, 1))
        self.assertEqual(self.store.get(job_id)["state"], "succeeded")

    def test_lost_response_may_repeat_inference_but_stores_one_result(self):
        job_id = self.store.submit(AUDIO, TYPE)
        backend = FakeBackend()
        computed = []
        def transcribe(job):
            computed.append(job["attempt"])
            if len(computed) == 1:
                raise OSError("Response lost after backend finished")
            return b"retried result", "text/plain", 200
        backend.transcribe = transcribe
        worker = Dispatcher(self.store, backend)
        worker.process(self.store.claim())
        worker.process(self.store.claim())
        self.assertEqual(len(computed), 2)
        self.assertEqual(self.store.get(job_id)["result"], b"retried result")

    def test_readiness_deadline_is_bounded(self):
        self.config.ready_timeout = 0.015
        job_id = self.store.submit(AUDIO, TYPE)
        backend = FakeBackend()
        backend.ready = lambda: False
        Dispatcher(self.store, backend).process(self.store.claim())
        self.assertEqual(backend.calls, 0)
        self.assertEqual(self.store.get(job_id)["error"], "backend_not_ready")

    def test_dead_dispatcher_makes_health_fail(self):
        self.app = App(self.store, healthy=lambda: False)
        self.assertEqual(self.call("/health")[0], 503)


if __name__ == "__main__":
    unittest.main()
