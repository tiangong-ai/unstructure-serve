import pytest

import src.services.vision_service_openai_compatible as openai_compatible
import src.services.vision_service_vllm as vision_vllm


@pytest.fixture(autouse=True)
def isolate_shared_vision_state(tmp_path, monkeypatch):
    monkeypatch.setenv("VLLM_VISION_SLOT_DIR", str(tmp_path / "slots"))
    monkeypatch.setattr(vision_vllm, "prepare_vision_request", lambda *args, **kwargs: {})


class _DummyMessage:
    def __init__(self, content: str):
        self.content = content


class _DummyChoice:
    def __init__(self, content: str):
        self.message = _DummyMessage(content)


class _DummyResponse:
    def __init__(self, content: str):
        self.choices = [_DummyChoice(content)]


class _DummyCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _DummyResponse("ok")


class _DummyChat:
    def __init__(self, completions: _DummyCompletions):
        self.completions = completions


class _DummyClient:
    def __init__(self, completions: _DummyCompletions):
        self.chat = _DummyChat(completions)


class _DummyPool:
    def __init__(self, client: _DummyClient):
        self._client = client

    def get_client(self):
        return self._client


def test_vllm_client_timeout_and_sdk_retry_budget_reach_each_endpoint(monkeypatch):
    kwargs_seen = []
    monkeypatch.setattr(
        openai_compatible, "OpenAI", lambda **kwargs: kwargs_seen.append(kwargs) or object()
    )
    monkeypatch.setenv("VLLM_VISION_TIMEOUT_SECONDS", "125")
    monkeypatch.setenv("VLLM_VISION_MAX_RETRIES", "0")
    pool = openai_compatible.OpenAICompatibleClientPool(
        "test-key", ["http://one/v1", "http://two/v1"], **vision_vllm._client_budgets()
    )
    assert len(pool.get_clients_in_priority_order()) == 2
    assert all(k["timeout"] == 125 and k["max_retries"] == 0 for k in kwargs_seen)


