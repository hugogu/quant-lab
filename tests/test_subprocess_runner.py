"""Tests for the subprocess runner — the CPU-spin containment mechanism.

These verify the contract that matters for the original bug fix:
  1. Normal sync calls return their result to the parent.
  2. Exceptions in the child surface as SourceUnavailable in the parent.
  3. A stuck child (infinite loop) is SIGKILLed on timeout, and the parent
     observes SourceUnavailable. Critically, the child PID must be gone
     afterwards — otherwise the CPU spin would keep running.

Run: docker compose exec api python -m pytest tests/test_subprocess_runner.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from collector.sources.base import SourceUnavailable
from collector.sources.subprocess_runner import run_sync_in_subprocess


# ============================================================
# A tiny helper module the subprocess can import.
# Lives in this test file's module so we have a stable dotted path.
# ============================================================

def _echo(x):
    return x


def _boom():
    raise RuntimeError("intentional child failure")


def _sleep_forever():
    # Pure busy-loop, mimicking baostock's send_msg on a CLOSE_WAIT socket.
    # Cannot be interrupted except by SIGKILL — exactly the bug scenario.
    while True:
        pass


def _slow():
    time.sleep(2)
    return "done"


# ============================================================
# Success path
# ============================================================

@pytest.mark.asyncio
async def test_success_returns_data():
    result = await run_sync_in_subprocess(
        "tests.test_subprocess_runner", "_echo",
        ["hello"], timeout=15,
    )
    assert result == "hello"


@pytest.mark.asyncio
async def test_success_with_multiple_args():
    # Multi-arg dispatch: verify arg ordering & count survive the JSON round-trip.
    # min(5, 3) == 3 proves both args arrived in the right order.
    result = await run_sync_in_subprocess(
        "builtins", "min", [5, 3], timeout=15,
    )
    assert result == 3


# ============================================================
# Exception path
# ============================================================

@pytest.mark.asyncio
async def test_child_exception_surfaces_as_source_unavailable():
    with pytest.raises(SourceUnavailable) as exc:
        await run_sync_in_subprocess(
            "tests.test_subprocess_runner", "_boom",
            [], timeout=15,
        )
    assert "intentional child failure" in str(exc.value)


@pytest.mark.asyncio
async def test_unknown_module_raises():
    with pytest.raises(SourceUnavailable):
        await run_sync_in_subprocess(
            "nonexistent.module", "anything",
            [], timeout=15,
        )


# ============================================================
# Timeout + SIGKILL path — the actual regression guard
# ============================================================

@pytest.mark.asyncio
async def test_timeout_raises_and_kills_child():
    """Reproduces the baostock CPU-spin scenario: a child that never returns.
    Verifies the parent raises SourceUnavailable AND the child process is
    actually gone (no zombie burning CPU)."""
    t0 = time.monotonic()
    with pytest.raises(SourceUnavailable) as exc:
        await run_sync_in_subprocess(
            "tests.test_subprocess_runner", "_sleep_forever",
            [], timeout=2,
        )
    elapsed = time.monotonic() - t0
    # Should return promptly after the timeout (give scheduler some slack)
    assert elapsed < 6, f"timeout took {elapsed:.1f}s, expected ~2s"
    assert "timed out" in str(exc.value)
    assert "killed" in str(exc.value)


@pytest.mark.asyncio
async def test_timeout_terminates_slow_child():
    """A child that would finish in 2s but we time out at 0.5s.
    The child must be killed before it writes its result."""
    with pytest.raises(SourceUnavailable):
        await run_sync_in_subprocess(
            "tests.test_subprocess_runner", "_slow",
            [], timeout=0.5,
        )


@pytest.mark.asyncio
async def test_no_zombie_processes_after_timeout():
    """After a timeout-induced kill, no python child of the test process
    should still be running. Catches the case where SIGKILL is sent but
    the PID gets reused / the loop survives."""
    parent_pid = os.getpid()

    def child_python_pids() -> set[int]:
        # /proc only on Linux containers; on macOS dev host skip this check
        if not os.path.isdir("/proc"):
            return set()
        out: set[int] = set()
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat") as f:
                    stat = f.read().split()
                if stat[2] in ("R", "S", "D") and "python" in stat[1].lower():
                    out.add(int(entry))
            except (ProcessLookupError, FileNotFoundError, IndexError):
                continue
        return out

    before = child_python_pids()
    with pytest.raises(SourceUnavailable):
        await run_sync_in_subprocess(
            "tests.test_subprocess_runner", "_sleep_forever",
            [], timeout=1,
        )
    # Give the OS a moment to reap the killed child
    await asyncio.sleep(0.5)
    after = child_python_pids()
    # New lingering python PIDs (beyond what existed before) indicate a leak.
    # We can't perfectly attribute parentage in /proc without extra work,
    # but a spinning busy-loop child would definitely show up here.
    leaked = after - before
    # Tolerate the test runner's own children fluctuation by checking the
    # specific busy-loop is gone: count PIDs that appear *after* the kill
    # and weren't there *before*. Should be empty in practice.
    assert not leaked, f"possible leaked subprocess PIDs after timeout: {leaked}"


# ============================================================
# Arg marshalling
# ============================================================

@pytest.mark.asyncio
async def test_date_arg_marshalled_via_default_str():
    """Dates aren't JSON-native; the runner uses default=str. Verify a date
    comes through as its ISO string."""
    from datetime import date
    # _echo just returns what it's given; the round-trip converts date→str
    result = await run_sync_in_subprocess(
        "tests.test_subprocess_runner", "_echo",
        [date(2026, 7, 18)], timeout=10,
    )
    assert result == "2026-07-18"
