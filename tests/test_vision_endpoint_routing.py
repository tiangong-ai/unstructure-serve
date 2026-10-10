from types import SimpleNamespace

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, BadRequestError
import pytest

from src.services import vision_service_openai_compatible as compatible
from src.services import vision_service_vllm as vllm
from src.utils.text_output import UnusableVisionOutput, validate_vision_output


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


def _response(content="52%"):
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))]
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


@pytest.mark.parametrize(
    "content", ["Image Description: [Page 300] [ChunkType=Image]", "<think>unfinished reasoning"]
)
def test_invalid_response_switches_endpoints_without_reencoding(monkeypatch, content):
    calls = []
    encoded = []
    monkeypatch.setattr(compatible, "encode_image", lambda path: encoded.append(path) or "abc")

    def wrapper_only(**payload):
        calls.append("wrapper")
        return _response(content)

    def useful(**payload):
        calls.append("useful")
        return _response("[图片内容：二维码]")

    pool = _pool(monkeypatch, [wrapper_only, useful])
    monkeypatch.setattr(
        pool,
        "get_endpoint_clients",
        lambda: [("0" * 64, pool._clients[0]), ("1" * 64, pool._clients[1])],
    )

    result = vllm.vision_completion_vllm("fixture.jpg", output_validator=validate_vision_output)
    assert result == "[图片内容：二维码]"
    assert calls == ["wrapper", "useful"]
    assert encoded == ["fixture.jpg"]


def test_all_wrapper_only_endpoints_still_fail(monkeypatch):
    calls = []
    monkeypatch.setattr(compatible, "encode_image", lambda _: "abc")

    def wrapper_only(**payload):
        calls.append(payload)
        return _response("<think>no answer</think>\nImage Description: [Page 1]")

    _pool(monkeypatch, [wrapper_only, wrapper_only])
    with pytest.raises(UnusableVisionOutput, match="no usable facts"):
        vllm.vision_completion_vllm("fixture.jpg", output_validator=validate_vision_output)
    assert len(calls) == 2


def test_incomplete_reasoning_is_not_classified_as_unrecognized_content(monkeypatch):
    calls = []
    monkeypatch.setattr(compatible, "encode_image", lambda _: "abc")

    def incomplete(**payload):
        calls.append(payload)
        return _response("<think>unfinished reasoning")

    _pool(monkeypatch, [incomplete, incomplete])
    with pytest.raises(RuntimeError, match="All configured vLLM vision endpoints failed"):
        vllm.vision_completion_vllm("fixture.jpg", output_validator=validate_vision_output)
    assert len(calls) == 2


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


@pytest.mark.parametrize("fault", ["empty", "truncated", "wrappers"])
def test_unusable_half_open_response_does_not_restore_parallel_traffic(monkeypatch, fault):
    from src.services.vision_capacity import EndpointScheduler

    monkeypatch.setattr(compatible, "encode_image", lambda _: "abc")
    monkeypatch.setenv("VLLM_VISION_COOLDOWN_SECONDS", "0")

    def unusable(**payload):
        response = _response()
        if fault == "empty":
            response.choices[0].message.content = ""
        elif fault == "truncated":
            response.choices[0].finish_reason = "length"
        else:
            response.choices[0].message.content = "Image Description: [Page 1]"
        return response

    pool = _pool(monkeypatch, [unusable])
    key = pool.get_endpoint_clients()[0][0]
    scheduler = EndpointScheduler.from_env()
    scheduler.mark_failed(key)
    with pytest.raises((RuntimeError, UnusableVisionOutput), match="All configured"):
        vllm.vision_completion_vllm("fixture.jpg", output_validator=validate_vision_output)
    assert scheduler.health_snapshot([key])[0]["circuit"] == "recovery"
