"""Standalone subprocess runner for sync fetch calls.

Invoked by ``collector.sources.subprocess_runner.run_sync_in_subprocess`` as:

    python -m collector.sources._runner <module> <function> <json_args>

The runner imports ``<module>``, calls ``<function>(*args)``, and prints a JSON
envelope to stdout:

    {"ok": true, "data": <result>}                  on success
    {"ok": false, "error": "<msg>", "traceback": …} on exception

The parent process owns the timeout: if this subprocess doesn't finish in time,
the parent SIGKILLs it. That's the whole point — third-party sync libs
(baostock, akshare) that busy-loop on dead sockets can't be cancelled from a
thread, but a subprocess kill always works.

Exits non-zero only on internal runner failure (bad argv). Normal application
exceptions are reported via the JSON envelope so the parent can raise a
structured ``SourceUnavailable``.
"""
from __future__ import annotations

import importlib
import json
import sys
import traceback


def main() -> None:
    if len(sys.argv) != 4:
        sys.stdout.write(json.dumps({
            "ok": False,
            "error": f"usage: {sys.argv[0]} <module> <function> <json_args>",
        }))
        sys.stdout.write("\n")
        sys.exit(2)

    mod_name, func_name, args_json = sys.argv[1], sys.argv[2], sys.argv[3]

    try:
        args = json.loads(args_json)
        mod = importlib.import_module(mod_name)
        func = getattr(mod, func_name)
        result = func(*args)
        payload = {"ok": True, "data": result}
    except Exception as e:  # noqa: BLE001 — we report everything to the parent
        payload = {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(limit=4),
        }

    sys.stdout.write(json.dumps(payload, default=str))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
