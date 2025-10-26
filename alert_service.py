# alert_service.py — Cảnh báo khí (Vout) + RFID xâm nhập
import time, json, ssl, smtplib
from email.message import EmailMessage
import paho.mqtt.client as mqtt
import config_alert as cfg

# ===== STATE =====
last_over_ts   = {}
alert_active   = {}
last_email_ts  = 0
rfid_hist      = {}
rfid_last_mail = 0

# ===== EMAIL =====
def send_email(subject: str, body: str, kind: str = "gas"):
    """Gửi email, tách cooldown riêng giữa cảnh báo khí và RFID"""
    global last_email_ts, rfid_last_mail
    now = time.time()
    if kind == "gas" and now - last_email_ts < cfg.EMAIL_COOLDOWN_S:
        return
    if kind == "rfid" and now - rfid_last_mail < cfg.RFID_EMAIL_COOLDOWN_S:
        return

    msg = EmailMessage()
    msg["From"] = cfg.SMTP_USER
    msg["To"]   = cfg.MAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(cfg.SMTP_HOST, cfg.SMTP_PORT, context=ctx) as s:
        s.login(cfg.SMTP_USER, cfg.SMTP_PASS)
        s.send_message(msg)

    if kind == "gas":  last_email_ts = now
    else:              rfid_last_mail = now

# ===== MQTT HELPERS =====
def say(text: str):
    client.publish(cfg.TOPIC_TTS, json.dumps({"text": text}), qos=1)

def actuate(device: str, action: str):
    payload = json.dumps({"device": device, "action": action})
    client.publish(cfg.TOPIC_CMD, payload, qos=1)

def publish_alert(state: str, topic: str, detail: dict):
    payload = {"state": state, "topic": topic, **detail, "ts": int(time.time())}
    client.publish(cfg.TOPIC_ALERT, json.dumps(payload), qos=1, retain=True)

# ===== EXTRACT Vout =====
def extract_vout(data: dict, topic: str):
    if not isinstance(data, dict): return None
    for k in ("volt","voltage","Vout","vout","V","v"):
        if k in data and isinstance(data[k], (int,float)): return float(data[k])
    if topic.endswith("gas_mq5") and isinstance(data.get("V_MQ5"), (int,float)):
        return float(data["V_MQ5"])
    if topic.endswith("gas_mics5524") and isinstance(data.get("V_MICS"), (int,float)):
        return float(data["V_MICS"])
    adc = data.get("adc")
    if isinstance(adc, dict):
        for k in ("v","volt","V"):
            if k in adc and isinstance(adc[k], (int,float)): return float(adc[k])
    return None

