"""
محرك وكيل مبيعات واتساب — نسخة محدّثة بنظام موافقة صاحب البزنس على الطلبات
"""

import os
import csv
import hmac
import hashlib
import io
import json
import re
import time
import uuid
import requests
from collections import defaultdict, deque
from functools import wraps
from flask import Flask, request, jsonify, Response, render_template_string

app = Flask(__name__)

# ==== إعدادات الاتصال ====
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "change-me")
SHEET_CSV_URL = os.environ.get("SHEET_CSV_URL")

# سر تطبيق Meta (Settings > Basic > App Secret) — لو موجود، يتم التحقق من توقيع كل ويبهوك وارد
APP_SECRET = os.environ.get("APP_SECRET", "")

# كلمة مرور صفحة إدارة الطلبات /admin/orders — إلزامية لتفعيل الصفحة
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# رقم صاحب البزنس — يستقبل إشعارات الطلبات ويوافق/يرفض عليها
OWNER_PHONE = os.environ.get("OWNER_PHONE", "").replace("+", "").strip()

# رابط اختياري لتسجيل الطلبات بجدول Google Sheet منفصل (Apps Script Web App)
ORDERS_APPEND_URL = os.environ.get("ORDERS_APPEND_URL", "")

# رابط قاعدة بيانات Postgres — لو موجود، الطلبات تُخزّن فيها بدل الذاكرة المؤقتة
# (تنجو من إعادة تشغيل السيرفر). لو غير موجود، يرجع نفس سلوك النسخة السابقة (ذاكرة مؤقتة).
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# ==== بيانات خاصة بكل عميل ====
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "المتجر")
DISCOUNT_TIERS = json.loads(os.environ.get("DISCOUNT_TIERS", "[]"))

# ==== ذاكرة تشغيلية (تُمسح عند إعادة تشغيل السيرفر) ====
conversation_memory = {}          # رقم الزبون -> آخر 15 تبادل رسائل
pending_orders = {}               # يُستخدم فقط لو DATABASE_URL غير مضبوط (احتياط/تجربة محلية)
seen_message_ids = deque(maxlen=500)   # لمنع الرد المكرر على نفس الرسالة
message_timestamps = defaultdict(list)  # رقم الزبون -> أوقات آخر رسائله (لضبط معدل الاستخدام)

MEMORY_TURNS = 15          # آخر 15 رسالة من الزبون (= 30 عنصر بالتاريخ: سؤال+رد)
RATE_LIMIT_MAX_MSGS = 25   # أقصى عدد رسائل بالساعة الواحدة لكل زبون
RATE_LIMIT_WINDOW = 3600   # ثانية (ساعة واحدة)
PRICES_CACHE_TTL = 180     # ثانية — كم نحتفظ بالأسعار قبل ما نعيد تحميلها من الشيت

_prices_cache = {"text": None, "fetched_at": 0}


# ============================================================
# قاعدة البيانات (Postgres) — تخزين دائم للطلبات ينجو من إعادة تشغيل السيرفر
# لو DATABASE_URL غير مضبوط: كل الدوال تحته ترجع لنفس سلوك قاموس pending_orders
# بالذاكرة (سلوك النسخة الأصلية بالضبط، بدون أي تغيير).
# ============================================================
def get_db_connection():
    import psycopg2  # استيراد كسول: ما نحتاج المكتبة مثبّتة إلا لو DATABASE_URL مضبوط فعلاً
    return psycopg2.connect(DATABASE_URL)


