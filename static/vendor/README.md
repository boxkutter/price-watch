# Vendored libraries

| File | What | Version | License | Source |
|---|---|---|---|---|
| `zxing-library-0.21.3.min.js` | ZXing, decodes barcodes from camera frames and photos (used by the example client's camera button) | 0.21.3 | Apache-2.0 (`zxing-library.LICENSE`) | npm `@zxing/library`, `umd/index.min.js`, sha256 `d7cc8f69dd70bdcf3ac00c9ae572bf2acb9f4132ba379c72df842e4db918652d` |

It is served by the app at `/vendor/zxing.min.js` and loaded only when someone taps the camera
button. It makes no network requests, stores nothing and uses no `eval` (checked when it was added).
