# Dollarstore (se.dollarstore)

Targeted product search, offers and per-store stock from [dollarstore.se](https://dollarstore.se).
No key or login.

## What it does

- **Search** by name (`/sokresultat?q=`), capped at the first 25 results (the site's other pages
  could not be reached; the result says when there is more).
- **Look a product up again** by its article number (`8068-12530`), which is also what a pasted
  product page link carries. Case does not matter; ids are kept in lower case.
- **Offers:** the site's `mixMatch` text ("2 för 35") becomes a public offer with the bundle price.
  Wording that is not "N för P" is kept as the store's own text with no amount.
- **Stock per store.** The site keeps the chosen store in the `selectedStore` cookie and answers
  with that store's stock on the product page. The connector sends the cookie for the store a
  request is for, and reads the product page for the first five search results (and for every
  product looked up by id). The site's default of 8000 (no store, or an unknown one) is treated as
  unknown. Prices and offers were identical across the stores compared.
- **Stores:** "Find stores" on the connector card reads the site's store list (about 145 stores).
  Type a town or a postcode to add only the matching ones; leave it blank to add all.

## What it does not do

- No barcode search: the site publishes none (the product image file name looks like the EAN,
  but that is not relied on).
- No newsletter offers (they arrive by email, which nothing here reads).
- No whole-store or category listings; every request is targeted.

## Politeness

`robots.txt` disallows nothing and sets no crawl delay. Requests are two seconds apart, at most
eight per scan and 400 a day. A 401/403 pauses the connector.

## Tests

`pytest tests/test_dollarstore.py`: fixtures in `tests/fixtures/dollarstore_*.html` are trimmed real
pages (no cookies, ids or phone numbers).
