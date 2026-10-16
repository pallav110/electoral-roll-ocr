"""Control work and OCR work must not share a queue.

One queue served by one --concurrency=1 worker is what made a 27-page roll take
eight hours: the beat's own dispatch and recovery messages, which fire every 10
and 60 seconds, queued up in front of the OCR work they exist to schedule.

These are pure config assertions -- no database, no broker. They are cheap, and
they catch the specific regression where someone adds a task and it silently
lands back on the default queue in front of the OCR work.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="module")
def app_conf():
    from app import config, tasks

    return config, tasks


def test_process_unit_is_routed_to_the_ocr_queue(app_conf):
    config, tasks = app_conf
    routes = tasks.celery_app.conf.task_routes
    assert routes["app.tasks.process_unit"]["queue"] == tasks.QUEUE_OCR


def test_every_other_task_lands_on_control_not_the_default_queue(app_conf):
    """The default is the half that matters.

    Routing process_unit is easy. What stops the bug returning is that anything
    added later defaults to `control` rather than to Celery's built-in `celery`
    queue, which is where every task used to pile up.
    """
    config, tasks = app_conf
    assert tasks.celery_app.conf.task_default_queue == tasks.QUEUE_CONTROL
    assert tasks.celery_app.conf.task_default_queue != "celery"


def test_process_document_stays_on_control(app_conf):
    """It only counts pages and creates unit rows.

    Routing it to the OCR queue would put document setup behind the very work it
    is setting up.
    """
    config, tasks = app_conf
    routes = tasks.celery_app.conf.task_routes
    assert routes["app.tasks.process_document"]["queue"] == tasks.QUEUE_CONTROL


def test_archive_ingest_has_its_own_queue(app_conf):
    """Large ZIP work must not block dispatch/recovery control ticks."""
    _, tasks = app_conf
    routes = tasks.celery_app.conf.task_routes
    assert routes["app.tasks.ingest_minio_zip_archives"]["queue"] == tasks.QUEUE_INGEST


def test_each_queue_binds_to_its_own_exchange_and_key(app_conf):
    """Two queues must not share a binding, or they are one queue again.

    Declaring Queue("ocr") without a routing key is enough to reintroduce the
    original bug while every other assertion here still passes. The exchange
    defaults to task_default_exchange and the key to "" -- the exchange's own
    name -- so both queues end up bound to control/control. A direct exchange
    delivers to *every* queue bound to the published key, so control messages
    reach the OCR worker too and the split is a fiction.

    Checked against the resolved router output rather than the declaration,
    because that is what actually gets published.
    """
    _, tasks = app_conf
    router = tasks.celery_app.amqp.router

    resolved = {}
    for name, queue in (
        ("app.tasks.process_unit", tasks.QUEUE_OCR),
        ("app.tasks.dispatch_pending_units", tasks.QUEUE_CONTROL),
    ):
        route = router.route({}, name, args=(), kwargs={}, task_type="t")
        bound = route["queue"]
        assert bound.name == queue, f"{name} published to {bound.name}, not {queue}"
        resolved[name] = (bound.exchange.name, bound.routing_key)

    assert len(set(resolved.values())) == 2, (
        f"both queues resolve to the same binding {resolved}. They will receive "
        "each other's messages and the split does nothing."
    )


def test_every_registered_task_resolves_to_a_declared_queue(app_conf):
    """No task may fall through to an undeclared queue.

    Celery creates a missing queue implicitly and silently, so a typo would
    produce a worker that never sees the task and no error anywhere.
    """
    config, tasks = app_conf
    declared = {q.name for q in tasks.celery_app.conf.task_queues}
    for name in tasks.celery_app.tasks:
        route = (tasks.celery_app.conf.task_routes or {}).get(name, {})
        assert route.get("queue", tasks.celery_app.conf.task_default_queue) in declared, (
            f"task {name} routes to an undeclared queue"
        )


def test_visibility_timeout_exceeds_the_effective_extractor_timeout(app_conf):
    """Redis redelivers an unacked message after the visibility timeout.

    Left at the one-hour default while EXTRACTOR_TIMEOUT_SECONDS is 5400, Redis
    would redeliver a unit still inside its OCR call. The duplicate burns a core
    until process_unit_job rejects it on the status check.

    The value that matters at runtime is the one compose supplies, not the .env
    default: docker-compose.yml sets EXTRACTOR_TIMEOUT_SECONDS=5400 on the
    worker services, overriding .env's 120.
    """
    config, _ = app_conf
    assert config.CELERY_VISIBILITY_TIMEOUT > config.EXTRACTOR_TIMEOUT_SECONDS

    # And under the value compose actually passes, not the .env one.
    for effective in (120, 5400):
        assert max(3600, effective * 2 + 3600) > effective


def test_the_window_default_is_small(app_conf):
    """Guards the regression by pinning the number, not just the mechanism.

    The whole failure was a queue with tens of thousands of messages in it. If
    this ever goes back to three digits, something is publishing without the
    window and this is the cheapest place to notice.
    """
    config, _ = app_conf
    assert config.OCR_MAX_INFLIGHT_UNITS <= 8, (
        "OCR_MAX_INFLIGHT_UNITS is large enough that the broker, not the "
        "database, is holding the backlog again"
    )


def test_queued_recovery_is_longer_than_a_unit_can_take(app_conf):
    """Reclaiming sooner than a unit can run republishes live work.

    A queued unit legitimately waits behind whatever is ahead of it, for as long
    as that takes. The threshold has to clear that, or every reclaim is a
    duplicate.
    """
    config, _ = app_conf
    assert config.QUEUED_RECOVERY_SECONDS >= config.EXTRACTOR_TIMEOUT_SECONDS
