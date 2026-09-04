"""
محرك وكيل مبيعات واتساب — نسخة محدّثة بنظام موافقة صاحب البزنس على الطلبات
"""

import os
import csv
import io
import json
import re
import time
import requests
from collections import defaultdict, deque
from flask import Flask, request, jsonify

app = Flask(__name__)

# ==== إعدادات الاتصال ====
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "change-me")
SHEET_CSV_URL = os.environ.get("SHEET_CSV_URL")

# رقم صاحب البزنس — يستقبل إشعارات الطلبات ويوافق/يرفض عليها
OWNER_PHONE = os.environ.get("OWNER_PHONE", "").replace("+", "").strip()

# رابط اختياري لتسجيل الطلبات بجدول Google Sheet منفصل (Apps Script Web App)
ORDERS_APPEND_URL = os.environ.get("ORDERS_APPEND_URL", "")

# ==== بيانات خاصة بكل عميل ====
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "المتجر")
DISCOUNT_TIERS = json.loads(os.environ.get("DISCOUNT_TIERS", "[]"))

# ==== ذاكرة تشغيلية (تُمسح عند إعادة تشغيل السيرفر) ====
conversation_memory = {}          # رقم الزبون -> آخر 15 تبادل رسائل
pending_orders = {}               # order_id -> تفاصيل الطلب وحالته
seen_message_ids = deque(maxlen=500)   # لمنع الرد المكرر على نفس الرسالة
message_timestamps = defaultdict(list)  # رقم الزبون -> أوقات آخر رسائله (لضبط معدل الاستخدام)

MEMORY_TURNS = 15          # آخر 15 رسالة من الزبون (= 30 عنصر بالتاريخ: سؤال+رد)
RATE_LIMIT_MAX_MSGS = 25   # أقصى عدد رسائل بالساعة الواحدة لكل زبون
RATE_LIMIT_WINDOW = 3600   # ثانية (ساعة واحدة)


# ============================================================
# قراءة الأسعار الحية
# ============================================================
def get_prices_text():
    try:
        response = requests.get(SHEET_CSV_URL, timeout=10)
        response.raise_for_status()
        reader = csv.reader(io.StringIO(response.text))
        rows = list(reader)
        lines = []
        for row in rows[1:]:
            if len(row) >= 2 and row[0].strip():
                lines.append(f"- {row[0].strip()} = {row[1].strip()} دينار عراقي")
        return "\n".join(lines) if lines else "لا توجد أسعار محدثة حالياً."
    except Exception as e:
        print(f"[خطأ] قراءة الأسعار: {e}")
        return "تعذر تحميل الأسعار حالياً — أخبر الزبون بالتواصل لاحقاً."


def build_discount_text():
    if not DISCOUNT_TIERS:
        return "لا توجد سياسة خصومات حالياً."
    lines = []
    for tier in sorted(DISCOUNT_TIERS, key=lambda t: t["min"]):
        lines.append(f"- طلبات فوق {tier['min']:,} دينار: خصم تلقائي {tier['percent']}%.")
    return "\n".join(lines)


# ============================================================
# تعليمات النظام — فيها صيغة الوسوم الخاصة بالطلبات والتصعيد
# ============================================================
def build_system_prompt():
    return f"""أنت مساعد مبيعات ذكي تعمل باسم "{BUSINESS_NAME}"، متجر عبر واتساب.

## أسلوبك:
- تتحدث بالعربية بأسلوب ودود ومباشر، بدون حشو.
- لا تقول إنك ذكاء اصطناعي، تصرف كموظف مبيعات حقيقي.

## قائمة المنتجات والأسعار الحالية:
{get_prices_text()}

## سياسة الخصومات (حدود صارمة، لا تتجاوزها أبداً):
{build_discount_text()}

## عند اكتمال تفاصيل الطلب (منتجات + كمية + عنوان + السعر النهائي):
لا تؤكد الطلب نهائياً بنفسك — كل طلب يحتاج موافقة الإدارة أولاً. استخدم بالضبط هذي الصيغة:

[ORDER_PENDING]
{{"items": "وصف مختصر للمنتجات والكميات", "total": الرقم_بدون_فواصل, "address": "العنوان اللي ذكره الزبون"}}
[/ORDER_PENDING]
تم استلام طلبك! راح تتأكد لك من الإدارة خلال دقائق ونرجعلك فوراً 🙏

## عند الحاجة لتحويل الزبون لإنسان (خصم يتجاوز الحد، شكوى، سؤال خارج القائمة):
استخدم بالضبط هذي الصيغة:

[ESCALATE]سبب مختصر للتحويل[/ESCALATE]
راح أوصلك بالمسؤول المختص خلال دقائق 🙏

## ممنوع:
- تأكيد أي طلب نهائياً بنفسك بدون وسم [ORDER_PENDING]
- تأكيد توفر منتج غير موجود بالقائمة
- اختراع أي خصم غير مذكور أعلاه
"""


