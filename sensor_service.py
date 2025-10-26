# ~/venvs/iot/sensor_service.py — DHT retry + EMA + đúng divider_mode MQ5/MiCS + Auto-learn R0 MQ-5
import os, time, math, json, sys
from statistics import median
import adafruit_dht, board, busio
from adafruit_ads1x15 import ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn
import paho.mqtt.client as mqtt

# ---- MQTT topics / config ----
try:
    import config_mqtt as cfg
except Exception:
    class cfg:
        BROKER="192.168.137.2"; PORT=1883; USER="kubbies03"; PASSWORD="1"; KEEPALIVE=60
        TOPIC_ENV_TEMP="environment/temperature"; TOPIC_ENV_HUM="environment/humidity"
        TOPIC_ENV_GAS1="environment/gas_mq5"; TOPIC_ENV_GAS2="environment/gas_mics5524"
        PUBLISH_INTERVAL=20  # đặt mặc định 20s

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CALIB_MICS = os.path.join(BASE_DIR, "calib_mics5524.json")
CALIB_MQ5  = os.path.join(BASE_DIR, "calib_mq5.json")

DEBUG = True
def log(*a):
    if DEBUG: print(*a, flush=True)

# ============== utils ==============
def load_calib(path):
    with open(path, "r", encoding="utf-8") as f:
        c = json.load(f)
    return c["meta"], c["gases"]

def fit_ab(points):
    (p1, r1), (p2, r2) = points
    x1, y1 = math.log10(r1), math.log10(p1)
    x2, y2 = math.log10(r2), math.log10(p2)
    a = (y2 - y1) / (x2 - x1)
    b = y1 - a * x1
    return a, b

def rs_from_vout(vout, rload, vcc, mode):
    vout = max(0.05, min(vout, vcc - 0.05))  # clamp chống chia 0
    if mode == "Rs_top":      # VCC->Rs->Vout->Rload->GND (Vout ↑ khi khí ↑)
        return rload * (vcc / vout - 1.0)
    else:                     # Rs_bottom: VCC->Rload->Vout->Rs->GND (Vout ↓ khi khí ↑)
        return rload * (vout / (vcc - vout))

def ppm_from_ratio(ratio, a, b):
    ratio = max(ratio, 0.05)
    return 10 ** (a * math.log10(ratio) + b)

def ema(prev, x, alpha=0.3):
    return x if prev is None else prev + alpha*(x - prev)

# ---- Auto-learn R0 (MQ-5) ----
MQ5_R0_WIN = 120                 # ~10 phút nếu PUBLISH_INTERVAL=5s (với 20s thì ~40 phút)
MQ5_R0_COOLDOWN_SEC = 3600       # cập nhật ≥ mỗi 60 phút
MQ5_R_RATIO_MIN, MQ5_R_RATIO_MAX = 0.9, 1.5
MQ5_MAD_MAX = 0.08
mq5_r0_buf = []
_last_r0_update_ts = 0.0

def _mad(xs, m=None):
    if not xs: return 0.0
    if m is None: m = median(xs)
    return median([abs(x - m) for x in xs])

def _persist_mq5_r0(path, meta_key="R0_OHM", new_r0=None):
    if new_r0 is None: return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "meta" in data and isinstance(data["meta"], dict):
            data["meta"][meta_key] = float(new_r0)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
    except Exception as e:
        log("Persist R0 failed:", e)
    return False

# ============== calib ==============
mics_meta, mics_pts = load_calib(CALIB_MICS)
mq5_meta,  mq5_pts  = load_calib(CALIB_MQ5)

# ÉP mode theo thực nghiệm
mq5_meta["divider_mode"]  = "Rs_bottom"  # MQ-5: Vout giảm khi có khí
mics_meta["divider_mode"] = "Rs_top"     # MiCS: Vout tăng khi có khí

mics_models = {g: fit_ab(mics_pts[g]) for g in mics_pts}
mq5_models  = {g: fit_ab(mq5_pts[g])  for g in mq5_pts}

