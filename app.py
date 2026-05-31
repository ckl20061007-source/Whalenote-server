"""
鲸记 · 微信公众号消息处理服务器
==========================================
功能：
  GET  /wechat → 微信服务器验证（SHA1 签名校验）
  POST /wechat → 接收消息 → 绑定码 / 自然语言记账
==========================================
部署：Railway（Flask + gunicorn）
"""

import os
import re
import json
import hashlib
import time
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

import requests
from flask import Flask, request, make_response
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── 环境变量 ──
WECHAT_TOKEN = os.getenv("WECHAT_TOKEN", "Whalenote2026")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_TIMEOUT = 4  # 秒 — 必须在 5 秒内回复微信

BINDING_CODE_RE = re.compile(r"^绑定\s*([A-Z0-9]{6})$")  # 匹配"绑定 XXXXXX"
BINDING_EXPIRE_MINUTES = 10  # 绑定码有效期

# ── Supabase 客户端（服务端用，不受 RLS 限制） ──
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# ========== 辅助函数 ==========

def sha1_signature(token, timestamp, nonce):
    """微信签名校验：按字典序排序 token/timestamp/nonce → SHA1"""
    s = "".join(sorted([token, timestamp, nonce]))
    return hashlib.sha1(s.encode()).hexdigest()


def reply_xml(to_user, from_user, content):
    """构造微信文本回复 XML"""
    return f"""<xml>
<ToUserName><![CDATA[{to_user}]]></ToUserName>
<FromUserName><![CDATA[{from_user}]]></FromUserName>
<CreateTime>{int(time.time())}</CreateTime>
<MsgType><![CDATA[text]]></MsgType>
<Content><![CDATA[{content}]]></Content>
</xml>"""


def parse_user_id(openid):
    """根据 openid 查已绑定 user_id，没绑定返回 None"""
    try:
        resp = supabase.table("wechat_bindings") \
            .select("user_id") \
            .eq("openid", openid) \
            .execute()
        if resp.data:
            return resp.data[0]["user_id"]
    except Exception as e:
        print(f"[wechat_bindings 查询失败] {e}")
    return None


def process_binding(openid, code):
    """
    处理绑定码验证：
    1. 查 binding_codes 找有效记录（10分钟内 + 未使用）
    2. 写入 wechat_bindings
    3. 标记 used = true
    """
    try:
        # 查有效绑定码
        cutoff = (datetime.utcnow() - timedelta(minutes=BINDING_EXPIRE_MINUTES)).isoformat()
        resp = supabase.table("binding_codes") \
            .select("*") \
            .eq("code", code) \
            .eq("used", False) \
            .gte("created_at", cutoff) \
            .execute()

        if not resp.data:
            return "❌ 绑定码无效或已过期，请在鲸记App中重新生成"

        user_id = resp.data[0]["user_id"]

        # 写入绑定关系（upsert：重复绑定则覆盖）
        supabase.table("wechat_bindings") \
            .upsert({"openid": openid, "user_id": user_id, "bound_at": datetime.utcnow().isoformat()}) \
            .execute()

        # 标记绑定码已使用
        supabase.table("binding_codes") \
            .update({"used": True}) \
            .eq("code", code) \
            .execute()

        return "✅ 绑定成功！现在你可以直接发消息记账了，例如：餐饮支出35元"

    except Exception as e:
        print(f"[绑定处理失败] {e}")
        return "❌ 绑定失败，请稍后重试"


SYSTEM_PROMPT = (
    "你是一个记账助手，从用户的自然语言中提取记账信息。"
    "返回严格的JSON格式，不要有任何其他文字："
    '{"type": "支出"或"收入", "category": "类别", "amount": 金额数字, "note": "备注或空字符串"}'
    "，类别只能是以下之一：餐饮、交通、购物、娱乐、医疗、工资、奖金、其他。"
    '如果无法识别为记账信息，返回 {"error": "无法识别"}'
)


def parse_with_deepseek(msg):
    """调用 DeepSeek 解析自然语言 → 记账记录"""
    try:
        resp = requests.post(
            DEEPSEEK_URL,
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": msg},
                ],
                "temperature": 0,
                "max_tokens": 200,
            },
            timeout=DEEPSEEK_TIMEOUT,
        )
        data = resp.json()
        raw = data["choices"][0]["message"]["content"].strip()

        # 提取 JSON（去掉可能的 markdown 反引号）
        json_match = re.search(r'\{[^}]*\}', raw)
        if json_match:
            return json.loads(json_match.group())
    except requests.Timeout:
        print("[DeepSeek 超时]")
    except Exception as e:
        print(f"[DeepSeek 调用失败] {e}")
    return None


def write_transaction(user_id, record):
    """写入 transactions 表"""
    try:
        supabase.table("transactions").insert({
            "user_id": user_id,
            "type": record["type"],
            "amount": record["amount"],
            "category": record["category"],
            "note": record.get("note") or "",
            "created_at": int(time.time() * 1000),  # 毫秒时间戳
            "source": "wechat",
        }).execute()
        return True
    except Exception as e:
        print(f"[写入交易失败] {e}")
        return False


def process_message(openid, content):
    """
    消息路由：
    1. "绑定 XXXXXX" → 绑定
    2. 已绑定用户的自然语言 → DeepSeek 解析 → 写入
    3. 未绑定用户 → 提示绑定
    """
    # 1. 绑定码匹配
    m = BINDING_CODE_RE.match(content.strip())
    if m:
        return process_binding(openid, m.group(1))

    # 2. 检查是否已绑定
    user_id = parse_user_id(openid)
    if not user_id:
        return "请先绑定鲸记账号，在App内生成绑定码后，发送：绑定 XXXXXX"

    # 3. 自然语言解析
    record = parse_with_deepseek(content)
    if not record or "error" in record:
        return "😅 没看懂这条消息，试试这样说：餐饮支出35元"

    # 4. 写入数据库
    if write_transaction(user_id, record):
        return f"✅ 已记录：{record['category']}{record['type']} {record['amount']}元"
    else:
        return "❌ 记录保存失败，请稍后重试"


# ========== Flask 路由 ==========

@app.route("/wechat", methods=["GET"])
def wechat_verify():
    """微信服务器验证"""
    signature = request.args.get("signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")
    echostr = request.args.get("echostr", "")

    if sha1_signature(WECHAT_TOKEN, timestamp, nonce) == signature:
        return echostr
    return "signature mismatch", 403


@app.route("/wechat", methods=["POST"])
def wechat_message():
    """接收微信消息 → 处理 → 回复"""
    raw = request.data.decode("utf-8")
    try:
        root = ET.fromstring(raw)
        msg_type = root.findtext("MsgType", "")
        from_user = root.findtext("FromUserName", "")
        to_user = root.findtext("ToUserName", "")

        # 只处理文本消息，其余类型回复静默
        if msg_type != "text":
            return make_response("success", 200)

        content = root.findtext("Content", "").strip()
        if not content:
            return make_response(reply_xml(from_user, to_user, "请发送文字消息"), 200, {'Content-Type': 'application/xml'})

        reply_content = process_message(from_user, content)
        xml = reply_xml(from_user, to_user, reply_content)
        return make_response(xml, 200, {'Content-Type': 'application/xml'})

    except ET.ParseError:
        return make_response("success", 200)
    except Exception as e:
        print(f"[POST /wechat 错误] {e}")
        return make_response("success", 200)


@app.route("/", methods=["GET"])
def index():
    """Railway 健康检查"""
    return "鲸记 WeChat Server is running."


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
