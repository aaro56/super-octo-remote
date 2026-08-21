#!/usr/bin/env python3
"""Start every project sandbox for the embedded accounts every two minutes."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


EMAILS = (
    "nviirionvw@proton.me",
)

API_BASE_URL = "https://api.lovable.dev"
FIREBASE_API_KEY = os.environ.get(
    "LOVABLE_FIREBASE_API_KEY",
    "AIzaSyBQNjlw9Vp4tP4VVeANzyPJnqbG2wLbYPw",
)
SIGN_IN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
REFRESH_URL = "https://securetoken.googleapis.com/v1/token"
INTERVAL_SECONDS = 120.0
TIMEOUT_SECONDS = 20.0
TOKEN_MARGIN_SECONDS = 120
ACCOUNT_WORKERS = 8
START_WORKERS = 32
DEFAULT_STATUS_PORT = 7860

_thread_local = threading.local()


class RequestError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class StatusHandler(BaseHTTPRequestHandler):
    body = b"status: ok\n"

    def respond(self, include_body: bool) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(self.body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if include_body:
            self.wfile.write(self.body)

    def do_GET(self) -> None:
        self.respond(include_body=True)

    def do_HEAD(self) -> None:
        self.respond(include_body=False)

    def log_message(self, format: str, *args: Any) -> None:
        return


def emit(event: str, **details: Any) -> None:
    print(
        json.dumps(
            {
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "event": event,
                **details,
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        flush=True,
    )


def start_status_server() -> ThreadingHTTPServer:
    raw_port = os.environ.get("STATUS_PORT", str(DEFAULT_STATUS_PORT))
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError(f"Invalid STATUS_PORT: {raw_port!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"Invalid STATUS_PORT: {raw_port!r}")
    server = ThreadingHTTPServer(("0.0.0.0", port), StatusHandler)
    threading.Thread(
        target=server.serve_forever,
        name="status-server",
        daemon=True,
    ).start()
    emit("status_server_started", port=port)
    return server


def opener():
    if not hasattr(_thread_local, "opener"):
        _thread_local.opener = build_opener(ProxyHandler({}))
    return _thread_local.opener


def error_message(raw: bytes, status: int) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"HTTP {status}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return f"HTTP {status}: {error['message']}"
        for key in ("message", "detail", "title", "type", "error"):
            if isinstance(payload.get(key), str) and payload[key]:
                return f"HTTP {status}: {payload[key]}"
    return f"HTTP {status}"


def request_json(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    json_body: Any = None,
    form_body: dict[str, str] | None = None,
) -> Any:
    headers = {
        "Accept": "application/json",
        "User-Agent": "lovable-all-account-cycle/1",
    }
    data = None
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is not None:
        data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif form_body is not None:
        data = urlencode(form_body).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = Request(url, data=data, method=method, headers=headers)
    try:
        with opener().open(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read()
    except HTTPError as exc:
        raw = exc.read()
        raise RequestError(error_message(raw, exc.code), exc.code) from exc
    except (URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RequestError(f"Network error: {reason}") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestError("Server returned malformed JSON") from exc


def required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RequestError(f"Response is missing {key}")
    return value


def expires_in(payload: dict[str, Any], key: str) -> int:
    try:
        value = int(payload.get(key))
    except (TypeError, ValueError) as exc:
        raise RequestError(f"Response has an invalid {key}") from exc
    if value <= 0:
        raise RequestError(f"Response has an invalid {key}")
    return value


class AccountSession:
    def __init__(self, email: str, password: str) -> None:
        self.email = email
        self.password = password
        self.id_token = ""
        self.refresh_token = ""
        self.expires_at = 0.0
        self.lock = threading.Lock()

    def apply_sign_in(self) -> None:
        payload = request_json(
            f"{SIGN_IN_URL}?{urlencode({'key': FIREBASE_API_KEY})}",
            method="POST",
            json_body={
                "email": self.email,
                "password": self.password,
                "returnSecureToken": True,
            },
        )
        if not isinstance(payload, dict):
            raise RequestError("Firebase returned an unexpected response")
        returned_email = required_string(payload, "email")
        if returned_email.casefold() != self.email.casefold():
            raise RequestError("Firebase returned a token for a different account")
        self.id_token = required_string(payload, "idToken")
        self.refresh_token = required_string(payload, "refreshToken")
        self.expires_at = time.time() + expires_in(payload, "expiresIn")

    def apply_refresh(self) -> None:
        payload = request_json(
            f"{REFRESH_URL}?{urlencode({'key': FIREBASE_API_KEY})}",
            method="POST",
            form_body={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
        )
        if not isinstance(payload, dict):
            raise RequestError("Firebase returned an unexpected response")
        self.id_token = required_string(payload, "id_token")
        self.refresh_token = required_string(payload, "refresh_token")
        self.expires_at = time.time() + expires_in(payload, "expires_in")

    def token(self) -> str:
        with self.lock:
            if (
                self.id_token
                and self.expires_at > time.time() + TOKEN_MARGIN_SECONDS
            ):
                return self.id_token
            if self.refresh_token:
                try:
                    self.apply_refresh()
                    return self.id_token
                except RequestError:
                    self.id_token = ""
                    self.refresh_token = ""
            self.apply_sign_in()
            return self.id_token

    def refresh_after_401(self, rejected_token: str) -> None:
        with self.lock:
            if self.id_token and self.id_token != rejected_token:
                return
            if self.refresh_token:
                try:
                    self.apply_refresh()
                    return
                except RequestError:
                    self.id_token = ""
                    self.refresh_token = ""
            self.apply_sign_in()

    def api_json(self, method: str, path: str, body: Any = None) -> Any:
        url = f"{API_BASE_URL}/{path.lstrip('/')}"
        token = self.token()
        try:
            return request_json(
                url,
                method=method,
                token=token,
                json_body=body,
            )
        except RequestError as exc:
            if exc.status != 401:
                raise
        self.refresh_after_401(token)
        return request_json(
            url,
            method=method,
            token=self.token(),
            json_body=body,
        )


def discover_projects(session: AccountSession) -> list[str]:
    payload = session.api_json("GET", "/user/workspaces")
    workspaces = payload.get("workspaces") if isinstance(payload, dict) else None
    if not isinstance(workspaces, list):
        raise RequestError("Unexpected workspaces response")
    workspace_ids = {
        workspace["id"]
        for workspace in workspaces
        if isinstance(workspace, dict) and isinstance(workspace.get("id"), str)
    }
    projects: set[str] = set()
    for workspace_id in workspace_ids:
        payload = session.api_json(
            "GET",
            f"/workspaces/{workspace_id}/projects/search?limit=100&offset=0",
        )
        items = payload.get("projects") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise RequestError(f"Unexpected projects response for {workspace_id}")
        projects.update(
            project["id"]
            for project in items
            if isinstance(project, dict) and isinstance(project.get("id"), str)
        )
    return sorted(projects)


def start_project(session: AccountSession, project_id: str) -> None:
    for attempt in range(2):
        try:
            session.api_json(
                "POST",
                f"/projects/{project_id}/sandbox/start",
                {"project_id": project_id},
            )
            return
        except RequestError as exc:
            transient = (
                exc.status is None
                or exc.status in (408, 429, 499)
                or (exc.status is not None and exc.status >= 500)
            )
            if attempt == 0 and transient:
                time.sleep(1)
                continue
            raise


def run_cycle(sessions: list[AccountSession], cycle: int) -> None:
    targets: list[tuple[AccountSession, str]] = []
    with ThreadPoolExecutor(max_workers=ACCOUNT_WORKERS) as pool:
        futures = {
            pool.submit(discover_projects, session): session for session in sessions
        }
        for future in as_completed(futures):
            session = futures[future]
            try:
                projects = future.result()
                targets.extend((session, project_id) for project_id in projects)
                emit(
                    "account_discovered",
                    cycle=cycle,
                    email=session.email,
                    project_count=len(projects),
                )
            except Exception as exc:
                emit(
                    "account_failed",
                    cycle=cycle,
                    email=session.email,
                    error=f"{type(exc).__name__}: {exc}",
                )

    emit(
        "cycle_started",
        cycle=cycle,
        account_count=len(sessions),
        project_count=len(targets),
    )
    accepted = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=START_WORKERS) as pool:
        futures = {
            pool.submit(start_project, session, project_id): (session, project_id)
            for session, project_id in targets
        }
        for future in as_completed(futures):
            session, project_id = futures[future]
            try:
                future.result()
                accepted += 1
            except Exception as exc:
                failed += 1
                emit(
                    "project_failed",
                    cycle=cycle,
                    email=session.email,
                    project_id=project_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
    emit(
        "cycle_finished",
        cycle=cycle,
        accepted=accepted,
        failed=failed,
        project_count=len(targets),
    )


def main() -> int:
    password = os.environ.get("PASSWORD")
    if not password:
        print("PASSWORD environment variable is required", file=sys.stderr)
        return 2
    try:
        status_server = start_status_server()
    except (OSError, ValueError) as exc:
        print(f"Cannot start status server: {exc}", file=sys.stderr)
        return 1
    sessions = [AccountSession(email, password) for email in EMAILS]
    cycle = 0
    try:
        while True:
            cycle += 1
            started = time.monotonic()
            try:
                run_cycle(sessions, cycle)
            except Exception as exc:
                emit("cycle_failed", cycle=cycle, error=f"{type(exc).__name__}: {exc}")
            time.sleep(max(0.0, INTERVAL_SECONDS - (time.monotonic() - started)))
    finally:
        status_server.shutdown()
        status_server.server_close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