# ============== MQTT ==============
client = mqtt.Client(client_id="sensor_service", protocol=mqtt.MQTTv311)
if getattr(cfg, "USER", None):
    client.username_pw_set(cfg.USER, cfg.PASSWORD)
client.connect(cfg.BROKER, cfg.PORT, cfg.KEEPALIVE)
client.loop_start()

# ============== sensors ============
# DHT: retry + last_good + sanity check
try:
    dht = adafruit_dht.DHT22(board.D4, use_pulseio=False)
except Exception:
    dht = None

i2c = busio.I2C(board.SCL, board.SDA)
ads = ADS.ADS1115(i2c); ads.gain = 1
ch_mq5  = AnalogIn(ads, 0)
ch_mics = AnalogIn(ads, 1)

LAST_T = None; LAST_H = None
_last_dht_read = 0.0
DHT_PERIOD = 3.0   # nới chu kỳ đọc nội bộ để giảm checksum fail

def _valid_dht(t, h, lt, lh):
    if t is None or h is None: return False
    if not (0 < t < 80 and 0 < h < 100): return False
    if lt is not None and abs(t - lt) > 10: return False
    if lh is not None and abs(h - lh) > 20: return False
    return True

def read_dht22(max_tries=4, gap=0.25):
    global _last_dht_read, dht
    now = time.monotonic()
    wait = _last_dht_read + DHT_PERIOD - now
    if wait > 0: time.sleep(wait)
    for i in range(max_tries):
        try:
            t = dht.temperature; h = dht.humidity
            if _valid_dht(t, h, LAST_T, LAST_H):
                _last_dht_read = time.monotonic()
                return float(t), float(h)
        except RuntimeError as e:
            log(f"DHT warn[{i+1}/{max_tries}]: {e}")
        except Exception as e:
            log("DHT error, re-init:", e)
            try: dht.exit()
            except: pass
            time.sleep(0.5)
            try:
                dht = adafruit_dht.DHT22(board.D4, use_pulseio=False)
            except Exception as ee:
                log("DHT re-init failed:", ee)
        time.sleep(gap + i*0.05)
    return None, None

# warm-up
if dht:
    for _ in range(2): _ = read_dht22(max_tries=2)

# EMA states
_vmq5_ema = None
_vmics_ema = None

# Đặt chu kỳ publish = 20s như yêu cầu
PUBLISH_INTERVAL = 20

# ============== main loop =========
gases = ["CO","Ethanol","H2","NH3","CH4"]

