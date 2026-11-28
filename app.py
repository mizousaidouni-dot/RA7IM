import os
import sys
import json
import time
import random
import hashlib
import hmac
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import urllib.request
import urllib.parse
import urllib.error
import ssl
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from flask import Flask, render_template, jsonify, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')
PROXY_FILE = os.path.join(BASE_DIR, 'proxies.txt')

app = Flask(__name__, template_folder=TEMPLATE_DIR)
SSL_CONTEXT = ssl._create_unverified_context()


class RateLimiter:
    def __init__(self, min_gap_sec=0.65):
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


RATE = RateLimiter(min_gap_sec=0.65)

_proxy_list = []
_proxy_lock = threading.Lock()
_proxy_idx = 0


def load_proxies():
    global _proxy_list
    try:
        with open(PROXY_FILE, "r", encoding="utf-8") as f:
            _proxy_list = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except FileNotFoundError:
        _proxy_list = []
    return _proxy_list


def next_proxy():
    global _proxy_idx
    if not _proxy_list:
        load_proxies()
    if not _proxy_list:
        return None
    with _proxy_lock:
        p = _proxy_list[_proxy_idx % len(_proxy_list)]
        _proxy_idx += 1
        return p


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
    ("US", "United States", "clientbp.us.freefiremobile.com"),
]

XOR_KEY = [
    0x30, 0x30, 0x30, 0x32, 0x30, 0x31, 0x37, 0x30, 0x30, 0x30, 0x30, 0x30, 0x32, 0x30, 0x31, 0x37,
    0x30, 0x30, 0x30, 0x30, 0x30, 0x32, 0x30, 0x31, 0x37, 0x30, 0x30, 0x30, 0x30, 0x30, 0x32, 0x30
]

FIELD_22_HEX = (
    "474752450101010062020000a78910bd098e3ff2e4345d59a31db114ea088f37e32e65"
    "212ff96621793d9eb78720d0bf2ac95176569765247ada5eb01376b3b9931794a4946"
    "d76ef8890779f3f2129317e6e1cb2fdcf7b06247cea343b8d4f167eff85a2e1dfe99"
    "b4583a7e1a155dbe7f7f85cff3b223eb77222ec1228f3ee1ef6ce7f8ca24b00a554"
    "e497328812f8df74c82d519ae3e3ceab436eb145e8a517089a7cef6a4efb22214b2"
    "a4b19989b74807584afe5e52825e7ac60e19a596a9bf02d961de6a0ed2515ec6023"
    "fdb7684d9464b97b21c527ce61b6bee4ef30d20a1fa33a996952d44d44e44f2f86c"
    "768aaf2a7808ad60f91048dca0207961ba7c2555c48341b30190debc775edb2cee24"
    "4cf51fca3760ce4388be2db45e80b813b5beb9c784007b7762d7a1e428affc8a3a4"
    "cd76cbaef648a274297fde33233dccd3f272cb77f39a1affe0365a24954111f768f72"
    "0e77535af024bea2b2726c3bac992374755c3deacf09235e6865d456e651680d115a"
    "751a797225eaacdf9a513cf104526e1a32e5296e111a33ae581a63850837921df848"
    "9adfd41ea895b7cf3f5b2e45d538a6e4f032f590ccbb7daf5fa9c50adadee0799661"
    "4c3f957bda349e6c484fdf55970d1943ad7955a76671298b6d98b636b69ebde6bc94"
    "dbd93ac3393a6ae230130445b2d744189167854a5617be2393e7d8fbb5719a1b4754"
    "1ba466167e3e05a6c244f1301ee2035acf94dffc8adbde747d5cd85e35ead3acc372"
    "a59c4220e54bf63f9d80485f3de2518495c1d0f78c911d2da595911fd2a1989cf17"
    "cf3ded5f6c92dea64d675c555c11df92d6c517ca5d0d61a8962f43f76ec7e87596c"
    "8325ebf9ab0f8e6d2eca33c511ceac6980906ebd68c478665591dcecf788ec6ef34c"
    "fbe3fcc279c6147c5bf91cd43cdc9704236"
)

_used_names = set()
_used_lock = threading.Lock()


def sanitize_base_name(raw_name):
    cleaned = "".join(ch for ch in str(raw_name or "").strip() if ch.isalnum())
    if not cleaned:
        cleaned = "RAHIM"
    # Ø¥Ø¨ÙØ§Ø¡ ÙØ³Ø§Ø­Ø© ÙØ§ÙÙØ© ÙÙØ£Ø±ÙØ§Ù Ø¨Ø­ÙØ« ÙØ§ ÙØªØ¬Ø§ÙØ² Ø§ÙØ·ÙÙ Ø§ÙÙÙÙ 12 Ø­Ø±ÙØ§Ù
    return cleaned[:9]


