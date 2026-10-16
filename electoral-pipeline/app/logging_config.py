"""Human-readable pipeline logs; quiet routine Celery scheduler/task trace noise."""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime

PIPELINE_LOGGER_NAME = "electoral.pipeline"

_CONFIGURED = False


def pipeline_log() -> logging.Logger:
    return logging.getLogger(PIPELINE_LOGGER_NAME)


def pipeline_step(message: str, *args) -> None:
    """Print and log one pipeline step (flush immediately for docker compose logs -f)."""
    text = (message % args) if args else message
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} | {text}"
    print(line, flush=True, file=sys.stdout)


def pipeline_tag(prefix: str, message: str, *args) -> None:
    """Structured pipeline log with a fixed prefix (same flush behavior as pipeline_step)."""
    text = (message % args) if args else message
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} | [{prefix.upper()}] {text}"
    print(line, flush=True, file=sys.stdout)


def configure_pipeline_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    level_name = os.getenv("PIPELINE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    show_scheduler = os.getenv("PIPELINE_LOG_SCHEDULER", "").lower() in ("1", "true", "yes")

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    pipeline = pipeline_log()
    pipeline.setLevel(level)
    pipeline.propagate = True

    celery_level = logging.DEBUG if show_scheduler else logging.WARNING
    for name in (
        "celery",
        "celery.app.trace",
        "celery.worker.strategy",
        "celery.beat",
        "celery.worker",
        "kombu",
    ):
        logging.getLogger(name).setLevel(celery_level)


def short_id(value) -> str:
    return str(value).split("-", 1)[0]
