"""Real native-form regression. Requires Playwright, Chromium, and openssl.

Run: python tests/browser_login.py
Optionally set BROWSER_EXECUTABLE_PATH to an installed Chromium/Chrome executable.
All credentials, TLS keys, and queue data are synthetic and temporary.
"""
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asr_gateway.core import Config, Store
from asr_gateway.web import App
from playwright.sync_api import sync_playwright


class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        cert, key = root / "cert.pem", root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cert), "-days", "1",
                        "-subj", "/CN=localhost"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        config = Config(database=str(root / "queue.sqlite3"), api_key="synthetic-api-key-for-browser-tests",
                        model="test-model", browser_password="synthetic browser test passphrase")
        application = App(Store(config))
        observed = []
        policy = ["no-referrer"]

        def wrapped(env, start):
            def response(status, headers):
                if env["PATH_INFO"] == "/login":
                    observed.append((env.get("HTTP_ORIGIN"), int(status.split()[0])))
                if env["PATH_INFO"] == "/" and policy[0] is not None:
                    headers = [(name, policy[0] if name.lower() == "referrer-policy" else value)
                               for name, value in headers]
                return start(status, headers)
            return application(env, response)

        server = make_server("127.0.0.1", 0, wrapped, server_class=ThreadedServer, handler_class=QuietHandler)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(str(cert), str(key))
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"https://localhost:{server.server_port}"
        try:
            with sync_playwright() as playwright:
                options = {"headless": True}
                if os.environ.get("BROWSER_EXECUTABLE_PATH"):
                    options["executable_path"] = os.environ["BROWSER_EXECUTABLE_PATH"]
                browser = playwright.chromium.launch(**options)
                try:
                    context = browser.new_context(ignore_https_errors=True)
                    page = context.new_page()

                    def login():
                        page.goto(url)
                        page.locator("#username").fill("owner")
                        page.locator("#password").fill(config.browser_password)
                        with page.expect_navigation():
                            page.locator("#login button").click()

                    login()
                    assert observed[-1] == ("null", 403), observed[-1]
                    print("PASS: old no-referrer policy reproduces Origin:null and login rejection")

                    policy[0] = None  # Use the real application's fixed header.
                    login()
                    assert observed[-1] == (url, 303), observed[-1]
                    page.locator("#model option[value='test-model']").wait_for(state="attached")
                    assert page.locator("#model").input_value() == "test-model"
                    assert page.locator("#login").is_hidden()
                    cookie = next(c for c in context.cookies() if c["name"] == "__Host-asr_session")
                    assert cookie["secure"] and cookie["httpOnly"] and cookie["sameSite"] == "Strict"
                    page.reload()
                    page.locator("#model option[value='test-model']").wait_for(state="attached")
                    assert page.locator("#login").is_hidden()
                    print("PASS: native login redirects, model loads, secure session survives reload")
                    context.close()
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
