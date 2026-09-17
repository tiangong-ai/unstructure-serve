import builtins

from src.utils import mineru_support


def test_service_extensions_keep_the_page_assets_contract():
    extensions = mineru_support.mineru_supported_extensions()
    assert extensions == {".pdf", ".png", ".jpeg", ".jpg", ".webp", ".bmp", ".tif", ".tiff"}
    assert not extensions & {".md", ".txt", ".epub", ".html", ".csv"}
    assert mineru_support.format_supported_extensions() == ", ".join(sorted(extensions))


def test_extension_validation_does_not_import_removed_mineru_modules(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.startswith("mineru"):
            raise AssertionError("Extension validation must not depend on MinerU internals")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    mineru_support.mineru_supported_extensions.cache_clear()
    assert ".pdf" in mineru_support.mineru_supported_extensions()
