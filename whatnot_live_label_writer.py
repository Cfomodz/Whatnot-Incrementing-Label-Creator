from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from brother_ql.conversion import convert
from brother_ql.backends.helpers import send
from brother_ql.raster import BrotherQLRaster
import datetime
import json
import os
from PIL import Image, ImageDraw, ImageFont
from barcode import Code128
from barcode.writer import ImageWriter
import re  # Add this at the top of the file
import zlib

# Optional templates: the repo ships without these modules; only the
# 'logo' / 'coupon' template types need them. Default + custom labels
# (everything the coin orchestrator uses) work without.
try:
    from templates.logo_template import generate_logo_label
except ImportError:
    generate_logo_label = None
try:
    from templates.coupon_template import generate_coupon_label
except ImportError:
    generate_coupon_label = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
CORS(app)  # Enable CORS

# Persistent counter storage
COUNTER_FILE = 'counters.json'

# Printer settings (override in .env / environment)
PRINTER_IP = os.getenv('PRINTER_IP', '10.0.0.13')
PRINTER_MODEL = os.getenv('PRINTER_MODEL', 'QL-700')  # QL-710W uses the QL-700 driver
LABEL_SIZE = os.getenv('LABEL_SIZE', '62')  # 62mm continuous roll

# Add a variable to track the most recent label type
MOST_RECENT_LABEL_TYPE = None

TEMPLATES = {
    'logo': {
        'image': 'logo.png',
        'height': 200,
        'include_text': True,
        'text_size': 30
    },
    'coupon': {
        'code_font_size': 40,
        'height': 300
    },
    'default': {
        'height': 280 + 173
    }
}

