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
 ("ME", "Middle East", "https://clientbp.ppmainecoonghj.com"),
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
     if out:
         return bytes(out)
         return bytes([value])

def write_string_field(f, v):
 b = v.encode("utf-8") if isinstance(v, str) else v
 return write_varint((f << 3) | 2) + write_varint(len(b)) + b

def write_bytes_field(f, v):
 return write_varint((f << 3) | 2) + write_varint(len(v)) + v

def assemble_proto(fields):
 data = bytearray()
 for k, v in fields.items():
     if isinstance(v, int):
         data.extend(write_varint((k << 3) | 0) + write_varint(v))
 elif isinstance(v, str):
     data.extend(write_string_field(k, v))
 elif isinstance(v, bytes):
     data.extend(write_bytes_field(k, v))
     return bytes(data)

def read_varint(data, offset):
 r, s = 0, 0
 while True:
     b = data[offset]
     offset += 1
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

# Constants
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
             return unpad(
             AES.new(AES_KEY, AES.MODE_CBC, AES_IV).decrypt(ciphertext),
             AES.block_size,
             )
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
 access_token = None
 open_id = None

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
 if r.status_code != 200:
     raise RuntimeError(f"HTTP {r.status_code}: {r.text}")

 d = r.json()
 if d.get("code") != 0:
     raise RuntimeError(f"register failed: {d.get('message')}")
 
 uid = str(d["data"]["uid"])

 # Step 2: Token
 # Note: Payload is usually the same as register body for guest token
 r = session.post(OAUTH_TOKEN_URL, data=body, headers=headers, timeout=30)
 d = r.json()
 access_token = d.get("access_token")
 open_id = d.get("open_id")
 
 if not access_token or not open_id:
     raise RuntimeError("Failed to get token or open_id")

 nickname = f"YAHYA{random.randint(1000, 9999)}"
 
 # Step 3: MajorRegister
 fields = {
 1: nickname,
 2: access_token,
 3: open_id,
 5: 102000007,
 6: 4,
 7: 1,
 13: 1,
 14: open_id.encode(),
 15: region,
 16: 2,
 20: "2.133.8",
 21: 1,
 22: bytes.fromhex("474752450101010062020000a78910bd098e3ff2e4345d59a31db114ea088f37e32e65212ff96621793d9eb78720d0bf2ac95176569765247ada5eb01376b3b9931794a4946d76ef8890779f3f2129317e6e1cb2fdcf7b06247cea343b8d4f167eff85a2e1dfe99b4583a7e1a155dbe7f7f85cff3b223eb77222ec1228f3ee1ef6ce7f8ca24b00a554e497328812f8df74c82d519ae3e3ceab436eb145e8a517089a7cef6a4efb22214b2a4b19989b74807584afe5e52825e7ac60e19a596a9bf02d961de6a0ed2515ec6023fdb7684d9464b97b21c527ce61b6bee4ef30d20a1fa33a996952d44d44e44f2f86c768aaf2a7808ad60f91048dca0207961ba7c2555c48341b30190debc775edb2cee244cf51fca3760ce4388be2db45e80b813b5beb9c784007b7762d7a1e428affc8a3a4cd76cbaef648a274297fde33233dccd3f272cb77f39a1affe0365a24954111f768f720e77535af024bea2b2726c3bac992374755c3deacf09235e6865d456e651680d115a751a797225eaacdf9a513cf104526e1a32e5296e111a33ae581a63850837921df8489adfd41ea895b7cf3f5b2e45d538a6e4f032f590ccbb7daf5fa9c50adadee07996614c3f957bda349e6c484fdf55970d1943ad7955a76671298b6d98b636b69ebde6bc94dbd93ac3393a6ae230130445b2d744189167854a5617be2393e7d8fbb5719a1b47541ba466167e3e05a6c244f1301ee2035acf94dffc8adbde747d5cd85e35ead3acc372a59c4220e54bf63f9d80485f3de2518495c1d0f78c911d2da595911fd2a1989cf17cf3ded5f6c92dea64d675c555c11df92d6c517ca5d0d61a8962f43f76ec7e87596c8325ebf9ab0f8e6d2eca33c511ceac6980906ebd68c478665591dcecf788ec6ef34cfbe3fcc279c6147c5bf91cd43cdc9704236")
 }
 encrypted = aes_encrypt(assemble_proto(fields))
 headers = {
 "Host": "loginbp.ppmainecoonghj.com",
 "User-Agent": "UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)",
 "Accept": "*/*",
 "Accept-Encoding": "deflate, gzip",
 "X-GA-SV": str(int(time.time())),
 "Authorization": "Bearer",
 "X-GA": "v1 1",
 "ReleaseVersion": "OB55",
 "Content-Type": "application/x-www-form-urlencoded",
 "X-Unity-Version": "2018.4.12f1",
 "Content-Length": str(len(encrypted)),
 }
 r = session.post(MAJOR_REGISTER_URL, data=encrypted, headers=headers, timeout=30)
 
 # Step 4: Newbie
 plain_newbie = assemble_proto({1: 12345678, 2: 1, 3: 3})
 encrypted_newbie = aes_encrypt(plain_newbie)
 headers = {
 "Host": "loginbp.ppmainecoonghj.com",
 "User-Agent": "UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)",
 "Accept": "*/*",
 "Accept-Encoding": "deflate, gzip",
 "X-GA-SV": str(int(time.time())),
 "Authorization": "Bearer",
 "X-GA": "v1 1",
 "ReleaseVersion": "OB55",
 "Content-Type": "application/x-www-form-urlencoded",
 "X-Unity-Version": "2018.4.12f1",
 "Content-Length": str(len(encrypted_newbie)),
 }
 session.post(NEWBIE_URL, headers=headers, data=encrypted_newbie, timeout=30)
 
 # Step 5: MajorLogin (Extract JWT)
 fields_login = {
 3: time.strftime("%Y-%m-%d %H:%M:%S"),
 4: "free fire", 5: 1, 7: "2.133.9",
 8: "Android OS 10 / API-29 (QP1A.190711.020/1617006012)",
 9: "Handheld", 10: "Vi India", 11: "WIFI",
 12: 1600, 13: 720, 14: "320",
 15: "ARM64 FP ASIMD AES | 2301 | 8", 16: 2799,
 17: "PowerVR Rogue GE8320",
 18: "OpenGL ES 3.2 build 1.11@5425693",
 19: "Google|9f7d6b8b-b10c-454a-852d-06332cd498eb",
 20: f"{random.randint(1,223)}.{random.randint(1,255)}.{random.randint(1,255)}.{random.randint(1,255)}",
 21: "en", 22: open_id, 23: "4",
 24: "Handheld", 25: "realme RMX2189", 26: region,
 29: access_token, 30: 1,
 41: "Vi India", 42: "WIFI",
 57: "1ac4b80ecf0478a44203bf8fac6120f5",
 60: 19799, 61: 2536, 62: 5056, 64: 2768,
 65: 19999, 66: 2536, 67: 19799,
 73: 1,
 74: "/data/app/com.dts.freefiremax-ShI7E0dK8p1IiZ785pvuVQ==/lib/arm64",
 76: 2,
 77: "38f4751a330688ab124c2c804cec90a5|/data/app/com.dts.freefiremax-ShI7E0dK8p1IiZ785pvuVQ==/base.apk",
 78: 2, 79: 2, 81: "64", 83: "2019118527",
 86: "OpenGLES3", 87: 3071, 88: 4, 92: 67920,
 93: "android_max",
 94: "KqsHT+UrR1HKqb6+1db+Ofei+NtZr2+hbiBo3yKDL8w+8E3S5qF2IgEEe1fFQFyHRzl4iyHjHp+QsfeLbjJ6+DidTiKxm0ak2uYYa6QR4nAUdlZR",
 95: 111107,
 96: '{"cur_rate":null,"support_etc2":true}',
 97: 1, 98: 1, 99: "4", 100: "4", 102: "",
 104: 83812, 105: 1,
 106: "https://dl-bs.ggpolarbear.com/live/ABHotUpdates/|https://core-bs.ggpolarbear.com/live/ABHotUpdates/|a4332cb1c1a84e51dd77441e4856ed5a",
 107: "1.9393e7b8e53e8aeb",
 }
 encrypted_login = aes_encrypt(assemble_proto(fields_login))
 r = session.post(MAJOR_LOGIN_URL, data=encrypted_login, headers=headers, timeout=30)
 
 raw = r.content
 decrypted = aes_decrypt(raw)
 parsed = parse_proto(decrypted)
 jwt = parsed.get(8)
 if isinstance(jwt, bytes):
     jwt = jwt.decode("utf-8", "ignore")
 
 return {
 "uid": uid,
 "password": password,
 "nickname": nickname,
 "open_id": open_id,
 "access_token": access_token,
 "account_id": 12345678,
 "region": region,
 "jwt": jwt,
 "session_key": "0x" + str(hashlib.sha256(open_id.encode()).hexdigest()[:16]),
 "session_iv": "0x" + str(hashlib.sha256(open_id.encode()).hexdigest()[:16]),
 }
 
 except Exception as e:
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
         if acc:
             result.append(acc)
 
 return jsonify({"success": True, "accounts": result})

if __name__ == '__main__':
 print("Server running at http://127.0.0.1:5000")
 app.run(debug=True, port=5000)
