# -*- coding: utf-8 -*-
"""Make `check-windows.ps1 -Quiet` actually quiet, for child processes too.

WHY THIS FILE EXISTS (2026-10-01): `-Quiet` only hid the gate script's own progress
lines, so the log stayed ~0.23 MB. The bulk of that is libraries logging every test
request at INFO (`httpx`) plus ResourceWarnings raised by a *passing* suite. Those
child processes are plain `python -m unittest ...`, so the only way to reach them from
PowerShell without a wrapper module is the interpreter's own startup hook.

HOW: CPython imports a module named `sitecustomize` at startup if it is importable.
`check-windows.ps1` puts the repo root on `PYTHONPATH` and sets
`ECHO_GATE_QUIET_LIBS=1` only under `-Quiet`, so:

  * nothing changes for a normal `python -m unittest` run (no env var, no-op);
  * nothing changes for ECHO itself (it never sets `ECHO_GATE_QUIET_LIBS`).

WHAT IT MUST NOT DO: swallow test results or tracebacks. unittest writes those to
stderr directly, not through `logging`, so muting loggers cannot hide a failure --
it only drops per-request chatter that no verdict depends on.

ASCII-only, and deliberately tiny: this runs on *every* interpreter start in the gate.
"""

import os

if os.environ.get("ECHO_GATE_QUIET_LIBS"):
    import logging

    for _name in ("httpx", "httpcore", "urllib3", "asyncio", "anyio"):
        logging.getLogger(_name).setLevel(logging.WARNING)

    # ResourceWarnings from an otherwise-green suite ("unclosed socket", "unclosed file")
    # are the other half of the gate log. They are not failures here -- the suite tolerates
    # them by design -- so under -Quiet they are noise. ECHO_GATE_KEEP_WARNINGS is the way
    # back when someone is deliberately hunting a leaked handle, and the tests that *assert*
    # on warnings install their own filters, so they are unaffected either way.
    if not os.environ.get("ECHO_GATE_KEEP_WARNINGS"):
        import warnings

        warnings.simplefilter("ignore", ResourceWarning)
