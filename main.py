#!/usr/bin/python
# -*- coding:utf-8 -*-

import sys
import os
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

# -------------------- MQTT --------------------
MQTT_BROKER = "HOMEASSISTANT_IP_ADDRESS"
MQTT_PORT = 1883
MQTT_USER = "YOUR_USERNAME"
MQTT_PASS = "YOUR_PASSWORD"

mqttc = mqtt.Client(client_id="epaper_dashboard")
mqttc.username_pw_set(MQTT_USER, MQTT_PASS)
mqttc.connect(MQTT_BROKER, MQTT_PORT, 60)
mqttc.loop_start()

# -------------------- TOUCH THREAD --------------------
def pthread_irq():
    global flag_t
    while flag_t == 1:
        if gt.digital_read(gt.INT) == 0:
            GT_Dev.Touch = 1
        else:
            GT_Dev.Touch = 0

# -------------------- BUTTONS --------------------
# Single source of truth: all button positions defined here.
# draw_buttons() and touch detection both use this list.
BUTTONS = [
    ("BUTTON_A", "homeassistant/epaper/button1",  10,  10, 120,  60),
    ("BUTTON_B", "homeassistant/epaper/button2", 130,  10, 240,  60),
    ("BUTTON_C", "homeassistant/epaper/button3",  10,  70, 120, 120),
    ("BUTTON_D", "homeassistant/epaper/button4", 130,  70, 240, 120),
]

# -------------------- MAIN --------------------
try:
    logging.info("Starting ePaper dashboard")

    epd = epd2in13_V4.EPD()
    gt = gt1151.GT1151()
    GT_Dev = gt1151.GT_Development()
    GT_Old = gt1151.GT_Development()

    epd.init(epd.FULL_UPDATE)
    gt.GT_Init()
    epd.Clear(0xFF)

    # start touch thread
    t = threading.Thread(target=pthread_irq)
    t.daemon = True
    t.start()

    # fonts
    font = ImageFont.truetype(os.path.join(fontdir, 'Font.ttc'), 18)

    # create blank screen
    image = Image.new('1', (epd.height, epd.width), 255)
    draw = ImageDraw.Draw(image)

    epd.displayPartBaseImage(epd.getbuffer(image))
    epd.init(epd.PART_UPDATE)

    # -------------------- DRAW BUTTONS --------------------
    def draw_buttons():
        draw.rectangle((0, 0, epd.height, epd.width), fill=255)

        for label, topic, x1, y1, x2, y2 in BUTTONS:
            draw.rectangle((x1, y1, x2, y2), outline=0)
            draw.text((x1 + 10, y1 + 20), label, font=font, fill=0)

        epd.display(epd.getbuffer(image))

    draw_buttons()

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

            logging.info(f"Touch: {x},{y}")

            # ---------------- BUTTON DETECTION ----------------
            # Checks touch coords against the same BUTTONS list used for drawing.
            for label, topic, x1, y1, x2, y2 in BUTTONS:
                if x1 < x < x2 and y1 < y < y2:
                    logging.info(f"{label} PRESSED")
                    mqttc.publish(topic, "PRESSED")
                    break

            time.sleep(0.3)

        time.sleep(0.05)

# -------------------- CLEAN EXIT --------------------
except KeyboardInterrupt:
    logging.info("Exiting...")
    flag_t = 0
    mqttc.loop_stop()       # stop the MQTT background thread
    mqttc.disconnect()      # cleanly disconnect from broker
    epd.sleep()
    time.sleep(1)
    t.join()
    epd.Dev_exit()
    exit()