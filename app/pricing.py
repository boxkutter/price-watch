from decimal import ROUND_HALF_UP, Decimal


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