while True:
    try:
        # DHT
        t = h = None
        if dht:
            t, h = read_dht22()
            if t is not None:
                LAST_T = t; client.publish(cfg.TOPIC_ENV_TEMP, f"{t:.2f}", qos=1)
            if h is not None:
                LAST_H = h; client.publish(cfg.TOPIC_ENV_HUM,  f"{h:.2f}", qos=1)
            # nếu fail tạm thời, phát lại last_good (không retain)
            if t is None and LAST_T is not None:
                client.publish(cfg.TOPIC_ENV_TEMP, f"{LAST_T:.2f}", qos=1, retain=False)
            if h is None and LAST_H is not None:
                client.publish(cfg.TOPIC_ENV_HUM,  f"{LAST_H:.2f}", qos=1, retain=False)

        # Voltages
        v_mq5_raw  = max(ch_mq5.voltage, 0.0)
        v_mics_raw = max(ch_mics.voltage, 0.0)
        _vmq5_ema  = ema(_vmq5_ema,  v_mq5_raw,  alpha=0.3)
        _vmics_ema = ema(_vmics_ema, v_mics_raw, alpha=0.3)
        v_mq5, v_mics = _vmq5_ema, _vmics_ema

        client.publish("environment/volt_raw/mq5",  f"{v_mq5_raw:.3f}",  qos=0)
        client.publish("environment/volt_raw/mics", f"{v_mics_raw:.3f}", qos=0)
        client.publish(cfg.TOPIC_ENV_GAS1, f"{v_mq5:.3f}",  qos=1)
        client.publish(cfg.TOPIC_ENV_GAS2, f"{v_mics:.3f}", qos=1)

        # Rs, ratios
        rs_mq5  = rs_from_vout(v_mq5,  mq5_meta["RLOAD_OHM"], mq5_meta["VCC"],  mq5_meta["divider_mode"])
        rs_mics = rs_from_vout(v_mics, mics_meta["RLOAD_OHM"], mics_meta["VCC"], mics_meta["divider_mode"])
        r_mq5   = rs_mq5  / max(mq5_meta["R0_OHM"],  1e-6)
        r_mics  = rs_mics / max(mics_meta["R0_OHM"], 1e-6)

        # MQ-5 ppm
        out_mq5_ppm = {}
        for g in gases:
            if g in mq5_models:
                a, b = mq5_models[g]
                out_mq5_ppm[g] = f"{ppm_from_ratio(r_mq5, a, b):.0f}"
            else:
                out_mq5_ppm[g] = "NA"

        # MiCS ppm
        out_mics_ppm = {}
        for g in gases:
            if g in mics_models:
                a, b = mics_models[g]
                out_mics_ppm[g] = f"{ppm_from_ratio(r_mics, a, b):.0f}"

        # publish ppm
        for k,v in out_mq5_ppm.items():
            client.publish(f"environment/mq5_ppm/{k}", v, qos=1)
        for k,v in out_mics_ppm.items():
            client.publish(f"environment/mics5524_ppm/{k}", v, qos=1)

        # --- Auto-learn R0 cho MQ-5 ---
        mq5_r0_buf.append(rs_mq5)
        if len(mq5_r0_buf) > MQ5_R0_WIN:
            mq5_r0_buf.pop(0)

        if len(mq5_r0_buf) == MQ5_R0_WIN:
            r_ratio_series = [x / max(mq5_meta["R0_OHM"], 1e-6) for x in mq5_r0_buf]
            r_min, r_max = min(r_ratio_series), max(r_ratio_series)
            r_med = median(r_ratio_series)
            r_mad = _mad(r_ratio_series, r_med)

            cond_clean = (MQ5_R_RATIO_MIN <= r_min) and (r_max <= MQ5_R_RATIO_MAX)
            cond_stable = (r_mad <= MQ5_MAD_MAX)
            now_ts = time.time()
            cooldown_ok = (now_ts - _last_r0_update_ts) >= MQ5_R0_COOLDOWN_SEC

            if cond_clean and cond_stable and cooldown_ok:
                new_r0 = median(mq5_r0_buf)
                old_r0 = mq5_meta["R0_OHM"]
                delta = abs(new_r0 - old_r0) / old_r0 if old_r0 > 0 else 1.0
                if delta >= 0.10:
                    log(f"[MQ5] R0 update {old_r0:.0f}Ω -> {new_r0:.0f}Ω (r_med={r_med:.2f}, MAD={r_mad:.3f})")
                    mq5_meta["R0_OHM"] = float(new_r0)
                    _last_r0_update_ts = now_ts
                    ok = _persist_mq5_r0(CALIB_MQ5, "R0_OHM", new_r0)
                    if not ok:
                        log("[MQ5] Warning: could not persist R0 to calib_mq5.json")

        print(
          f"DHT22 T={LAST_T if LAST_T is not None else t}°C "
          f"H={LAST_H if LAST_H is not None else h}% | "
          f"V_MQ5={v_mq5:.3f}V V_MICS={v_mics:.3f}V | "
          f"Rs/R0 MQ5={r_mq5:.2f} MiCS={r_mics:.2f} | "
          f"MQ5_ppm={out_mq5_ppm} | MICS_ppm={out_mics_ppm}"
        )

        time.sleep(PUBLISH_INTERVAL)

    except Exception as e:
        print("Sensor loop error:", e)
        time.sleep(2)
