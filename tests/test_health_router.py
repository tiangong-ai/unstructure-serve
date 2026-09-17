def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_health_endpoint_pretty_json(client):
    response = client.get("/health", params={"pretty": "true"})
    assert response.status_code == 200
    # FastAPI TestClient automatically parses JSON regardless of formatting, so reuse assertion
    assert response.json() == {"status": "healthy"}
    # Ensure pretty flag adds indentation to raw text payload
    assert "\n  " in response.text


def test_ready_reports_model_outage_without_changing_liveness(client, monkeypatch):
    import httpx
    from src.routers import health_router

    async def unavailable(self, url, **kwargs):
        raise httpx.ConnectError("private-model-host:secret")

    monkeypatch.setattr(health_router.httpx.AsyncClient, "get", unavailable)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "mineru_vlm": "unavailable"}
    assert "private-model-host" not in response.text
    assert client.get("/health").status_code == 200


def test_ready_checks_all_pool_endpoints_and_uses_auth(client, monkeypatch):
    import httpx
    from src.routers import health_router

    monkeypatch.delenv("MINERU_MODEL_VLM_SERVER_URL", raising=False)
    monkeypatch.setenv("MINERU_VLLM_SERVER_URLS", "http://model-a:30000,http://model-b:30000/v1")
    monkeypatch.setenv("MINERU_MODEL_VLM_API_KEY", "probe-secret")
    calls = []
    statuses = {"model-a": 200, "model-b": 503}

    async def get(self, url, **kwargs):
        request = httpx.Request("GET", url)
        assert kwargs["headers"]["Authorization"] == "Bearer probe-secret"
        assert request.url.path == "/health"
        calls.append(request.url.host)
        return httpx.Response(statuses[request.url.host], request=request)

    monkeypatch.setattr(health_router.httpx.AsyncClient, "get", get)
    assert client.get("/ready").status_code == 503
    assert set(calls) == {"model-a", "model-b"}
    statuses["model-b"] = 200
    response = client.get("/ready", params={"pretty": "true"})
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "mineru_vlm": "healthy"}


def test_ready_respects_sdk_config_fallback(client, monkeypatch):
    import httpx
    from mineru.config import config
    from src.routers import health_router
    from src.services.mineru_service_full import _SERVER_URL_ENV_KEYS

    for key in (
        *_SERVER_URL_ENV_KEYS,
        "MINERU_MODEL_VLM_API_KEY",
        "MINERU_VLLM_API_KEY",
        "MINERU_VLLM_AUTH_HEADER",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config.model.vlm, "server_url", "http://configured-model:30000/v1")
    monkeypatch.setattr(config.model.vlm, "api_key", "configured-key")

    async def get(self, url, **kwargs):
        assert url == "http://configured-model:30000/health"
        assert kwargs["headers"] == {"Authorization": "Bearer configured-key"}
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(health_router.httpx.AsyncClient, "get", get)
    assert client.get("/ready").status_code == 200
