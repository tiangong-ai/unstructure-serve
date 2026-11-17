"""Queue diagnostics count both Redis priority lists and retain legacy queues."""

from types import SimpleNamespace

from src.routers import two_stage_router


def test_queue_status_counts_shared_normal_messages(client, monkeypatch):
    class FakeRedis:
        def llen(self, name):
            return {"queue_parse_shared": 2, "queue_parse_shared\x06\x169": 5}.get(name, 0)

        def hvals(self, name):
            return [b'["body", "", "queue_parse_shared"]']

    monkeypatch.setattr(
        two_stage_router.celery_app,
        "conf",
        SimpleNamespace(
            broker_url="redis://localhost:6379/0",
            broker_transport_options={"priority_steps": [0, 9]},
        ),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "redis",
        SimpleNamespace(
            Redis=SimpleNamespace(from_url=lambda _url: FakeRedis()),
        ),
    )
    result = client.get("/two_stage/queue_status")
    assert result.status_code == 200
    payload = result.json()
    assert payload["queues"]["queue_parse_shared"] == 7
    assert payload["unacked"]["queue_parse_shared"] == 1
    assert payload["shared_parse"] == {"queue": "queue_parse_shared", "urgent": 2, "normal": 5}
    assert two_stage_router.resolve_two_stage_queues("normal")["parse"] in payload["queues"]