def load_counters():
    if os.path.exists(COUNTER_FILE):
        with open(COUNTER_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_counters(counters):
    with open(COUNTER_FILE, 'w') as f:
        json.dump(counters, f)

counters = load_counters()

def generate_barcode_data(label_type, number, cert=None):
    if cert:
        # A coin label's barcode should BE the cert number — stable, and a
        # scan of the label looks up the exact coin.
        return str(cert)
    today = datetime.datetime.now().strftime("%m%d%Y")  # MMDDYYYY format
    # crc32, not hash(): Python randomizes str hash() per process, so the
    # old type codes changed on every restart and barcodes were unstable.
    type_code = f"{zlib.crc32(label_type.encode()) % 100:02d}"  # 2-digit type code
    padded_number = f"{number:04d}"  # 4-digit number with leading zeros
    return f"{today}{type_code}{padded_number}"

def draw_wrapped_text(draw, text, font, x, y, max_width, fill="black", callback=None):
    words = text.split(' ')
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
    
    line_height = font.getbbox(lines[0])[3] + 10  # Add 10px padding between lines
    total_height = len(lines) * line_height
    
    for i, line in enumerate(lines):
        draw.text((x, y + (i * line_height)), line, font=font, fill=fill)
    
    if callback:
        callback(y + total_height)  # Call the callback with final y position

def crop_white_space(image):
    """Crop the image to remove extra white space at the bottom"""
    # Convert to grayscale
    grayscale = image.convert('L')
    # Get the pixel data
    pixels = grayscale.load()
    width, height = grayscale.size
    
    # Find the first row from the bottom that has non-white pixels
    bottom = height - 1
    while bottom >= 0:
        # Check if any pixel in this row is not white
        if any(pixels[x, bottom] < 255 for x in range(width)):
            break
        bottom -= 1
    
    # Crop the image
    return image.crop((0, 0, width, bottom + 1))

def print_label(label_type, number, template='default', custom_text=None):
    template_settings = TEMPLATES.get(template, TEMPLATES['default'])
    image_width = 696
    image_height = template_settings['height']
    
    if custom_text:
        # Handle custom text printing
        qlr = BrotherQLRaster(PRINTER_MODEL)
        
        try:
            font = ImageFont.truetype("arial.ttf", 60)
        except IOError:
            font = ImageFont.load_default()

        # Calculate required height based on text
        lines = custom_text.split('\n')
        line_height = font.getbbox(lines[0])[3] + 5
        image_height = len(lines) * line_height + 20
        
        image = Image.new('1', (image_width, image_height), 1)
        draw = ImageDraw.Draw(image)
        
        # Draw each line of text
        y = 10
        for line in lines:
            draw_wrapped_text(draw, line, font, 10, y, image_width - 20)
            y += line_height
            
        # Save and print
        today = datetime.datetime.now().strftime("%m%d%Y")
        image.save(f"custom_label_{today}.png")
        
        instructions = convert(
            qlr=qlr,
            images=[image],
            label=LABEL_SIZE,
            cut=True,
            margin=0,  # Remove extra margin
            rotate='0',  # Keep orientation as is
        )
        
        try:
            send(
                instructions=instructions,
                printer_identifier=f'tcp://{PRINTER_IP}',
                blocking=True
            )
        except Exception as e:
            print(f"Print error: {e}")
        return
    
    if template == 'logo':
        if generate_logo_label is None:
            raise RuntimeError("logo template requested but templates/logo_template.py is not present")
        image = generate_logo_label(label_type, number, template_settings, image_width, image_height)
    elif template == 'coupon':
        if generate_coupon_label is None:
            raise RuntimeError("coupon template requested but templates/coupon_template.py is not present")
        image, coupon_code = generate_coupon_label(label_type, number, template_settings, image_width, image_height)
        # Here you would store the coupon code in your database
    else:
        # Default template (original code)
        today = datetime.datetime.now().strftime("%m/%d/%Y")
        barcode_data = generate_barcode_data(label_type, number)
        barcode = Code128(barcode_data, writer=ImageWriter())
        barcode_image_path = "barcode.png"
        barcode.save(barcode_image_path.split('.')[0])
        # Load the barcode image
        barcode_image = Image.open(barcode_image_path)

        qlr = BrotherQLRaster(PRINTER_MODEL)
        
        try:
            font = ImageFont.truetype("arial.ttf", 60)
            bold_font = ImageFont.truetype("arialbd.ttf", 60)
        except IOError:
            font = ImageFont.load_default()
            bold_font = ImageFont.load_default()

        main_text = label_type
        emphasized_text = f" # {number}"
        image_width = 696  # Fixed width for QL-700
        
        # Calculate image height based on barcode and text
        # image_height = max(173, barcode_height + 50)  # Ensure minimum height and space for barcode
        image_height = 280 + 173
        background_color = 1

        image = Image.new('1', (image_width, image_height), background_color)
        draw = ImageDraw.Draw(image)
        
        # Set initial text positions
        text_x = (image_width - font.getlength(main_text + emphasized_text)) // 2
        text_y = 0

        # Draw the main part of the product name
        draw.text((text_x, text_y), main_text, font=font, fill="black")

        # Calculate the width of the main text to position the emphasized text
        main_text_width = font.getlength(main_text)

        # Draw the emphasized part of the product name
        draw.text((text_x + main_text_width, text_y), emphasized_text, font=bold_font, fill="black")

        # Calculate date position
        date_text = today
        date_x = (image_width - font.getlength(date_text)) // 2
        date_y = text_y + max(font.getbbox(main_text)[3], bold_font.getbbox(emphasized_text)[3]) + 10

        # Draw the date
        draw.text((date_x, date_y), date_text, font=font, fill="black")

        # Calculate barcode position
        barcode_x = (image_width - barcode_image.width) // 2
        barcode_y = date_y + font.getbbox(date_text)[3] + 10

        # Paste the barcode image onto the label
        image.paste(barcode_image, (barcode_x, barcode_y))

    # Save and print
    today = datetime.datetime.now().strftime("%m/%d/%Y")
    date_string = today.replace('/','')
    image.save(f"{label_type}_{date_string}_product_label_{number}.png")
    
    qlr = BrotherQLRaster(PRINTER_MODEL)
    instructions = convert(
        qlr=qlr,
        images=[image],
        label=LABEL_SIZE,
        cut=True,
        margin=0,  # Remove extra margin
        rotate='0',  # Keep orientation as is
    )

    try:
        send(
            instructions=instructions,
            printer_identifier=f'tcp://{PRINTER_IP}',
            blocking=True
        )
    except Exception as e:
        print(f"Print error: {e}")

@app.route('/print', methods=['POST'])
def handle_print():
    global MOST_RECENT_LABEL_TYPE
    print("Received request:", request.json)  # Log the request
    data = request.json
    label_type = data.get('label_type', MOST_RECENT_LABEL_TYPE or 'Coin').strip()
    
    # Update the most recent label type
    MOST_RECENT_LABEL_TYPE = label_type
    
    if label_type not in counters:
        counters[label_type] = 0
    counters[label_type] += 1
    save_counters(counters)
    
    print_label(label_type, counters[label_type])
    return jsonify({
        "status": "success",
        "label_type": label_type,
        "number": counters[label_type]
    })

@app.route('/clear_counters', methods=['POST'])
def clear_counters():
    data = request.json
    label_type = data.get('label_type', 'all').strip().lower()
    
    if label_type == 'all':
        counters.clear()
    elif label_type in counters:
        del counters[label_type]
    else:
        return jsonify({
            "status": "error",
            "message": f"Label type '{label_type}' not found"
        }), 404
    
    save_counters(counters)
    return jsonify({
        "status": "success",
        "cleared": label_type,
        "counters": counters
    })

@app.route('/print_custom', methods=['POST'])
def handle_custom_print():
    data = request.json
    custom_text = data.get('text', '').strip()
    
    # Double the text for testing
    custom_text = f"{custom_text}"
    
    if not custom_text:
        return jsonify({
            "status": "error",
            "message": "No text provided"
        }), 400
        
    # Use "Custom" as the label type and get next number
    label_type = "Custom"
    if label_type not in counters:
        counters[label_type] = 0
    counters[label_type] += 1
    save_counters(counters)
    
    # Check for number pattern using regex
    number_match = re.search(r'#(\d+)', custom_text)
    if number_match:
        item_number = int(number_match.group(1))
        has_emphasis = True
        number_pattern = f"#{item_number}"
    else:
        # Use the counter if no number is found in the text
        item_number = counters[label_type]
        has_emphasis = False

    # If the text carries a cert number ("... | Cert 34464855 ..."), the
    # barcode becomes the cert itself — scan the label, identify the coin.
    cert_match = re.search(r"[Cc]ert\.?\s*#?\s*(\d{6,})", custom_text)
    barcode_data = generate_barcode_data(label_type, item_number,
                                         cert=cert_match.group(1) if cert_match else None)
    print(f"barcode_data: {barcode_data}")
    # Create label with same structure as regular print
    qlr = BrotherQLRaster(PRINTER_MODEL)
    image_width = 696  # Fixed width for QL-700
    
    try:
        font = ImageFont.truetype("arial.ttf", 60)
        bold_font = ImageFont.truetype("arialbd.ttf", 60)
    except IOError:
        font = ImageFont.load_default()
        bold_font = ImageFont.load_default()

    # Create a test image to calculate text dimensions
    test_image = Image.new('1', (image_width, 1000), 1)
    test_draw = ImageDraw.Draw(test_image)
    
    # Calculate actual text height using draw_wrapped_text
    y_pos = 0
    def wrapped_text_callback(current_y):
        nonlocal y_pos
        y_pos = current_y  # Update y_pos with each line's position
    
    # Draw text and track position
    draw_wrapped_text(test_draw, custom_text, font, 10, y_pos, image_width - 20, callback=wrapped_text_callback)
    text_height = y_pos  # draw_wrapped_text updates y_pos internally
    
    # Add space for date and barcode
    date_height = font.getbbox("MM/DD/YYYY")[3] + 5
    barcode_height = 173  # Standard barcode height
    image_height = text_height + date_height + barcode_height + 100 # Extra padding
    
    # Create actual image with initial height
    image = Image.new('1', (image_width, image_height), 1)
    draw = ImageDraw.Draw(image)
    
    # Draw custom text and get final y position
    y = 0
    final_y = y  # Initialize final_y
    
    if has_emphasis:
        # Split text into main text, number, and after text
        parts = custom_text.split(number_pattern)
        main_text = parts[0]
        after_text = parts[1] if len(parts) > 1 else ""
        
        # Draw wrapped main text
        y = 0
        final_y = y
        def wrapped_callback(current_y):
            nonlocal final_y
            final_y = current_y
        
        # Draw wrapped main text
        draw_wrapped_text(draw, main_text, font, 10, y, image_width - 20, callback=wrapped_callback)
        
        # Calculate remaining width after main text
        remaining_width = image_width - 20 - font.getlength(main_text)
        
        # Calculate total width needed for number and after text
        total_after_width = font.getlength(number_pattern + after_text)
        
        if remaining_width >= total_after_width:
            # Draw number and after text on same line
            draw.text((10 + font.getlength(main_text), final_y - font.getbbox(main_text)[3]), 
                    number_pattern, font=bold_font, fill="black")
            draw.text((10 + font.getlength(main_text + number_pattern), final_y - font.getbbox(main_text)[3]), 
                    after_text, font=font, fill="black")
        elif remaining_width >= font.getlength(number_pattern):
            # Draw number on same line, wrap after text
            draw.text((10 + font.getlength(main_text), final_y - font.getbbox(main_text)[3]), 
                    number_pattern, font=bold_font, fill="black")
            draw_wrapped_text(draw, after_text, font, 10, final_y, 
                            image_width - 20, callback=wrapped_callback)
        else:
            # Draw number and after text on new line
            draw.text((10, final_y), number_pattern, font=bold_font, fill="black")
            draw_wrapped_text(draw, after_text, font, 10 + font.getlength(number_pattern), 
                            final_y, image_width - 20, callback=wrapped_callback)
            final_y += bold_font.getbbox(number_pattern)[3]
    else:
        # Regular text drawing
        def wrapped_text_callback(current_y):
            nonlocal final_y
            final_y = current_y
        
        draw_wrapped_text(draw, custom_text, font, 10, y, image_width - 20, callback=wrapped_text_callback)

    # Add date with proper spacing
    today = datetime.datetime.now().strftime("%m/%d/%Y")
    date_x = (image_width - font.getlength(today)) // 2
    date_y = final_y + 10  # Position after text with padding
    draw.text((date_x, date_y), today, font=font, fill="black")
    
    # Generate barcode
    barcode = Code128(barcode_data, writer=ImageWriter())
    barcode_image_path = "barcode.png"
    barcode.save(barcode_image_path.split('.')[0])
    barcode_image = Image.open(barcode_image_path)
    
    # Position barcode
    barcode_x = (image_width - barcode_image.width) // 2
    barcode_y = date_y + date_height + 10  # Position after date with padding
    image.paste(barcode_image, (barcode_x, barcode_y))
    # Crop the image to remove extra white space at the bottom
    image = crop_white_space(image)
    
    # Save and print
    today = datetime.datetime.now().strftime("%m%d%Y")
    image.save(f"custom_label_{today}.png")
    
    instructions = convert(
        qlr=qlr,
        images=[image],
        label=LABEL_SIZE,
        cut=True,
        margin=0,  # Remove extra margin
    )
    
    try:
        send(
            instructions=instructions,
            printer_identifier=f'tcp://{PRINTER_IP}',
            blocking=True
        )
    except Exception as e:
        print(f"Print error: {e}")
        return jsonify({
            "status": "error",
            "message": f"Print error: {e}"
        }), 500
        
    return jsonify({
        "status": "success",
        "label_type": label_type,
        "number": counters[label_type],
        "printed_text": custom_text,
        "label_height": image_height
    })

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)