#!/usr/bin/env python3
"""
Weekly Ops Dashboard - local server + NerdGraph proxy.

Why a proxy is needed: New Relic's NerdGraph endpoint does not send CORS
headers, so a browser page cannot call it directly. This tiny stdlib-only
server sits in front of it, keeps your API key out of the HTML, and serves
the dashboard.

Run:  python3 server.py
Then: http://localhost:8787

On startup it refreshes the Jira delivery data in the background, unless
delivery.json is already recent or the credentials are missing. The server
answers immediately either way; the page picks the data up when it lands.

  --no-jira           do not refresh Jira at all
  --refresh-jira      refresh even if delivery.json is recent
  --jira-max-age N    hours before it is considered stale (default 12)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
KEY_PATH = os.path.join(BASE_DIR, "api_key.txt")
JIRA_SCRIPT = os.path.join(BASE_DIR, "fetch_jira.py")
JIRA_TOKEN_PATH = os.path.join(BASE_DIR, "jira_token.txt")
DELIVERY_PATH = os.path.join(BASE_DIR, "delivery.json")

# What the last Jira refresh did, so the page can explain itself instead of
# only telling the reader to go and run a script.
JIRA_STATUS = {"state": "idle", "message": "not started", "detail": "",
               "startedAt": None, "finishedAt": None}

ENDPOINTS = {
    "US": "https://api.newrelic.com/graphql",
    "EU": "https://api.eu.newrelic.com/graphql",
}

PORT = int(os.environ.get("PORT", "8787"))


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


def load_api_key():
    key = os.environ.get("NEW_RELIC_API_KEY", "").strip()
    if key:
        return key
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
        # ignore the shipped placeholder and any commented-out lines
        key = "\n".join(l for l in key.splitlines() if not l.startswith("#")).strip()
        if key and "PASTE_YOUR" not in key:
            return key
    return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "WeeklyOpsDash/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    # ---------- helpers ----------

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # ---------- routes ----------

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            fpath = os.path.join(BASE_DIR, "index.html")
            if not os.path.exists(fpath):
                return self._send(404, "index.html not found", "text/plain")
            with open(fpath, "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")

        # the dashboard reads these as plain static files in both modes
        if path in ("/data.json", "/delivery.json", "/engagement.json"):
            fpath = os.path.join(BASE_DIR, path.lstrip("/"))
            if not os.path.exists(fpath):
                return self._send(404, {"error": path.lstrip("/") + " not generated yet"})
            with open(fpath, "rb") as fh:
                return self._send(200, fh.read())

        if path == "/api/jira-status":
            st = dict(JIRA_STATUS)
            st["hasFile"] = os.path.exists(DELIVERY_PATH)
            age = delivery_age_hours()
            st["ageHours"] = None if age is None else round(age, 2)
            return self._send(200, st)

        if path == "/api/config":
            cfg = load_config()
            cfg["_hasKey"] = bool(load_api_key())
            return self._send(200, cfg)

        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/config":
            try:
                cfg = self._read_json()
            except Exception as exc:
                return self._send(400, {"error": str(exc)})
            cfg.pop("_hasKey", None)
            save_config(cfg)
            return self._send(200, {"ok": True})

        if path == "/api/jira-refresh":
            if JIRA_STATUS.get("state") == "running":
                return self._send(200, {"ok": True, "already": True})
            refresh_jira(force=True)
            return self._send(200, {"ok": True})

        if path == "/api/nerdgraph":
            return self._proxy()

        return self._send(404, {"error": "not found"})

    def _proxy(self):
        api_key = load_api_key()
        if not api_key:
            return self._send(400, {
                "error": "No API key. Put a New Relic User key in api_key.txt "
                         "next to server.py, or set NEW_RELIC_API_KEY, then restart."
            })

        try:
            payload = self._read_json()
        except Exception as exc:
            return self._send(400, {"error": str(exc)})

        region = (payload.get("region") or load_config().get("region") or "US").upper()
        url = ENDPOINTS.get(region, ENDPOINTS["US"])

        body = json.dumps({
            "query": payload.get("query", ""),
            "variables": payload.get("variables", {}),
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "API-Key": api_key,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read()
            return self._send(200, raw)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:2000]
            return self._send(200, {
                "errors": [{"message": "NerdGraph HTTP %s: %s" % (exc.code, detail)}]
            })
        except Exception as exc:
            return self._send(200, {"errors": [{"message": str(exc)}]})


def jira_ready():
    """Credentials present, and something to run."""
    if not os.path.exists(JIRA_SCRIPT):
        return False, "fetch_jira.py is missing"
    if os.environ.get("JIRA_EMAIL") and os.environ.get("JIRA_API_TOKEN"):
        return True, "from the environment"
    if os.path.exists(JIRA_TOKEN_PATH):
        with open(JIRA_TOKEN_PATH, encoding="utf-8") as fh:
            lines = [l.strip() for l in fh if l.strip() and not l.startswith("#")]
        if lines and (len(lines) >= 2 or ":" in lines[0]):
            return True, "from jira_token.txt"
        return False, "jira_token.txt needs the email on line 1 and the token on line 2"
    return False, "no jira_token.txt and no JIRA_EMAIL / JIRA_API_TOKEN"


def delivery_age_hours():
    if not os.path.exists(DELIVERY_PATH):
        return None
    return (time.time() - os.path.getmtime(DELIVERY_PATH)) / 3600.0


def set_jira_status(state, message, detail=""):
    JIRA_STATUS.update(state=state, message=message, detail=detail[-4000:])
    if state == "running":
        JIRA_STATUS["startedAt"] = time.time()
        JIRA_STATUS["finishedAt"] = None
    else:
        JIRA_STATUS["finishedAt"] = time.time()


def refresh_jira(force=False, max_age=12.0):
    """
    Run fetch_jira.py in the background so startup is not blocked — 52 weeks
    is a few hundred requests. A failure is reported and otherwise ignored:
    the ops dashboard does not depend on it.
    """
    ok, why = jira_ready()
    if not ok:
        set_jira_status("skipped", "No Jira credentials", why)
        print("  Jira      : skipped (%s)" % why, flush=True)
        return

    age = delivery_age_hours()
    if age is not None and not force and age < max_age:
        print("  Jira      : delivery.json is %.1fh old, keeping it "
              "(--refresh-jira to force)" % age)
        return

    print("  Jira      : refreshing in the background (%s)%s"
          % (why, "" if age is None else " — %.1fh old" % age))

    set_jira_status("running", "Fetching from Jira")

    def run():
        started = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, JIRA_SCRIPT, "--config", CONFIG_PATH,
                 "--out", DELIVERY_PATH],
                cwd=BASE_DIR, capture_output=True, text=True, timeout=900)
        except Exception as exc:
            set_jira_status("failed", "Could not start fetch_jira.py", str(exc))
            print("  Jira      : could not start — %s" % exc, flush=True)
            return
        secs = time.time() - started
        out = (proc.stdout or "") + (proc.stderr or "")
        lines = [l for l in out.splitlines() if l.strip()]
        if proc.returncode == 0:
            set_jira_status("ok", lines[-1] if lines else "delivery.json written", out)
            print("  Jira      : done in %.0fs — %s" % (secs, lines[-1] if lines else "written"), flush=True)
            print("              the page picks it up on its own", flush=True)
        else:
            set_jira_status("failed", lines[-1] if lines else "no output", out)
            print("  Jira      : FAILED after %.0fs — %s" % (secs, lines[-1] if lines else "no output"), flush=True)
            print("              the rest of the dashboard is unaffected", flush=True)

    threading.Thread(target=run, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description="Weekly Ops Dashboard local server")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-jira", action="store_true",
                    help="do not refresh the Jira delivery data on startup")
    ap.add_argument("--refresh-jira", action="store_true",
                    help="refresh Jira even if delivery.json is recent")
    ap.add_argument("--jira-max-age", type=float, default=12.0,
                    help="hours before delivery.json is considered stale")
    args = ap.parse_args()

    # Claim the port before anything slow happens. An older copy of this script
    # still holding it is the usual cause, and it serves the routes it was
    # started with — Python does not reload its own source — so the fix is to
    # stop it, not to work around it.
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        if exc.errno not in (48, 98):                     # EADDRINUSE on mac/linux
            raise
        print("", flush=True)
        print("  Port %d is already in use — most likely an older server.py."
              % args.port, flush=True)
        print("  That one is still serving the routes it was started with, so it", flush=True)
        print("  will not pick up any changes made since.", flush=True)
        print("", flush=True)
        print("  Stop it:      kill $(lsof -ti tcp:%d)" % args.port, flush=True)
        print("  Or use another port:   python3 server.py --port %d"
              % (args.port + 1), flush=True)
        print("", flush=True)
        return 1

    key = load_api_key()
    team = (load_config().get("team") or {}).get("name") or "Weekly Ops Dashboard"
    print("", flush=True)
    print("  " + team, flush=True)
    print("  " + "-" * len(team), flush=True)
    print("  Serving   : http://localhost:%d" % args.port, flush=True)
    print("  API key   : %s" % ("loaded (%d chars)" % len(key) if key else
                                "MISSING - add it to api_key.txt and restart"))
    print("  Config    : %s" % CONFIG_PATH, flush=True)
    if args.no_jira:
        print("  Jira      : disabled (--no-jira)", flush=True)
    else:
        refresh_jira(force=args.refresh_jira, max_age=args.jira_max_age)
    print("", flush=True)
    print("  Press Ctrl+C to stop.", flush=True)
    print("", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
