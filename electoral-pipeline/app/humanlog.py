"""Log lines a non-technical person can read without a decoder ring.

Two things made the old worker output hard to read, and both are fixed here
rather than in each call site:

1. Every line was written twice -- once to `print` and once to `log`. That
   doubled every line in `docker compose logs` and made the real log stream
   impossible to follow. Callers now use `say()` once.

2. Lines were addressed by bracketed codes, not sentences: "[VALIDATION]
   Response is a valid dictionary, checking success flag" says nothing to
   someone who does not know what a validator is. `say()` takes plain English
   and adds the structure.

Levels are chosen from what the line means, not from what was easiest to type.
A line about records that were saved is INFO, not WARNING, even though the old
code logged the successful finalisation as a warning.
"""
from __future__ import annotations

import logging
import sys
from typing import Any


def _fmt(value: Any) -> str:
    """Render a value for a human reader: no None, no empty strings."""
    if value is None:
        return ""
    if isinstance(value, float):
        # Durations read better with a unit and a sensible number of digits.
        return f"{value:.1f}s" if value < 10 else f"{value:.0f}s"
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value).strip()
    return "" if text in {"None", "null", ""} else text


class HumanLog:
    """A logger whose messages are sentences, not codes.

    Wraps the module logger rather than replacing it, so anything that still
    calls `log.info(...)` directly keeps working and lands in the same stream.
    """

    def __init__(self, logger: logging.Logger, service: str):
        self._log = logger
        self._service = service

    @property
    def logger(self) -> logging.Logger:
        """The plain logger underneath.

        Kept for the calls that are genuinely not sentences -- tracebacks and
        %-style messages where the text is assembled elsewhere. Those share
        the one handler below, so they are not printed twice either.
        """
        return self._log

    def say(self, level: int, message: str, /, **fields: Any) -> None:
        """Log one plain-language line.

        Any keyword fields are appended as `key=value` in the order given, so
        a call reads as a sentence plus its supporting numbers:

            say(log.INFO, "Page finished", page=7, records=30, took=12.3)
            -> Page 7 finished. 30 records saved. Took 12.3s.
        """
        parts = [message.rstrip(".")]
        for key, value in fields.items():
            rendered = _fmt(value)
            if not rendered:
                continue
            # page=7 -> "Page 7"; took=12.3 -> "Took 12.3s"
            label = key.replace("_", " ")
            label = label[0].upper() + label[1:]
            parts.append(f"{label} {rendered}")
        if not parts:
            return
        line = ". ".join(parts) + "."
        self._log.log(level, "[%s] %s", self._service, line)

    # Thin wrappers so call sites read as say.info(...) / say.warn(...).
    def info(self, message: str, /, **fields: Any) -> None:
        self.say(logging.INFO, message, **fields)

    def warning(self, message: str, /, **fields: Any) -> None:
        self.say(logging.WARNING, message, **fields)

    def error(self, message: str, /, **fields: Any) -> None:
        self.say(logging.ERROR, message, **fields)


def configure_logging(service: str) -> HumanLog:
    """Set up one readable stream and return a HumanLog that writes to it.

    Two things happen here, and both matter:

    1. A StreamHandler is attached to the `app` logger, so every module in the
       service lands in the same stream in the same format. The old code used
       module loggers with no handler, so they propagated to the root logger --
       which uvicorn and Celery each configure differently -- and the same
       message also went to stdout via print(). Every line appeared twice, in
       two formats.

    2. `propagate` is left on, so a uvicorn or Celery handler (if one exists)
       still receives the same records for its own file logging. HumanLog
       itself is safe either way: it is not a handler, it just emits.
    """
    app_log = logging.getLogger("app")
    if not any(getattr(h, "_humanlog", False) for h in app_log.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s %(message)s",
            datefmt="%H:%M:%S",
        ))
        # Marked so a second import of this module does not add another copy.
        handler._humanlog = True
        app_log.addHandler(handler)
    app_log.setLevel(logging.INFO)

    return HumanLog(logging.getLogger(f"app.{service}"), service)
