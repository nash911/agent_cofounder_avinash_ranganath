"""Harness logging. **stderr only** -- stdout belongs to the Pi event stream.

Colour is opt-in through ``HARNESS_COLOR=1``, which the runner sets only when
its own stderr is a TTY and ``NO_COLOR`` is unset. An optional file sink lets
the harness keep a copy under ``artifacts/runs/<id>/harness/harness.log``.
"""

from __future__ import annotations

import os
import pathlib
import sys
import threading
import time
from typing import Any, Optional, TextIO

_LOCK = threading.Lock()
_FILE_SINK: Optional[TextIO] = None

_RESET = "\033[0m"
_ROLE_COLORS = {
    "harness": "36",
    "builder": "35",
    "pi": "33",
    "session": "34",
    "usage": "32",
    "ok": "32",
    "warn": "33",
    "error": "31",
}
_DEFAULT_COLOR = "36"


def color_enabled() -> bool:
    """Colour is on only when the runner explicitly asked for it."""
    return os.environ.get("HARNESS_COLOR") == "1"


def set_file_sink(path: Any) -> None:
    """Tee subsequent log lines into ``path`` as well as stderr."""
    global _FILE_SINK
    close_file_sink()
    target = pathlib.Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _FILE_SINK = target.open("a", encoding="utf-8")
    except OSError:
        _FILE_SINK = None


def close_file_sink() -> None:
    global _FILE_SINK
    sink = _FILE_SINK
    _FILE_SINK = None
    if sink is not None:
        try:
            sink.close()
        except OSError:
            pass


def log(role: str, text: str) -> None:
    """Write one ``<role> · <text>`` line to stderr (and the file sink)."""
    plain = "{0} · {1}".format(role, text)
    if color_enabled():
        code = _ROLE_COLORS.get(role, _DEFAULT_COLOR)
        pretty = "\033[{0}m{1}{2} · {3}".format(code, role, _RESET, text)
    else:
        pretty = plain
    stamp = time.strftime("%H:%M:%S", time.gmtime())
    with _LOCK:
        try:
            sys.stderr.write(pretty + "\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
        sink = _FILE_SINK
        if sink is not None:
            try:
                sink.write("{0} {1}\n".format(stamp, plain))
                sink.flush()
            except (OSError, ValueError):
                pass


def warn(text: str) -> None:
    log("warn", text)


def error(text: str) -> None:
    log("error", text)