# ============================================================
# استدعاء Claude
# ============================================================
def ask_claude(user_message, phone_number):
    history = conversation_memory.get(phone_number, [])
    history.append({"role": "user", "content": user_message})

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 500,
            "system": build_system_prompt(),
            "messages": history[-(MEMORY_TURNS * 2):],
        },
        timeout=30,
    )
    response.raise_for_status()
    reply_text = response.json()["content"][0]["text"]

    history.append({"role": "assistant", "content": reply_text})
    conversation_memory[phone_number] = history[-(MEMORY_TURNS * 2):]
    return reply_text


# ============================================================
# إرسال رسالة واتساب
# ============================================================
def send_whatsapp_message(to_number, message_text):
    if not to_number:
        print("[تحذير] محاولة إرسال بدون رقم مستلم — تم التجاهل")
        return None
    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp", "recipient_type": "individual",
        "to": to_number, "type": "text", "text": {"body": message_text},
    }
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    if r.status_code != 200:
        print(f"[خطأ] إرسال الرسالة: {r.status_code} - {r.text}")
    return r


# ============================================================
# إضافة 1: إشعار صاحب البزنس بالطلبات الجديدة + تسجيلها
# ============================================================
def notify_owner_new_order(order_id, order_data, customer_number):
    text = (
        f"📦 طلب جديد بانتظار موافقتك\n\n"
        f"رقم الطلب: {order_id}\n"
        f"الزبون: {customer_number}\n"
        f"التفاصيل: {order_data.get('items', '-')}\n"
        f"العنوان: {order_data.get('address', '-')}\n"
        f"المجموع: {order_data.get('total', 0):,} دينار\n\n"
        f"للموافقة رد بـ: قبول {order_id}\n"
        f"للرفض رد بـ: رفض {order_id}"
    )
    send_whatsapp_message(OWNER_PHONE, text)
    log_order_to_sheet(order_id, order_data, customer_number, status="بانتظار الموافقة")


def log_order_to_sheet(order_id, order_data, customer_number, status):
    """تسجيل اختياري بجدول خارجي — لا يوقف التشغيل لو فشل أو لو ما تم إعداده"""
    if not ORDERS_APPEND_URL:
        print(f"[سجل الطلبات] {order_id} | {customer_number} | {status} | {order_data}")
        return
    try:
        requests.post(ORDERS_APPEND_URL, json={
            "order_id": order_id, "customer": customer_number,
            "status": status, "timestamp": time.strftime("%Y-%m-%d %H:%M"),
            **order_data,
        }, timeout=8)
    except Exception as e:
        print(f"[خطأ] تسجيل الطلب بالجدول الخارجي: {e}")


def extract_order_block(reply_text):
    match = re.search(r"\[ORDER_PENDING\](.*?)\[/ORDER_PENDING\]", reply_text, re.DOTALL)
    if not match:
        return None, reply_text
    try:
        order_data = json.loads(match.group(1).strip())
    except Exception:
        return None, reply_text
    cleaned = reply_text.replace(match.group(0), "").strip()
    return order_data, cleaned


def extract_escalate_block(reply_text):
    match = re.search(r"\[ESCALATE\](.*?)\[/ESCALATE\]", reply_text, re.DOTALL)
    if not match:
        return None, reply_text
    reason = match.group(1).strip()
    cleaned = reply_text.replace(match.group(0), "").strip()
    return reason, cleaned