def build_nickname(base_name="RAHIM"):
    prefix = sanitize_base_name(base_name)
    digits_len = max(2, 12 - len(prefix))
    low = 10 ** (digits_len - 1)
    high = (10 ** digits_len) - 1

    with _used_lock:
        num = random.randint(low, high)
        candidate = f"{prefix}{num}"[:12]
        attempts = 0
        while candidate in _used_names and attempts < 200:
            num = random.randint(low, high)
            candidate = f"{prefix}{num}"[:12]
            attempts += 1
        _used_names.add(candidate)
    return candidate


def xor_open_id(open_id: str) -> bytes:
    b = open_id.encode("utf-8")
    return bytes(x ^ XOR_KEY[i % len(XOR_KEY)] for i, x in enumerate(b))


class SimpleResponse:
    def __init__(self, status_code, content):
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", errors="ignore")

    def json(self):
        return json.loads(self.text)


def http_post(url, data=None, headers=None, timeout=30):
    RATE.wait()
    if isinstance(data, str):
        data = data.encode("utf-8")
    req_headers = dict(headers or {})
    req_headers.pop("Accept-Encoding", None)
    proxy = next_proxy()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=SSL_CONTEXT),
        )
    else:
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=SSL_CONTEXT)
        )
    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    try:
        with opener.open(req, timeout=timeout) as resp:
            return SimpleResponse(resp.getcode(), resp.read())
    except urllib.error.HTTPError as e:
        return SimpleResponse(e.code, e.read())


def retry_step(func, retries=3, delay=1.2, backoff=2.0):
    last = None
    for attempt in range(1, retries + 1):
        try:
            return func()
        except Exception as e:
            last = e
            msg = str(e)
            if "429" in msg:
                wait = random.uniform(1.5, 3.0) if _proxy_list else random.uniform(8.0, 14.0)
                print(f"[RATE_LIMIT] 429 hit, sleeping {wait:.1f}s (attempt {attempt}/{retries})", flush=True)
                time.sleep(wait)
            elif attempt < retries:
                time.sleep(delay * (backoff ** (attempt - 1)))
    raise last


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
        if isinstance(v, bool):
            data.extend(write_varint((k << 3) | 0) + write_varint(int(v)))
        elif isinstance(v, int):
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


