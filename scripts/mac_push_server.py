"""
Tiny HTTP webhook on Mac. Receives POST /push from the RPi dashboard's
'Run now' button, runs push_local_data.sh, returns the result.

Auth: shared secret token in 'X-Token' header (set MAC_WEBHOOK_TOKEN env).
Bind: 127.0.0.1 by default (use Tailscale 100.x.x.x if you want LAN access).
"""
import os, subprocess, json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PORT = int(os.environ.get("MAC_WEBHOOK_PORT", "8089"))
HOST = os.environ.get("MAC_WEBHOOK_HOST", "0.0.0.0")
TOKEN = os.environ.get("MAC_WEBHOOK_TOKEN", "")
SCRIPT = Path(__file__).parent / "push_local_data.sh"

class H(BaseHTTPRequestHandler):
    def _json(self, code, body):
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok"})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if TOKEN and self.headers.get("x-token") != TOKEN:
            return self._json(401, {"error": "unauthorized"})
        if self.path != "/push":
            return self._json(404, {"error": "not found"})
        env = os.environ.copy()
        env.setdefault("RPI", "bikramghosh@raspberrypi.local")
        try:
            r = subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120)
            return self._json(
                200 if r.returncode == 0 else 500,
                {"returncode": r.returncode, "stdout": r.stdout[-2000:], "stderr": r.stderr[-2000:]},
            )
        except subprocess.TimeoutExpired:
            return self._json(504, {"error": "timeout"})

    def log_message(self, fmt, *args): pass  # quiet

if __name__ == "__main__":
    print(f"Mac push webhook listening on {HOST}:{PORT}")
    HTTPServer((HOST, PORT), H).serve_forever()
