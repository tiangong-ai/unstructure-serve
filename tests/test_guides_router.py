from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routers.guides_router import router


def test_integration_guide_is_the_versioned_markdown(client):
    response = client.get("/guides/ai-integration.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    source = Path(__file__).parents[1] / "docs" / "ai-integration.md"
    assert response.text == source.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "path",
    ["/guides/performance-tuning.md", "/docs/performance-tuning.md", "/guides/AGENTS.md"],
)
def test_operator_documents_are_not_served(client, path):
    assert client.get(path).status_code == 404


def test_index_only_links_integration_resources_and_keeps_proxy_prefix():
    app = FastAPI(root_path="/parser")
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/llms.txt")
    assert response.status_code == 200
    assert "(/parser/guides/ai-integration.md)" in response.text
    assert "(/parser/openapi.json)" in response.text
    assert "performance-tuning" not in response.text
    assert "AGENTS.md" not in response.text