def _step1_register(password):
    payload = {"app_id": APP_ID, "client_type": 2, "password": password, "source": 2}
    body = json.dumps(payload, separators=(",", ":"))
    sig = hmac.new(CLIENT_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    headers = {
        "User-Agent": "GarenaMSDK/4.0.42(KB2003 ;Android 13;en;HK;app 2.130.1 2019118332;)",
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "Connection": "Keep-Alive",
        "Authorization": f"Signature {sig}",
    }
    r = http_post(OAUTH_REGISTER_URL, data=body, headers=headers, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Step 1 HTTP {r.status_code}: {r.text[:200]}")
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"Step 1 register failed: {d.get('message') or d}")
    return str(d["data"]["uid"])


def _step2_token(uid, password):
    token_payload = {
        "uid": uid,
        "password": password,
        "response_type": "token",
        "client_type": "2",
        "client_secret": CLIENT_SECRET,
        "client_id": "100067",
    }
    token_body = urllib.parse.urlencode(token_payload)
    token_headers = {
        "User-Agent": "GarenaMSDK/4.0.30",
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Connection": "Keep-Alive",
    }
    r = http_post(OAUTH_TOKEN_URL, data=token_body, headers=token_headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Step 2 HTTP {r.status_code}: {r.text[:200]}")
    d = r.json()
    access_token = d.get("access_token")
    open_id = d.get("open_id")
    if not access_token or not open_id:
        raise RuntimeError(f"Step 2 missing token/open_id: {d}")
    return access_token, open_id


def _step3_major_register(region, nickname, access_token, open_id):
    fields = {
        1: nickname,
        2: access_token,
        3: open_id,
        5: 102000007,
        6: 4,
        7: 1,
        13: 1,
        14: xor_open_id(open_id),
        15: region,
        16: 2,
        20: "2.133.8",
        21: 1,
        22: bytes.fromhex(FIELD_22_HEX),
    }
    encrypted = aes_encrypt(assemble_proto(fields))
    game_headers = {
        "Host": "loginbp.ppmainecoonghj.com",
        "User-Agent": "UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)",
        "Accept": "*/*",
        "X-GA-SV": str(int(time.time())),
        "Authorization": "Bearer",
        "X-GA": "v1 1",
        "ReleaseVersion": "OB55",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Unity-Version": "2018.4.12f1",
        "Content-Length": str(len(encrypted)),
    }
    r = http_post(MAJOR_REGISTER_URL, data=encrypted, headers=game_headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Step 3 HTTP {r.status_code}: {r.text[:150]}")
    parsed_reg = parse_proto(r.content)
    account_id = parsed_reg.get(3)
    if not account_id:
        raise RuntimeError("Step 3 missing account_id")
    return int(account_id), game_headers


def _step4_newbie(account_id, game_headers):
    plain_newbie = assemble_proto({1: account_id, 2: 1, 3: 3})
    encrypted_newbie = aes_encrypt(plain_newbie)
    hdrs = dict(game_headers)
    hdrs["X-GA-SV"] = str(int(time.time()))
    hdrs["Content-Length"] = str(len(encrypted_newbie))
    r_newbie = http_post(NEWBIE_URL, headers=hdrs, data=encrypted_newbie, timeout=30)
    if r_newbie.status_code != 200:
        raise RuntimeError(f"Step 4 HTTP {r_newbie.status_code}")
    return True


def _step5_major_login(region, access_token, open_id, game_headers):
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
    hdrs = dict(game_headers)
    hdrs["X-GA-SV"] = str(int(time.time()))
    hdrs["Content-Length"] = str(len(encrypted_login))
    r = http_post(MAJOR_LOGIN_URL, data=encrypted_login, headers=hdrs, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Step 5 HTTP {r.status_code}")

    raw = r.content
    jwt = None
    session_key = AES_KEY
    session_iv = AES_IV

    for buf in (aes_decrypt(raw), raw[64:] if len(raw) > 64 else b"", raw):
        if not buf:
            continue
        try:
            parsed = parse_proto(buf)
        except Exception:
            continue
        candidate = parsed.get(8)
        if isinstance(candidate, bytes):
            candidate = candidate.decode("utf-8", "ignore")
        if isinstance(candidate, str) and candidate.startswith("eyJ") and candidate.count(".") == 2:
            jwt = candidate
            sixteen = []
            for fnum in sorted(parsed.keys()):
                v = parsed[fnum]
                vals = v if isinstance(v, list) else [v]
                for val in vals:
                    if isinstance(val, (bytes, bytearray)) and len(val) == 16:
                        sixteen.append(bytes(val))
            if len(sixteen) >= 2:
                session_key, session_iv = sixteen[-2], sixteen[-1]
            break

    if not jwt:
        raise RuntimeError("Step 5 missing JWT")
    return jwt, session_key, session_iv


def generate_one_backend(region, base_name="RAHIM", retries=3):
    password = hashlib.sha256(os.urandom(32)).hexdigest().upper()
    try:
        uid = retry_step(lambda: _step1_register(password), retries=retries)
        access_token, open_id = retry_step(lambda: _step2_token(uid, password), retries=retries)
        nickname = build_nickname(base_name)
        account_id, game_headers = retry_step(
            lambda: _step3_major_register(region, nickname, access_token, open_id),
            retries=retries,
        )
        retry_step(lambda: _step4_newbie(account_id, game_headers), retries=retries)
        jwt, session_key, session_iv = retry_step(
            lambda: _step5_major_login(region, access_token, open_id, game_headers),
            retries=retries,
        )

        return {
            "ok": True,
            "data": {
                "uid": uid,
                "password": password,
                "nickname": nickname,
                "open_id": open_id,
                "access_token": access_token,
                "account_id": account_id,
                "region": region,
                "jwt": jwt,
                "session_key": session_key.hex(),
                "session_iv": session_iv.hex(),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        }

    except Exception as e:
        err_msg = f"{type(e).__name__}: {e}"
        print(f"[GEN_ERROR] region={region} -> {err_msg}", flush=True)
        return {"ok": False, "error": err_msg}


@app.route('/')
def home():
    if os.path.exists(os.path.join(TEMPLATE_DIR, 'index.html')):
        return render_template('index.html')
    root_index = os.path.join(BASE_DIR, 'index.html')
    if os.path.exists(root_index):
        with open(root_index, 'r', encoding='utf-8') as f:
            return f.read()
    return "Error: index.html not found.", 404


@app.route('/health')
def health():
    return jsonify({"status": "ok", "proxies_loaded": len(_proxy_list)}), 200


@app.route('/api/generate', methods=['POST'])
def generate():
    data = request.get_json(silent=True) or {}
    region = str(data.get('region', 'IND')).upper()
    base_name = sanitize_base_name(data.get('base_name', 'RAHIM'))
    try:
        count = max(1, min(100, int(data.get('count', 1))))
    except Exception:
        count = 1

    workers = 5 if _proxy_list else 2
    accounts = []
    errors = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(generate_one_backend, region, base_name, 3) for _ in range(count)]
        for future in as_completed(futures):
            res = future.result()
            if res and res.get("ok"):
                accounts.append(res["data"])
            elif res:
                errors.append(res.get("error", "Unknown error"))

    return jsonify({
        "success": len(accounts) > 0,
        "requested": count,
        "generated_count": len(accounts),
        "accounts": accounts,
        "errors": errors
    })


if __name__ == '__main__':
    load_proxies()
    port = int(os.environ.get("PORT", 8080))
    print(f"Server running on 0.0.0.0:{port}", flush=True)
    app.run(host="0.0.0.0", port=port, debug=False)
