"""Kenyan phone number normalisation.

M-Pesa (Daraja) and WhatsApp both identify people by phone number in the
international format without '+', e.g. 254712345678. Users type numbers many
ways, so we normalise everything to that single form before storing it.
"""

import re

# Kenyan mobile numbers: 7XXXXXXXX or 1XXXXXXXX after the country code.
_KENYAN_MOBILE = re.compile(r"^254[71][0-9]{8}$")


def normalize_kenyan_phone(raw: str) -> str:
    """Return ``raw`` as 2547XXXXXXXX / 2541XXXXXXXX, or raise ValueError.

    Accepts: 0712345678, 712345678, +254712345678, 254712345678,
    and the same with spaces or dashes.
    """
    digits = re.sub(r"[\s\-()]", "", raw)
    if digits.startswith("+"):
        digits = digits[1:]

    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif len(digits) == 9 and digits[0] in "71":
        digits = "254" + digits

    if not _KENYAN_MOBILE.match(digits):
        raise ValueError("Enter a valid Kenyan mobile number, e.g. 0712345678")
    return digits
