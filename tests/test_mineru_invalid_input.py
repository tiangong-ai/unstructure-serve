"""Check that MinerU's typed input failures survive the service adapter."""

import pytest
from mineru.errors import InvalidRequestError
from reportlab.pdfgen import canvas

from src.services.mineru_service_full import parse_doc


@pytest.mark.parametrize("error_code", ["file_corrupted", "file_encrypted"])
def test_invalid_pdf_cannot_produce_a_successful_parse(tmp_path, error_code):
    source = tmp_path / "input.pdf"
    if error_code == "file_corrupted":
        source.write_bytes(b"%PDF-1.4\n1 0 obj\n")
    else:
        document = canvas.Canvas(str(source), encrypt="example-password")
        document.drawString(72, 720, "Protected text")
        document.save()

    output = tmp_path / "output"
    with pytest.raises(InvalidRequestError) as error:
        parse_doc([source], output, tier="flash")

    assert error.value.code == error_code
    assert not (output / source.stem / "flash" / "middle_json.json").exists()
