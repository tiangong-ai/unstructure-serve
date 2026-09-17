from functools import lru_cache
from typing import Set

# Office extensions are accepted separately and converted by file_conversion.
_SERVICE_EXTENSIONS = {".pdf", ".png", ".jpeg", ".jpg", ".webp", ".bmp", ".tif", ".tiff"}


@lru_cache()
def mineru_supported_extensions() -> Set[str]:
    """Formats supported by the service's page-based MinerU 4 pipeline."""
    return set(_SERVICE_EXTENSIONS)


def format_supported_extensions() -> str:
    return ", ".join(sorted(mineru_supported_extensions()))


__all__ = ["mineru_supported_extensions", "format_supported_extensions"]
