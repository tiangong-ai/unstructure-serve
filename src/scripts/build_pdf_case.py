"""Build a private repeated-page PDF with an exact source-page manifest."""

import argparse
import hashlib
import json
from pathlib import Path

import pypdfium2 as pdfium


def build_case(sources: list[Path], pages: int, output: Path) -> dict:
    if pages < 1 or not sources:
        raise ValueError("At least one source and a positive page count are required")
    if output.exists():
        raise FileExistsError(output)
    inputs = []
    for source in sources:
        with pdfium.PdfDocument(source) as document, source.open("rb") as stream:
            count = len(document)
            if not count:
                raise ValueError(f"Empty source PDF: {source.name}")
            inputs.append(
                {
                    "name": source.name,
                    "pages": count,
                    "sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
                }
            )
    output.mkdir(parents=True, exist_ok=False)
    mapping = []
    try:
        with pdfium.PdfDocument.new() as target:
            while len(mapping) < pages:
                for index, source in enumerate(sources):
                    count = min(inputs[index]["pages"], pages - len(mapping))
                    if not count:
                        break
                    with pdfium.PdfDocument(source) as document:
                        target.import_pages(document, list(range(count)))
                    mapping.extend(
                        {"source_index": index, "source_page": i + 1} for i in range(count)
                    )
            target.save(output / "case.pdf")
        with (output / "case.pdf").open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest = {
            "synthetic": True,
            "pages": pages,
            "sources": inputs,
            "sha256": digest,
            "page_map": mapping,
        }
        (output / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest
    except BaseException:
        # Only remove the files this invocation owns, never an existing directory.
        for name in ("case.pdf", "manifest.json"):
            (output / name).unlink(missing_ok=True)
        output.rmdir()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", nargs="+", type=Path, required=True)
    parser.add_argument("--pages", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_case(args.sources, args.pages, args.output)
    print(json.dumps({"pages": manifest["pages"], "sha256": manifest["sha256"]}))


if __name__ == "__main__":
    main()