# ============================================================
# إضافة 2: التحقق من موافقة/رفض صاحب البزنس على الطلب
# ============================================================
def handle_owner_reply(text, owner_number):
    match = re.match(r"^(قبول|رفض)\s+(\S+)", text.strip())
    if not match:
        return False  # مو رسالة موافقة/رفض، تجاهل

    action, order_id = match.group(1), match.group(2)
    order = pending_orders.get(order_id)
    if not order:
        send_whatsapp_message(owner_number, f"ما لقيت طلب بالرقم {order_id} — تأكد من الرقم.")
        return True

    customer_number = order["customer_number"]
    if action == "قبول":
        order["status"] = "مقبول"
        send_whatsapp_message(
            customer_number,
            f"تم تأكيد طلبك رقم {order_id} ✅\nالمجموع: {order['data'].get('total', 0):,} دينار\nراح نوصلك بأقرب وقت، شكراً لثقتك!"
        )
        send_whatsapp_message(owner_number, f"تم إعلام الزبون بقبول الطلب {order_id} ✅")
    else:
        order["status"] = "مرفوض"
        send_whatsapp_message(
            customer_number,
            f"نعتذر، ما نقدر ننفذ طلبك رقم {order_id} حالياً 🙏 تواصل معنا لمعرفة السبب أو لتعديل الطلب."
        )
        send_whatsapp_message(owner_number, f"تم إعلام الزبون برفض الطلب {order_id}.")

    log_order_to_sheet(order_id, order["data"], customer_number, status=order["status"])
    return True


# ============================================================
# إضافة 3: حماية من الرسائل المكررة + إضافة 4: تحديد معدل الاستخدام
# ============================================================
def is_duplicate_message(message_id):
    if message_id in seen_message_ids:
        return True
    seen_message_ids.append(message_id)
    return False


def is_rate_limited(phone_number):
    now = time.time()
    timestamps = message_timestamps[phone_number]
    timestamps[:] = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    if len(timestamps) >= RATE_LIMIT_MAX_MSGS:
        return True
    timestamps.append(now)
    return False


# ============================================================
# نقاط استقبال Webhook
# ============================================================
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "خطأ بالتحقق", 403


@app.route("/webhook", methods=["POST"])
def receive_message():
    data = request.get_json()
    from_number = None
    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" not in value:
            return jsonify({"status": "ignored"}), 200

        message = value["messages"][0]
        message_id = message.get("id", "")
        from_number = message["from"]
        user_text = message.get("text", {}).get("body", "")

        if is_duplicate_message(message_id):
            return jsonify({"status": "duplicate_ignored"}), 200

        if not user_text:
            send_whatsapp_message(from_number, "استلمت رسالتك، بس أقدر أتعامل حالياً مع النصوص فقط 🙏")
            return jsonify({"status": "non_text_handled"}), 200

        # -- رسالة من صاحب البزنس (قبول/رفض طلب) --
        if OWNER_PHONE and from_number == OWNER_PHONE:
            handled = handle_owner_reply(user_text, from_number)
            if handled:
                return jsonify({"status": "owner_action_handled"}), 200
            # إذا ما كانت قبول/رفض، تكمل كرسالة عادية (نادراً ما تحتاجها)

        # -- حماية من إغراق الرسائل --
        if is_rate_limited(from_number):
            send_whatsapp_message(from_number, "وصلتنا رسائل كثيرة منك بوقت قصير — نرجع لك خلال شوي 🙏")
            return jsonify({"status": "rate_limited"}), 200

        # -- المسار العادي: رد الذكاء الاصطناعي --
        reply = ask_claude(user_text, from_number)

        order_data, reply = extract_order_block(reply)
        if order_data:
            order_id = f"{BUSINESS_NAME[:2]}{int(time.time()) % 100000}"
            pending_orders[order_id] = {
                "data": order_data, "customer_number": from_number, "status": "بانتظار الموافقة"
            }
            notify_owner_new_order(order_id, order_data, from_number)

        reason, reply = extract_escalate_block(reply)
        if reason and OWNER_PHONE:
            send_whatsapp_message(
                OWNER_PHONE,
                f"⚠️ تحويل يحتاج تدخلك\nالزبون: {from_number}\nالسبب: {reason}"
            )

        send_whatsapp_message(from_number, reply)

    except Exception as e:
        print(f"[خطأ] معالجة الرسالة: {e}")
        if from_number:
            try:
                send_whatsapp_message(from_number, "صار خلل تقني بسيط، راح يتواصل معك أحد فريقنا قريباً 🙏")
            except Exception:
                pass
    return jsonify({"status": "received"}), 200


@app.route("/", methods=["GET"])
def health_check():
    return f"وكيل {BUSINESS_NAME} يعمل ✅ | طلبات معلّقة: {len(pending_orders)}", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
