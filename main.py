#!/usr/bin/python
# -*- coding:utf-8 -*-

import sys
import os
import json
import time
import logging
import threading
from PIL import Image, ImageDraw, ImageFont
import paho.mqtt.client as mqtt

# -------------------- PATH SETUP --------------------
picdir = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'pic/2in13')
fontdir = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'pic')
libdir = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'lib')
if os.path.exists(libdir):
    sys.path.append(libdir)

from TP_lib import gt1151
from TP_lib import epd2in13_V4

# -------------------- LOGGING --------------------
logging.basicConfig(level=logging.INFO)
flag_t = 1

# -------------------- LOAD .env --------------------
def load_env(filepath=".env"):
    config = {}
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
    return config

env = load_env(".env")
MQTT_BROKER = env["MQTT_BROKER"]
MQTT_PORT   = int(env["MQTT_PORT"])
MQTT_USER   = env["MQTT_USER"]
MQTT_PASS   = env["MQTT_PASS"]

# -------------------- LOAD buttons.json --------------------
def load_buttons(filepath):
    try:
        with open(filepath) as f:
            data = json.load(f)
        return [
            (b["label"], b["topic"], b["x1"], b["y1"], b["x2"], b["y2"])
            for b in data["buttons"]
        ]
    except FileNotFoundError:
        logging.warning(f"{filepath} not found, returning empty button list.")
        return []

BUTTONS_PAGE1 = load_buttons("buttons.json")
BUTTONS_PAGE2 = load_buttons("buttons2.json")

# -------------------- LOAD status_widgets.json --------------------
# Format: [{"label": "Solar Today", "mqtt_topic": "sensor/solar_today", "unit": "kWh"}, ...]
def load_widgets(filepath="status_widgets.json"):
    try:
        with open(filepath) as f:
            data = json.load(f)
        return data.get("widgets", [])
    except FileNotFoundError:
        logging.warning("status_widgets.json not found, using defaults.")
        return [
            {"label": "Solar Today",  "mqtt_topic": "sensor/solar_today",  "unit": "kWh"},
            {"label": "Solar Power",  "mqtt_topic": "sensor/solar_power",  "unit": "W"},
            {"label": "Grid Import",  "mqtt_topic": "sensor/grid_import",  "unit": "kWh"},
            {"label": "Home Usage",   "mqtt_topic": "sensor/home_usage",   "unit": "W"},
        ]

WIDGETS = load_widgets()

# Widget state: topic -> current value string
widget_values = {w["mqtt_topic"]: "—" for w in WIDGETS}

# -------------------- SCREEN STATE --------------------
# 0 = buttons page 1, 1 = buttons page 2, 2 = HA status
current_screen = 0
TOTAL_SCREENS  = 3

# -------------------- MQTT --------------------
def on_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload.decode("utf-8", errors="replace").strip()
    if topic in widget_values:
        widget_values[topic] = payload
        logging.info(f"Widget update: {topic} = {payload}")
        # Redraw if we're on the status screen
        if current_screen == 2:
            draw_status_screen()

mqttc = mqtt.Client(client_id="epaper_dashboard")
mqttc.username_pw_set(MQTT_USER, MQTT_PASS)
mqttc.on_message = on_message
mqttc.connect(MQTT_BROKER, MQTT_PORT, 60)

# Subscribe to all widget topics
for w in WIDGETS:
    mqttc.subscribe(w["mqtt_topic"])
    logging.info(f"Subscribed to {w['mqtt_topic']}")

mqttc.loop_start()

# -------------------- TOUCH THREAD --------------------
def pthread_irq():
    global flag_t
    while flag_t == 1:
        if gt.digital_read(gt.INT) == 0:
            GT_Dev.Touch = 1
        else:
            GT_Dev.Touch = 0

# -------------------- ARROW GEOMETRY --------------------
# Touch sensor reports coords in portrait space internally.
# Measured touch centers (from user testing):
#   Left  arrow (top-left  physical button): touch x=115, y=0
#   Right arrow (top-right physical button): touch x=30,  y=10
# Hitbox is ±20px around each center.
ARROW_HIT = 20   # half-size of the square hitbox around touch center

