import pytest

from core.phone import normalize_kenyan_phone


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0712345678", "254712345678"),
        ("712345678", "254712345678"),
        ("+254712345678", "254712345678"),
        ("254712345678", "254712345678"),
        ("0712 345 678", "254712345678"),
        ("0712-345-678", "254712345678"),
        ("(0712) 345 678", "254712345678"),
        ("0110123456", "254110123456"),  # newer 01xx Safaricom/Airtel range
        ("110123456", "254110123456"),  # 9-digit 01xx without leading 0
        ("+254 110 123 456", "254110123456"),
    ],
)
def test_valid_numbers_are_normalised(raw: str, expected: str) -> None:
    assert normalize_kenyan_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "12345",
        "0212345678",  # landline-style prefix
        "07123456789",  # too long
        "071234567",  # too short
        "+255712345678",  # Tanzania
        "07123abc78",
        "254812345678",  # invalid mobile prefix
        "071234567８",  # noqa: RUF001 - non-ASCII fullwidth digit
    ],
)
def test_invalid_numbers_raise(raw: str) -> None:
    with pytest.raises(ValueError, match="valid Kenyan mobile"):
        normalize_kenyan_phone(raw)
