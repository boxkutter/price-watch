"""English and Swedish words for the same grocery, so a search or a comparison does not depend on
which language a shop happened to use ("Oat Drink" and "Havredryck" are the same thing).

A small built-in list covers common groceries; people add their own, shared by everyone like the
barcode links. Everything is compared folded (no case, no accents) and mapped to one canonical
Swedish form, so both spellings of a product end up with the same words.
"""
import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session


def fold(text: str | None) -> str:
    """Lower case without accents (Saltå -> salta, Mjölk -> mjolk) so spellings compare equal."""
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


# English (a word or a short phrase) -> Swedish. Swedish is the canonical side: shops here are
# Swedish, and a Swedish word needs no entry.
BUILTIN: dict[str, str] = {
    "oat drink": "havredryck", "oat milk": "havredryck", "oat": "havre", "oats": "havregryn",
    "rolled oats": "havregryn", "porridge": "gröt", "porridge oats": "havregryn",
    "milk": "mjölk", "whole milk": "standardmjölk", "skimmed milk": "lättmjölk",
    "cream": "grädde", "cooking cream": "matlagningsgrädde", "sour cream": "gräddfil",
    "butter": "smör", "cheese": "ost", "yoghurt": "yoghurt", "yogurt": "yoghurt",
    "egg": "ägg", "eggs": "ägg", "bread": "bröd", "crispbread": "knäckebröd", "toast": "rostbröd",
    "flour": "mjöl", "wheat flour": "vetemjöl", "sugar": "socker", "salt": "salt",
    "rice": "ris", "pasta": "pasta", "noodles": "nudlar", "potato": "potatis", "potatoes": "potatis",
    "tomato": "tomat", "tomatoes": "tomat", "onion": "lök", "onions": "lök", "garlic": "vitlök",
    "carrot": "morot", "carrots": "morötter", "cucumber": "gurka", "lettuce": "sallad",
    "apple": "äpple", "apples": "äpplen", "banana": "banan", "bananas": "bananer",
    "orange": "apelsin", "oranges": "apelsiner", "lemon": "citron", "strawberry": "jordgubb",
    "strawberries": "jordgubbar", "blueberry": "blåbär", "blueberries": "blåbär",
    "raspberry": "hallon", "raspberries": "hallon", "pear": "päron", "peach": "persika",
    "chicken": "kyckling", "beef": "nötkött", "pork": "fläskkött", "ham": "skinka",
    "sausage": "korv", "bacon": "bacon", "fish": "fisk", "salmon": "lax", "tuna": "tonfisk",
    "coffee": "kaffe", "tea": "te", "water": "vatten", "sparkling water": "kolsyrat vatten",
    "juice": "juice", "beer": "öl", "wine": "vin", "cereal": "flingor", "cornflakes": "cornflakes",
    "muesli": "müsli", "granola": "granola", "chocolate": "choklad", "biscuit": "kex",
    "biscuits": "kex", "cookie": "kaka", "cookies": "kakor", "crisps": "chips", "chips": "chips",
    "candy": "godis", "sweets": "godis", "ice cream": "glass", "jam": "sylt", "honey": "honung",
    "oil": "olja", "olive oil": "olivolja", "vinegar": "vinäger", "ketchup": "ketchup",
    "mustard": "senap", "soup": "soppa", "sauce": "sås", "beans": "bönor", "lentils": "linser",
    "peas": "ärtor", "nuts": "nötter", "peanut": "jordnöt", "peanuts": "jordnötter",
    "almond": "mandel", "almonds": "mandlar", "frozen": "fryst", "fresh": "färsk",
    "organic": "eko", "gluten free": "glutenfri", "lactose free": "laktosfri",
    "wholegrain": "fullkorn", "whole grain": "fullkorn", "low fat": "lättfett", "plant based": "växtbaserad",
    "vanilla": "vanilj", "hazelnut": "hasselnöt", "strawberry jam": "jordgubbssylt",
    # the same Swedish word in another form (organic, gluten free, lactose free)
    "ekologisk": "eko", "ekologiska": "eko", "ekologiskt": "eko", "glutenfria": "glutenfri",
    "glutenfritt": "glutenfri", "laktosfria": "laktosfri", "laktosfritt": "laktosfri",
    "toothpaste": "tandkräm", "soap": "tvål", "shampoo": "schampo", "toilet paper": "toalettpapper",
}

CUSTOM: dict[str, str] = {}          # added by people; loaded from the database by refresh()


def _folded(table: dict[str, str]) -> dict[str, str]:
    return {fold(" ".join(k.split())): fold(" ".join(v.split())) for k, v in table.items()}


_BUILT = _folded(BUILTIN)
_all: dict[str, str] = dict(_BUILT)
_longest = max(len(k.split()) for k in _BUILT)


def _rebuild() -> None:
    global _all, _longest
    _all = {**_BUILT, **_folded(CUSTOM)}
    _longest = max(len(k.split()) for k in _all)


def refresh(db: Session) -> None:
    """Load the words people added. Cheap; called at the start of a search."""
    from app.models import GlossaryEntry
    CUSTOM.clear()
    CUSTOM.update({row.term: row.canonical for row in db.scalars(select(GlossaryEntry))})
    _rebuild()


def set_custom(entries: dict[str, str]) -> None:
    """Replace the added words (the API process keeps its own copy current with this)."""
    CUSTOM.clear()
    CUSTOM.update(entries)
    _rebuild()


def _map_words(words: list[str]) -> list[str]:
    """Folded words with every known phrase/word replaced by its canonical Swedish form."""
    out, i = [], 0
    while i < len(words):
        for size in range(min(_longest, len(words) - i), 0, -1):
            phrase = " ".join(words[i:i + size])
            if phrase in _all:
                out.extend(_all[phrase].split())
                i += size
                break
        else:
            out.append(words[i])
            i += 1
    return out


def canonical_tokens(text: str | None) -> list[str]:
    """The words of a text, folded, with English grocery words turned into their Swedish form."""
    return _map_words(re.findall(r"[a-z0-9]+", fold(text)))


def translate(text: str) -> str:
    """A typed search with its English words in Swedish, for asking Swedish shops. Words with no
    entry stay as typed. Equal to the input when nothing was translated."""
    words = text.split()
    folded = [fold(w).strip(".,;:!?") for w in words]
    out, i = [], 0
    while i < len(words):
        for size in range(min(_longest, len(words) - i), 0, -1):
            phrase = " ".join(folded[i:i + size])
            if phrase in _all:
                # the Swedish spelling as written in the list (with å ä ö), else the folded form
                original = next((v for k, v in {**BUILTIN, **CUSTOM}.items()
                                 if fold(" ".join(k.split())) == phrase), _all[phrase])
                out.append(original)
                i += size
                break
        else:
            out.append(words[i])
            i += 1
    return " ".join(out)


def entries() -> list[dict]:
    """The built-in words, for showing (the added ones come from the database)."""
    return [{"term": k, "canonical": v, "source": "builtin"} for k, v in sorted(BUILTIN.items())]
