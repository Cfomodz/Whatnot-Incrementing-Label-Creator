# Full Repository Review

A code review of the Brother QL-710W Incrementing Label Creator covering bugs,
feedback, and opportunities. File/line references point at the code as of this
review.

## Critical: the app crashes on a fresh clone

**`whatnot_live_label_writer.py:12-13` imports modules that aren't in the
repo.** The script does `from templates.logo_template import
generate_logo_label` and `from templates.coupon_template import
generate_coupon_label`, but `templates/` only contains an empty `__init__.py`.
Anyone cloning the repo gets a `ModuleNotFoundError` before Flask even starts.
Either commit those two template files, or delete the imports and the
`'logo'`/`'coupon'` branches in `print_label()` (they're unreachable anyway —
no route ever passes a `template` argument, so only the `'default'` branch is
ever used).

## Functional bugs

- **Barcode "type code" is different every time the server restarts**
  (`whatnot_live_label_writer.py:60`). `hash(label_type) % 100` uses Python's
  built-in `hash()`, which is randomized per process (`PYTHONHASHSEED`). The
  same label type gets a different 2-digit code after every restart, so a
  barcode can never be decoded back to its type later. Use a stable hash
  (e.g. `zlib.crc32(label_type.encode()) % 100`) or persist a type→code
  registry next to the counters.

- **`/clear_counters` can never clear a specific counter**
  (`whatnot_live_label_writer.py:270`). The input is lowercased
  (`.strip().lower()`), but counter keys keep their original casing
  (`"Coin"`, `"Custom"`). Sending `"Coin"` becomes `"coin"`, fails the
  `in counters` check, and returns 404. Only `"all"` works. Either don't
  lowercase, or do a case-insensitive key lookup.

- **Multi-line custom labels overlap when a line wraps**
  (`whatnot_live_label_writer.py:129-132`). In the `custom_text` branch of
  `print_label()`, each `\n`-separated line is drawn with
  `draw_wrapped_text()` (which may render several visual lines), but `y` only
  advances by one `line_height` per input line, so a long wrapped line gets
  overprinted by the next one. (This branch is currently dead code — no
  caller passes `custom_text`.)

- **The web page only works from the machine running the server**
  (`index.html:21`). The fetch URL is hardcoded to
  `http://localhost:5000/print`, but the server binds `0.0.0.0` specifically
  so other devices can reach it. From any other device the page loads but
  printing fails. A relative URL (`fetch('/print', ...)`) works from anywhere
  the page is served — and would let you drop `flask_cors` entirely.

