"""Run a sync callable in a fresh subprocess with hard timeout + SIGKILL.

Why subprocess (not ``asyncio.to_thread``)?
  Python threads are not cancellable. When a third-party sync library
  busy-loops on a dead socket (e.g. baostock after the remote closes the
  TCP connection), the thread pegs one core forever and nothing in the
  asyncio event loop can stop it. A subprocess, by contrast, can be
  killed — ``proc.kill()`` ends the spin instantly and the OS reclaims
  the resources.

The runner protocol is documented in ``_runner.py``. The parent spawns
``python -m collector.sources._runner <mod> <func> <args_json>``, reads
the JSON envelope from stdout, and kills the subprocess if it doesn't
finish within ``timeout`` seconds.

Cost: ~150-250ms per fetch for Python interpreter startup + import. For
a personal-scale platform (<100 fetches/day) this is negligible compared
to upstream network I/O.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

from .base import SourceUnavailable

log = logging.getLogger(__name__)


async def run_sync_in_subprocess(
    mod_name: str,
    func_name: str,
    args: list[Any],
    *,
    timeout: float = 60.0,
    env_extra: dict[str, str] | None = None,
    python: str | None = None,
) -> Any:
    """Run ``mod_name.func_name(*args)`` in a subprocess; SIGKILL on timeout.

    Parameters
    ----------
    mod_name, func_name
        Dotted module path and the function name to call. The function must
        be importable at module top-level (no closures, no lambdas).
    args
        Positional arguments, JSON-serializable (use ``default=str`` for
        dates/Decimals — the runner round-trips through ``json.dumps``).
    timeout
        Hard wall-clock seconds. On expiry the subprocess is SIGKILLed and
        ``SourceUnavailable`` is raised so the registry can failover.
    env_extra
        Extra env vars for the child (e.g. ``{"TUSHARE_TOKEN": ...}``).
        Merged on top of the parent env. Avoid passing secrets via ``args``
        because argv is visible in ``/proc/<pid>/cmdline``.
    """
    py = python or sys.executable
    args_json = json.dumps(args, default=str)

    child_env = os.environ.copy()
    if env_extra:
        child_env.update(env_extra)

    log.debug("spawning subprocess: %s.%s(%s) timeout=%.0fs", mod_name, func_name, args_json, timeout)
    try:
        proc = await asyncio.create_subprocess_exec(
            py, "-m", "collector.sources._runner",
            mod_name, func_name, args_json,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )
    except Exception as e:
        raise SourceUnavailable(f"failed to spawn fetch subprocess: {e}") from e

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        # SIGKILL is uncatchable — ends any CPU spin inside the child instantly.
        proc.kill()
        await proc.wait()
        log.warning("%s.%s timed out after %.0fs — subprocess killed", mod_name, func_name, timeout)
        raise SourceUnavailable(
            f"{mod_name}.{func_name} timed out after {timeout:.0f}s (subprocess killed)"
        )

    stdout = stdout_b.decode(errors="replace") if stdout_b else ""
    stderr = stderr_b.decode(errors="replace") if stderr_b else ""

    # Runner crashed before printing its envelope (e.g. import error, OOM, SIGKILL).
    if not stdout:
        raise SourceUnavailable(
            f"subprocess produced no stdout (exit={proc.returncode}): {stderr[:500]}"
        )

    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise SourceUnavailable(
            f"subprocess returned non-JSON stdout: {stdout[:200]!r}; stderr={stderr[:200]!r}"
        ) from e

    if not envelope.get("ok"):
        msg = envelope.get("error", "subprocess returned ok=False without error")
        # Include last frame of traceback for debuggability (truncated by runner to 4 frames)
        tb = envelope.get("traceback")
        if tb:
            log.debug("subprocess traceback for %s.%s:\n%s", mod_name, func_name, tb)
        raise SourceUnavailable(msg)

    return envelope["data"]
