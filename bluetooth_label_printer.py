"""
bluetooth_label_printer.py
Brother PT-D610BT Bluetooth Label Printer Service

Mirrors the Flask API of whatnot_live_label_writer.py but targets the
PT-D610BT over Bluetooth SPP using the P-Touch raster protocol.

Supports two Bluetooth connection modes (set BT_MODE in config):
  - 'rfcomm'  : pyserial on /dev/rfcomm0  (requires: rfcomm bind 0 <MAC>)
  - 'socket'  : PyBluez BluetoothSocket   (connects directly by MAC)

Protocol reference:
  - stecman gist: Brother PT-P300BT Bluetooth driver (Python)
  - treideme/brother_pt: P-Touch raster command reference
"""

import datetime
import json
import os
import re
import struct

import packbits
import serial
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from PIL import Image, ImageDraw, ImageFont
from barcode import Code128
from barcode.writer import ImageWriter

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Bluetooth settings
BT_MODE = os.environ.get("BT_MODE", "rfcomm")  # 'rfcomm' or 'socket'
BT_DEVICE = os.environ.get("BT_DEVICE", "/dev/rfcomm0")   # rfcomm mode
BT_MAC = os.environ.get("BT_MAC", "")                      # socket mode: e.g. "AA:BB:CC:DD:EE:FF"
BT_PORT = int(os.environ.get("BT_PORT", "1"))              # RFCOMM channel (socket mode)

# Tape settings
TAPE_WIDTH_MM = int(os.environ.get("TAPE_WIDTH_MM", "12"))

# Label / counter settings
COUNTER_FILE = os.environ.get("COUNTER_FILE", "counters.json")

# ---------------------------------------------------------------------------
# Tape geometry (P-Touch print head = 128 pixels across)
# ---------------------------------------------------------------------------

# Maps tape width (mm) → (print_pixels, margin_bits_each_side)
# 128 total bits per raster line; margin fills the unused head positions.
_TAPE_SPECS = {
    4:  (24,  52),
    6:  (32,  48),
    9:  (50,  39),
    12: (70,  29),
    18: (112,  8),
    24: (128,  0),
}

HEAD_PIXELS = 128  # full print head width in pixels


def tape_print_width(mm: int) -> int:
    """Return usable print pixels for a given tape width."""
    return _TAPE_SPECS[mm][0]


def tape_margin(mm: int) -> int:
    """Return margin bits on each side for a given tape width."""
    return _TAPE_SPECS[mm][1]


# ---------------------------------------------------------------------------
# Counter persistence (shared with whatnot_live_label_writer.py)
# ---------------------------------------------------------------------------

def load_counters() -> dict:
    if os.path.exists(COUNTER_FILE):
        with open(COUNTER_FILE, "r") as f:
            return json.load(f)
    return {}


def save_counters(counters: dict) -> None:
    with open(COUNTER_FILE, "w") as f:
        json.dump(counters, f)


counters = load_counters()

# ---------------------------------------------------------------------------
# Barcode / text helpers (ported from whatnot_live_label_writer.py)
# ---------------------------------------------------------------------------

def generate_barcode_data(label_type: str, number: int) -> str:
    """Return a 14-char Code128 payload: MMDDYYYY + 2-digit type code + 4-digit number."""
    today = datetime.datetime.now().strftime("%m%d%Y")
    type_code = f"{hash(label_type) % 100:02d}"
    padded_number = f"{number:04d}"
    return f"{today}{type_code}{padded_number}"


def draw_wrapped_text(draw, text, font, x, y, max_width, fill="black", callback=None):
    words = text.split(" ")
    lines = []
    current_line = words[0]
    for word in words[1:]:
        test_line = f"{current_line} {word}"
        if font.getlength(test_line) <= max_width:
            current_line = test_line
        else:
            lines.append(current_line)
            current_line = word
    lines.append(current_line)

    line_height = font.getbbox(lines[0])[3] + 10
    for i, line in enumerate(lines):
        draw.text((x, y + i * line_height), line, font=font, fill=fill)
    if callback:
        callback(y + len(lines) * line_height)


def crop_white_space(image: Image.Image) -> Image.Image:
    gray = image.convert("L")
    pixels = gray.load()
    w, h = gray.size
    bottom = h - 1
    while bottom >= 0:
        if any(pixels[x, bottom] < 255 for x in range(w)):
            break
        bottom -= 1
    return image.crop((0, 0, w, bottom + 1))


# ---------------------------------------------------------------------------
# Label rendering for TZe tape
# ---------------------------------------------------------------------------

