import os
import sys
import json
import time
import random
import hashlib
import hmac
import threading
import itertools
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
import urllib3
from flask import Flask, render_template, jsonify

# --- إعداد الواجهة ---
app = Flask(__name__, template_folder='templates')

# --- تحميل مكتبة urllib3 وتعطيل التنبيهات ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# 1. Rate Limiter (الحد من الطلبات)
# ============================================================
class RateLimiter:
 def __init__(self, min_gap_sec=0.2):
     self.min_gap = min_gap_sec
     self._lock = threading.Lock()
     self._last = 0.0

 def wait(self):
     with self._lock:
         now = time.time()
         delta = now - self._last
         if delta < self.min_gap:
             time.sleep(self.min_gap - delta)
             self._last = time.time()

RATE = RateLimiter(min_gap_sec=0.2)

# ============================================================
# 2. Server Configuration
# ============================================================
SERVERS = [
 ("IND", "India", "clientbp.ind.freefiremobile.com"),
 ("BD", "Bangladesh", "clientbp.bd.freefiremobile.com"),
 ("SG", "Singapore", "clientbp.sg.freefiremobile.com"),
 ("ID", "Indonesia", "clientbp.id.freefiremobile.com"),
 ("TH", "Thailand", "clientbp.th.freefiremobile.com"),
 ("PK", "Pakistan", "clientbp.pk.freefiremobile.com"),
 ("ME", "Middle East", "https://clientbp.ppmainecoonghj.com"), # ME موجود هنا
 ("VN", "Vietnam", "clientbp.vn.freefiremobile.com"),
 ("BR", "Brazil", "clientbp.br.freefiremobile.com"),
 ("US", "United States", "client.gz.freefiremobile.com"),
]

# ============================================================
# 3. Utils (Proto, AES, etc)
# ============================================================
def write_varint(value: int) -> bytes:
 out = []
 while value > 127:
     out.append((value & 0x7F) | 0x80)
     value >>= 7
     out.append(value)
     return bytes(out)

def write_string_field(f, v):
 b = v.encode("utf-8") if isinstance(v, str) else v
 return write_varint((f << 3) | 2) + write_varint(len(b)) + b

def write_bytes_field(f, v):
 return write_varint((f << 3) | 2) + write_varint(len(v)) + v

def assemble_proto(fields):
 data = bytearray()
 for k, v in fields.items():
     if isinstance(v, int):
         data.extend(write_varint_field(k, v))
 elif isinstance(v, str):
     data.extend(write_string_field(k, v))
 elif isinstance(v, bytes):
     data.extend(write_bytes_field(k, v))
     return bytes(data)

def read_varint(data, offset):
 r, s = 0, 0
 while True:
     b = data[offset]; offset += 1
     r |= (b & 0x7F) << s
     if not (b & 0x80):
         break
         s += 7
         return r, offset

def parse_proto(data):
 out = {}
 off = 0
 while off < len(data):
     try:
         tag, off = read_varint(data, off)
 except Exception:
     break
     f = tag >> 3
     w = tag & 0x7
     if f == 0:
         break
         try:
             if w == 0:
                 v, off = read_varint(data, off)
                 out[f] = v
 elif w == 2:
     ln, off = read_varint(data, off)
     val = data[off:off + ln]
     off += ln
     try:
         out[f] = val.decode("utf-8")
 except Exception:
     out[f] = val.hex()
 else:
     break
 except Exception:
     break
     return out

AES_KEY = bytes([89, 103, 38, 116, 99, 37, 68, 69, 117, 104, 54, 37, 90, 99, 94, 56])
AES_IV = bytes([54, 111, 121, 90, 68, 114, 50, 50, 69, 51, 121, 99, 104, 106, 77, 37])

CLIENT_SECRET = "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3"
APP_ID = 100067

OAUTH_REGISTER_URL = "https://100067.connect.garena.com/api/v2/oauth/guest:register"
OAUTH_TOKEN_URL = "https://100067.connect.garena.com/oauth/guest/token/grant"
MAJOR_REGISTER_URL = "https://loginbp.ppmainecoonghj.com/MajorRegister"
NEWBIE_URL = "https://loginbp.ppmainecoonghj.com/ChooseNewbieChoice"
MAJOR_LOGIN_URL = "https://loginbp.ppmainecoonghj.com/MajorLogin"

def aes_encrypt(plain):
 return AES.new(AES_KEY, AES.MODE_CBC, AES_IV).encrypt(pad(plain, AES.block_size))

