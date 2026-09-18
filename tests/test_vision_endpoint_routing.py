from types import SimpleNamespace

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, BadRequestError
import pytest

from src.services import vision_service_openai_compatible as compatible
from src.services import vision_service_vllm as vllm


@pytest.fixture(autouse=True)
def private_capacity(tmp_path, monkeypatch):
    monkeypatch.setenv("VLLM_VISION_SLOT_DIR", str(tmp_path / "slots"))
    monkeypatch.setenv("VLLM_VISION_ENDPOINT_SLOTS", "1")
    monkeypatch.setenv("VLLM_VISION_SLOT_WAIT_SECONDS", "0.1")
    monkeypatch.setenv("VLLM_VISION_COOLDOWN_SECONDS", "30")


def _pool(monkeypatch, functions):
    clients = [
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=f)))
        for f in functions
    ]
    monkeypatch.setattr(
        compatible.OpenAICompatibleClientPool, "_build_clients", staticmethod(lambda *args: clients)
    )
    pool = compatible.OpenAICompatibleClientPool(
        "unused", [f"http://test-{i}/v1" for i in range(len(clients))]
    )
    monkeypatch.setattr(vllm, "_CLIENT_POOL", pool)
    return pool


def _response():
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="52%"))]
    )


@pytest.mark.parametrize(
    "name,value",
    [
        ("VLLM_VISION_TEMPERATURE", "nan"),
        ("VLLM_VISION_TEMPERATURE", "-1"),
        ("VLLM_VISION_TOP_P", "2"),
        ("VLLM_VISION_PRESENCE_PENALTY", "inf"),
    ],
)
def test_invalid_sampling_is_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        vllm._build_request_options()


@pytest.mark.parametrize(
    "name,value",
    [
        ("VLLM_VISION_TOP_K", "0"),
        ("VLLM_VISION_MIN_P", "-0.1"),
        ("VLLM_VISION_REPETITION_PENALTY", "0"),
        ("VLLM_ENABLE_THINKING", "maybe"),
    ],
)
def test_invalid_extended_sampling_is_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        vllm._build_extra_body()


def test_failover_prepares_image_once_and_preserves_png_mime(tmp_path, monkeypatch):
    encoded = []
    received = []
    path = tmp_path / "figure.png"
    monkeypatch.setattr(compatible, "encode_image", lambda p: encoded.append(p) or "abc")

    def call(**payload):
        received.append(payload)
        if len(received) == 1:
            raise APIConnectionError(request=httpx.Request("POST", "http://test"))
        return _response()

    _pool(monkeypatch, [call, call])
    assert vllm.vision_completion_vllm(str(path)) == "52%"
    assert len(encoded) == 1
    assert len(received) == 2 and received[0] == received[1]
    assert received[0]["messages"][-1]["content"][-1]["image_url"]["url"].startswith(
        "data:image/png;"
    )


def test_bad_request_is_not_retried_on_other_endpoints(monkeypatch):
    calls = []
    monkeypatch.setattr(compatible, "encode_image", lambda _: "abc")

    def fail(**payload):
        calls.append(payload)
        raise BadRequestError(
            "invalid request",
            response=httpx.Response(400, request=httpx.Request("POST", "http://test")),
            body=None,
        )

    _pool(monkeypatch, [fail, fail])
    with pytest.raises(BadRequestError):
        vllm.vision_completion_vllm("fake.jpg")
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["connection", "timeout", 408, 429, 503])
def test_connection_failure_cools_endpoint_for_next_call(monkeypatch, failure):
    calls = []
    monkeypatch.setattr(compatible, "encode_image", lambda _: "abc")

    def flaky(**payload):
        calls.append("flaky")
        request = httpx.Request("POST", "http://test")
        if failure == "connection":
            raise APIConnectionError(request=request)
        if failure == "timeout":
            raise APITimeoutError(request=request)
        raise APIStatusError(
            "temporary failure", response=httpx.Response(failure, request=request), body=None
        )

    def healthy(**payload):
        calls.append("healthy")
        return _response()

    pool = _pool(monkeypatch, [flaky, healthy])
    # Arrange deterministic first choice independently of the URL digest order.
    monkeypatch.setattr(
        pool,
        "get_endpoint_clients",
        lambda: [("0" * 64, pool._clients[0]), ("1" * 64, pool._clients[1])],
    )
    assert vllm.vision_completion_vllm("fake.jpg") == "52%"
    assert vllm.vision_completion_vllm("fake.jpg") == "52%"
    assert calls == ["flaky", "healthy", "healthy"]