def init_db():
    if not DATABASE_URL:
        return
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS orders (
                        order_id TEXT PRIMARY KEY,
                        customer_number TEXT NOT NULL,
                        status TEXT NOT NULL,
                        items TEXT,
                        address TEXT,
                        total BIGINT DEFAULT 0,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                """)
    finally:
        conn.close()


def order_exists(order_id):
    if not DATABASE_URL:
        return order_id in pending_orders
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM orders WHERE order_id = %s", (order_id,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def create_order(order_id, order_data, customer_number, status="بانتظار الموافقة"):
    if not DATABASE_URL:
        pending_orders[order_id] = {
            "data": order_data,
            "customer_number": customer_number,
            "status": status,
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        return
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO orders (order_id, customer_number, status, items, address, total)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (order_id, customer_number, status,
                     order_data.get("items", ""), order_data.get("address", ""),
                     order_data.get("total", 0)),
                )
    finally:
        conn.close()


def _row_to_order(row):
    order_id, customer_number, status, items, address, total, created_at = row
    return order_id, {
        "data": {"items": items, "address": address, "total": total},
        "customer_number": customer_number,
        "status": status,
        "created_at": created_at.strftime("%Y-%m-%d %H:%M") if hasattr(created_at, "strftime") else str(created_at),
    }


def get_order(order_id):
    if not DATABASE_URL:
        return pending_orders.get(order_id)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT order_id, customer_number, status, items, address, total, created_at
                   FROM orders WHERE order_id = %s""",
                (order_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            _, order = _row_to_order(row)
            return order
    finally:
        conn.close()


def update_order_status(order_id, new_status, expected_current_status="بانتظار الموافقة"):
    """يحدّث حالة الطلب فقط لو حالته الحالية مطابقة للمتوقع (يمنع الموافقة/الرفض المزدوج
    حتى لو وصل طلبان بنفس اللحظة). يرجع True لو تم التحديث فعلاً."""
    if not DATABASE_URL:
        order = pending_orders.get(order_id)
        if not order or order["status"] != expected_current_status:
            return False
        order["status"] = new_status
        return True
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE orders SET status = %s, updated_at = now()
                       WHERE order_id = %s AND status = %s""",
                    (new_status, order_id, expected_current_status),
                )
                return cur.rowcount > 0
    finally:
        conn.close()


def list_orders():
    """يرجع لستة (order_id, order) — الأحدث أولاً."""
    if not DATABASE_URL:
        return list(reversed(list(pending_orders.items())))
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT order_id, customer_number, status, items, address, total, created_at
                   FROM orders ORDER BY created_at DESC"""
            )
            return [_row_to_order(row) for row in cur.fetchall()]
    finally:
        conn.close()


def count_pending_orders():
    if not DATABASE_URL:
        return sum(1 for o in pending_orders.values() if o["status"] == "بانتظار الموافقة")
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM orders WHERE status = %s", ("بانتظار الموافقة",))
            return cur.fetchone()[0]
    finally:
        conn.close()


if DATABASE_URL:
    init_db()


# ============================================================
# قراءة الأسعار الحية (مع كاش قصير لتقليل زمن الاستجابة وحمل الشبكة)
# ============================================================
def get_prices_text(force_refresh=False):
    now = time.time()
    if not force_refresh and _prices_cache["text"] is not None and (now - _prices_cache["fetched_at"]) < PRICES_CACHE_TTL:
        return _prices_cache["text"]
    try:
        response = requests.get(SHEET_CSV_URL, timeout=10)
        response.raise_for_status()
        reader = csv.reader(io.StringIO(response.text))
        rows = list(reader)
        lines = []
        for row in rows[1:]:
            if len(row) >= 2 and row[0].strip():
                lines.append(f"- {row[0].strip()} = {row[1].strip()} دينار عراقي")
        text = "\n".join(lines) if lines else "لا توجد أسعار محدثة حالياً."
        _prices_cache["text"] = text
        _prices_cache["fetched_at"] = now
        return text
    except Exception as e:
        print(f"[خطأ] قراءة الأسعار: {e}")
        # لو عندنا نسخة قديمة بالكاش نرجعها بدل ما نوقف الرد كلياً
        if _prices_cache["text"] is not None:
            return _prices_cache["text"]
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
def generate_order_id():
    prefix = "".join(ch for ch in BUSINESS_NAME[:2] if not ch.isspace()) or "طل"
    for _ in range(5):
        candidate = f"{prefix}-{uuid.uuid4().hex[:5].upper()}"
        if not order_exists(candidate):
            return candidate
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"  # احتياط بعيد الاحتمال


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
def resolve_order(order_id, action, notify_owner=None):
    """action: 'قبول' أو 'رفض'. يرجع (نجح: bool, رسالة نصية للعرض)."""
    order = get_order(order_id)
    if not order:
        return False, f"ما لقيت طلب بالرقم {order_id} — تأكد من الرقم."

    new_status = "مقبول" if action == "قبول" else "مرفوض"
    updated = update_order_status(order_id, new_status, expected_current_status="بانتظار الموافقة")
    if not updated:
        current = get_order(order_id)
        current_status = current["status"] if current else order["status"]
        return False, f"الطلب {order_id} تم التعامل معه مسبقاً (الحالة الحالية: {current_status})."

    customer_number = order["customer_number"]
    if action == "قبول":
        send_whatsapp_message(
            customer_number,
            f"تم تأكيد طلبك رقم {order_id} ✅\nالمجموع: {order['data'].get('total', 0):,} دينار\nراح نوصلك بأقرب وقت، شكراً لثقتك!"
        )
        result_text = f"تم إعلام الزبون بقبول الطلب {order_id} ✅"
    else:
        send_whatsapp_message(
            customer_number,
            f"نعتذر، ما نقدر ننفذ طلبك رقم {order_id} حالياً 🙏 تواصل معنا لمعرفة السبب أو لتعديل الطلب."
        )
        result_text = f"تم إعلام الزبون برفض الطلب {order_id}."

    if notify_owner:
        send_whatsapp_message(notify_owner, result_text)
    log_order_to_sheet(order_id, order["data"], customer_number, status=new_status)
    return True, result_text


def handle_owner_reply(text, owner_number):
    match = re.match(r"^(قبول|رفض)\s+(\S+)", text.strip())
    if not match:
        return False  # مو رسالة موافقة/رفض، تجاهل

    action, order_id = match.group(1), match.group(2)
    _, message = resolve_order(order_id, action)
    send_whatsapp_message(owner_number, message)
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


def verify_meta_signature(request_obj):
    """يتحقق أن الطلب فعلاً من Meta (وليس من أي طرف يعرف رابط الويبهوك) عبر مقارنة
    التوقيع X-Hub-Signature-256 مع HMAC-SHA256 لمحتوى الطلب باستخدام App Secret.
    يعمل فقط لو تم ضبط APP_SECRET — إذا ما كان مضبوط، يسمح بالمرور (سلوك النسخة الأصلية)."""
    if not APP_SECRET:
        return True
    signature = request_obj.headers.get("X-Hub-Signature-256", "")
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), request_obj.get_data(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[len("sha256="):], expected)