- **Unsanitized label type becomes a filename**
  (`whatnot_live_label_writer.py:224`). `image.save(f"{label_type}_...png")`
  uses raw user input. A label type containing `/` (or `\`, `:` on Windows)
  makes the save throw and the request 500 — after the counter has already
  been incremented, so a number is burned without printing. Sanitize the
  filename (e.g. keep `[A-Za-z0-9 _-]`) or save under a fixed name.

- **Counter increments even if the print fails.** In both `/print` and
  `/print_custom`, the counter is bumped and saved before `send()` runs, and
  print errors in `print_label()` are swallowed
  (`whatnot_live_label_writer.py:242-243` prints to console and the route
  still returns `"success"`). If the printer is off or out of labels, numbers
  are silently skipped while the UI reports success. Print first and
  increment on success, or at least return an error status.

## Smaller robustness issues

- **No request-body guards**: `request.json` is `None` when a client posts
  without a JSON body, so `data.get(...)` raises and returns an HTML 500.
  Use `request.get_json(silent=True) or {}`.
- **Concurrency**: Flask ≥1.0 runs `app.run()` threaded by default, and the
  module-level `counters` dict plus read-modify-write of `counters.json`
  isn't locked. Two rapid requests can race and reuse a number. A
  `threading.Lock` around the increment+save fixes it.
- **Requests block on the printer**: `send(..., blocking=True)` runs inside
  the request handler, so a jammed/offline printer stalls the HTTP request
  until timeout. A print-queue thread would make the UI feel instant.
- **`service_server.py` has no protection against double-starts**: every POST
  to `/start-service` spawns another subprocess of the label server; the
  second crashes on the port bind and the thread lingers. Track the process
  handle and check `poll()` before spawning; consider `/stop-service` and
  `/status` endpoints. Neither server has auth — anyone on the LAN can print
  or clear counters. Probably fine for a home network, but worth a README
  note.
- **PNG litter**: every default-template print writes `barcode.png` plus a
  `{type}_{date}_product_label_{n}.png` into the working directory forever
  (gitignored, but they accumulate on disk). Consider an `output/` folder or
  skipping the debug save.

## Code quality / cleanup

- **`handle_custom_print` is a ~170-line route** duplicating most of
  `print_label()` (font loading, barcode generation, convert/send).
  Extracting shared helpers (`load_fonts()`, `make_barcode()`,
  `print_image(image)`) would roughly halve the file.
- **Dead/no-op code**: `custom_text = f"{custom_text}"` at line 295 (with a
  stale "Double the text for testing" comment); the unused
  `template`/`custom_text` parameters of `print_label`; `image_height` from
  `TEMPLATES` computed at line 109 then unconditionally overwritten at
  line 187; the `import re  # Add this at the top of the file` comment.
- **`crop_white_space()`** (lines 87-104) scans pixels in a Python loop.
  `ImageOps.invert(image.convert('L')).getbbox()` gives the same answer at C
  speed.
- **`arial.ttf` won't exist on Linux/most setups**, and the
  `load_default()` fallback is a tiny bitmap font that looks broken on a
  696px-wide label. Bundle a free font (DejaVu Sans, Liberation Sans) or make
  the font path configurable.
- **Hardcoded printer IP** (`10.0.0.13`, line 23): `service_server.py`
  already uses `python-dotenv`, so read `PRINTER_IP`, `PRINTER_MODEL`, and
  `LABEL_SIZE` from `.env` too.
- **`counters.json` is committed but also gitignored** (`*.json` in
  `.gitignore`). Since it's already tracked, live counter state shows up as a
  dirty file after every print. `git rm --cached counters.json` and let the
  code create it on first run.
- **`/print_custom` response inconsistency**: `"number"` is always the
  counter value even when the printed number came from a `#123` in the text.

## Onboarding & docs opportunities

- **No `requirements.txt`** — dependencies (`flask`, `flask-cors`,
  `brother_ql`, `Pillow`, `python-barcode`, `python-dotenv`) are
  undocumented. Note the PyPI `brother_ql` package is unmaintained and breaks
  with Pillow ≥10 (`Image.ANTIALIAS` was removed) — pin `Pillow<10` or use a
  maintained fork such as `brother_ql_next`.
- **No setup/run instructions** in the README — installing deps, setting the
  printer IP, ports, how the Stream Deck integration is wired (an HTTP-POST
  action hitting `/print`), or what `service_server.py` is for. A "Quick
  start" section with a few lines of shell would make the project usable by
  its target audience.
- **README badges point to the old repo name**
  (`Cfomodz/Whatnot-Item-Label-Creator`) rather than
  `Brother-QL-710W_Incrementing-Label-Creator`.
- A `.env.example` documenting `PYTHON_PATH`, `SERVICE_PATH`, and (once
  moved) `PRINTER_IP` would tie it together.

## Feature opportunities

- **Session support**: a "start new show" button that archives/resets
  counters (building on `/clear_counters`) plus a per-show prefix in the
  barcode would map nicely to Whatnot streams.
- **Preview endpoint**: return the rendered PNG
  (`/preview?label_type=Coin`) so the web page can show the label before
  printing — cheap since images are already generated.
- **Richer web UI**: expose custom text and clear-counters, show current
  counter per type, list recent prints. Right now `/print_custom` and
  `/clear_counters` are only reachable via curl/Stream Deck.
- **USB fallback**: make `printer_identifier` configurable
  (`tcp://` vs `usb://0x04f9:...`) for sellers using USB instead of Wi-Fi.

## TL;DR

Fix first: (1) the missing `templates/logo_template.py` /
`coupon_template.py` imports — the repo doesn't run when cloned; (2) the
randomized `hash()` in the barcode type code. After that, a
`requirements.txt` + README quick-start delivers the most value for the
effort, followed by the `/clear_counters` case bug and the hardcoded
`localhost` in `index.html`.
