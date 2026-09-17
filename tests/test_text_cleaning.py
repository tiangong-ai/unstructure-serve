import pytest

from src.utils.text_output import clean_text


@pytest.mark.parametrize(
    "text,expected",
    [
        ("中文🙂\n\t café", "中文🙂\n\t café"),
        ("a\ud800b\udfffc", "abc"),
        ("\ud83d\ude42", ""),
        ("\x00\U0010ffff", "\x00\U0010ffff"),
        ("", ""),
    ],
)
def test_clean_text_preserves_valid_characters_and_removes_surrogates(text, expected):
    assert clean_text(text) == expected
    assert clean_text(text).encode("utf-8").decode("utf-8") == expected
