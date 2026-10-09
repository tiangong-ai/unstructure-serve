import pytest

import src.services.vision_service as vision
from src.utils.text_output import UnusableVisionOutput


def test_vision_completion_invalid_model_falls_back_to_env_model(monkeypatch):
    provider = next(iter(vision.VisionProvider))
    env_model = "env-model"
    captured: dict[str, object] = {}

    def fake_call(
        image_path: str,
        context: str,
        model: str | None,
        prompt: str | None,
        output_validator,
    ) -> str:
        captured["image_path"] = image_path
        captured["context"] = context
        captured["model"] = model
        captured["prompt"] = prompt
        output_validator("ok")
        return "ok"

    monkeypatch.setenv("VISION_PROVIDER", provider.value)
    monkeypatch.setenv("VISION_MODEL", env_model)
    monkeypatch.setitem(
        vision.PROVIDER_SPECS,
        provider.value,
        vision.ProviderSpec(
            key=provider.value,
            models=[env_model],
            default_model=env_model,
            call=fake_call,
            has_credentials=lambda: True,
        ),
    )
    monkeypatch.setattr(vision, "DEFAULT_MODELS", {provider: env_model})
    monkeypatch.setattr(vision, "MODEL_PROVIDER_LOOKUP", {env_model: provider})

    result = vision.vision_completion(
        "fake.jpg",
        context="ctx",
        prompt="prompt",
        provider=provider.value,
        model="missing-model",
    )

    assert result == "ok"
    assert captured == {
        "image_path": "fake.jpg",
        "context": "ctx",
        "model": env_model,
        "prompt": "prompt",
    }


def test_strict_ocr_skips_normal_output_validation(monkeypatch):
    provider = next(iter(vision.VisionProvider))
    literal = "Image Description: [Page 1]"

    def fake_call(image_path, context, model, prompt, output_validator):
        assert output_validator is None
        return literal

    monkeypatch.setitem(
        vision.PROVIDER_SPECS,
        provider.value,
        vision.ProviderSpec(
            key=provider.value,
            models=["literal-ocr"],
            default_model="literal-ocr",
            call=fake_call,
            has_credentials=lambda: True,
        ),
    )
    assert (
        vision.vision_completion("image.jpg", provider=provider, validate_output=False) == literal
    )


@pytest.mark.parametrize("mixed_failure", [False, True])
def test_only_content_failures_can_become_unrecognized_marker(monkeypatch, mixed_failure):
    providers = list(vision.VisionProvider)
    primary = providers[0]
    calls = []

    for provider in providers:

        def call(image_path, context, model, prompt, output_validator, *, key=provider.value):
            calls.append(key)
            if mixed_failure and key == providers[-1].value:
                raise TimeoutError("vision endpoint unavailable")
            raise UnusableVisionOutput("no usable facts")

        monkeypatch.setitem(
            vision.PROVIDER_SPECS,
            provider.value,
            vision.ProviderSpec(
                key=provider.value,
                models=["fixture-model"],
                default_model="fixture-model",
                call=call,
                has_credentials=lambda: True,
            ),
        )

    error = RuntimeError if mixed_failure else UnusableVisionOutput
    with pytest.raises(error):
        vision.vision_completion("image.jpg", provider=primary)
    assert calls == [provider.value for provider in providers]


@pytest.mark.parametrize("configured", [False, True])
def test_upstream_failure_is_distinct_from_missing_configuration(monkeypatch, configured):
    providers = list(vision.VisionProvider)
    messages = []
    private = "private-token-and-document-body"
    monkeypatch.setattr(
        vision.logger, "info", lambda text, *args: messages.append(text.format(*args))
    )

    def failed_call(*args):
        try:
            raise TimeoutError(private)
        except TimeoutError as exc:
            raise RuntimeError(private) from exc

    for provider in providers:
        monkeypatch.setitem(
            vision.PROVIDER_SPECS,
            provider.value,
            vision.ProviderSpec(
                key=provider.value,
                models=["fixture-model"],
                default_model="fixture-model",
                call=failed_call,
                has_credentials=lambda: configured,
            ),
        )
    with pytest.raises(RuntimeError) as raised:
        vision.vision_completion("image.jpg", provider=providers[0])
    message = str(raised.value)
    if configured:
        assert "TimeoutError" in message
        assert "configuration and API keys" not in message
        assert raised.value.__suppress_context__ is True
    else:
        assert "configuration and API keys" in message
        assert "TimeoutError" not in message
    assert private not in message
    assert private not in "\n".join(messages)
