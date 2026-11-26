#!/usr/bin/env python3
"""TraceNcode status page.

Probes the HTTPS services in services.json and serves a status page plus /api/status.
History is stored in SQLite under DATA_DIR (default ./data).
"""

from __future__ import annotations

import json
import os
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVICES_FILE = Path(os.environ.get("SERVICES_FILE", ROOT / "services.json"))
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
STATIC_DIR = ROOT / "static"
DB_PATH = DATA_DIR / "status.db"
PORT = int(os.environ.get("PORT", "8080"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))
CHECK_TIMEOUT = float(os.environ.get("CHECK_TIMEOUT", "12"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))
USER_AGENT = "TraceNcodeStatus/1.0 (+https://status.tracencode.tech)"

_lock = threading.Lock()
_services: list[dict] = []
_last_round_at: str | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_services() -> list[dict]:
    raw = json.loads(SERVICES_FILE.read_text())
    services = []
    for item in raw:
        url = item["url"].strip()
        if not url.startswith(("https://", "http://")):
            raise SystemExit(f"Refusing non-HTTP url in services.json: {url}")
        services.append(
            {
                "id": item["id"],
                "name": item["name"],
                "url": url,
                "description": item.get("description") or "",
            }
        )
    if not services:
        raise SystemExit("services.json is empty")
    return services


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY,
            service_id TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            status TEXT NOT NULL,
            http_code INTEGER,
            latency_ms INTEGER,
            error TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_checks_service_time ON checks(service_id, checked_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS state (
            service_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            since TEXT NOT NULL,
            http_code INTEGER,
            latency_ms INTEGER,
            error TEXT,
            checked_at TEXT NOT NULL
        )
        """
    )
    return conn


def classify(code: int | None) -> str:
    if code is None:
        return "down"
    if 200 <= code < 400:
        return "up"
    if 400 <= code < 500:
        return "degraded"
    return "down"


def probe(service: dict) -> dict:
    started = time.perf_counter()
    req = urllib.request.Request(
        service["url"],
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"},
        method="GET",
    )
    ctx = ssl.create_default_context()
    code = None
    error = None
    try:
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT, context=ctx) as resp:
            code = resp.status
            resp.read(512)
    except urllib.error.HTTPError as exc:
        code = exc.code
        error = f"HTTP {exc.code}"
    except Exception as exc:
        error = str(exc).strip() or exc.__class__.__name__
        if len(error) > 240:
            error = error[:240]
    latency_ms = int((time.perf_counter() - started) * 1000)
    status = classify(code)
    if status != "up" and error is None and code is not None:
        error = f"HTTP {code}"
    return {
        "service_id": service["id"],
        "checked_at": iso(utcnow()),
        "status": status,
        "http_code": code,
        "latency_ms": latency_ms if code is not None else None,
        "error": error,
    }


def record(results: list[dict]) -> None:
    with _lock:
        conn = connect()
        try:
            for row in results:
                previous = conn.execute(
                    "SELECT status, since FROM state WHERE service_id = ?",
                    (row["service_id"],),
                ).fetchone()
                since = row["checked_at"]
                if previous and previous["status"] == row["status"]:
                    since = previous["since"]
                conn.execute(
                    """
                    INSERT INTO checks (service_id, checked_at, status, http_code, latency_ms, error)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["service_id"],
                        row["checked_at"],
                        row["status"],
                        row["http_code"],
                        row["latency_ms"],
                        row["error"],
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO state (service_id, status, since, http_code, latency_ms, error, checked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(service_id) DO UPDATE SET
                        status = excluded.status,
                        since = excluded.since,
                        http_code = excluded.http_code,
                        latency_ms = excluded.latency_ms,
                        error = excluded.error,
                        checked_at = excluded.checked_at
                    """,
                    (
                        row["service_id"],
                        row["status"],
                        since,
                        row["http_code"],
                        row["latency_ms"],
                        row["error"],
                        row["checked_at"],
                    ),
                )
            cutoff = iso(utcnow() - timedelta(days=RETENTION_DAYS))
            conn.execute("DELETE FROM checks WHERE checked_at < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()


def run_round() -> None:
    global _last_round_at
    services = load_services()
    results = []
    with ThreadPoolExecutor(max_workers=min(8, len(services))) as pool:
        futures = {pool.submit(probe, service): service for service in services}
        for future in as_completed(futures):
            service = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append(
                    {
                        "service_id": service["id"],
                        "checked_at": iso(utcnow()),
                        "status": "down",
                        "http_code": None,
                        "latency_ms": None,
                        "error": str(exc)[:240],
                    }
                )
    record(results)
    _last_round_at = iso(utcnow())
    summary = ", ".join(f"{row['service_id']}={row['status']}" for row in results)
    print(f"[{_last_round_at}] {summary}", flush=True)


def checker_loop() -> None:
    while True:
        try:
            run_round()
        except Exception as exc:
            print(f"[checker] {exc}", flush=True)
        time.sleep(CHECK_INTERVAL)


def uptime_between(conn: sqlite3.Connection, service_id: str, start: str) -> float | None:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN status = 'up' THEN 1 ELSE 0 END) AS up
        FROM checks
        WHERE service_id = ? AND checked_at >= ?
        """,
        (service_id, start),
    ).fetchone()
    if not row or not row["n"]:
        return None
    return round(100.0 * row["up"] / row["n"], 2)


def snapshot() -> dict:
    now = utcnow()
    day_keys = [(now - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(89, -1, -1)]
    hour_keys = [
        (now - timedelta(hours=offset)).strftime("%Y-%m-%dT%H")
        for offset in range(23, -1, -1)
    ]
    start_90 = iso(now - timedelta(days=90))
    start_24h = iso(now - timedelta(hours=24))
    start_7d = iso(now - timedelta(days=7))

    with _lock:
        conn = connect()
        try:
            daily_rows = conn.execute(
                """
                SELECT service_id,
                       substr(checked_at, 1, 10) AS day,
                       COUNT(*) AS n,
                       SUM(CASE WHEN status = 'up' THEN 1 ELSE 0 END) AS up
                FROM checks
                WHERE checked_at >= ?
                GROUP BY service_id, day
                """,
                (start_90,),
            ).fetchall()
            hour_rows = conn.execute(
                """
                SELECT service_id,
                       substr(checked_at, 1, 13) AS hour,
                       ROUND(AVG(latency_ms)) AS ms
                FROM checks
                WHERE checked_at >= ? AND latency_ms IS NOT NULL
                GROUP BY service_id, hour
                """,
                (start_24h,),
            ).fetchall()
            states = {
                row["service_id"]: row
                for row in conn.execute("SELECT * FROM state")
            }
            services_out = []
            for service in _services:
                sid = service["id"]
                days_map = {
                    row["day"]: row
                    for row in daily_rows
                    if row["service_id"] == sid
                }
                hours_map = {
                    row["hour"]: row["ms"]
                    for row in hour_rows
                    if row["service_id"] == sid
                }
                state = states.get(sid)
                services_out.append(
                    {
                        **service,
                        "status": state["status"] if state else "unknown",
                        "http_code": state["http_code"] if state else None,
                        "latency_ms": state["latency_ms"] if state else None,
                        "error": state["error"] if state else None,
                        "checked_at": state["checked_at"] if state else None,
                        "since": state["since"] if state else None,
                        "uptime_24h": uptime_between(conn, sid, start_24h),
                        "uptime_7d": uptime_between(conn, sid, start_7d),
                        "uptime_90d": uptime_between(conn, sid, start_90),
                        "days": [
                            {
                                "date": day,
                                "uptime": (
                                    round(100.0 * days_map[day]["up"] / days_map[day]["n"], 1)
                                    if day in days_map and days_map[day]["n"]
                                    else None
                                ),
                            }
                            for day in day_keys
                        ],
                        "latency_24h": [
                            {"hour": hour, "ms": hours_map.get(hour)}
                            for hour in hour_keys
                        ],
                    }
                )
        finally:
            conn.close()

    statuses = [item["status"] for item in services_out]
    if any(status == "unknown" for status in statuses) and all(
        status in ("unknown",) for status in statuses
    ):
        overall = "unknown"
    elif any(status == "down" for status in statuses):
        overall = "outage"
    elif any(status in ("degraded", "unknown") for status in statuses):
        overall = "degraded"
    else:
        overall = "operational"

    return {
        "overall": overall,
        "checked_at": _last_round_at,
        "interval_seconds": CHECK_INTERVAL,
        "services": services_out,
    }


STATIC_TYPES = {
    ".png": "image/png",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def static_asset(url_path: str) -> tuple[bytes, str] | None:
    name = url_path.lstrip("/")
    if not name or "/" in name or name.startswith("."):
        return None
    candidate = (STATIC_DIR / name).resolve()
    if candidate.parent != STATIC_DIR.resolve() or not candidate.is_file():
        return None
    content_type = STATIC_TYPES.get(candidate.suffix.lower())
    if content_type is None:
        return None
    return candidate.read_bytes(), content_type


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        if self.path.startswith("/api/") or self.path in ("/", "/health"):
            return
        super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            body = json.dumps(snapshot()).encode()
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/health":
            self._send(200, b'{"ok":true}\n', "application/json")
            return
        if path in ("/", "/index.html"):
            page = (STATIC_DIR / "index.html").read_bytes()
            self._send(200, page, "text/html; charset=utf-8")
            return
        static = static_asset(path)
        if static is not None:
            body, content_type = static
            self._send(200, body, content_type)
            return
        self._send(404, b"Not found\n", "text/plain; charset=utf-8")


def main() -> None:
    global _services
    _services = load_services()
    connect().close()
    thread = threading.Thread(target=checker_loop, name="checker", daemon=True)
    thread.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"status page on :{PORT}  checking every {CHECK_INTERVAL}s", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
