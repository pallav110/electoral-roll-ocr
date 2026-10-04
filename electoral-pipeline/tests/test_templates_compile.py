"""Do all the templates still compile?

This exists because of a real break: making the session counter's script block
unconditional meant deleting an `{% if %}` and forgetting its `{% endif %}`.
Jinja compiles templates lazily, so nothing failed until someone opened that
one page -- and then it was a 500 with a stack trace in the container log
rather than a test failure.

Templates are baked into the image (`COPY app ./app`), so a syntax error ships
invisible until the page is requested. Compiling all of them here turns that
into a local `pytest` failure in under a second.

Also checks that every global the templates call is actually registered, since
an unregistered name renders as empty in Jinja rather than raising -- the same
silent-blank-cell class of bug as the missing duration column.
"""
import os
import re

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from jinja2 import meta as jinja_meta
from jinja2.exceptions import TemplateSyntaxError

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "templates")


def _templates():
    if not os.path.isdir(TEMPLATE_DIR):
        pytest.skip(f"no template dir at {TEMPLATE_DIR}")
    return sorted(f for f in os.listdir(TEMPLATE_DIR) if f.endswith(".html"))


@pytest.mark.parametrize("name", _templates())
def test_the_template_compiles(name):
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    try:
        env.get_template(name)
    except TemplateSyntaxError as exc:
        pytest.fail(f"{name} does not compile: {exc}")


@pytest.mark.parametrize("name", _templates())
def test_it_balances_its_block_tags(name):
    """An unclosed {% if %} is the specific break that bit above.

    Jinja reports it at compile time too, but the message names the tag it
    wanted rather than the one you forgot, so a direct count is the faster
    read when a page 500s.
    """
    with open(os.path.join(TEMPLATE_DIR, name), encoding="utf-8") as fh:
        source = fh.read()

    # Comments first, so a tag mentioned in prose is not counted.
    source = re.sub(r"(?s)\{#.*?#\}", "", source)

    openers = len(re.findall(r"\{%-?\s*(if|for|block|with|macro|call|filter)\b", source))
    closers = len(re.findall(r"\{%-?\s*end(if|for|block|with|macro|call|filter)\b", source))
    # `else`/`elif` sit inside an `if` and do not close it.
    assert openers == closers, (
        f"{name}: {openers} opening block tags but {closers} end tags"
    )


def test_the_session_counter_helpers_are_registered():
    """Every helper session.html calls must be a real Jinja global.

    An unregistered name is Undefined, which is falsy and renders as an empty
    string rather than raising. That is exactly how the blank "How long" column
    happened in the first place, and it looks identical to an empty database.
    """
    from app.main import templates

    required = [
        "live_progress", "eta_summary", "duration_between", "event_detail",
        "status_label", "records_summary", "duration_summary", "friendly_error",
        "describe_error", "short_time", "progress_summary",
    ]
    missing = [name for name in required if name not in templates.env.globals]
    assert not missing, f"registered as globals nowhere: {missing}"


def test_the_session_template_uses_no_removed_helpers():
    """Guard the two calls that were wrong in the first place.

    duration_summary(u.processing_time_ms) silently rendered a dash for every
    unit, because processing_time_ms is a session column and the unit class
    starts below it. If that expression comes back, the blank column comes
    back with it.
    """
    with open(os.path.join(TEMPLATE_DIR, "session.html"), encoding="utf-8") as fh:
        source = fh.read()

    assert "u.processing_time_ms" not in source, (
        "ExtractionUnit has no processing_time_ms column; "
        "use duration_between(u.started_at, u.completed_at)"
    )
    assert "unit.processing_time_ms" not in source, (
        "same, via the unit.html spelling"
    )


@pytest.mark.parametrize("name", ["session.html", "unit.html", "document.html", "dashboard.html"])
def test_history_tables_read_details_not_just_message(name):
    """message is NULL for 8 of 10 event types, so reading only it gave dashes.

    Every history table now goes through event_detail(details, message).
    """
    with open(os.path.join(TEMPLATE_DIR, name), encoding="utf-8") as fh:
        source = fh.read()

    if "{% for e in" not in source:
        pytest.skip(f"{name} has no event loop")

    bare = re.findall(r"\{\{\s*e\.message\s+or\s+", source)
    assert not bare, (
        f"{name} renders e.message on its own in {len(bare)} place(s); "
        "that is the column that is NULL for most events"
    )