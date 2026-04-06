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

# -------------------- LOAD BUTTONS --------------------
# JSON only contains "label" and "topic" — no coordinates.
# Touch regions and visual positions are hardcoded separately below.
def load_buttons(filepath):
    try:
        with open(filepath) as f:
            data = json.load(f)
        return [(b["label"], b["topic"]) for b in data["buttons"]]
    except FileNotFoundError:
        logging.warning(f"{filepath} not found, returning empty list.")
        return []

BUTTONS_PAGE1 = load_buttons("buttons.json")   # up to 4 entries -> slots 0-3
BUTTONS_PAGE2 = load_buttons("buttons2.json")  # up to 4 entries -> slots 0-3

# -------------------- HARDCODED TOUCH REGIONS --------------------
# Raw touch sensor coordinates, exactly from the original working code.
# Format: (x1, x2, y1, y2)
#   Slot 0 -> Button 1  (x: 20-60,  y: 160-200)  bottom-left
#   Slot 1 -> Button 2  (x: 20-60,  y:  60-100)  top-left
#   Slot 2 -> Button 3  (x: 80-120, y: 160-200)  bottom-right
#   Slot 3 -> Button 4  (x: 80-120, y:  60-100)  top-right
TOUCH_REGIONS = [
    (20,  60, 160, 200),
    (20,  60,  60, 100),
    (80, 120, 160, 200),
    (80, 120,  60, 100),
]

def button_hit(slot, tx, ty):
    x1, x2, y1, y2 = TOUCH_REGIONS[slot]
    return x1 < tx < x2 and y1 < ty < y2

# -------------------- HARDCODED ARROW TOUCH REGIONS --------------------
# Physical corner buttons used for screen navigation.
#   Prev screen (top-left  button): center x=115, y=0
#   Next screen (top-right button): center x=30,  y=10
ARROW_HIT              = 20
ARROW_PREV_TX, ARROW_PREV_TY = 115,  0
ARROW_NEXT_TX, ARROW_NEXT_TY =  30, 10

def arrow_prev_hit(tx, ty):
    return abs(tx - ARROW_PREV_TX) <= ARROW_HIT and abs(ty - ARROW_PREV_TY) <= ARROW_HIT

def arrow_next_hit(tx, ty):
    return abs(tx - ARROW_NEXT_TX) <= ARROW_HIT and abs(ty - ARROW_NEXT_TY) <= ARROW_HIT

# -------------------- HARDCODED VISUAL POSITIONS --------------------
# Display pixel coords for the 4 button boxes — independent of touch coords.
# 2 columns x 2 rows; right ~30px reserved for the arrow strip.
BTN_W      = 100
BTN_H      =  45
BTN_MARGIN =   6
BTN_GAP    =   8

VISUAL_POSITIONS = [
    # (x1, y1, x2, y2) in display pixels
    (BTN_MARGIN,                           52, BTN_MARGIN + BTN_W,                           52 + BTN_H),  # slot 0
    (BTN_MARGIN,                            4, BTN_MARGIN + BTN_W,                            4 + BTN_H),  # slot 1
    (BTN_MARGIN + BTN_W + BTN_GAP,         52, BTN_MARGIN + BTN_W + BTN_GAP + BTN_W,         52 + BTN_H),  # slot 2
    (BTN_MARGIN + BTN_W + BTN_GAP,          4, BTN_MARGIN + BTN_W + BTN_GAP + BTN_W,          4 + BTN_H),  # slot 3
]

# -------------------- HARDCODED ARROW VISUAL POSITIONS --------------------
ARROW_DRAW_SIZE = 28

def arrow_prev_draw(W, H):
    """Bottom-right corner of display."""
    return (W - ARROW_DRAW_SIZE, H - ARROW_DRAW_SIZE, W, H)

def arrow_next_draw(W, H):
    """Top-right corner of display."""
    return (W - ARROW_DRAW_SIZE, 0, W, ARROW_DRAW_SIZE)

# -------------------- LOAD WIDGETS --------------------
def load_widgets(filepath="status_widgets.json"):
    try:
        with open(filepath) as f:
            data = json.load(f)
        return data.get("widgets", [])
    except FileNotFoundError:
        logging.warning("status_widgets.json not found, using defaults.")
        return [
            {"label": "Solar Today", "mqtt_topic": "sensor/solar_today", "unit": "kWh"},
            {"label": "Solar Power",  "mqtt_topic": "sensor/solar_power",  "unit": "W"},
            {"label": "Grid Import",  "mqtt_topic": "sensor/grid_import",  "unit": "kWh"},
            {"label": "Home Usage",   "mqtt_topic": "sensor/home_usage",   "unit": "W"},
        ]

