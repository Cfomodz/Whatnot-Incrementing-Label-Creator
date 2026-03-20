#!/usr/bin/env python3
"""
Flask label printing server for Brother P-touch D610BT.

Dependencies:
    pip install flask flask-cors PyBluez2 Pillow python-barcode

Connection:  Bluetooth Classic RFCOMM (port 1)
Protocol:    P-touch CBP raster mode (Brother Communication-Based Protocol)
Tape:        TZe continuous tape — default 24 mm (128 dots at 180 dpi)
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import bluetooth          # PyBluez2: pip install PyBluez2
from PIL import Image, ImageDraw, ImageFont
from barcode import Code128
from barcode.writer import ImageWriter
import datetime
import json
import os
import re
import struct

app = Flask(__name__)
CORS(app)

COUNTER_FILE = 'counters.json'

# ── Printer settings ──────────────────────────────────────────────────────────

# Set to your printer's Bluetooth MAC (e.g. 'XX:XX:XX:XX:XX:XX').
# Leave as None to auto-discover by name on each print (slower).
PRINTER_BT_ADDRESS = None
PRINTER_BT_NAME    = 'PT-D610BT'   # Bluetooth device name for auto-discovery
PRINTER_BT_PORT    = 1             # RFCOMM channel (1 for most P-touch models)

# Tape width in mm. Supported: 3.5, 6, 9, 12, 18, 24
TAPE_WIDTH_MM = 24

# Print-head dot widths per tape size at 180 dpi
TAPE_PRINT_WIDTHS = {
    3.5: 24,
    6:   32,
    9:   52,
    12:  76,
    18: 112,
    24: 128,
}

MOST_RECENT_LABEL_TYPE = None


def get_print_width() -> int:
    return TAPE_PRINT_WIDTHS.get(TAPE_WIDTH_MM, 128)


# ── Counter persistence ───────────────────────────────────────────────────────

def load_counters():
    if os.path.exists(COUNTER_FILE):
        with open(COUNTER_FILE, 'r') as f:
            return json.load(f)
    return {}


def save_counters(counters):
    with open(COUNTER_FILE, 'w') as f:
        json.dump(counters, f)


counters = load_counters()


# ── Bluetooth helpers ─────────────────────────────────────────────────────────

def find_printer_address() -> str:
    """Return the configured MAC address or auto-discover by device name."""
    if PRINTER_BT_ADDRESS:
        return PRINTER_BT_ADDRESS
    print(f"Scanning for Bluetooth device '{PRINTER_BT_NAME}'...")
    nearby = bluetooth.discover_devices(lookup_names=True, duration=8)
    for addr, name in nearby:
        if PRINTER_BT_NAME in name:
            print(f"Found: {name} @ {addr}")
            return addr
    raise RuntimeError(
        f"Printer '{PRINTER_BT_NAME}' not found via Bluetooth scan. "
        "Set PRINTER_BT_ADDRESS directly or bring the printer within range."
    )


def send_to_printer(data: bytes):
    """Open an RFCOMM socket, transmit data, close."""
    addr = find_printer_address()
    sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
    try:
        sock.connect((addr, PRINTER_BT_PORT))
        sock.send(data)
    finally:
        sock.close()


# ── P-touch CBP raster protocol ───────────────────────────────────────────────

def build_ptouch_raster(image: Image.Image) -> bytes:
    """
    Encode a monochrome PIL image as P-touch CBP raster bytes.

    The image must be:
      - mode '1' (1-bit monochrome)
      - width == get_print_width()  (e.g. 128 dots for 24 mm tape)
      - height == desired label length in dots
    Black pixels (value 0) are printed; white (255) are not.
    """
    img = image.convert('1')
    width, height = img.size
    bytes_per_row = width // 8

    buf = bytearray()

    # 1. Invalidate — clear any pending state
    buf += b'\x00' * 100

    # 2. Initialize
    buf += b'\x1b\x40'

    # 3. Switch to ESC/P raster mode
    buf += b'\x1b\x69\x61\x01'

    # 4. Auto-cut after each label
    buf += b'\x1b\x69\x4d\x40'

    # 5. Set print information (ESC i z + 10 data bytes)
    tape_w = int(TAPE_WIDTH_MM)
    buf += bytes([
        0x1b, 0x69, 0x7a,   # command
        0x84,               # valid flags: media type + media width
        0x0a,               # media type: continuous tape
        tape_w,             # tape width in mm
        0x00, 0x00,         # tape length bytes (0 = continuous)
        0x00, 0x00, 0x00, 0x00, 0x00,  # reserved
    ])

    # 6. Zero feed margin
    buf += b'\x1b\x69\x64\x00\x00'

    # 7. No compression
    buf += b'\x4d\x00'

    # 8. Raster data — one command per horizontal dot row
    for y in range(height):
        row = bytearray(bytes_per_row)
        for x in range(width):
            if img.getpixel((x, y)) == 0:       # black dot
                row[x // 8] |= 0x80 >> (x % 8)  # MSB = leftmost dot
        if any(row):
            buf += bytes([0x47]) + struct.pack('<H', bytes_per_row) + bytes(row)
        else:
            buf += b'\x5a'  # empty raster line (no ink)

    # 9. Print and feed/cut
    buf += b'\x1a'

    return bytes(buf)


# ── Image generation ──────────────────────────────────────────────────────────

def _load_fonts(base_size: int):
    try:
        font  = ImageFont.truetype("arial.ttf",   base_size)
        bold  = ImageFont.truetype("arialbd.ttf", base_size)
        small = ImageFont.truetype("arial.ttf",   max(10, base_size - 6))
    except IOError:
        font = bold = small = ImageFont.load_default()
    return font, bold, small


def _generate_barcode_data(label_type: str, number: int) -> str:
    today     = datetime.datetime.now().strftime("%m%d%Y")
    type_code = f"{hash(label_type) % 100:02d}"
    return f"{today}{type_code}{number:04d}"


def create_standard_label(label_type: str, number: int) -> Image.Image:
    """
    Build a tape label containing:
      Line 1 — label type + bold item number
      Line 2 — today's date (smaller font)
      Below  — Code128 barcode rotated 90° to run along the tape length
    """
    pw        = get_print_width()
    font_size = max(14, pw // 7)
    font, bold, small = _load_fonts(font_size)

    today    = datetime.datetime.now().strftime("%m/%d/%Y")
    num_text = f"#{number}"

    # Row heights
    line1_h = max(font.getbbox(label_type)[3], bold.getbbox(num_text)[3]) + 4
    line2_h = small.getbbox(today)[3] + 4

    # Generate barcode, rotate 90° so it runs along the tape length,
    # then scale width to exactly match the print width.
    bc_obj  = Code128(_generate_barcode_data(label_type, number), writer=ImageWriter())
    bc_path = "barcode_tmp"
    bc_obj.save(bc_path, options={"module_height": pw * 0.55, "quiet_zone": 2})
    bc_img  = Image.open(bc_path + ".png").convert('1')
    bc_img  = bc_img.rotate(90, expand=True)
    scale   = pw / bc_img.width
    bc_img  = bc_img.resize((pw, max(1, int(bc_img.height * scale))), Image.NEAREST)

    total_h = line1_h + line2_h + bc_img.height + 8

    img  = Image.new('1', (pw, total_h), 1)
    draw = ImageDraw.Draw(img)

    # Line 1: type name + number
    y       = 2
    main_w  = int(font.getlength(label_type))
    num_w   = int(bold.getlength(num_text))
    row1_w  = main_w + 4 + num_w
    x       = max(0, (pw - row1_w) // 2)
    draw.text((x,              y), label_type, font=font, fill="black")
    draw.text((x + main_w + 4, y), num_text,   font=bold, fill="black")

    # Line 2: date
    y      += line1_h
    date_x  = max(0, (pw - int(small.getlength(today))) // 2)
    draw.text((date_x, y), today, font=small, fill="black")

    # Barcode below
    y += line2_h + 4
    img.paste(bc_img, (0, y))

    return img


def _draw_wrapped(draw, text, font, x, y, max_w):
    """Draw word-wrapped text; return the y position after the last line."""
    words    = text.split()
    lines    = []
    cur_line = words[0] if words else ""
    for word in words[1:]:
        test = f"{cur_line} {word}"
        if font.getlength(test) <= max_w:
            cur_line = test
        else:
            lines.append(cur_line)
            cur_line = word
    lines.append(cur_line)

    lh = font.getbbox(lines[0])[3] + 5
    for i, line in enumerate(lines):
        draw.text((x, y + i * lh), line, font=font, fill="black")
    return y + len(lines) * lh


def create_custom_label(text: str) -> Image.Image:
    """
    Custom tape label with optional bold number emphasis (#123 pattern).
    Supports multi-line input (\n).
    """
    pw        = get_print_width()
    font_size = max(14, pw // 7)
    font, bold, _ = _load_fonts(font_size)
    lh            = font.getbbox("Ag")[3] + 5

    # Estimate height with a scratch image
    scratch = Image.new('1', (pw, 2000), 1)
    tmp_draw = ImageDraw.Draw(scratch)
    y = 4
    for line in text.split('\n'):
        y = _draw_wrapped(tmp_draw, line, font, 4, y, pw - 8)

    img  = Image.new('1', (pw, max(lh, y + 4)), 1)
    draw = ImageDraw.Draw(img)

    y = 4
    for line in text.split('\n'):
        # Emphasise any #number token
        match = re.search(r'#(\d+)', line)
        if match:
            before = line[:match.start()]
            token  = match.group(0)
            after  = line[match.end():]
            draw.text((4, y), before, font=font, fill="black")
            bx = 4 + int(font.getlength(before))
            draw.text((bx, y), token, font=bold, fill="black")
            draw.text((bx + int(bold.getlength(token)), y), after, font=font, fill="black")
            y += lh
        else:
            y = _draw_wrapped(draw, line, font, 4, y, pw - 8)

    return img


def print_label(image: Image.Image):
    """Encode image as P-touch raster and transmit via Bluetooth."""
    send_to_printer(build_ptouch_raster(image))


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route('/print', methods=['POST'])
def handle_print():
    global MOST_RECENT_LABEL_TYPE
    data       = request.json
    label_type = data.get('label_type', MOST_RECENT_LABEL_TYPE or 'Coin').strip()
    MOST_RECENT_LABEL_TYPE = label_type

    counters.setdefault(label_type, 0)
    counters[label_type] += 1
    save_counters(counters)

    try:
        print_label(create_standard_label(label_type, counters[label_type]))
        return jsonify({
            "status":     "success",
            "label_type": label_type,
            "number":     counters[label_type],
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/clear_counters', methods=['POST'])
def clear_counters_route():
    data   = request.json
    target = data.get('label_type', 'all').strip().lower()

    if target == 'all':
        counters.clear()
    elif target in counters:
        del counters[target]
    else:
        return jsonify({"status": "error", "message": f"'{target}' not found"}), 404

    save_counters(counters)
    return jsonify({"status": "success", "cleared": target, "counters": counters})


@app.route('/print_custom', methods=['POST'])
def handle_custom_print():
    data        = request.json
    custom_text = data.get('text', '').strip()
    if not custom_text:
        return jsonify({"status": "error", "message": "No text provided"}), 400

    label_type = "Custom"
    counters.setdefault(label_type, 0)
    counters[label_type] += 1
    save_counters(counters)

    try:
        print_label(create_custom_label(custom_text))
        return jsonify({
            "status":       "success",
            "label_type":   label_type,
            "number":       counters[label_type],
            "printed_text": custom_text,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/set_printer', methods=['POST'])
def set_printer():
    """Override the Bluetooth MAC address at runtime (persists until restart)."""
    global PRINTER_BT_ADDRESS
    data               = request.json
    PRINTER_BT_ADDRESS = data.get('address', '').strip() or None
    return jsonify({"status": "success", "address": PRINTER_BT_ADDRESS})


@app.route('/set_tape_width', methods=['POST'])
def set_tape_width():
    """Change tape width at runtime. Valid values: 3.5, 6, 9, 12, 18, 24 (mm)."""
    global TAPE_WIDTH_MM
    data  = request.json
    width = data.get('width')
    if width not in TAPE_PRINT_WIDTHS:
        return jsonify({
            "status":  "error",
            "message": f"Unsupported tape width. Choose from: {list(TAPE_PRINT_WIDTHS.keys())}",
        }), 400
    TAPE_WIDTH_MM = width
    return jsonify({
        "status":          "success",
        "tape_width_mm":   TAPE_WIDTH_MM,
        "print_width_dots": get_print_width(),
    })


@app.route('/scan_printers', methods=['GET'])
def scan_printers():
    """Scan for nearby Bluetooth devices. Useful for finding the printer MAC."""
    try:
        devices = bluetooth.discover_devices(lookup_names=True, duration=8)
        return jsonify({
            "status":  "success",
            "devices": [{"address": a, "name": n} for a, n in devices],
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
