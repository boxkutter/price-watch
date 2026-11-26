def normalize_gtin(value: str) -> str:
    if len(value) not in (8, 12, 13, 14) or not value.isascii() or not value.isdigit():
        raise ValueError("GTIN must have 8, 12, 13 or 14 digits")
    digits = [int(char) for char in value]
    total = sum(digit * (3 if index % 2 == 0 else 1)
                for index, digit in enumerate(reversed(digits[:-1])))
    if (10 - total % 10) % 10 != digits[-1]:
        raise ValueError("Invalid GTIN check digit")
    return value.zfill(14)
