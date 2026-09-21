"""One Gunicorn process owns the dispatcher; fail closed on a second owner."""
import fcntl
import logging
import os
import sys
import threading

from .core import Backend, Config, Dispatcher, Store
from .web import App

os.umask(0o077)
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(message)s")
config = Config.from_env()
config.validate()
store = Store(config)
lock = open(config.database + ".lock", "a")
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
dispatcher = Dispatcher(store, Backend(config))
thread = threading.Thread(target=dispatcher.run, daemon=True, name="dispatcher")
thread.start()
application = App(store, healthy=thread.is_alive)


if __name__ == "__main__":
    # Local development only. Container uses Gunicorn gthread.
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIServer, make_server, WSGIRequestHandler

    class ThreadedServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    class QuietHandler(WSGIRequestHandler):
        def log_message(self, *args):
            pass

    with make_server("127.0.0.1", int(os.environ.get("PORT", "8080")), application,
                     server_class=ThreadedServer, handler_class=QuietHandler) as server:
        server.serve_forever()
