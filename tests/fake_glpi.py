"""
fake_glpi.py - atrapa serwera GLPI REST API do testów (prawdziwy serwer HTTP na localhost).

Dzięki niej testy sprawdzają całą drogę: requests -> sieć -> odpowiedź, wraz z paginacją (206/200),
błędami 403/5xx i ponawianiem żądań. Nic nie łączy się z prawdziwym GLPI.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

API_PREFIX = "/api.php/v1"


class FakeGLPI:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # cisza w testach

            def do_GET(self):
                fake.handle(self, "GET")

            def do_POST(self):
                fake.handle(self, "POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}{API_PREFIX}"

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def reset(self):
        with self.lock:
            self.data = {"Phone": [], "User": [], "Location": []}
            self.requests = []       # [(metoda, sciezka)]
            self.tickets = []        # przyjete payloady ticketow
            self.fail_first = {}     # (metoda, sciezka) -> [kody, ktore zwrocic na poczatku]
            self.always = {}         # (metoda, sciezka) -> (kod, body)
            self.next_ticket_id = 100

    def count(self, method, path):
        with self.lock:
            return sum(1 for item in self.requests if item == (method, path))

    # ---- obsluga zapytan ----

    def handle(self, request, method):
        path = urlparse(request.path).path[len(API_PREFIX):]

        length = int(request.headers.get("Content-Length") or 0)
        raw_body = request.rfile.read(length) if length else b""

        with self.lock:
            self.requests.append((method, path))

            queue = self.fail_first.get((method, path))
            if queue:
                return self.send(request, queue.pop(0), {"error": "injected failure"})

            if (method, path) in self.always:
                status, body = self.always[(method, path)]
                return self.send(request, status, body)

            if method == "GET" and path == "/initSession":
                if not request.headers.get("App-Token") or not request.headers.get("Authorization"):
                    return self.send(request, 400, ["ERROR_APP_TOKEN_PARAMETERS_MISSING", ""])
                return self.send(request, 200, {"session_token": "SESSION"})

            if method == "GET" and path == "/killSession":
                return self.send(request, 200, "")

            if method == "GET" and path.lstrip("/") in self.data:
                if request.headers.get("Session-Token") != "SESSION":
                    return self.send(request, 401, ["ERROR_SESSION_TOKEN_INVALID", ""])

                items = self.data[path.lstrip("/")]
                start, end = 0, len(items) - 1
                header = request.headers.get("Range")

                if header:
                    start, end = [int(part) for part in header.split("-")]

                page = items[start:end + 1]
                status = 206 if end + 1 < len(items) else 200
                return self.send(request, status, page)

            if method == "POST" and path == "/Ticket":
                payload = json.loads(raw_body.decode("utf-8"))
                self.tickets.append(payload)
                self.next_ticket_id += 1
                return self.send(request, 201, {"id": self.next_ticket_id, "message": "Item successfully added"})

        self.send(request, 404, ["ERROR_ITEM_NOT_FOUND", path])

    @staticmethod
    def send(request, status, body):
        data = json.dumps(body).encode("utf-8")
        request.send_response(status)
        request.send_header("Content-Type", "application/json")
        request.send_header("Content-Length", str(len(data)))
        request.end_headers()
        request.wfile.write(data)