# Touch centers in raw sensor coordinates (measured on device)
ARROW_LEFT_TX,  ARROW_LEFT_TY  = 115, 0    # top-left physical button
ARROW_RIGHT_TX, ARROW_RIGHT_TY = 30,  10   # top-right physical button

# For drawing we map the arrows to the display corners.
# Display is landscape 250×122; arrows drawn at the right edge, top and bottom corners.
# Left  arrow (prev) → drawn at bottom-right of display (high Y in portrait = right side)
# Right arrow (next) → drawn at top-right    of display (low  Y in portrait = left side)
ARROW_DRAW_SIZE = 28   # px square for the drawn arrow box

def arrow_left_draw(W, H):
    """Draw position for the 'previous' arrow — bottom-right corner of display."""
    return (W - ARROW_DRAW_SIZE, H - ARROW_DRAW_SIZE, W, H)

def arrow_right_draw(W, H):
    """Draw position for the 'next' arrow — top-right corner of display."""
    return (W - ARROW_DRAW_SIZE, 0, W, ARROW_DRAW_SIZE)

def arrow_left_hit(tx, ty):
    """Return True if raw touch (tx,ty) hits the left/prev arrow."""
    return (abs(tx - ARROW_LEFT_TX)  <= ARROW_HIT and
            abs(ty - ARROW_LEFT_TY)  <= ARROW_HIT)

def arrow_right_hit(tx, ty):
    """Return True if raw touch (tx,ty) hits the right/next arrow."""
    return (abs(tx - ARROW_RIGHT_TX) <= ARROW_HIT and
            abs(ty - ARROW_RIGHT_TY) <= ARROW_HIT)

def draw_arrow_left(draw, W, H, font_small):
    x1, y1, x2, y2 = arrow_left_draw(W, H)
    draw.rectangle((x1, y1, x2, y2), outline=0, fill=255)
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    # Left-pointing triangle
    pts = [(cx + 7, cy - 7), (cx - 7, cy), (cx + 7, cy + 7)]
    draw.polygon(pts, fill=0)

def draw_arrow_right(draw, W, H, font_small):
    x1, y1, x2, y2 = arrow_right_draw(W, H)
    draw.rectangle((x1, y1, x2, y2), outline=0, fill=255)
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    # Right-pointing triangle
    pts = [(cx - 7, cy - 7), (cx + 7, cy), (cx - 7, cy + 7)]
    draw.polygon(pts, fill=0)

def draw_page_indicator(draw, W, H, font_small):
    """Tiny dots along the right edge between the two arrows."""
    dot_r = 3
    spacing = 10
    # Place dots vertically centred on the right edge between the arrows
    x = W - ARROW_DRAW_SIZE // 2
    total_h = (TOTAL_SCREENS - 1) * spacing
    start_y = H // 2 - total_h // 2
    for i in range(TOTAL_SCREENS):
        cy = start_y + i * spacing
        if i == current_screen:
            draw.ellipse((x - dot_r, cy - dot_r, x + dot_r, cy + dot_r), fill=0)
        else:
            draw.ellipse((x - dot_r, cy - dot_r, x + dot_r, cy + dot_r), outline=0, fill=255)

# -------------------- DRAW SCREENS --------------------
def draw_button_screen(buttons):
    """Draw a grid of buttons + navigation arrows on the right edge."""
    W, H = epd.height, epd.width
    draw.rectangle((0, 0, W, H), fill=255)
    # Buttons live in the left portion; right ARROW_DRAW_SIZE+2 px reserved for arrows
    for label, topic, x1, y1, x2, y2 in buttons:
        # Clamp button right edge away from arrow strip
        x2c = min(x2, W - ARROW_DRAW_SIZE - 2)
        draw.rectangle((x1, y1, x2c, y2), outline=0)
        draw.text((x1 + 5, y1 + 5), label, font=font, fill=0)
    draw_arrow_left(draw, W, H, font_small)
    draw_arrow_right(draw, W, H, font_small)
    draw_page_indicator(draw, W, H, font_small)
    epd.display(epd.getbuffer(image))

