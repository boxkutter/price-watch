"""Reading "2 för 35 kr" as what it really costs per item.

A multi-buy ("2 för 35 kr", "3 för 2", "Köp 2 betala för 1", "19 kr/st vid köp av 2") is stored as an
offer, with the retailer's own wording. This turns that wording into a `Deal`: how many items, and
what they cost together. A monitor's alert price is then compared with the price per item, so
"2 för 35 kr" (17,50 kr each) reaches an alert price of 19 kr even though one item costs 20 kr.

Only wording that states the deal is understood; anything else is left as the store's own text.
"""
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# "2 för 35 kr", "3 st för 50:-", "2 for 12,50"; without a currency, a second number smaller than
# the first ("3 för 2", "2 för 1") is a quantity (pay for 2, get 3), not a price.
_BUNDLE = re.compile(r"(?<![\d.])(\d+)\s*(?:st\s*)?(?:för|for)\s*(\d+(?:\.\d+)?)\s*(kr|:-)?(?![\d])")
# "Köp 2 betala för 1", "köp 3, betala för 2"
_PAY_FOR = re.compile(r"köp\s*(\d+)\D{0,12}?betala\s*(?:för\s*)?(\d+)(?!\d)")
# "19 kr/st vid köp av 2", "vid köp av 3: 15 kr/st"
_EACH_AFTER = re.compile(r"(\d+(?:\.\d+)?)\s*(?:kr)?\s*/\s*st\D{0,25}?(?:köp|vid)\D{0,8}?(\d+)(?!\d)")
_EACH_BEFORE = re.compile(r"(?:köp|vid)\D{0,8}?(\d+)\D{1,15}?(\d+(?:\.\d+)?)\s*(?:kr)?\s*/\s*st")

_MAX_ITEMS = 50


@dataclass(frozen=True)
class Deal:
    quantity: int
    total: Decimal  # what all the items cost together

    @property
    def unit(self) -> Decimal:
        """The price per item, to the öre, for showing."""
        return (self.total / self.quantity).quantize(Decimal("0.01"), ROUND_HALF_UP)

    def reaches(self, limit: Decimal) -> bool:
        """Whether the price per item is at or below [limit]. Compared exactly: 3 för 50 is not 16.67."""
        return self.total <= limit * self.quantity


def _deal(quantity: int, total: Decimal) -> Deal | None:
    return Deal(quantity, total) if 2 <= quantity <= _MAX_ITEMS and total > 0 else None


def parse_deal(terms: str | None, single_price: Decimal | None = None) -> Deal | None:
    """The deal a multi-buy's wording states, or None.

    [single_price] is the price of one item, needed only for "pay for" wording ("3 för 2"), which
    states a count, not a price.
    """
    if not terms:
        return None
    text = terms.casefold().replace(",", ".")
    if match := _PAY_FOR.search(text):
        bought, paid = int(match.group(1)), int(match.group(2))
        return _pay_for(bought, paid, single_price)
    if match := _BUNDLE.search(text):
        count, amount, currency = int(match.group(1)), Decimal(match.group(2)), match.group(3)
        if currency is None and amount == amount.to_integral_value() and int(amount) < count:
            return _pay_for(count, int(amount), single_price)
        return _deal(count, amount)
    if match := _EACH_AFTER.search(text):
        return _each(int(match.group(2)), Decimal(match.group(1)))
    if match := _EACH_BEFORE.search(text):
        return _each(int(match.group(1)), Decimal(match.group(2)))
    return None


def _pay_for(bought: int, paid: int, single_price: Decimal | None) -> Deal | None:
    if single_price is None or not 0 < paid < bought:
        return None
    return _deal(bought, single_price * paid)


def _each(quantity: int, each: Decimal) -> Deal | None:
    return _deal(quantity, each * quantity)