def test_duplicate_endpoint_spellings_do_not_create_extra_clients(monkeypatch):
    kwargs_seen = []
    monkeypatch.setattr(
        openai_compatible, "OpenAI", lambda **kwargs: kwargs_seen.append(kwargs) or object()
    )
    pool = openai_compatible.OpenAICompatibleClientPool(
        "test-key", ["http://example.com/v1", "HTTP://Example.COM:80/v1/"]
    )
    assert len(kwargs_seen) == len(pool.get_endpoint_clients()) == 1


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_vision_timeout_must_be_finite_and_positive(monkeypatch, value):
    monkeypatch.setenv("VLLM_VISION_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError):
        vision_vllm._client_budgets()


def test_openai_compatible_passes_extra_body(monkeypatch):
    completions = _DummyCompletions()
    pool = _DummyPool(_DummyClient(completions))
    monkeypatch.setattr(openai_compatible, "encode_image", lambda _path: "YmFzZTY0")

    result = openai_compatible.vision_completion_openai_compatible(
        "fake.jpg",
        context="ctx",
        model="Qwen/Qwen3.5-122B-A10B-FP8",
        prompt="p",
        default_model="unused",
        client_pool=pool,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        request_options={"temperature": 1.0, "top_p": 1.0},
    )

    assert result == "ok"
    assert len(completions.calls) == 1
    assert completions.calls[0]["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    assert completions.calls[0]["temperature"] == 1.0
    assert completions.calls[0]["top_p"] == 1.0


def test_openai_compatible_omits_extra_body_when_empty(monkeypatch):
    completions = _DummyCompletions()
    pool = _DummyPool(_DummyClient(completions))
    monkeypatch.setattr(openai_compatible, "encode_image", lambda _path: "YmFzZTY0")

    openai_compatible.vision_completion_openai_compatible(
        "fake.jpg",
        default_model="unused",
        client_pool=pool,
    )

    assert len(completions.calls) == 1
    assert "extra_body" not in completions.calls[0]


class _DummyVllmPool:
    def __init__(self, clients=None):
        self.clients = list(clients or [object()])

    def has_clients(self) -> bool:
        return bool(self.clients)

    def get_clients_in_priority_order(self):
        return list(self.clients)

    def get_endpoint_clients(self):
        return [(f"{index:064x}", client) for index, client in enumerate(self.clients)]


def test_vllm_vision_defaults_to_disable_thinking(monkeypatch):
    captured = {}

    monkeypatch.setattr(vision_vllm, "_CLIENT_POOL", _DummyVllmPool())

    def _fake_openai_compatible(*args, **kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(vision_vllm, "vision_completion_openai_compatible", _fake_openai_compatible)
    # Deployment .env values must not change a test of code defaults.
    for name in (
        "VLLM_ENABLE_THINKING",
        "VLLM_VISION_TEMPERATURE",
        "VLLM_VISION_TOP_P",
        "VLLM_VISION_TOP_K",
        "VLLM_VISION_MIN_P",
        "VLLM_VISION_PRESENCE_PENALTY",
        "VLLM_VISION_REPETITION_PENALTY",
    ):
        monkeypatch.delenv(name, raising=False)

    result = vision_vllm.vision_completion_vllm("fake.jpg")

    assert result == "ok"
    assert captured["request_options"] == {
        "temperature": 1.0,
        "top_p": 1.0,
        "presence_penalty": 2.0,
    }
    assert captured["extra_body"] == {
        "top_k": 40,
        "min_p": 0.0,
        "repetition_penalty": 1.0,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_vllm_vision_allows_sampling_env_override(monkeypatch):
    captured = {}

    monkeypatch.setattr(vision_vllm, "_CLIENT_POOL", _DummyVllmPool())

    def _fake_openai_compatible(*args, **kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(vision_vllm, "vision_completion_openai_compatible", _fake_openai_compatible)
    monkeypatch.setenv("VLLM_ENABLE_THINKING", "true")
    monkeypatch.setenv("VLLM_VISION_TEMPERATURE", "0.33")
    monkeypatch.setenv("VLLM_VISION_TOP_P", "0.77")
    monkeypatch.setenv("VLLM_VISION_TOP_K", "64")
    monkeypatch.setenv("VLLM_VISION_MIN_P", "0.09")
    monkeypatch.setenv("VLLM_VISION_PRESENCE_PENALTY", "1.2")
    monkeypatch.setenv("VLLM_VISION_REPETITION_PENALTY", "1.05")

    result = vision_vllm.vision_completion_vllm("fake.jpg")

    assert result == "ok"
    assert captured["request_options"] == {
        "temperature": 0.33,
        "top_p": 0.77,
        "presence_penalty": 1.2,
    }
    assert captured["extra_body"] == {
        "top_k": 64,
        "min_p": 0.09,
        "repetition_penalty": 1.05,
        "chat_template_kwargs": {"enable_thinking": True},
    }


def test_vllm_requires_base_url(monkeypatch):
    monkeypatch.setattr(vision_vllm, "_resolve_base_urls", lambda: [])
    assert vision_vllm.has_vllm_credentials() is False


def test_vllm_vision_retries_next_client(monkeypatch):
    attempts = []
    messages = []

    monkeypatch.setattr(vision_vllm, "_CLIENT_POOL", _DummyVllmPool(["first", "second"]))

    def _fake_openai_compatible(*args, **kwargs):
        attempts.append(kwargs["client_pool"].get_client())
        if len(attempts) == 1:
            raise RuntimeError("first endpoint down")
        return "ok"

    monkeypatch.setattr(vision_vllm, "vision_completion_openai_compatible", _fake_openai_compatible)

    sink = vision_vllm.logger.add(messages.append, format="{message}")
    try:
        result = vision_vllm.vision_completion_vllm("fake.jpg")
    finally:
        vision_vllm.logger.remove(sink)

    assert result == "ok"
    assert attempts == ["first", "second"]
    assert any("attempt 1/2 failed: RuntimeError" in message for message in messages)
    assert all("first endpoint down" not in message for message in messages)


def test_vllm_vision_raises_when_all_clients_fail(monkeypatch):
    monkeypatch.setattr(vision_vllm, "_CLIENT_POOL", _DummyVllmPool(["first", "second"]))

    def _fake_openai_compatible(*args, **kwargs):
        raise RuntimeError(f"{kwargs['client_pool'].get_client()} down")

    monkeypatch.setattr(vision_vllm, "vision_completion_openai_compatible", _fake_openai_compatible)

    with pytest.raises(RuntimeError, match="All configured vLLM vision endpoints failed"):
        vision_vllm.vision_completion_vllm("fake.jpg")
