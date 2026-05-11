"""
WeChat → Claude Code 桥接服务

- 接收微信消息，存入队列
- PC 轮询取消息
- PC 回复后推回微信
"""

import hashlib
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, jsonify, request, Response

app = Flask(__name__)

# === 配置（环境变量覆盖） ===
WECHAT_TOKEN = os.getenv("WECHAT_TOKEN", "claude_bridge_2026")
WECHAT_APPID = os.getenv("WECHAT_APPID", "")
WECHAT_APPSECRET = os.getenv("WECHAT_APPSECRET", "")
BRIDGE_SECRET = os.getenv("BRIDGE_SECRET", "bridge_secret_change_me")

DATA_FILE = Path(__file__).parent / "data.json"

# 消息队列: [{id, from_user, content, created_at, status, reply}]
messages: list[dict] = []
access_token_cache = {"token": "", "expires": 0}


def load_data():
    global messages
    if DATA_FILE.exists():
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        messages = data.get("messages", [])
        # 只保留 24 小时内的消息
        cutoff = datetime.now().timestamp() - 86400
        messages = [m for m in messages if m.get("created_at", 0) > cutoff]


def save_data():
    DATA_FILE.write_text(
        json.dumps({"messages": messages}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_access_token():
    """获取微信 access_token，带缓存"""
    now = time.time()
    if access_token_cache["token"] and now < access_token_cache["expires"] - 300:
        return access_token_cache["token"]

    if not WECHAT_APPID or not WECHAT_APPSECRET:
        print("[WARN] WECHAT_APPID/WECHAT_APPSECRET not set")
        return ""

    resp = requests.get(
        "https://api.weixin.qq.com/cgi-bin/token",
        params={
            "grant_type": "client_credential",
            "appid": WECHAT_APPID,
            "secret": WECHAT_APPSECRET,
        },
        timeout=10,
    ).json()

    token = resp.get("access_token", "")
    expires = resp.get("expires_in", 7200)
    access_token_cache["token"] = token
    access_token_cache["expires"] = now + expires
    return token


def send_wechat_reply(openid: str, text: str):
    """通过微信客服消息 API 向用户回复"""
    token = get_access_token()
    if not token:
        return False

    # 截断过长的回复（微信限制 2048 字符）
    if len(text) > 2000:
        text = text[:1997] + "..."

    resp = requests.post(
        f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={token}",
        json={
            "touser": openid,
            "msgtype": "text",
            "text": {"content": text},
        },
        timeout=10,
    ).json()

    ok = resp.get("errcode") == 0
    if not ok:
        print(f"[ERROR] WeChat reply failed: {resp}")
    return ok


def check_signature(signature: str, timestamp: str, nonce: str) -> bool:
    """验证微信签名"""
    items = sorted([WECHAT_TOKEN, timestamp, nonce])
    raw = "".join(items)
    return hashlib.sha1(raw.encode()).hexdigest() == signature


# === 微信回调路由 ===

@app.route("/wechat", methods=["GET", "POST"])
def wechat_callback():
    # 微信服务器验证
    if request.method == "GET":
        sig = request.args.get("signature", "")
        ts = request.args.get("timestamp", "")
        nonce = request.args.get("nonce", "")
        echostr = request.args.get("echostr", "")
        if check_signature(sig, ts, nonce):
            return Response(echostr, content_type="text/plain")
        return "signature check failed", 403

    # 微信消息推送
    body = request.data.decode("utf-8")
    sig = request.args.get("signature", "")
    ts = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")
    if not check_signature(sig, ts, nonce):
        return "signature check failed", 403

    # 解析 XML 消息
    from_user = ""
    content = ""
    msg_type = ""

    import re
    from_user_m = re.search(r"<FromUserName><!\[CDATA\[(.*?)\]\]></FromUserName>", body)
    content_m = re.search(r"<Content><!\[CDATA\[(.*?)\]\]></Content>", body)
    type_m = re.search(r"<MsgType><!\[CDATA\[(.*?)\]\]></MsgType>", body)

    if from_user_m:
        from_user = from_user_m.group(1)
    if content_m:
        content = content_m.group(1)
    if type_m:
        msg_type = type_m.group(1)

    if msg_type != "text" or not content:
        # 非文本消息，返回默认提示
        return reply_xml(from_user, "目前只支持文字消息，直接发指令就行 👇", "")

    # 存入消息队列
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

    # 微信要求 5 秒内回复，先回一个"收到了"
    return reply_xml(from_user, "收到，正在执行...", msg_id)


def reply_xml(to_user: str, text: str, msg_id: str) -> str:
    """构造微信被动回复 XML"""
    return f"""<xml>
<ToUserName><![CDATA[{to_user}]]></ToUserName>
<FromUserName><![CDATA[{WECHAT_APPID or 'bridge'}]]></FromUserName>
<CreateTime>{int(time.time())}</CreateTime>
<MsgType><![CDATA[text]]></MsgType>
<Content><![CDATA[{text}]]></Content>
</xml>"""


# === PC 轮询 API ===

@app.route("/api/messages/pending", methods=["GET"])
def get_pending():
    """PC 端取待处理消息"""
    secret = request.headers.get("X-Bridge-Secret", "")
    if secret != BRIDGE_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    pending = [m for m in messages if m["status"] == "pending"]
    return jsonify({"ok": True, "count": len(pending), "messages": pending})


@app.route("/api/messages/<msg_id>/reply", methods=["POST"])
def post_reply(msg_id):
    """PC 端提交执行结果"""
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

            # 通过微信推回给用户
            if m.get("from_user"):
                ok = send_wechat_reply(m["from_user"], reply_text)
                m["delivered"] = ok

            return jsonify({"ok": True, "delivered": m.get("delivered", False)})

    return jsonify({"ok": False, "error": "not found"}), 404


@app.route("/api/messages/<msg_id>/fail", methods=["POST"])
def post_fail(msg_id):
    """PC 端提交执行失败"""
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