def render_label_for_tape(
    label_type: str,
    number: int,
    tape_width_mm: int,
    custom_text: str = None,
) -> Image.Image:
    """
    Build a 1-bit PIL image sized for the tape.

    The label is composed vertically (tall image) then rotated 90° CW so
    the long axis feeds along the tape.  The final image dimensions are:
        width  = label length (raster lines) — variable
        height = print pixels (70 for 12mm, 112 for 18mm, …)

    Returns a mode-'1' PIL Image ready for raster conversion.
    """
    print_px = tape_print_width(tape_width_mm)

    # --- pick font size that fits tape width ---
    font_size = max(10, int(print_px * 0.55))
    try:
        font = ImageFont.truetype("arial.ttf", font_size)
        bold_font = ImageFont.truetype("arialbd.ttf", font_size)
    except IOError:
        font = ImageFont.load_default()
        bold_font = font

    today_str = datetime.datetime.now().strftime("%m/%d/%Y")
    barcode_data = generate_barcode_data(label_type, number)

    # --- generate barcode PNG then load it ---
    bc = Code128(barcode_data, writer=ImageWriter())
    bc_path_base = "/tmp/bt_barcode"
    bc.save(bc_path_base)
    bc_image = Image.open(bc_path_base + ".png").convert("L")

    # Scale barcode so its height fits within print_px
    bc_target_h = max(20, int(print_px * 0.55))
    bc_scale = bc_target_h / bc_image.height
    bc_new_w = int(bc_image.width * bc_scale)
    bc_image = bc_image.resize((bc_new_w, bc_target_h), Image.LANCZOS)
    bc_image = bc_image.convert("1")

    # --- calculate overall label height (vertical layout before rotation) ---
    text_to_render = custom_text if custom_text else f"{label_type} #{number}"
    # Estimate text block height
    text_bbox_h = font.getbbox(text_to_render)[3]
    date_bbox_h = font.getbbox(today_str)[3]
    padding = 6

    label_h_pre_rot = (
        padding
        + text_bbox_h + padding
        + date_bbox_h + padding
        + bc_image.height + padding
    )
    label_w_pre_rot = print_px

    # --- draw vertical label ---
    img = Image.new("L", (label_w_pre_rot, label_h_pre_rot), 255)
    draw = ImageDraw.Draw(img)

    y = padding

    # Text line
    final_y = [y + text_bbox_h]

    def _cb(ny):
        final_y[0] = ny

    draw_wrapped_text(draw, text_to_render, font, 0, y, label_w_pre_rot, callback=_cb)
    y = final_y[0] + padding

    # Date line
    date_x = (label_w_pre_rot - int(font.getlength(today_str))) // 2
    draw.text((date_x, y), today_str, font=font, fill="black")
    y += date_bbox_h + padding

    # Barcode
    bc_x = (label_w_pre_rot - bc_image.width) // 2
    img.paste(bc_image, (bc_x, y))

    img = crop_white_space(img)

    # --- rotate 90° CW so label feeds along tape ---
    # After rotation: width = label length (along tape), height = print_px
    img = img.rotate(-90, expand=True)

    # Threshold to strict 1-bit
    img = img.convert("L").point(lambda p: 0 if p < 128 else 255)
    img = img.convert("1")

    return img


# ---------------------------------------------------------------------------
# Raster conversion
# ---------------------------------------------------------------------------

