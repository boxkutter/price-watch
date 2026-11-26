import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def base_quantity(value: Decimal | None, unit: str | None) -> tuple[Decimal, str] | None:
    if value is None or not value.is_finite() or value <= 0 or unit is None:
        return None
    conversion = {"g": (Decimal("0.001"), "kg"), "kg": (Decimal(1), "kg"),
                  "ml": (Decimal("0.001"), "L"), "L": (Decimal(1), "L"),
                  "st": (Decimal(1), "count"), "count": (Decimal(1), "count")}
    if unit not in conversion:
        return None
    factor, base_unit = conversion[unit]
    return value * factor, base_unit


def unit_price(price: Decimal, value: Decimal | None,
               unit: str | None) -> tuple[Decimal, str] | None:
    quantity = base_quantity(value, unit)
    if not quantity:
        return None
    amount, base_unit = quantity
    return (price / amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), base_unit


_SIZE = re.compile(r"^(?:(\d+)\s*[xX]\s*)?(\d+(?:[.,]\d+)?)\s*(kg|g|l|dl|cl|ml|st)$", re.IGNORECASE)
# Units base_quantity() understands; decilitres and centilitres are expressed in millilitres.
_SIZE_UNITS = {"kg": (Decimal(1), "kg"), "g": (Decimal(1), "g"), "l": (Decimal(1), "L"),
               "dl": (Decimal(100), "ml"), "cl": (Decimal(10), "ml"), "ml": (Decimal(1), "ml"),
               "st": (Decimal(1), "st")}


def parse_size(text: str | None) -> tuple[Decimal, str] | None:
    """Parse Swedish package labels such as '1,5KG', '450G', '33CL' or '6X33CL'.

    Returns (value, unit) in a unit base_quantity() accepts, or None when the label is not
    a plain quantity. Never guesses: '6-PACK', 'CA 500G' or '' all give None.
    """
    match = _SIZE.match((text or "").strip())
    if not match:
        return None
    count, number, unit = match.groups()
    try:
        value = Decimal(number.replace(",", "."))
    except InvalidOperation:
        return None
    factor, base_unit = _SIZE_UNITS[unit.lower()]
    total = value * factor * (Decimal(count) if count else 1)
    return (total, base_unit) if total > 0 else None
