"""
WeChat → Claude Code 桥接服务

- 接收微信消息（支持安全模式加密），存入队列
- PC 轮询取消息
- PC 回复后推回微信
"""

import base64
import hashlib
import json
import os
import re
import struct
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests
from Crypto.Cipher import AES
from flask import Flask, jsonify, request, Response

app = Flask(__name__)

# === 配置（环境变量覆盖） ===
WECHAT_TOKEN = os.getenv("WECHAT_TOKEN", "claude_bridge_2026")
WECHAT_APPID = os.getenv("WECHAT_APPID", "")
WECHAT_APPSECRET = os.getenv("WECHAT_APPSECRET", "")
WECHAT_AES_KEY = os.getenv("WECHAT_AES_KEY", "")
BRIDGE_SECRET = os.getenv("BRIDGE_SECRET", "bridge_secret_change_me")

DATA_FILE = Path(__file__).parent / "data.json"

messages: list[dict] = []
access_token_cache = {"token": "", "expires": 0}


def load_data():
    global messages
    if DATA_FILE.exists():
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        messages = data.get("messages", [])
        cutoff = datetime.now().timestamp() - 86400
        messages = [m for m in messages if m.get("created_at", 0) > cutoff]


def save_data():
    DATA_FILE.write_text(
        json.dumps({"messages": messages}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_access_token():
    now = time.time()
    if access_token_cache["token"] and now < access_token_cache["expires"] - 300:
        return access_token_cache["token"]
    if not WECHAT_APPID or not WECHAT_APPSECRET:
        return ""
    resp = requests.get(
        "https://api.weixin.qq.com/cgi-bin/token",
        params={"grant_type": "client_credential", "appid": WECHAT_APPID, "secret": WECHAT_APPSECRET},
        timeout=10,
    ).json()
    token = resp.get("access_token", "")
    expires = resp.get("expires_in", 7200)
    access_token_cache["token"] = token
    access_token_cache["expires"] = now + expires
    return token


def send_wechat_reply(openid: str, text: str):
    token = get_access_token()
    if not token:
        return False
    if len(text) > 2000:
        text = text[:1997] + "..."
    resp = requests.post(
        f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={token}",
        json={"touser": openid, "msgtype": "text", "text": {"content": text}},
        timeout=10,
    ).json()
    # Log the result for debugging
    if resp.get("errcode") != 0:
        print(f"[WARN] WeChat reply failed: {resp}", flush=True)
    return resp.get("errcode") == 0


def check_signature(signature: str, timestamp: str, nonce: str) -> bool:
    items = sorted([WECHAT_TOKEN, timestamp, nonce])
    raw = "".join(items)
    return hashlib.sha1(raw.encode()).hexdigest() == signature


# === AES 解密（微信安全模式） ===

def _aes_key() -> bytes:
    return base64.b64decode(WECHAT_AES_KEY + "=")


def _decrypt_msg(encrypted: str) -> str:
    """解密微信安全模式消息，返回原始 XML"""
    key = _aes_key()
    cipher = AES.new(key, AES.MODE_CBC, iv=key[:16])
    raw = cipher.decrypt(base64.b64decode(encrypted))
    # 去除 PKCS7 padding
    pad = raw[-1]
    raw = raw[:-pad]
    # 格式: random(16) + msg_len(4) + msg + appid
    msg_len = struct.unpack("!I", raw[16:20])[0]
    xml = raw[20:20 + msg_len].decode("utf-8")
    return xml


def _encrypt_msg(xml: str, timestamp: str, nonce: str) -> str:
    """加密回复 XML"""
    key = _aes_key()
    # 格式: random(16) + msg_len(4) + xml + appid
    raw = xml.encode("utf-8")
    random_bytes = os.urandom(16)
    msg_len = struct.pack("!I", len(raw))
    appid_bytes = WECHAT_APPID.encode("utf-8")
    plain = random_bytes + msg_len + raw + appid_bytes
    # PKCS7 padding
    block_size = 32
    pad = block_size - len(plain) % block_size
    plain += bytes([pad] * pad)
    cipher = AES.new(key, AES.MODE_CBC, iv=key[:16])
    encrypted = base64.b64encode(cipher.encrypt(plain)).decode()

    # 生成签名
    items = sorted([WECHAT_TOKEN, timestamp, nonce, encrypted])
    sign = hashlib.sha1("".join(items).encode()).hexdigest()

    return f"""<xml>
<Encrypt><![CDATA[{encrypted}]]></Encrypt>
<MsgSignature><![CDATA[{sign}]]></MsgSignature>
<TimeStamp>{timestamp}</TimeStamp>
<Nonce><![CDATA[{nonce}]]></Nonce>
</xml>"""


def parse_msg_xml(xml: str) -> tuple[str, str]:
    """从 XML 提取 msgType 和 content"""
    from_user = ""
    content = ""
    msg_type = ""
    fu = re.search(r"<(?:FromUserName|FromUserName_x0020)><!\[CDATA\[(.*?)\]\]>", xml)
    if not fu:
        fu = re.search(r"<FromUserName>(.*?)</FromUserName>", xml)
    ct = re.search(r"<(?:Content|Content_x0020)><!\[CDATA\[(.*?)\]\]>", xml)
    if not ct:
        ct = re.search(r"<Content>(.*?)</Content>", xml)
    mt = re.search(r"<(?:MsgType|MsgType_x0020)><!\[CDATA\[(.*?)\]\]>", xml)
    if not mt:
        mt = re.search(r"<MsgType>(.*?)</MsgType>", xml)
    if fu:
        from_user = fu.group(1)
    if ct:
        content = ct.group(1)
    if mt:
        msg_type = mt.group(1)
    return msg_type, from_user, content


# === 微信回调路由 ===

@app.route("/wechat", methods=["GET", "POST"])
def wechat_callback():
    if request.method == "GET":
        sig = request.args.get("signature", "")
        ts = request.args.get("timestamp", "")
        nonce = request.args.get("nonce", "")
        echostr = request.args.get("echostr", "")
        if check_signature(sig, ts, nonce):
            return Response(echostr, content_type="text/plain")
        return "signature check failed", 403

    # POST: 消息回调
    body = request.data.decode("utf-8")
    sig = request.args.get("signature", "")
    ts = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")

    if not check_signature(sig, ts, nonce) and WECHAT_AES_KEY:
        # 安全模式：验证 msg_signature
        msg_sig = request.args.get("msg_signature", "")
        if not msg_sig:
            return "signature check failed", 403
    else:
        msg_sig = ""

    # 解密安全模式消息
    xml = body
    if WECHAT_AES_KEY:
        enc_match = re.search(r"<Encrypt><!\[CDATA\[(.*?)\]\]></Encrypt>", body)
        if enc_match:
            xml = _decrypt_msg(enc_match.group(1))

    msg_type, from_user, content = parse_msg_xml(xml)

    if msg_type != "text" or not content:
        return "ok", 200

    msg_id = str(uuid.uuid4())[:8]
    msg = {
        "id": msg_id,
        "from_user": from_user,
        "content": content,
        "created_at": datetime.now().timestamp(),
        "status": "pending",
        "reply": "",
    }
    messages.append(msg)
    save_data()

    if WECHAT_AES_KEY:
        # 安全模式：加密回复
        reply_xml = f"<xml><ToUserName><![CDATA[{from_user}]]></ToUserName><FromUserName><![CDATA[{WECHAT_APPID}]]></FromUserName><CreateTime>{int(time.time())}</CreateTime><MsgType><![CDATA[text]]></MsgType><Content><![CDATA[收到，正在执行...]]></Content></xml>"
        return Response(_encrypt_msg(reply_xml, ts, nonce), content_type="application/xml")

    return "收到，正在执行..."


# === PC 轮询 API ===

@app.route("/api/messages/pending", methods=["GET"])
def get_pending():
    secret = request.headers.get("X-Bridge-Secret", "")
    if secret != BRIDGE_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    pending = [m for m in messages if m["status"] == "pending"]
    return jsonify({"ok": True, "count": len(pending), "messages": pending})


@app.route("/api/messages/<msg_id>/reply", methods=["POST"])
def post_reply(msg_id):
    secret = request.headers.get("X-Bridge-Secret", "")
    if secret != BRIDGE_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = request.get_json()
    reply_text = data.get("reply", "")
    for m in messages:
        if m["id"] == msg_id:
            m["reply"] = reply_text
            m["status"] = "replied"
            save_data()
            if m.get("from_user"):
                ok = send_wechat_reply(m["from_user"], reply_text)
                m["delivered"] = ok
            return jsonify({"ok": True, "delivered": m.get("delivered", False)})
    return jsonify({"ok": False, "error": "not found"}), 404


@app.route("/api/messages/<msg_id>/fail", methods=["POST"])
def post_fail(msg_id):
    secret = request.headers.get("X-Bridge-Secret", "")
    if secret != BRIDGE_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = request.get_json()
    for m in messages:
        if m["id"] == msg_id:
            m["reply"] = data.get("error", "执行失败")
            m["status"] = "failed"
            save_data()
            return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not found"}), 404


@app.route("/api/stats", methods=["GET"])
def stats():
    pending = sum(1 for m in messages if m["status"] == "pending")
    replied = sum(1 for m in messages if m["status"] == "replied")
    return jsonify({"pending": pending, "replied": replied, "total": len(messages)})


@app.route("/")
def index():
    return jsonify({"service": "WeChat-Claude Bridge", "status": "running"})


if __name__ == "__main__":
    load_data()
    port = int(os.getenv("PORT", 5000))
    print(f"Bridge running on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
