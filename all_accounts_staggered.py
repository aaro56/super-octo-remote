#!/usr/bin/env python3
"""Run cached Lovable project starts on a bounded, staggered schedule."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from all_accounts_2min_cycle import (
    AccountSession,
    discover_projects,
    emit,
    start_project,
    start_status_server,
)

DEFAULT_INTERVAL_SECONDS = 120.0
DEFAULT_HEADROOM_SECONDS = 15.0
DEFAULT_REFRESH_SECONDS = 900.0
DEFAULT_ACCOUNT_WORKERS = 2
DEFAULT_START_WORKERS = 12


def env_number(name: str, default: float) -> float:
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid {name}: {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def env_integer(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid {name}: {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def load_sessions() -> list[AccountSession]:
    raw = os.environ.get("ACCOUNTS_JSON")
    if not raw:
        raise ValueError("ACCOUNTS_JSON is required")
    try:
        accounts = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid ACCOUNTS_JSON: {exc}") from exc
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("ACCOUNTS_JSON must be a nonempty JSON array")

    sessions: list[AccountSession] = []
    seen: set[str] = set()
    for item in accounts:
        if not isinstance(item, dict):
            raise ValueError("Each ACCOUNTS_JSON entry must be an object")
        email = item.get("email")
        password = item.get("password")
        if not isinstance(email, str) or not email:
            raise ValueError("Each account requires an email")
        if not isinstance(password, str) or not password:
            raise ValueError(f"Account {email!r} requires a password")
        normalized = email.casefold()
        if normalized in seen:
            raise ValueError(f"Duplicate account: {email}")
        seen.add(normalized)
        sessions.append(AccountSession(email, password))
    return sessions


def memory_mb() -> float | None:
    for filename in (
        "/sys/fs/cgroup/memory.current",
        "/sys/fs/cgroup/memory/memory.usage_in_bytes",
    ):
        try:
            value = int(Path(filename).read_text(encoding="ascii").strip())
        except (OSError, UnicodeError, ValueError):
            continue
        return round(value / (1024 * 1024), 1)

    try:
        for line in Path("/proc/self/status").read_text(
            encoding="ascii"
        ).splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except (OSError, UnicodeError, ValueError):
        pass
    return None


async def refresh_projects(
    sessions: list[AccountSession],
    pool: ThreadPoolExecutor,
    cache: dict[str, list[str]],
) -> None:
    loop = asyncio.get_running_loop()

    async def refresh(session: AccountSession) -> None:
        try:
            projects = await loop.run_in_executor(
                pool, discover_projects, session
            )
        except Exception as exc:
            emit(
                "account_failed",
                email=session.email,
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        cache[session.email] = projects
        emit(
            "account_discovered",
            email=session.email,
            project_count=len(projects),
        )

    await asyncio.gather(*(refresh(session) for session in sessions))


def interleaved_targets(
    sessions: list[AccountSession],
    cache: dict[str, list[str]],
) -> list[tuple[AccountSession, str]]:
    longest = max((len(cache.get(session.email, ())) for session in sessions), default=0)
    targets: list[tuple[AccountSession, str]] = []
    for project_index in range(longest):
        for session in sessions:
            projects = cache.get(session.email, ())
            if project_index < len(projects):
                targets.append((session, projects[project_index]))
    return targets


async def run_staggered_cycle(
    sessions: list[AccountSession],
    targets: list[tuple[AccountSession, str]],
    pool: ThreadPoolExecutor,
    *,
    cycle: int,
    interval_seconds: float,
    headroom_seconds: float,
    start_workers: int,
) -> None:
    loop = asyncio.get_running_loop()
    cycle_started = time.monotonic()
    active_window = max(1.0, interval_seconds - headroom_seconds)
    spacing = active_window / len(targets) if targets else 0.0
    pending: set[asyncio.Task[tuple[AccountSession, str, Exception | None]]] = set()
    accepted = 0
    failed = 0
    maximum_lag = 0.0

    async def run_start(
        session: AccountSession, project_id: str
    ) -> tuple[AccountSession, str, Exception | None]:
        try:
            await loop.run_in_executor(
                pool, start_project, session, project_id
            )
            return session, project_id, None
        except Exception as exc:
            return session, project_id, exc

    def consume(
        finished: set[
            asyncio.Task[tuple[AccountSession, str, Exception | None]]
        ],
    ) -> None:
        nonlocal accepted, failed
        for task in finished:
            session, project_id, error = task.result()
            if error is None:
                accepted += 1
                continue
            failed += 1
            emit(
                "project_failed",
                cycle=cycle,
                email=session.email,
                project_id=project_id,
                error=f"{type(error).__name__}: {error}",
            )

    emit(
        "cycle_started",
        cycle=cycle,
        account_count=len(sessions),
        project_count=len(targets),
        start_workers=start_workers,
    )

    for index, (session, project_id) in enumerate(targets):
        due_at = cycle_started + index * spacing
        delay = due_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

        already_finished = {task for task in pending if task.done()}
        pending.difference_update(already_finished)
        consume(already_finished)

        while len(pending) >= start_workers:
            finished, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            consume(finished)

        maximum_lag = max(maximum_lag, time.monotonic() - due_at)
        pending.add(asyncio.create_task(run_start(session, project_id)))

    if pending:
        finished, _ = await asyncio.wait(pending)
        consume(finished)

    duration = time.monotonic() - cycle_started
    emit(
        "cycle_finished",
        cycle=cycle,
        accepted=accepted,
        failed=failed,
        project_count=len(targets),
        duration_seconds=round(duration, 1),
        maximum_lag_seconds=round(maximum_lag, 1),
        memory_mb=memory_mb(),
    )


async def async_main() -> int:
    try:
        sessions = load_sessions()
        interval_seconds = env_number(
            "INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS
        )
        headroom_seconds = env_number(
            "HEADROOM_SECONDS", DEFAULT_HEADROOM_SECONDS
        )
        refresh_seconds = env_number(
            "PROJECT_REFRESH_SECONDS", DEFAULT_REFRESH_SECONDS
        )
        account_workers = env_integer(
            "ACCOUNT_WORKERS", DEFAULT_ACCOUNT_WORKERS
        )
        start_workers = env_integer("START_WORKERS", DEFAULT_START_WORKERS)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if headroom_seconds >= interval_seconds:
        print("HEADROOM_SECONDS must be smaller than INTERVAL_SECONDS", file=sys.stderr)
        return 2

    try:
        status_server = start_status_server()
    except (OSError, ValueError) as exc:
        print(f"Cannot start status server: {exc}", file=sys.stderr)
        return 1

    project_cache: dict[str, list[str]] = {}
    refresh_task: asyncio.Task[None] | None = None
    with (
        ThreadPoolExecutor(max_workers=account_workers) as account_pool,
        ThreadPoolExecutor(max_workers=start_workers) as start_pool,
    ):
        try:
            await refresh_projects(sessions, account_pool, project_cache)
            next_refresh = time.monotonic() + refresh_seconds
            cycle = 0
            while True:
                cycle_started = time.monotonic()
                if refresh_task is not None and refresh_task.done():
                    await refresh_task
                    refresh_task = None

                if (
                    refresh_task is None
                    and time.monotonic() >= next_refresh
                ):
                    refresh_task = asyncio.create_task(
                        refresh_projects(
                            sessions, account_pool, project_cache
                        )
                    )
                    next_refresh = time.monotonic() + refresh_seconds

                cycle += 1
                await run_staggered_cycle(
                    sessions,
                    interleaved_targets(sessions, project_cache),
                    start_pool,
                    cycle=cycle,
                    interval_seconds=interval_seconds,
                    headroom_seconds=headroom_seconds,
                    start_workers=start_workers,
                )
                await asyncio.sleep(
                    max(
                        0.0,
                        interval_seconds
                        - (time.monotonic() - cycle_started),
                    )
                )
        finally:
            if refresh_task is not None:
                refresh_task.cancel()
            status_server.shutdown()
            status_server.server_close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(async_main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
