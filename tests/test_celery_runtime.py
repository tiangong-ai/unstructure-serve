import pytest


def test_both_apps_use_the_same_redis_delivery_and_result_budget(monkeypatch):
    from src.services.celery_runtime import runtime_options
    from celery import Celery

    monkeypatch.setenv("CELERY_VISIBILITY_TIMEOUT", "21600")
    for name in ("ordinary", "two-stage"):
        app = Celery(name, broker="redis://localhost/0")
        app.conf.update(runtime_options(app.conf.broker_url, 86400))
        assert app.conf.broker_transport_options["visibility_timeout"] == 21600
        assert app.conf.result_backend_transport_options["visibility_timeout"] == 21600
        assert app.conf.visibility_timeout == 21600
        assert app.conf.result_expires == 86400
        assert app.conf.broker_transport_options["queue_order_strategy"] == "priority"


@pytest.mark.parametrize("value", ["0", "-1", "oops"])
def test_invalid_redis_budget_fails_at_startup(monkeypatch, value):
    from src.services.celery_runtime import runtime_options

    monkeypatch.setenv("CELERY_VISIBILITY_TIMEOUT", value)
    with pytest.raises(ValueError, match="CELERY_VISIBILITY_TIMEOUT"):
        runtime_options("redis://localhost/0", 86400)


def test_non_redis_broker_does_not_receive_redis_options():
    from src.services.celery_runtime import runtime_options

    assert runtime_options("amqp://localhost", 86400) == {"result_expires": 86400}