# ===== GAS LOGIC (Vout thresholds) =====
def handle_gas(topic: str, data: dict):
    v = extract_vout(data, topic)
    if v is None: return
    thr = cfg.THRESH_V.get(topic)
    if thr is None: return
    key = topic

    # **Đối với MQ-5**: Vout sẽ tăng khi có khí
    if topic == "environment/gas_mq5":
        if v >= thr:  # Nếu Vout >= ngưỡng, bật cảnh báo
            if key not in last_over_ts: last_over_ts[key] = time.time()
            if time.time() - last_over_ts[key] >= cfg.DEBOUNCE_SEC:
                if not alert_active.get(key, False):
                    alert_active[key] = True
                    publish_alert("ALARM", topic, {"vout": round(v,3)})
                    say("Cảnh báo! Nồng độ khí vượt ngưỡng an toàn.")
                    actuate("buzzer1", "ON")
                    actuate("fan1", "ON")
                    send_email(
                        "[SmartAccess] Cảnh báo khí độc",
                        f"Topic: {topic}\nVout={v:.3f} V >= {thr:.3f} V\nThời điểm: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                        kind="gas"
                    )
        else:  # Nếu Vout thấp hơn ngưỡng, chuyển về SAFE
            last_over_ts.pop(key, None)
            if alert_active.get(key, False) and v <= thr * cfg.HYSTERESIS_PCT:
                alert_active[key] = False
                publish_alert("SAFE", topic, {"vout": round(v,3)})
                say("Mức khí đã trở lại an toàn.")
                actuate("buzzer1", "OFF")
    
    # **Đối với MiCS-5524**: Vout sẽ giảm khi có khí
    if topic == "environment/gas_mics5524":
        if v <= thr:  # Nếu Vout <= ngưỡng, bật cảnh báo
            if key not in last_over_ts: last_over_ts[key] = time.time()
            if time.time() - last_over_ts[key] >= cfg.DEBOUNCE_SEC:
                if not alert_active.get(key, False):
                    alert_active[key] = True
                    publish_alert("ALARM", topic, {"vout": round(v,3)})
                    say("Cảnh báo! Nồng độ khí vượt ngưỡng an toàn.")
                    actuate("buzzer1", "ON")
                    actuate("fan1", "ON")
                    send_email(
                        "[SmartAccess] Cảnh báo khí độc",
                        f"Topic: {topic}\nVout={v:.3f} V <= {thr:.3f} V\nThời điểm: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                        kind="gas"
                    )
        else:  # Nếu Vout lớn hơn ngưỡng, chuyển về SAFE
            last_over_ts.pop(key, None)
            if alert_active.get(key, False) and v >= thr * cfg.HYSTERESIS_PCT:
                alert_active[key] = False
                publish_alert("SAFE", topic, {"vout": round(v,3)})
                say("Mức khí đã trở lại an toàn.")
                actuate("buzzer1", "OFF")

# ===== RFID LOGIC =====
def handle_rfid(topic: str, data: dict):
    status = str(data.get("status","")).lower()
    if status != "denied": return
    device = data.get("device") or "gate"
    uid = data.get("uid", "unknown")
    now = int(data.get("ts") or time.time())

    hist = rfid_hist.setdefault(device, [])
    hist.append(now)
    cutoff = now - cfg.RFID_FAIL_WINDOW_S
    hist[:] = [t for t in hist if t >= cutoff]

    if len(hist) >= cfg.RFID_FAIL_THRESHOLD:
        publish_alert("INTRUSION", topic, {"device": device, "fails": len(hist), "uid": uid})
        say("Cảnh báo an ninh. Có người cố gắng mở cửa trái phép.")
        actuate("buzzer1", "ON")
        send_email(
            "[SmartAccess] Cảnh báo xâm nhập: RFID bị từ chối nhiều lần",
            f"Thiết bị: {device}\nUID cuối: {uid}\n"
            f"Số lần bị từ chối trong {cfg.RFID_FAIL_WINDOW_S}s: {len(hist)} "
            f"(ngưỡng: {cfg.RFID_FAIL_THRESHOLD})\n"
            f"Thời điểm: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            kind="rfid"
        )
        hist.clear()

# ===== MQTT CALLBACKS =====
def on_connect(c, udata, flags, rc):
    if rc == 0:
        for t in cfg.TOPICS_IN: c.subscribe(t, qos=1)
        c.subscribe(cfg.TOPIC_RFID_RESULT, qos=1)
        print("MQTT connected & subscribed.")
    else:
        print("MQTT connect failed:", rc)

def on_message(c, udata, msg):
    try:
        data = json.loads(msg.payload.decode("utf-8"))
    except Exception:
        return
    if msg.topic in cfg.TOPICS_IN:
        handle_gas(msg.topic, data)
    elif msg.topic == cfg.TOPIC_RFID_RESULT:
        handle_rfid(msg.topic, data)

# ===== MAIN =====
client = mqtt.Client(client_id="alert_service", protocol=mqtt.MQTTv311)
client.username_pw_set(cfg.USER, cfg.PASSWORD)
client.on_connect = on_connect
client.on_message = on_message
client.connect(cfg.BROKER, cfg.PORT, cfg.KEEPALIVE)

if __name__ == "__main__":
    print("Starting alert_service...")
    client.loop_forever()
