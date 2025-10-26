# voice_service.py — Vosk STT + Gemini NLU + eSpeak-NG TTS
import os, json, re, base64, queue, threading, soundfile as sf
import paho.mqtt.client as mqtt
import google.generativeai as genai
from vosk import Model, KaldiRecognizer
import subprocess, tempfile
import config_mqtt as cfg

# ===== INIT =====
genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
model_path = "/home/pi/models/vosk-vi"
vosk_model = Model(model_path)
rec = KaldiRecognizer(vosk_model, 16000)
audio_q = queue.Queue()

# ===== MQTT setup =====
client = mqtt.Client(client_id="voice_service", protocol=mqtt.MQTTv311)
client.username_pw_set(cfg.USER, getattr(cfg, "PASSWORD", getattr(cfg, "PASS", "")))
client.connect(cfg.BROKER, cfg.PORT, getattr(cfg, "KEEPALIVE", 60))

TOPIC_AUDIO_UP = cfg.TOPIC_AUDIO_UP      # ESP32-UI → Pi (base64 PCM)
TOPIC_TTS_TEXT = cfg.TOPIC_TTS_TEXT      # Pi ← text từ các service khác
TOPIC_TTS_AUDIO = cfg.TOPIC_TTS_AUDIO    # Pi → ESP32-UI (base64 PCM TTS)
TOPIC_CMD       = cfg.TOPIC_CMD          # publish điều khiển thiết bị

# ====== STT worker ======
def stt_worker():
    while True:
        pcm = audio_q.get()
        if rec.AcceptWaveform(pcm):
            res = json.loads(rec.Result())
            text = res.get("text", "").strip()
            if text:
                print("STT:", text)
                handle_text(text)

# ====== NLU ======
def handle_text(text):
    """Gửi text sang Gemini NLU và xử lý kết quả"""
    try:
        prompt = f"""
Hiểu lệnh tiếng Việt cho hệ thống nhà thông minh.
Các thiết bị có thể điều khiển:
- Đèn 1 (den1): ["đèn 1", "den 1", "den1", "đèn một", "đèn đầu"]
- Đèn 2 (den2): ["đèn 2", "den 2", "den2", "đèn hai"]
- Quạt 1 (quat1): ["quạt 1", "quat 1", "quat1", "quạt một"]
- Quạt 2 (quat2): ["quạt 2", "quat 2", "quat2", "quạt hai"]
- Tất cả (tatca): bật/tắt toàn bộ thiết bị.

Trả về JSON đúng cấu trúc:
{{"intent":"DEVICE_CONTROL|STATUS_QUERY|UNKNOWN",
  "device":"den1|den2|quat1|quat2|tatca|null",
  "action":"ON|OFF|QUERY|null",
  "confidence":0.0}}

Lệnh người dùng: {text}
"""
        resp = genai.GenerativeModel("models/gemini-2.5-flash").generate_content(prompt)
        raw = resp.text.strip()
        raw = re.sub(r"^```json", "", raw)
        raw = re.sub(r"^```", "", raw)
        raw = re.sub(r"```$", "", raw).strip()

        data = json.loads(raw)
        print("NLU:", data)

        if data.get("intent") == "DEVICE_CONTROL" and data.get("device"):
            client.publish(TOPIC_CMD, json.dumps(data), qos=1)
            tts_say(f"Đã {data.get('action','')} {data.get('device','')}")
        else:
            tts_say("Không hiểu lệnh.")
    except Exception as e:
        print("NLU error:", e)
        tts_say("Lỗi hiểu lệnh.")

# ====== TTS ======
def tts_say(text):
    print("TTS:", text)
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
            subprocess.run(["espeak-ng", "-v", "vi", "-s", "140", text, "--stdout"], stdout=tmp)
            tmp.seek(0)
            data, rate = sf.read(tmp.name, dtype="int16")
            pcm_bytes = data.tobytes()
            b64 = base64.b64encode(pcm_bytes).decode("ascii")
            client.publish(TOPIC_TTS_AUDIO, b64, qos=1)
    except Exception as e:
        print("TTS error:", e)

# ===== MQTT callbacks =====
def on_connect(c, u, f, rc):
    print("MQTT connected:", rc)
    c.subscribe([(TOPIC_AUDIO_UP, 1), (TOPIC_TTS_TEXT, 1)])

def on_message(c, u, msg):
    if msg.topic == TOPIC_AUDIO_UP:
        try:
            pcm = base64.b64decode(msg.payload)
            audio_q.put(pcm)
        except Exception as e:
            print("Decode error:", e)
    elif msg.topic == TOPIC_TTS_TEXT:
        try:
            data = json.loads(msg.payload)
            tts_say(data.get("text",""))
        except Exception as e:
            print("TTS text error:", e)

client.on_connect = on_connect
client.on_message = on_message

# ===== START SERVICE =====
threading.Thread(target=stt_worker, daemon=True).start()
client.loop_forever()
