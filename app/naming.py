"""Reading product names: brand, package size and the words that say what the product is.

Stores write the same item differently ("Havregryn Extra Fylliga 825G AXA", "Havregryn Extra
Fylliga AXA 825g", "Havregryn Extra Fylliga Axa"), so names are folded (case, accents, symbols) and
split into three parts that can be compared on their own: the brand, the package size in one base
unit, and the remaining "core" words. Nothing here guesses: a part that cannot be read is None,
and an item is only called the same as another when its brand, size and core words all agree.
"""
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher

from app.pricing import base_quantity, parse_size

# Words that describe a variant, not the brand: "Garant Eko" is the brand Garant, organic.
_BRAND_NOISE = {"eko", "ekologisk", "ekologiska", "krav"}
_SIZE_IN_TEXT = re.compile(
    r"(?<![\w.,])(?:(\d+)\s*[xX]\s*)?(\d+(?:[.,]\d+)?)\s*(kg|gram|gr|g|liter|litre|l|dl|cl|ml|st)(?![a-z0-9])",
    re.IGNORECASE)
_PACK_IN_TEXT = re.compile(r"(?<![\w.,])(\d{1,3})\s*-?\s*(pack|port|portioner)(?![a-z0-9])", re.IGNORECASE)


def fold(text: str | None) -> str:
    """Lower case without accents (Saltå -> salta, Mjölk -> mjolk) so spellings compare equal."""
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def tokens(text: str | None) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(text))


Size = tuple[Decimal, str]      # (amount, base unit): kg, L or count


def _base(parsed: tuple[Decimal, str] | None) -> Size | None:
    return base_quantity(*parsed) if parsed else None


def size_in_text(text: str | None) -> tuple[Size, tuple[int, int]] | None:
    """The first package size written in free text ('750 g', '1,5KG', '6x33cl') and where it is."""
    for match in _SIZE_IN_TEXT.finditer(text or ""):
        count, number, unit = match.groups()
        unit = {"gr": "g", "gram": "g", "liter": "l", "litre": "l"}.get(unit.lower(), unit)
        size = _base(parse_size(f"{count + 'x' if count else ''}{number}{unit}"))
        if size:
            return size, match.span()
    return None


def pack_in_text(text: str | None) -> tuple[str, tuple[int, int]] | None:
    """A multipack or portion count written in the name ('8-PACK', '6 pack', '13 port')."""
    match = _PACK_IN_TEXT.search(text or "")
    if not match:
        return None
    count, word = match.groups()
    label = f"{int(count)}-pack" if word.lower() == "pack" else f"{int(count)} portioner"
    return label, match.span()


def _without(text: str, *spans: tuple[int, int] | None) -> str:
    for start, end in sorted((s for s in spans if s), reverse=True):
        text = text[:start] + " " + text[end:]
    return text


def size_label(size: Size | None) -> str | None:
    """A size the way people say it: 750 g, 1.5 kg, 33 cl is shown as 0.33 L."""
    if not size:
        return None
    amount, unit = size
    if unit == "kg":
        return f"{(amount * 1000).normalize():f} g" if amount < 1 else f"{amount.normalize():f} kg"
    if unit == "L":
        return f"{(amount * 1000).normalize():f} ml" if amount < Decimal("0.1") else \
               f"{amount.normalize():f} L"
    return f"{amount.normalize():f} st"


def brand_key(brand: str | None) -> str | None:
    words = tokens(brand)
    kept = [w for w in words if w not in _BRAND_NOISE] or words
    return " ".join(kept) or None


def _is_brand(word: str, brand_words: list[str]) -> bool:
    """The brand word as it appears in the name, allowing a store's own spelling slip
    (Gyllenhammars / Gyllenhamars)."""
    return any(word == b or (min(len(word), len(b)) >= 5
                             and SequenceMatcher(None, word, b).ratio() >= 0.85)
               for b in brand_words)


@dataclass(frozen=True)
class Described:
    brand: str | None            # as the store shows it
    brand_key: str | None        # folded, for comparing
    size: Size | None
    core: frozenset[str]         # what is left of the name once brand, size and pack are removed
    pack: str | None = None      # '8-pack': part of what makes an item the same as another

    @property
    def pack_count(self) -> int | None:
        return int(self.pack.split()[0].split("-")[0]) if self.pack else None

    @property
    def size_text(self) -> str | None:
        return size_label(self.size)

    def like_key(self, gtin14: str | None = None) -> tuple[str, str] | None:
        """What makes two listings the same item: ('barcode', gtin) when the barcode is known,
        else ('name', brand|size|core words). None when brand or size is unknown, because then
        saying 'same' would be a guess."""
        if gtin14:
            return "barcode", gtin14
        if not (self.brand_key and self.size and self.core):
            return None
        return "name", f"{self.brand_key}|{self.size[0].normalize():f}{self.size[1]}|" \
                       f"{self.pack or ''}|" + " ".join(sorted(self.core))