def draw_status_screen():
    """Draw Home Assistant widget values."""
    W, H = epd.height, epd.width
    draw.rectangle((0, 0, W, H), fill=255)

    # Title bar
    draw.rectangle((0, 0, W, 18), fill=0)
    draw.text((4, 1), "Home Assistant", font=font_small, fill=255)

    # Draw up to 4 widgets in a 2×2 grid; right ARROW_DRAW_SIZE+2 px reserved for arrows
    usable_w = W - ARROW_DRAW_SIZE - 2
    col_w = usable_w // 2
    row_h = (H - 18 - ARROW_DRAW_SIZE) // 2
    for idx, w in enumerate(WIDGETS[:4]):
        col = idx % 2
        row = idx // 2
        bx = col * col_w
        by = 18 + row * row_h
        # Box border
        draw.rectangle((bx + 2, by + 2, bx + col_w - 2, by + row_h - 2), outline=0)
        # Label (small)
        draw.text((bx + 6, by + 4), w["label"], font=font_small, fill=0)
        # Value (large)
        val = widget_values.get(w["mqtt_topic"], "—")
        unit = w.get("unit", "")
        draw.text((bx + 6, by + 16), f"{val} {unit}", font=font, fill=0)

    draw_arrow_left(draw, W, H, font_small)
    draw_arrow_right(draw, W, H, font_small)
    draw_page_indicator(draw, W, H, font_small)
    epd.display(epd.getbuffer(image))

def draw_current_screen():
    if current_screen == 0:
        draw_button_screen(BUTTONS_PAGE1)
    elif current_screen == 1:
        draw_button_screen(BUTTONS_PAGE2)
    elif current_screen == 2:
        draw_status_screen()

# -------------------- MAIN --------------------
try:
    logging.info("Starting ePaper dashboard")

    epd = epd2in13_V4.EPD()
    gt  = gt1151.GT1151()
    GT_Dev = gt1151.GT_Development()
    GT_Old = gt1151.GT_Development()

    epd.init(epd.FULL_UPDATE)
    gt.GT_Init()
    epd.Clear(0xFF)

    t = threading.Thread(target=pthread_irq)
    t.daemon = True
    t.start()

    W, H = epd.height, epd.width   # landscape: 250 x 122

    font       = ImageFont.truetype(os.path.join(fontdir, 'Font.ttc'), 18)
    font_small = ImageFont.truetype(os.path.join(fontdir, 'Font.ttc'), 12)

    image = Image.new('1', (W, H), 255)
    draw  = ImageDraw.Draw(image)

    epd.displayPartBaseImage(epd.getbuffer(image))
    epd.init(epd.PART_UPDATE)

    draw_current_screen()

    # -------------------- MAIN LOOP --------------------
    while True:
        gt.GT_Scan(GT_Dev, GT_Old)

        if (GT_Old.X[0] == GT_Dev.X[0] and
            GT_Old.Y[0] == GT_Dev.Y[0] and
            GT_Old.S[0] == GT_Dev.S[0]):
            continue

        if GT_Dev.TouchpointFlag:
            GT_Dev.TouchpointFlag = 0

            x = GT_Dev.X[0]
            y = GT_Dev.Y[0]

            logging.info(f"Touch: {x},{y}  screen={current_screen}")

            # ---- Check arrow navigation first (raw touch coords) ----
            if arrow_left_hit(x, y):
                current_screen = (current_screen - 1) % TOTAL_SCREENS
                logging.info(f"← prev screen {current_screen}")
                draw_current_screen()
                time.sleep(0.3)
                continue

            if arrow_right_hit(x, y):
                current_screen = (current_screen + 1) % TOTAL_SCREENS
                logging.info(f"→ next screen {current_screen}")
                draw_current_screen()
                time.sleep(0.3)
                continue

            # ---- Check buttons on current screen ----
            if current_screen == 0:
                buttons = BUTTONS_PAGE1
            elif current_screen == 1:
                buttons = BUTTONS_PAGE2
            else:
                buttons = []

            for label, topic, x1, y1, x2, y2 in buttons:
                if x1 < x < x2 and y1 < y < y2:
                    logging.info(f"{label} PRESSED → {topic}")
                    mqttc.publish(topic, "PRESSED")
                    break

            time.sleep(0.3)

        time.sleep(0.05)

# -------------------- CLEAN EXIT --------------------
except KeyboardInterrupt:
    logging.info("Exiting...")
    flag_t = 0
    mqttc.loop_stop()
    mqttc.disconnect()
    epd.sleep()
    time.sleep(1)
    t.join()
    epd.Dev_exit()
    exit()
