"""Helpers for consistent JSON responses across FastAPI routers."""

from __future__ import annotations

import json
from typing import Any

from fastapi import Query
from fastapi.responses import Response
from pydantic import BaseModel


def pretty_response_flag(
    pretty: bool = Query(
        default=False,
        description="Return pretty-printed JSON (indentation and new lines).",
    ),
) -> bool:
    """Expose a shared query parameter for toggling pretty JSON responses."""

    return pretty


def json_response(content: Any, pretty: bool, status_code: int = 200) -> Response:
    """Serialize ``content`` to JSON with optional pretty formatting."""

    if isinstance(content, BaseModel):
        return Response(
            content=content.model_dump_json(exclude_none=True, indent=2 if pretty else None),
            status_code=status_code,
            media_type="application/json",
        )
    else:
        payload = content

    json_kwargs: dict[str, Any] = {"ensure_ascii": False}
    if pretty:
        json_kwargs["indent"] = 2
    else:
        json_kwargs["separators"] = (",", ":")

    body = json.dumps(payload, **json_kwargs)
    return Response(content=body, status_code=status_code, media_type="application/json")