WIDGETS = load_widgets()
widget_values = {w["mqtt_topic"]: "—" for w in WIDGETS}

# -------------------- SCREEN STATE --------------------
current_screen = 0
TOTAL_SCREENS  = 3   # 0=page1 buttons, 1=page2 buttons, 2=HA status

# -------------------- MQTT --------------------
def on_message(client, userdata, msg):
    topic   = msg.topic
    payload = msg.payload.decode("utf-8", errors="replace").strip()
    if topic in widget_values:
        widget_values[topic] = payload
        logging.info(f"Widget update: {topic} = {payload}")
        if current_screen == 2:
            draw_status_screen()

mqttc = mqtt.Client(client_id="epaper_dashboard")
mqttc.username_pw_set(MQTT_USER, MQTT_PASS)
mqttc.on_message = on_message
mqttc.connect(MQTT_BROKER, MQTT_PORT, 60)

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

# -------------------- DRAW HELPERS --------------------
def draw_arrows(W, H):
    # Prev arrow
    x1, y1, x2, y2 = arrow_prev_draw(W, H)
    draw.rectangle((x1, y1, x2, y2), outline=0, fill=255)
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    draw.polygon([(cx + 7, cy - 7), (cx - 7, cy), (cx + 7, cy + 7)], fill=0)

    # Next arrow
    x1, y1, x2, y2 = arrow_next_draw(W, H)
    draw.rectangle((x1, y1, x2, y2), outline=0, fill=255)
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    draw.polygon([(cx - 7, cy - 7), (cx + 7, cy), (cx - 7, cy + 7)], fill=0)

    # Page indicator dots between arrows
    dot_r, spacing = 3, 10
    x      = W - ARROW_DRAW_SIZE // 2
    start_y = H // 2 - ((TOTAL_SCREENS - 1) * spacing) // 2
    for i in range(TOTAL_SCREENS):
        cy = start_y + i * spacing
        if i == current_screen:
            draw.ellipse((x - dot_r, cy - dot_r, x + dot_r, cy + dot_r), fill=0)
        else:
            draw.ellipse((x - dot_r, cy - dot_r, x + dot_r, cy + dot_r), outline=0, fill=255)

# -------------------- DRAW SCREENS --------------------
def draw_button_screen(buttons):
    W, H = epd.height, epd.width
    draw.rectangle((0, 0, W, H), fill=255)
    for slot, (label, topic) in enumerate(buttons[:4]):
        x1, y1, x2, y2 = VISUAL_POSITIONS[slot]
        draw.rectangle((x1, y1, x2, y2), outline=0)
        draw.text((x1 + 5, y1 + 5), label, font=font, fill=0)
    draw_arrows(W, H)
    epd.display(epd.getbuffer(image))

def draw_status_screen():
    W, H = epd.height, epd.width
    draw.rectangle((0, 0, W, H), fill=255)

    draw.rectangle((0, 0, W, 18), fill=0)
    draw.text((4, 1), "Home Assistant", font=font_small, fill=255)

    usable_w = W - ARROW_DRAW_SIZE - 2
    col_w    = usable_w // 2
    row_h    = (H - 18) // 2
    for idx, w in enumerate(WIDGETS[:4]):
        col = idx % 2
        row = idx // 2
        bx  = col * col_w
        by  = 18 + row * row_h
        draw.rectangle((bx + 2, by + 2, bx + col_w - 2, by + row_h - 2), outline=0)
        draw.text((bx + 6, by + 4),  w["label"], font=font_small, fill=0)
        val  = widget_values.get(w["mqtt_topic"], "—")
        unit = w.get("unit", "")
        draw.text((bx + 6, by + 16), f"{val} {unit}", font=font, fill=0)

    draw_arrows(W, H)
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

    epd    = epd2in13_V4.EPD()
    gt     = gt1151.GT1151()
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

            # ---- Arrow navigation ----
            if arrow_prev_hit(x, y):
                current_screen = (current_screen - 1) % TOTAL_SCREENS
                logging.info(f"← prev -> screen {current_screen}")
                draw_current_screen()
                time.sleep(0.3)
                continue

            if arrow_next_hit(x, y):
                current_screen = (current_screen + 1) % TOTAL_SCREENS
                logging.info(f"-> next -> screen {current_screen}")
                draw_current_screen()
                time.sleep(0.3)
                continue

            # ---- Button slots ----
            if current_screen in (0, 1):
                buttons = BUTTONS_PAGE1 if current_screen == 0 else BUTTONS_PAGE2
                for slot, (label, topic) in enumerate(buttons[:4]):
                    if button_hit(slot, x, y):
                        logging.info(f"Slot {slot} '{label}' PRESSED -> {topic}")
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