class RasterConverter:
    """
    Convert a mode-'1' PIL Image to a list of P-Touch raster line bytes.

    Image orientation expected:
        width  = number of raster lines (label length along tape)
        height = HEAD_PIXELS (128) — with the tape's print area centred

    Each raster line = 16 bytes (128 bits), PackBits compressed.
    Command byte:
        0x47 + length(2B LE) + <packbits data>  for non-blank lines
        0x5A                                     for all-zero lines
    """

    def convert(self, image: Image.Image, tape_mm: int) -> list:
        """Return list[bytes], one entry per raster line."""
        margin = tape_margin(tape_mm)
        print_px = tape_print_width(tape_mm)

        if image.mode != "1":
            image = image.convert("1")

        # Pad image height to exactly print_px if needed
        if image.height != print_px:
            padded = Image.new("1", (image.width, print_px), 1)  # white
            offset = (print_px - image.height) // 2
            padded.paste(image, (0, max(0, offset)))
            image = padded

        lines = []
        for col in range(image.width):
            line_bits = [0] * HEAD_PIXELS  # 0 = white, 1 = black

            for row in range(print_px):
                px = image.getpixel((col, row))
                # In mode '1': 0 = black, 255 = white
                bit = 0 if px else 1
                line_bits[margin + row] = bit

            line_bytes = self._pack_bits_to_bytes(line_bits)
            lines.append(self._encode_line(line_bytes))

        return lines

    @staticmethod
    def _pack_bits_to_bytes(bits: list) -> bytes:
        """Pack 128 bits (MSB first) into 16 bytes."""
        assert len(bits) == HEAD_PIXELS
        result = bytearray(16)
        for i, bit in enumerate(bits):
            if bit:
                result[i // 8] |= 1 << (7 - (i % 8))
        return bytes(result)

    @staticmethod
    def _encode_line(line: bytes) -> bytes:
        """Return raster command bytes for one 16-byte line."""
        if line == b"\x00" * 16:
            return b"\x5A"
        compressed = packbits.encode(line)
        return b"\x47" + struct.pack("<H", len(compressed)) + compressed


# ---------------------------------------------------------------------------
# Bluetooth printer
# ---------------------------------------------------------------------------

class BluetoothPrinter:
    """
    Manages the Bluetooth SPP connection and P-Touch protocol.

    Two connection modes:
        'rfcomm'  — pyserial on a bound rfcomm device (e.g. /dev/rfcomm0)
        'socket'  — PyBluez RFCOMM socket, connects directly by MAC address
    """

    def __init__(self, mode=BT_MODE, device=BT_DEVICE, mac=BT_MAC, port=BT_PORT):
        self.mode = mode
        self.device = device
        self.mac = mac
        self.port = port
        self._conn = None  # serial.Serial or bluetooth socket

    # -- connection management ------------------------------------------

    def connect(self):
        if self.mode == "rfcomm":
            self._conn = serial.Serial(
                self.device,
                baudrate=9600,
                stopbits=serial.STOPBITS_ONE,
                parity=serial.PARITY_NONE,
                bytesize=8,
                dsrdtr=True,
                timeout=10,
            )
        elif self.mode == "socket":
            try:
                import bluetooth
            except ImportError as exc:
                raise RuntimeError(
                    "PyBluez is required for socket mode: pip install pybluez"
                ) from exc
            sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
            sock.connect((self.mac, self.port))
            self._conn = sock
        else:
            raise ValueError(f"Unknown BT_MODE '{self.mode}'. Use 'rfcomm' or 'socket'.")

    def disconnect(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()

    # -- low-level I/O ---------------------------------------------------

    def _write(self, data: bytes):
        if self.mode == "rfcomm":
            self._conn.write(data)
        else:
            # PyBluez socket — send in chunks
            chunk = 1024
            for i in range(0, len(data), chunk):
                self._conn.send(data[i : i + chunk])

    def _read(self, n: int) -> bytes:
        if self.mode == "rfcomm":
            return self._conn.read(n)
        else:
            buf = b""
            while len(buf) < n:
                buf += self._conn.recv(n - len(buf))
            return buf

    # -- protocol --------------------------------------------------------

    def _initialize(self):
        """Send invalidate + initialize + mode-setup sequence."""
        self._write(b"\x00" * 100)          # invalidate
        self._write(b"\x1b\x40")             # initialize
        self._write(b"\x1b\x69\x61\x01")    # enter dynamic command mode
        self._write(b"\x1b\x69\x21\x00")    # enable status notification

    def _request_status(self) -> bytes:
        """Ask for printer status and return raw 32-byte response."""
        self._write(b"\x1b\x69\x53")
        return self._read(32)

    def _send_print_info(self, raster_line_count: int, tape_mm: int):
        """Inform printer of media width and expected page length."""
        # \x1b\x69\x7a flags: 0x84 = raster mode, media type = 0x00 (auto)
        cmd = (
            b"\x1b\x69\x7a"
            b"\x84\x00"
            + struct.pack("<B", tape_mm)        # media width mm
            + b"\x00"
            + struct.pack("<I", raster_line_count)  # page count (# raster lines)
            + b"\x00\x00"
        )
        self._write(cmd)

    def _set_modes(self):
        self._write(b"\x4d\x02")             # compression: PackBits
        self._write(b"\x1b\x69\x4d\x40")    # print mode: auto-cut
        self._write(b"\x1b\x69\x4b\x08")    # advanced mode: no chain print

    def _print_with_feed(self):
        self._write(b"\x1a")

    # -- public print API ------------------------------------------------

    def print_label(self, image: Image.Image, tape_mm: int, debug: bool = False):
        """
        Convert image to raster and print.

        Args:
            image:    mode-'1' PIL Image (width=label length, height=print_px)
            tape_mm:  loaded tape width in mm (must be a key in _TAPE_SPECS)
            debug:    if True, dump raw raster bytes to stdout instead of printing
        """
        converter = RasterConverter()
        raster_lines = converter.convert(image, tape_mm)
        raster_payload = b"".join(raster_lines)

        if debug:
            print(f"[DEBUG] {len(raster_lines)} raster lines, "
                  f"{len(raster_payload)} bytes payload")
            return

        self._initialize()
        status = self._request_status()
        print(f"[BT] Printer status ({len(status)}B): {status.hex()}")

        self._send_print_info(len(raster_lines), tape_mm)
        self._set_modes()
        self._write(raster_payload)
        self._print_with_feed()
        print("[BT] Print command sent.")


# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------

app = Flask(__name__)
CORS(app)

MOST_RECENT_LABEL_TYPE = None


@app.route("/print", methods=["POST"])
def handle_print():
    global MOST_RECENT_LABEL_TYPE
    data = request.json or {}
    label_type = data.get("label_type", MOST_RECENT_LABEL_TYPE or "Item").strip()
    MOST_RECENT_LABEL_TYPE = label_type

    if label_type not in counters:
        counters[label_type] = 0
    counters[label_type] += 1
    save_counters(counters)

    image = render_label_for_tape(label_type, counters[label_type], TAPE_WIDTH_MM)

    try:
        with BluetoothPrinter() as printer:
            printer.print_label(image, TAPE_WIDTH_MM)
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500

    return jsonify({
        "status": "success",
        "label_type": label_type,
        "number": counters[label_type],
    })


@app.route("/print_custom", methods=["POST"])
def handle_custom_print():
    data = request.json or {}
    custom_text = data.get("text", "").strip()

    if not custom_text:
        return jsonify({"status": "error", "message": "No text provided"}), 400

    label_type = "Custom"
    if label_type not in counters:
        counters[label_type] = 0
    counters[label_type] += 1
    save_counters(counters)

    number_match = re.search(r"#(\d+)", custom_text)
    item_number = int(number_match.group(1)) if number_match else counters[label_type]

    image = render_label_for_tape(
        label_type, item_number, TAPE_WIDTH_MM, custom_text=custom_text
    )

    try:
        with BluetoothPrinter() as printer:
            printer.print_label(image, TAPE_WIDTH_MM)
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500

    return jsonify({
        "status": "success",
        "label_type": label_type,
        "number": counters[label_type],
        "printed_text": custom_text,
    })


@app.route("/clear_counters", methods=["POST"])
def clear_counters():
    data = request.json or {}
    label_type = data.get("label_type", "all").strip().lower()

    if label_type == "all":
        counters.clear()
    elif label_type in counters:
        del counters[label_type]
    else:
        return jsonify({
            "status": "error",
            "message": f"Label type '{label_type}' not found",
        }), 404

    save_counters(counters)
    return jsonify({"status": "success", "cleared": label_type, "counters": counters})


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Brother PT-D610BT Bluetooth label printer service")
    parser.add_argument("--port", type=int, default=5001, help="Flask port (default 5001)")
    parser.add_argument("--tape", type=int, default=TAPE_WIDTH_MM,
                        help=f"Tape width mm (default {TAPE_WIDTH_MM})")
    parser.add_argument("--bt-mode", choices=["rfcomm", "socket"], default=BT_MODE,
                        help="Bluetooth connection mode")
    parser.add_argument("--bt-device", default=BT_DEVICE,
                        help="rfcomm device path (rfcomm mode)")
    parser.add_argument("--bt-mac", default=BT_MAC,
                        help="Printer Bluetooth MAC address (socket mode)")
    parser.add_argument("--debug-raster", action="store_true",
                        help="Dump raster bytes to stdout instead of printing")
    args = parser.parse_args()

    TAPE_WIDTH_MM = args.tape
    BT_MODE = args.bt_mode
    BT_DEVICE = args.bt_device
    BT_MAC = args.bt_mac

    if args.debug_raster:
        # Quick smoke-test: render one label and show raster stats without connecting
        img = render_label_for_tape("Test", 1, TAPE_WIDTH_MM)
        converter = RasterConverter()
        lines = converter.convert(img, TAPE_WIDTH_MM)
        print(f"Raster lines: {len(lines)}")
        print(f"Total bytes:  {sum(len(l) for l in lines)}")
        print(f"Image size:   {img.size}")
        print(f"Tape width:   {TAPE_WIDTH_MM}mm ({tape_print_width(TAPE_WIDTH_MM)}px print area)")
    else:
        print(f"Starting PT-D610BT printer service on port {args.port}")
        print(f"  Tape:     {TAPE_WIDTH_MM}mm")
        print(f"  BT mode:  {BT_MODE}")
        if BT_MODE == "rfcomm":
            print(f"  Device:   {BT_DEVICE}")
        else:
            print(f"  MAC:      {BT_MAC}")
        app.run(host="0.0.0.0", port=args.port)
