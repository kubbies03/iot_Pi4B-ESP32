import os, json, re
import google.generativeai as genai

# ===== CẤU HÌNH API =====
genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))

# ===== LỆNH MẪU =====
text = "bật đèn hai"

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

# ===== GỌI GEMINI =====
model = genai.GenerativeModel("models/gemini-2.5-flash")
resp = model.generate_content(prompt)
print("Raw response:\n", resp.text)

# ===== LỌC CODEBLOCK =====
raw = resp.text.strip()
raw = re.sub(r"^```json", "", raw)
raw = re.sub(r"^```", "", raw)
raw = re.sub(r"```$", "", raw).strip()

# ===== PHÂN TÍCH JSON =====
try:
    data = json.loads(raw)
    print("\nParsed JSON:\n", json.dumps(data, indent=2, ensure_ascii=False))
except Exception as e:
    print("\nJSON parse error:", e)
    print("Raw text:\n", resp.text)