def describe(name: str, brand: str | None, package_value: Decimal | None = None,
             package_unit: str | None = None) -> Described:
    from_name = size_in_text(name)
    size = base_quantity(package_value, package_unit) or (from_name[0] if from_name else None)
    bkey = brand_key(brand)
    pack = pack_in_text(name)
    text = _without(name, from_name[1] if from_name else None, pack[1] if pack else None)
    drop = bkey.split() if bkey else []
    return Described(brand=brand, brand_key=bkey, size=size, pack=pack[0] if pack else None,
                     core=frozenset(w for w in tokens(text) if len(w) > 1 and not _is_brand(w, drop)))


def normalize_brand(brand: str | None) -> str | None:
    """One way to write a brand, whichever store it came from: 'AXA' and 'Axa' -> 'AXA' (a short
    single word is an acronym), 'GARANT EKO' -> 'Garant Eko', 'OATLY' -> 'Oatly'. A brand already
    in mixed case is the store's own styling and is kept ('Kellogg\'s', 'Oatly', 'iKaffe')."""
    text = " ".join((brand or "").split())
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return text or None
    if " " not in text and len(letters) <= 3:
        return text.upper()
    if text.isupper() or text.islower():
        return re.sub(r"[^\s-]+", lambda m: m.group()[:1].upper() + m.group()[1:].lower(), text)
    return text


def display_name(name: str, brand: str | None) -> str:
    """The name without the brand and the package size, which are shown on their own:
    'Havregryn Extra Fylliga 825G AXA' -> 'Havregryn Extra Fylliga'. Word order and spelling of
    what is left are the store's own. Falls back to the full name if nothing would be left."""
    found, pack = size_in_text(name), pack_in_text(name)
    text = _without(name, found[1] if found else None, pack[1] if pack else None)
    brand_words = tokens(brand)
    kept = [w for w in text.split()
            if not (brand_words and (parts := tokens(w)) and all(_is_brand(p, brand_words) for p in parts))]
    return " ".join(kept).strip(" -,/&") or name


@dataclass(frozen=True)
class Query:
    """A typed search split into what to ask the store and what to check the answers against."""
    store_text: str              # sent to the store's search (a size is left out of it)
    terms: tuple[str, ...]       # folded words every result must contain
    size: Size | None            # a size typed in the query ('1l', '500 g')

    def accepts(self, name: str, brand: str | None, item_size: Size | None) -> bool:
        words = tokens(name) + tokens(brand)
        # a typed word may be part of a longer Swedish compound (havregryn in havregrynsgrot)
        if not all(any(term in word for word in words) for term in self.terms):
            return False
        return not (self.size and item_size and item_size != self.size)


def parse_query(text: str) -> Query:
    found = size_in_text(text)
    rest = (text[:found[1][0]] + " " + text[found[1][1]:]) if found else text
    rest = re.sub(r"\s+", " ", rest).strip()
    if len(rest) < 2:                        # only a size was typed: ask the store as typed
        return Query(store_text=text.strip(), terms=(), size=found[0] if found else None)
    return Query(store_text=rest, terms=tuple(w for w in tokens(rest) if len(w) > 1),
                 size=found[0] if found else None)


_RANGE_UNITS = {"g": (Decimal("0.001"), "kg"), "kg": (Decimal(1), "kg"), "ml": (Decimal("0.001"), "L"),
                "cl": (Decimal("0.01"), "L"), "dl": (Decimal("0.1"), "L"), "l": (Decimal(1), "L")}


def to_base(value: Decimal, unit: str) -> Size:
    factor, base = _RANGE_UNITS[unit.lower()]
    return value * factor, base


@dataclass(frozen=True)
class Narrow:
    """Filters the person asked for on their own, apart from the words of the search: a brand, a
    multipack count and a weight or volume range. A result of unknown size passes a size range
    (the store did not say), but a result that is known to differ is dropped."""
    brand: tuple[str, ...] = ()
    pack_low: int | None = None
    pack_high: int | None = None
    low: Size | None = None
    high: Size | None = None

    def accepts(self, name: str, brand: str | None, size: Size | None, pack_count: int | None) -> bool:
        if self.brand:
            words = tokens(brand) or tokens(name)       # the name only when the store gave no brand
            if not all(any(b in w for w in words) for b in self.brand):
                return False
        count = pack_count or 1                          # no pack in the name: a single item
        if (self.pack_low is not None and count < self.pack_low) or (
                self.pack_high is not None and count > self.pack_high):
            return False
        if size and (self.low or self.high):
            wanted_unit = (self.low or self.high)[1]
            if size[1] != wanted_unit:
                return False                            # grams against litres: not comparable
            if (self.low and size[0] < self.low[0]) or (self.high and size[0] > self.high[0]):
                return False
        return True


def make_narrow(brand: str | None, pack_min: int | None, pack_max: int | None,
                weight_min: str | None, weight_max: str | None, unit: str) -> Narrow:
    return Narrow(brand=tuple(tokens(brand)), pack_low=pack_min, pack_high=pack_max,
                  low=to_base(Decimal(weight_min), unit) if weight_min else None,
                  high=to_base(Decimal(weight_max), unit) if weight_max else None)