def aes_decrypt(ciphertext):
 if not ciphertext or len(ciphertext) % 16 != 0:
     return ciphertext
     try:
         try:
             return unpad(AES.new(AES_KEY, AES.MODE_CBC, AES_IV).decrypt(ciphertext),
             AES.block_size)
 except ValueError:
     return AES.new(AES_KEY, AES.MODE_CBC, AES_IV).decrypt(ciphertext)
 except Exception:
     return ciphertext

# ============================================================
# 4. Back-end Logic (The Generator)
# ============================================================
def generate_one_backend(region):
 session = requests.Session()
 session.verify = False
 
 password = hashlib.sha256(os.urandom(32)).hexdigest().upper()
 uid = None
 
 try:
     # Step 1: Register
     payload = {"app_id": APP_ID, "client_type": 2, "password": password, "source": 2}
     body = json.dumps(payload, separators=(",", ":"))
     sig = hmac.new(CLIENT_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
     headers = {
     "User-Agent": "GarenaMSDK/4.0.42(KB2003 ;Android 13;en;HK;app 2.130.1 2019118332;)",
     "Accept": "application/json",
     "Content-Type": "application/json; charset=utf-8",
     "Connection": "Keep-Alive",
     "Accept-Encoding": "gzip",
     "Authorization": f"Signature {sig}",
     }
     r = session.post(OAUTH_REGISTER_URL, data=body, headers=headers, timeout=15)
     if r.status_code != 200: raise RuntimeError("HTTP {r.status_code}")
     d = r.json()
     if d.get("code") != 0: raise RuntimeError(d.get("message"))
     uid = str(d["data"]["uid"])

 # Step 2: Token
 payload = {
 "uid": uid, "password": password, "response_type": "token",
 "client_type": "2", "client_secret": CLIENT_SECRET, "client_id": "100067",
 }
 r = session.post(OAUTH_TOKEN_URL, data=payload, headers={...}, timeout=30)
 d = r.json()
 access_token = d.get("access_token")
 open_id = d.get("open_id")
 
 nickname = f"YAHYA{random.randint(1, 9999)}"
 fields = {
 1: nickname, 2: access_token, 3: open_id, 5: 102000007, 6: 4,
 7: 1, 13: 1, 14: open_id.encode(), 15: region, 16: 2, 20: "2.133.8",
 21: 1, 22: bytes.fromhex("474752450101010062020000a78910bd098e3ff2e4345d59a31db114ea088f37e32e65...")
 }
 encrypted = aes_encrypt(assemble_proto(fields))
 r = session.post(MAJOR_REGISTER_URL, data=encrypted, headers={...}, timeout=30)
 
 # Step 3: Newbie
 session.post(NEWBIE_URL, data=encrypted, headers={...}, timeout=30)
 
 # Step 4: Login (Extract JWT)
 fields_login = {...} # Same fields logic
 encrypted_login = aes_encrypt(assemble_proto(fields_login))
 r = session.post(MAJOR_LOGIN_URL, data=encrypted_login, headers={...}, timeout=30)
 
 # Extract logic (simplified for brevity)
 raw = r.content
 decrypted = aes_decrypt(raw)
 parsed = parse_proto(decrypted)
 jwt = parsed.get(8)
 if isinstance(jwt, bytes): jwt = jwt.decode("utf-8", "ignore")
 
 return {
 "uid": uid, "password": password, "nickname": nickname,
 "open_id": open_id, "access_token": access_token,
 "account_id": 12345678, "region": region, "jwt": jwt,
 "session_key": "0x123", "session_iv": "0x456"
 }
 
 except Exception as e:
     # print(f"Error: {e}") # Optional logging
     return None

# ============================================================
# 5. Flask Routes
# ============================================================
@app.route('/')
def home():
 return render_template('index.html')

@app.route('/api/generate', methods=['POST'])
def generate():
 data = request.json
 region = data.get('region', 'IND')
 count = int(data.get('count', 1))
 
 result = []
 with ThreadPoolExecutor(max_workers=5) as executor:
     futures = [executor.submit(generate_one_backend, region) for _ in range(count)]
     for future in as_completed(futures):
         acc = future.result()
         if acc: result.append(acc)
 
 return jsonify({"success": True, "accounts": result})

if __name__ == '__main__':
 print("Server running at http://127.0.0.1:5000")
 app.run(debug=True, port=5000)