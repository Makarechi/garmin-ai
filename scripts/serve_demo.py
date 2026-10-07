"""Serve the synthetic dashboard demo with only Python's standard library."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "src/garmin_ai/static/dashboard"
FILES = {
    "/dashboard": ("index.html", "text/html; charset=utf-8"),
    "/dashboard-assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/dashboard-assets/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class DemoHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in FILES:
            self.send_error(404)
            return
        name, content_type = FILES[self.path]
        body = (ASSETS / name).read_bytes()
        if name == "index.html":
            body = body.replace(
                b'<button id="connect"',
                b'<button id="connect" hidden disabled',
            )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="Run the account-free synthetic dashboard demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), DemoHandler)
    print(f"Open http://{args.host}:{server.server_port}/dashboard", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
