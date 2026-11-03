"""A small gate in front of Ollama: a shared secret, and inference calls only (no pull, delete or create).

    CF_PROXY_SECRET=<secret> python3 ollama_proxy.py --port 11435 --target http://127.0.0.1:11434

Standard library only, so it runs on any machine with Python 3.9+, with or without CinemaFabric installed.
"""

import argparse
import hmac
import http.client
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ALLOWED = {
    ("GET", "/api/tags"), ("GET", "/api/version"), ("GET", "/api/ps"),
    ("POST", "/api/show"), ("POST", "/api/chat"), ("POST", "/api/generate"), ("POST", "/api/embed"),
}
MAX_BODY = 64 * 2**20


def handler(target: str, secret: str):
    parsed = urlparse(target)

    class Gate(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

        def _reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _forward(self) -> None:
            path = urlparse(self.path).path
            if not hmac.compare_digest(self.headers.get("X-Worker-Secret", ""), secret):
                return self._reply(401, b'{"error":"missing or wrong secret"}')
            if (self.command, path) not in ALLOWED:
                return self._reply(403, b'{"error":"only inference endpoints are available through this gate"}')
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._reply(413, b'{"error":"request too large"}')
            body = self.rfile.read(length) if length else None
            conn = http.client.HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port or 80, timeout=900)
            try:
                conn.request(self.command, path, body=body, headers={"Content-Type": "application/json"})
                upstream = conn.getresponse()
                data = upstream.read()
            except OSError as e:
                return self._reply(502, f'{{"error":"ollama unreachable: {e.__class__.__name__}"}}'.encode())
            finally:
                conn.close()
            self._reply(upstream.status, data, upstream.getheader("Content-Type", "application/json"))

        do_GET = _forward
        do_POST = _forward
        do_PUT = _forward
        do_DELETE = _forward
        do_PATCH = _forward

    return Gate


def main() -> None:
    parser = argparse.ArgumentParser(description="Secret-checking gate in front of Ollama")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=11435)
    parser.add_argument("--target", default="http://127.0.0.1:11434")
    args = parser.parse_args()
    secret = os.environ.get("CF_PROXY_SECRET") or secrets.token_urlsafe(24)
    if not os.environ.get("CF_PROXY_SECRET"):
        print(f"secret: {secret}", flush=True)
    server = ThreadingHTTPServer((args.host, args.port), handler(args.target, secret))
    print(f"gate on {args.host}:{args.port} -> {args.target}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
