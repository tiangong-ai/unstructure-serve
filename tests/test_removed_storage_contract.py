import json

import pytest

from src.models.models import ResponseWithPageNum


def test_openapi_has_no_removed_storage_api_or_fields(client):
    schema = client.get("/openapi.json").json()
    assert "minio" not in json.dumps(schema).lower()


def test_response_contains_only_document_fields():
    assert ResponseWithPageNum(result=[]).model_dump() == {"result": [], "txt": None}


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/minio/upload"),
        ("get", "/minio/download"),
    ],
)
def test_removed_storage_endpoints_are_not_registered(client, method, path):
    assert getattr(client, method)(path).status_code == 404