@app.route("/webhook", methods=["POST"])
def receive_message():
    if not verify_meta_signature(request):
        print("[أمان] توقيع ويبهوك غير صالح — تم رفض الطلب")
        return jsonify({"status": "invalid_signature"}), 403

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
            order_id = generate_order_id()
            create_order(order_id, order_data, from_number)
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
    return f"وكيل {BUSINESS_NAME} يعمل ✅ | طلبات معلّقة: {count_pending_orders()}", 200


# ============================================================
# صفحة إدارة الطلبات — /admin/orders
# ============================================================
def require_admin_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not ADMIN_PASSWORD:
            return "لازم تضبط متغيّر ADMIN_PASSWORD بالسيرفر أولاً لتفعيل هذه الصفحة.", 503
        auth = request.authorization
        if not auth or not hmac.compare_digest(auth.password or "", ADMIN_PASSWORD):
            return Response(
                "يلزم تسجيل الدخول للوصول لصفحة الطلبات.", 401,
                {"WWW-Authenticate": 'Basic realm="Orders Dashboard"'},
            )
        return view(*args, **kwargs)
    return wrapped


ORDERS_PAGE_TEMPLATE = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>طلبات {{ business_name }}</title>
<meta http-equiv="refresh" content="30">
<style>
  body { font-family: -apple-system, Tahoma, Arial, sans-serif; background:#f5f5f4; margin:0; padding:16px; color:#1c1917; }
  h1 { font-size:1.3rem; margin:0 0 4px; }
  .sub { color:#78716c; font-size:.85rem; margin-bottom:16px; }
  .stats { display:flex; gap:10px; margin-bottom:18px; flex-wrap:wrap; }
  .stat { background:#fff; border-radius:10px; padding:10px 16px; box-shadow:0 1px 3px rgba(0,0,0,.08); min-width:100px; text-align:center; }
  .stat b { display:block; font-size:1.4rem; }
  table { width:100%; border-collapse:collapse; background:#fff; border-radius:10px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,.08); margin-bottom:24px; }
  th, td { padding:10px 12px; text-align:right; font-size:.9rem; border-bottom:1px solid #f0f0ef; vertical-align:top; }
  th { background:#292524; color:#fff; font-weight:600; }
  tr:last-child td { border-bottom:none; }
  .badge { padding:3px 9px; border-radius:999px; font-size:.75rem; font-weight:600; white-space:nowrap; }
  .badge.pending { background:#fef3c7; color:#92400e; }
  .badge.accepted { background:#dcfce7; color:#166534; }
  .badge.rejected { background:#fee2e2; color:#991b1b; }
  form.inline { display:inline; }
  button { border:none; border-radius:6px; padding:6px 12px; font-size:.8rem; cursor:pointer; margin-inline-start:4px; }
  button.accept { background:#16a34a; color:#fff; }
  button.reject { background:#dc2626; color:#fff; }
  .empty { color:#78716c; padding:20px; text-align:center; }
  section h2 { font-size:1rem; color:#57534e; margin:0 0 8px; }
</style>
</head>
<body>
  <h1>📦 طلبات {{ business_name }}</h1>
  <div class="sub">تحديث تلقائي كل 30 ثانية · آخر تحميل: {{ now }}</div>

  <div class="stats">
    <div class="stat"><b>{{ pending|length }}</b>بانتظار الموافقة</div>
    <div class="stat"><b>{{ accepted|length }}</b>مقبولة</div>
    <div class="stat"><b>{{ rejected|length }}</b>مرفوضة</div>
  </div>

  <section>
    <h2>بانتظار الموافقة</h2>
    {% if pending %}
    <table>
      <tr><th>الوقت</th><th>رقم الطلب</th><th>الزبون</th><th>التفاصيل</th><th>العنوان</th><th>المجموع</th><th>إجراء</th></tr>
      {% for oid, o in pending %}
      <tr>
        <td>{{ o.created_at or '-' }}</td>
        <td>{{ oid }}</td>
        <td dir="ltr">{{ o.customer_number }}</td>
        <td>{{ o.data.get('items','-') }}</td>
        <td>{{ o.data.get('address','-') }}</td>
        <td>{{ "{:,}".format(o.data.get('total',0)) }} د.ع</td>
        <td>
          <form class="inline" method="post" action="/admin/orders/{{ oid }}/accept"><button class="accept">قبول</button></form>
          <form class="inline" method="post" action="/admin/orders/{{ oid }}/reject"><button class="reject">رفض</button></form>
        </td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">لا توجد طلبات بانتظار الموافقة حالياً.</div>
    {% endif %}
  </section>

  <section>
    <h2>سجل الطلبات المنتهية (هذه الجلسة)</h2>
    {% if accepted or rejected %}
    <table>
      <tr><th>الوقت</th><th>رقم الطلب</th><th>الزبون</th><th>التفاصيل</th><th>المجموع</th><th>الحالة</th></tr>
      {% for oid, o in (accepted + rejected) %}
      <tr>
        <td>{{ o.created_at or '-' }}</td>
        <td>{{ oid }}</td>
        <td dir="ltr">{{ o.customer_number }}</td>
        <td>{{ o.data.get('items','-') }}</td>
        <td>{{ "{:,}".format(o.data.get('total',0)) }} د.ع</td>
        <td>
          {% if o.status == 'مقبول' %}<span class="badge accepted">مقبول</span>
          {% else %}<span class="badge rejected">مرفوض</span>{% endif %}
        </td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">لا يوجد سجل بعد.</div>
    {% endif %}
  </section>
</body>
</html>
"""


@app.route("/admin/orders", methods=["GET"])
@require_admin_auth
def admin_orders_page():
    items = list_orders()  # الأحدث أولاً
    pending = [(oid, o) for oid, o in items if o["status"] == "بانتظار الموافقة"]
    accepted = [(oid, o) for oid, o in items if o["status"] == "مقبول"]
    rejected = [(oid, o) for oid, o in items if o["status"] == "مرفوض"]
    return render_template_string(
        ORDERS_PAGE_TEMPLATE,
        business_name=BUSINESS_NAME,
        now=time.strftime("%Y-%m-%d %H:%M:%S"),
        pending=pending, accepted=accepted, rejected=rejected,
    )


@app.route("/admin/orders/<order_id>/<action>", methods=["POST"])
@require_admin_auth
def admin_orders_action(order_id, action):
    if action not in ("accept", "reject"):
        return "إجراء غير معروف", 400
    resolve_order(order_id, "قبول" if action == "accept" else "رفض", notify_owner=OWNER_PHONE or None)
    return Response(status=302, headers={"Location": "/admin/orders"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
