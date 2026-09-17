import os
from enum import Enum
from typing import Optional


class MinerUTier(str, Enum):
    FLASH = "flash"
    BASIC = "basic"
    STANDARD = "standard"
    ADVANCED = "advanced"


# Legacy task payloads remain valid during a rolling upgrade.
SUPPORTED_MINERU_TIERS = {tier.value for tier in MinerUTier}
SUPPORTED_MINERU_BACKENDS = {
    "pipeline",
    "vlm-transformers",
    "vlm-vllm-engine",
    "vlm-lmdeploy-engine",
    "vlm-http-client",
    "vlm-mlx-engine",
    "hybrid-auto-engine",
    "hybrid-http-client",
} | SUPPORTED_MINERU_TIERS

# Preserve legacy task payload names; resolve_tier maps them at the SDK boundary.
BACKEND_FALLBACKS: dict[str, str] = {}


def normalize_backend(backend: Optional[str]) -> Optional[str]:
    """Normalize and validate a MinerU backend string.

    Returns the normalized backend (lowercased) or None when empty.
    Raises ValueError for unsupported values.
    """
    if backend is None:
        return None

    candidate = backend.strip()
    if not candidate:
        return None

    candidate = candidate.lower()
    if candidate not in SUPPORTED_MINERU_BACKENDS:
        supported = ", ".join(sorted(SUPPORTED_MINERU_BACKENDS))
        raise ValueError(f"Unsupported MinerU backend '{backend}'. Supported values: {supported}")

    return candidate


def resolve_backend(normalized_backend: Optional[str]) -> Optional[str]:
    """Keep the backend name carried in existing task payloads."""
    if normalized_backend is None:
        return None
    return BACKEND_FALLBACKS.get(normalized_backend, normalized_backend)


def resolve_backend_from_env() -> Optional[str]:
    """Snapshot the configured tier into existing task payloads."""
    tier = os.getenv("MINERU_DEFAULT_TIER", "").strip().lower()
    if tier:
        return normalize_tier(tier)
    raw = os.getenv("MINERU_DEFAULT_BACKEND")
    normalized = normalize_backend(raw)
    return resolve_backend(normalized)


def normalize_tier(tier: str) -> str:
    candidate = tier.strip().lower()
    if candidate not in SUPPORTED_MINERU_TIERS:
        raise ValueError(
            f"Unsupported MinerU tier '{tier}'. Expected flash/basic/standard/advanced."
        )
    return candidate


def resolve_tier(backend: Optional[str] = None, tier: Optional[str] = None) -> str:
    """Translate old quality choices without silently downgrading VLM tasks."""
    if tier is not None:
        return normalize_tier(tier)
    choice = normalize_backend(backend) or resolve_backend_from_env() or "standard"
    if choice in SUPPORTED_MINERU_TIERS:
        return choice
    if choice == "pipeline":
        return "basic"
    if choice.startswith("vlm-"):
        return "advanced"
    return "standard"
