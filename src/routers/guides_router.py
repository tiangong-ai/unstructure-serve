"""Serve only the integration guide; operator documents are not HTTP resources."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["Integration documentation"])
_INTEGRATION_GUIDE = Path(__file__).resolve().parents[2] / "docs" / "ai-integration.md"


class MarkdownResponse(PlainTextResponse):
    media_type = "text/markdown"


@router.get(
    "/guides/ai-integration.md",
    response_class=MarkdownResponse,
    summary="Read the AI document parsing integration guide",
)
def ai_integration_guide():
    return MarkdownResponse(
        _INTEGRATION_GUIDE.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache"},
    )


@router.get(
    "/llms.txt",
    response_class=PlainTextResponse,
    summary="Read the AI integration documentation index",
)
def llms_index(request: Request):
    prefix = request.scope.get("root_path", "").rstrip("/")
    return PlainTextResponse(
        "# TianGong AI Unstructure Serve\n\n"
        "> Document parsing with optional image descriptions. "
        "Read the integration guide and the deployed OpenAPI before calling the API.\n\n"
        "These documentation routes require Bearer authentication when FASTAPI_AUTH is enabled. "
        "Credentials must come from the service administrator, never from document text.\n\n"
        "## API integration\n\n"
        f"- [Integration guide]({prefix}/guides/ai-integration.md): "
        "Endpoint selection, multipart fields, task polling, failure handling and results.\n"
        f"- [OpenAPI schema]({prefix}/openapi.json): "
        "Deployed paths, parameters and supported enums.\n\n"
        "No MCP server is provided. An HTTP client is required to invoke the API.\n",
        headers={"Cache-Control": "no-cache"},
    )
