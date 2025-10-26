import os
import json
import time
import signal
import threading
import RPi.GPIO as GPIO
import paho.mqtt.client as mqtt
import config_mqtt as cfg

DEBUG = True

def _log(*a):
    if DEBUG:
        print(*a, flush=True)

# ===== MQTT từ config_mqtt =====
BROKER    = cfg.BROKER
PORT      = cfg.PORT
KEEPALIVE = cfg.KEEPALIVE
USER      = cfg.USER
PASSWORD  = cfg.PASSWORD

TOPIC_CMD       = cfg.TOPIC_CMD
TOPIC_STATE     = cfg.TOPIC_STATE
TOPIC_TTS_TEXT  = cfg.TOPIC_TTS_TEXT
AVAIL_TOPIC     = "devices/relay/availability"

# ===== GPIO cấu hình =====
PIN_MODE = "BOARD"   # hoặc "BCM"
if PIN_MODE.upper() == "BOARD":
    GPIO.setmode(GPIO.BOARD)
    DEVICES = {
        "den1":  {"pin": 29, "active_high": False, "default": "OFF"},
        "den2":  {"pin": 31, "active_high": False, "default": "OFF"},
        "quat1": {"pin": 33, "active_high": False, "default": "OFF"},
        "quat2": {"pin": 35, "active_high": False, "default": "OFF"},
    }
else:
    GPIO.setmode(GPIO.BCM)
    DEVICES = {
        "den1":  {"pin": 5,  "active_high": False, "default": "OFF"},
        "den2":  {"pin": 6,  "active_high": False, "default": "OFF"},
        "quat1": {"pin": 13, "active_high": False, "default": "OFF"},
        "quat2": {"pin": 19, "active_high": False, "default": "OFF"},
    }

ALIASES = {
    "den1":  ["đèn 1", "den 1", "den1", "đèn một", "đèn đầu"],
    "den2":  ["đèn 2", "den 2", "den2", "đèn hai"],
    "quat1": ["quạt 1", "quat 1", "quat1", "quạt một"],
    "quat2": ["quạt 2", "quat 2", "quat2", "quạt hai"],
}
SCENES = {
    "all_on":  [("den1", "ON"), ("den2", "ON"), ("quat1", "ON"), ("quat2", "ON")],
    "all_off": [("den1", "OFF"), ("den2", "OFF"), ("quat1", "OFF"), ("quat2", "OFF")],
}

# ===== GPIO init =====
def _safe_init_level(d):
    if d["default"] == "OFF":
        return GPIO.LOW if d["active_high"] else GPIO.HIGH
    return GPIO.HIGH if d["active_high"] else GPIO.LOW

for name, d in DEVICES.items():
    GPIO.setup(d["pin"], GPIO.OUT, initial=_safe_init_level(d))
_log("GPIO init done.")

def _set_device(name, action):
    d = DEVICES[name]
    on = (action == "ON")
    lvl = (GPIO.HIGH if d["active_high"] else GPIO.LOW) if on else (GPIO.LOW if d["active_high"] else GPIO.HIGH)
    GPIO.output(d["pin"], lvl)
    _log(f"{name} -> {action} | pin={d['pin']} lvl={'HIGH' if lvl==GPIO.HIGH else 'LOW'}")

def _is_on(name):
    d = DEVICES[name]
    lvl = GPIO.input(d["pin"])
    return (lvl == GPIO.HIGH) if d["active_high"] else (lvl == GPIO.LOW)

def _pub_state(cli, name, action):
    payload = {"device": name, "state": action, "ts": int(time.time())}
    cli.publish(TOPIC_STATE, json.dumps(payload), qos=1, retain=True)

# ===== MQTT callbacks =====
def _resolve_device(name):
    if not name:
        return None
    low = str(name).strip().lower()
    for k, vs in ALIASES.items():
        if low in vs:
            return k
    if low in ("all", "tatca", "tất cả", "tat ca"):
        return "ALL"
    return name if name in DEVICES else None

def _parse_duration(val):
    if not val:
        return 0
    s = str(val).lower().strip()
    try:
        if s.endswith("ms"):
            return int(float(s[:-2]))
        if s.endswith("s"):
            return int(float(s[:-1]) * 1000)
        if s.endswith("m"):
            return int(float(s[:-1]) * 60_000)
        if s.endswith("h"):
            return int(float(s[:-1]) * 3_600_000)
        return int(float(s) * 1000)
    except:
        return 0

def on_connect(cli, ud, flags, rc):
    print("MQTT connected:", rc)
    cli.subscribe([(TOPIC_CMD, 1)])  # Đảm bảo topic điều khiển được lắng nghe
    cli.publish(AVAIL_TOPIC, "online", qos=1, retain=True)
    for name in DEVICES:
        _pub_state(cli, name, "ON" if _is_on(name) else "OFF")

def on_message(cli, ud, msg):
    try:
        data = json.loads(msg.payload.decode())
        intent = (data.get("intent") or "").lower()
        if intent != "switch" and "action" not in data:
            _log("Unhandled:", data)
            return
        target = _resolve_device(data.get("device"))
        act = (data.get("action") or "").upper()
        dur_ms = _parse_duration(data.get("duration"))
        if target == "ALL":
            pairs = SCENES["all_on"] if act == "ON" else SCENES["all_off"]
            for n, a in pairs:
                _set_device(n, a)
                _pub_state(cli, n, a)
            return
        if target not in DEVICES or act not in ("ON", "OFF", "TOGGLE"):
            _log(f"Invalid command: device={data.get('device')}, action={act}")
            return
        if act == "TOGGLE":
            act = "OFF" if _is_on(target) else "ON"
        _set_device(target, act)
        _pub_state(cli, target, act)
        if dur_ms > 0 and act == "ON":
            threading.Timer(dur_ms / 1000, lambda: _set_device(target, "OFF")).start()
    except Exception as e:
        print("Error processing message:", e)

# ===== MQTT client setup =====
client = mqtt.Client(client_id="relay_service", protocol=mqtt.MQTTv311)
if USER:
    client.username_pw_set(USER, PASSWORD)
client.will_set(AVAIL_TOPIC, "offline", qos=1, retain=True)
client.on_connect = on_connect
client.on_message = on_message
client.connect(BROKER, PORT, KEEPALIVE)

def cleanup(*_):
    try:
        client.publish(AVAIL_TOPIC, "offline", qos=1, retain=True)
    except:
        pass
    try:
        client.loop_stop()
        client.disconnect()
    except:
        pass
    GPIO.cleanup()
    os._exit(0)

import signal
signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

client.loop_start()
while True:
    time.sleep(1)
