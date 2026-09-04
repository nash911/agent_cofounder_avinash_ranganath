"""Harness logging. **stderr only** -- stdout belongs to the Pi event stream.

Colour is opt-in through ``HARNESS_COLOR=1``, which the runner sets only when
its own stderr is a TTY and ``NO_COLOR`` is unset. An optional file sink lets
the harness keep a copy under ``artifacts/runs/<id>/harness/harness.log``.

Two surfaces, one writer: :func:`log` is the technical line (``role · text``),
:func:`narrate` is the plain-English progress line a non-technical viewer of a
demo recording is meant to read. Both go through :func:`_emit`, so they share
the one lock, the one stderr stream and the one file sink -- and neither can
ever reach stdout.
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

#: The narration's marker and colour. Deliberately *not* a role in
#: ``_ROLE_COLORS``: a narration line carries no ``role ·`` prefix at all, so it
#: reads as prose next to the technical lines instead of as one more of them.
_NARRATE_MARKER = "▶ "
_NARRATE_COLOR = "1;36"

#: ``HARNESS_NARRATE=0`` silences the narration (default on). It exists for a
#: run whose stderr is being read by a person or a tool that only wants the
#: technical lines; it changes nothing else about the run.
NARRATE_ENV = "HARNESS_NARRATE"


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


def _emit(pretty: str, plain: str) -> None:
    """The one writer: stderr under the lock, then the file sink. Never stdout."""
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


def log(role: str, text: str) -> None:
    """Write one ``<role> · <text>`` line to stderr (and the file sink)."""
    plain = "{0} · {1}".format(role, text)
    if color_enabled():
        code = _ROLE_COLORS.get(role, _DEFAULT_COLOR)
        pretty = "\033[{0}m{1}{2} · {3}".format(code, role, _RESET, text)
    else:
        pretty = plain
    _emit(pretty, plain)


def narration_enabled() -> bool:
    """Whether :func:`narrate` writes anything; ``HARNESS_NARRATE=0`` turns it off."""
    return (os.environ.get(NARRATE_ENV) or "").strip() != "0"


def narrate(text: str) -> None:
    """One plain-English progress line for whoever is watching the terminal.

    The demo screen recording shows this process's stderr, and the technical
    lines are unreadable to the audience the demo is for. So each stage
    boundary also says, in one marked line and no jargon, what the run is
    doing. It is additive: nothing here replaces or reorders a :func:`log`
    line, and like every other line in this module it goes to stderr only --
    stdout is the Pi event stream and must stay byte-exact.
    """
    if not narration_enabled():
        return
    plain = _NARRATE_MARKER + text
    pretty = "\033[{0}m{1}{2}".format(_NARRATE_COLOR, plain, _RESET) if color_enabled() else plain
    _emit(pretty, plain)


def warn(text: str) -> None:
    log("warn", text)


def error(text: str) -> None:
    log("error", text)
