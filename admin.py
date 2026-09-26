import os, json, functools, secrets, sqlite3, datetime as dt
from markupsafe import Markup
from urllib.parse import quote as urlq
from flask import (Blueprint, render_template, request, redirect, url_for, session, abort, jsonify, flash)
from db import one, many, run, now, num, audit
import backup as bk
from verticals import get as vget, V, FLOW, STEP_NAMES
import orders as od, reports as rp, pricing, menu_ocr
from flask import send_file

bp = Blueprint("smart", __name__)
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
CRON_KEY = os.getenv("CRON_KEY", "")
BAGHDAD = dt.timezone(dt.timedelta(hours=3))

# ---------- CSRF: every POST must carry the token from this session ----------
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(24)
    return session["_csrf"]

@bp.app_context_processor
def inject_csrf():
    return {"csrf": lambda: Markup(f'<input type="hidden" name="_csrf" value="{csrf_token()}">'),
            "csrf_value": csrf_token}

@bp.before_app_request
def check_csrf():
    if request.method == "POST" and request.blueprint == "smart":
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF")
        if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
            abort(400, "انتهت صلاحية الصفحة. حدّثها وحاول مرة ثانية.")

def safe_next(url, fallback):
    return url if url and url.startswith("/") and not url.startswith("//") and "\\" not in url else fallback

# ---------- login lockout: 5 wrong PINs -> 15 minutes ----------
def locked(key):
    r = one("SELECT * FROM login_attempts WHERE k=?", key)
    return bool(r and r["locked_until"] and r["locked_until"] > now())

def register_fail(key):
    r = one("SELECT * FROM login_attempts WHERE k=?", key)
    fails = (r["fails"] if r else 0) + 1
    until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=15)).isoformat(timespec="seconds") if fails >= 5 else None
    run("INSERT OR REPLACE INTO login_attempts(k, fails, locked_until) VALUES(?,?,?)", key, 0 if until else fails, until)

@bp.app_template_filter("local")
def local(iso, fmt="%d/%m %I:%M %p"):
    if not iso:
        return ""
    t = dt.datetime.fromisoformat(iso).astimezone(BAGHDAD).strftime(fmt)
    return t.replace("AM", "ص").replace("PM", "م")

@bp.app_template_filter("iqd")
def iqd(n):
    return f"{int(n or 0):,}"

@bp.app_template_filter("qty")
def qty(n):
    return int(n) if float(n).is_integer() else n

def biz(slug):
    b = one("SELECT * FROM businesses WHERE slug=?", slug)
    if not b:
        abort(404)
    b["v"] = vget(b["vertical"])
    return b

def login_required(owner_only=False):
    def deco(f):
        @functools.wraps(f)
        def w(slug, *a, **k):
            role = session.get(f"role:{slug}")
            if not role or (owner_only and role != "owner"):
                return redirect(url_for("smart.login", slug=slug, next=request.path))
            return f(slug, *a, **k)
        return w
    return deco

# ---------- setup & login ----------
@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if not ADMIN_KEY or request.args.get("key") != ADMIN_KEY:
        abort(403)
    if request.method == "POST":
        f = request.form
        slug = f["slug"].strip().lower()
        if len(f["owner_pin"]) < 6 or len(f["staff_pin"]) < 6 or f["owner_pin"] == f["staff_pin"]:
            flash("الرموز لازم تكون 6 أرقام أو أكثر، ورمز المالك يختلف عن رمز الموظفين.")
            return render_template("setup.html", verticals=V, b={"v": vget(f["vertical"])}, f=f)
        try:
            run("""INSERT INTO businesses(slug,name,vertical,phone,phone_number_id,owner_phone,owner_pin,staff_pin,
                   address,late_after_min) VALUES(?,?,?,?,?,?,?,?,?,?)""", slug, f["name"], f["vertical"],
                pricing.norm(f["phone"]), f.get("phone_number_id", "").strip() or None, pricing.norm(f["owner_phone"]),
                f["owner_pin"], f["staff_pin"], f.get("address", ""), num(f.get("late_after_min"), default=60, lo=10))
        except sqlite3.IntegrityError:
            flash(f"الرابط «{slug}» مستخدم لعميل ثاني. اختر رابط غيره.")
            return render_template("setup.html", verticals=V, b={"v": vget(f["vertical"])}, f=f)
        return redirect(url_for("smart.login", slug=slug))
    return render_template("setup.html", verticals=V, b={"v": vget("restaurant")}, f={})

@bp.route("/b/<slug>/login", methods=["GET", "POST"])
def login(slug):
    b = biz(slug)
    key = f"{slug}|{request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()}"
    if request.method == "POST":
        if locked(key):
            flash("محاولات خاطئة كثيرة. انتظر ربع ساعة وجرّب مرة ثانية.")
            return render_template("login.html", b=b)
        pin = request.form.get("pin", "")
        role = ("owner" if secrets.compare_digest(pin, b["owner_pin"]) else
                "staff" if secrets.compare_digest(pin, b["staff_pin"]) else None)
        if role:
            run("DELETE FROM login_attempts WHERE k=?", key)
            session.permanent = True
            session[f"role:{slug}"] = role
            session[f"name:{slug}"] = (request.form.get("name") or "").strip()[:40] or ("المالك" if role == "owner" else "موظف")
            return redirect(safe_next(request.args.get("next"), url_for("smart.orders_list", slug=slug)))
        register_fail(key)
        flash("الرمز غير صحيح. تأكد منه واكتبه مرة ثانية.")
    return render_template("login.html", b=b)

@bp.route("/b/<slug>/logout")
def logout(slug):
    session.pop(f"role:{slug}", None)
    return redirect(url_for("smart.login", slug=slug))

# ---------- orders ----------
@bp.route("/b/<slug>/")
@bp.route("/b/<slug>/orders")
@login_required()
def orders_list(slug):
    b = biz(slug)
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=14)).isoformat()
    rows = many("""SELECT * FROM orders WHERE business_id=? AND (status IN ('received','preparing','out')
                   OR (status IN ('delivered','cancelled') AND created_at >= ?)) ORDER BY id""", b["id"], since)
    for o in rows:
        o["items"] = many("SELECT * FROM order_items WHERE order_id=?", o["id"])
    cols = {s: [o for o in rows if o["status"] == s] for s in ("received", "preparing", "out")}
    cols["done"] = [o for o in reversed(rows) if o["status"] in ("delivered", "cancelled")][:25]
    return render_template("orders.html", b=b, cols=cols, base_url=od.BASE_URL,
                           is_owner=session.get(f"role:{slug}") == "owner")

@bp.route("/b/<slug>/orders/<int:oid>/status", methods=["POST"])
@login_required()
def order_status(slug, oid):
    b = biz(slug)
    o = one("SELECT * FROM orders WHERE id=? AND business_id=?", oid, b["id"]) or abort(404)
    status = request.form.get("status")
    if status == "cancelled":
        if session.get(f"role:{slug}") != "owner":
            abort(403)
        reason = (request.form.get("reason") or "").strip()[:200]
        if not reason:
            flash("اكتب سبب الإلغاء حتى ينحفظ بالسجل.")
            return redirect(request.referrer or url_for("smart.orders_list", slug=slug))
        od.set_status(o["id"], status, session.get(f"name:{slug}"), reason=reason)
    else:
        od.set_status(o["id"], status, session.get(f"name:{slug}"),
                      driver_name=(request.form.get("driver_name") or "").strip()[:40] or None)
    return redirect(request.referrer or url_for("smart.orders_list", slug=slug))

@bp.route("/b/<slug>/orders/new", methods=["GET", "POST"])
@login_required()
def order_new(slug):
    b = biz(slug)
    if request.method == "POST":
        lines = [{"product_id": num(k[2:]), "qty": num(v, float, lo=0.01)} for k, v in request.form.items()
                 if k.startswith("q_")]
        lines = [l for l in lines if l["product_id"] and l["qty"] and l["qty"] <= 1000]
        try:
            o = od.create_order(b["id"], lines, request.form.get("phone") or None, request.form.get("cname", ""),
                                request.form.get("address", ""), request.form.get("notes", ""),
                                source=request.form.get("source", "phone"), entered_by=session.get(f"name:{slug}"))
        except ValueError:
            flash("اختر صنفاً واحداً على الأقل قبل حفظ الطلب.")
            return redirect(request.url)
        flash(f"انحفظ الطلب — {o['total']:,} دينار.")
        return redirect(url_for("smart.orders_list", slug=slug))
    products = many("SELECT * FROM products WHERE business_id=? AND active=1 ORDER BY sort, category, name", b["id"])
    return render_template("order_new.html", b=b, products=products)

@bp.route("/b/<slug>/quote", methods=["POST"])
@login_required()
def quote_api(slug):
    b = biz(slug)
    d = request.get_json(force=True)
    return jsonify(pricing.quote(b["id"], d.get("phone"), d.get("lines", [])))

# ---------- driver ----------
@bp.route("/d/<token>", methods=["GET", "POST"])
def driver(token):
    o = one("SELECT * FROM orders WHERE driver_token=?", token) or abort(404)
    b = one("SELECT * FROM businesses WHERE id=?", o["business_id"]); b["v"] = vget(b["vertical"])
    if request.method == "POST" and request.form.get("status") in ("out", "delivered"):
        od.set_status(o["id"], request.form["status"], "المندوب " + (o["driver_name"] or ""))
        return redirect(request.url)
    items = many("SELECT * FROM order_items WHERE order_id=?", o["id"])
    return render_template("driver.html", b=b, o=o, items=items)

# ---------- public tracking ----------
@bp.route("/track/<public_id>")
def track(public_id):
    o = one("SELECT * FROM orders WHERE public_id=?", public_id) or abort(404)
    b = one("SELECT * FROM businesses WHERE id=?", o["business_id"]); b["v"] = vget(b["vertical"])
    items = many("SELECT * FROM order_items WHERE order_id=?", o["id"])
    events = {e["status"]: e["at"] for e in many("SELECT * FROM order_events WHERE order_id=? ORDER BY id", o["id"])}
    step = FLOW.index(o["status"]) if o["status"] in FLOW else -1
    confirm_link = f"https://wa.me/{b['phone']}?text={urlq('نعم، وصلني الطلب')}"
    return render_template("track.html", b=b, o=o, items=items, events=events, step=step, flow=FLOW,
                           names=STEP_NAMES, confirm_link=confirm_link)

@bp.route("/b/<slug>/orders/<int:oid>/edit", methods=["GET", "POST"])
@login_required()
def order_edit(slug, oid):
    b = biz(slug)
    o = one("SELECT * FROM orders WHERE id=? AND business_id=?", oid, b["id"]) or abort(404)
    if o["status"] not in od.EDITABLE:
        flash("الطلب طلع من المحل، ما ينعدل. إذا اكو مشكلة، المالك يلغيه ويسجّل طلب جديد.")
        return redirect(url_for("smart.orders_list", slug=slug))
    if request.method == "POST":
        lines = [{"product_id": num(k[2:]), "qty": num(v, float, lo=0.01)} for k, v in request.form.items() if k.startswith("q_")]
        lines = [l for l in lines if l["product_id"] and l["qty"] and l["qty"] <= 1000]
        try:
            od.update_order(oid, lines, session.get(f"name:{slug}"), request.form.get("cname", "").strip()[:60],
                            request.form.get("address", "").strip()[:200], request.form.get("notes", "").strip()[:300])
        except ValueError as e:
            flash("ما يصير الطلب يكون فارغ. إذا الزبون ما يريد شي، المالك يلغي الطلب." if str(e) != "locked"
                  else "الطلب طلع من المحل قبل ما تحفظ التعديل.")
            return redirect(request.url)
        flash("انحفظ التعديل.")
        return redirect(url_for("smart.orders_list", slug=slug))
    products = many("SELECT * FROM products WHERE business_id=? AND active=1 ORDER BY sort, category, name", b["id"])
    current = {i["product_id"]: i["qty"] for i in many("SELECT * FROM order_items WHERE order_id=?", oid)}
    return render_template("order_new.html", b=b, products=products, o=o, current=current)

@bp.route("/b/<slug>/orders/<int:oid>/print")
@login_required()
def order_print(slug, oid):
    b = biz(slug)
    o = one("SELECT * FROM orders WHERE id=? AND business_id=?", oid, b["id"]) or abort(404)
    items = many("SELECT * FROM order_items WHERE order_id=?", oid)
    return render_template("print.html", b=b, o=o, items=items, copy=request.args.get("copy", "kitchen"))

@bp.route("/b/<slug>/log")
@login_required(owner_only=True)
def audit_view(slug):
    b = biz(slug)
    rows = many("SELECT * FROM audit_log WHERE business_id=? ORDER BY id DESC LIMIT 300", b["id"])
    return render_template("log.html", b=b, rows=rows)

@bp.route("/track/<public_id>/rate", methods=["POST"])
def rate(public_id):
    o = one("SELECT * FROM orders WHERE public_id=?", public_id) or abort(404)
    od.save_rating(o["id"], num(request.form.get("stars")), via="الصفحة")
    return redirect(url_for("smart.track", public_id=public_id))

# ---------- public catalog (the page customers browse and order from) ----------
@bp.route("/c/<slug>")
def catalog(slug):
    b = biz(slug)
    rows = many("SELECT * FROM products WHERE business_id=? AND active=1 ORDER BY sort, category, name", b["id"])
    sell = [p for p in rows if p["orderable"]]
    agency = [p for p in rows if not p["orderable"]]
    hero = sell[0] if sell else None
    look = {  # image file -> sack label style, section glow, headline, brand line
        "mandi_khojah": ("gold", "#7A4F16", "أرز المندي اليمني", "الخوجة أنور"),
        "sella1121": ("purple", "#5E1F55", "سيلا 1121", "أرز الخوجة أنور"),
        "haleema": ("white", "#2F4A2A", "حليمة الفاخر", "أرز هندي لمطاعم المندي"),
        "abidi": ("sand", "#6A3A1A", "العابدي ذهب", "أرز حبة طويلة"),
        "tea_kraft": ("kraft", "#5A3A1A", "شاي المناسبات", "سيلاني معطّر بإيرل غراي"),
    }
    chapters = []
    for p in sell:
        key = (p["image_url"] or "").rsplit("/", 1)[-1].split(".")[0]
        style, glow, title, brand = look.get(key, ("gold", "#7A4F16", p["name"].split("—")[0].strip(), ""))
        chapters.append({"id": p["id"], "name": p["name"], "title": title, "brand": brand, "note": p["note"] or "",
                         "unit": (p["unit"] or "").replace(" (الوزن يُؤكد)", ""), "style": style, "glow": glow,
                         "price": p["price"] if p["price_confirmed"] else None})
    palette = request.args.get("palette") or b.get("palette") or "tobacco"
    if palette not in ("tobacco", "ivory", "navy"):
        palette = "tobacco"
    return render_template("catalog.html", b=b, chapters=chapters, agency=agency, palette=palette)

# ---------- menu from photo ----------
@bp.route("/b/<slug>/menu", methods=["GET", "POST"])
@login_required(owner_only=True)
def menu(slug):
    b = biz(slug)
    if request.method == "POST" and "photo" in request.files:
        f = request.files["photo"]
        try:
            items = menu_ocr.extract(f.read(), f.mimetype or "image/jpeg")
        except menu_ocr.MenuReadError as e:
            flash(menu_ocr.ERRORS[str(e)])
            return redirect(request.url)
        except Exception as e:
            print("MENU_OCR_ERROR", repr(e), flush=True)
            flash(menu_ocr.ERRORS["parse"])
            return redirect(request.url)
        did = menu_ocr.save_draft(b["id"], items)
        return redirect(url_for("smart.menu_review", slug=slug, did=did))
    products = many("SELECT * FROM products WHERE business_id=? ORDER BY sort, category, name", b["id"])
    return render_template("menu.html", b=b, products=products)

@bp.route("/b/<slug>/menu/review/<int:did>", methods=["GET", "POST"])
@login_required(owner_only=True)
def menu_review(slug, did):
    b = biz(slug)
    d = one("SELECT * FROM menu_drafts WHERE id=? AND business_id=?", did, b["id"]) or abort(404)
    items = json.loads(d["items_json"])
    if request.method == "POST":
        f = request.form
        if f.get("mode") == "replace":
            run("UPDATE products SET active=0 WHERE business_id=?", b["id"])
        added = 0
        for i in range(min(num(f.get("count"), default=0), 1000)):
            name, price = f.get(f"name_{i}", "").strip()[:120], num(f.get(f"price_{i}"), lo=1)
            if not f.get(f"keep_{i}") or not name or not price:
                continue
            ex = one("SELECT id FROM products WHERE business_id=? AND name=?", b["id"], name)
            if ex:
                run("UPDATE products SET price=?, category=?, unit=?, active=1, price_confirmed=1 WHERE id=?",
                    price, f.get(f"cat_{i}", ""), f.get(f"unit_{i}", ""), ex["id"])
            else:
                run("INSERT INTO products(business_id,name,category,price,unit) VALUES(?,?,?,?,?)",
                    b["id"], name, f.get(f"cat_{i}", ""), price, f.get(f"unit_{i}", ""))
            added += 1
        audit(b["id"], session.get(f"name:{slug}"), "تحديث المنيو من صورة",
              f"{added} صنف" + ("، واستُبدل المنيو القديم" if f.get("mode") == "replace" else ""))
        flash(f"انحفظت {added} أصناف. البوت صار يسعّر منها من هسه.")
        return redirect(url_for("smart.menu", slug=slug))
    return render_template("menu_review.html", b=b, items=items, did=did)

@bp.route("/b/<slug>/menu/<int:pid>", methods=["POST"])
@login_required(owner_only=True)
def product_edit(slug, pid):
    b = biz(slug)
    f = request.form
    p = one("SELECT * FROM products WHERE id=? AND business_id=?", pid, b["id"]) or abort(404)
    who = session.get(f"name:{slug}")
    if f.get("toggle"):
        run("UPDATE products SET active=1-active WHERE id=? AND business_id=?", pid, b["id"])
        audit(b["id"], who, "إخفاء صنف" if p["active"] else "إظهار صنف", p["name"])
    else:
        price = num(f.get("price"), lo=1)
        if not price:
            flash("اكتب السعر كرقم أكبر من صفر.")
        else:
            run("UPDATE products SET price=?, price_confirmed=1 WHERE id=? AND business_id=?", price, pid, b["id"])
            if price != p["price"]:
                audit(b["id"], who, "تغيير سعر", f"{p['name']}: {p['price']:,} ← {price:,}")
            elif not p["price_confirmed"]:
                audit(b["id"], who, "تأكيد سعر", f"{p['name']}: {price:,}")
    return redirect(url_for("smart.menu", slug=slug))

# ---------- VIP customers ----------
@bp.route("/b/<slug>/vip", methods=["GET", "POST"])
@login_required(owner_only=True)
def vip(slug):
    b = biz(slug)
    if request.method == "POST":
        f = request.form
        phone = pricing.norm(f["phone"])
        pct = num(f.get("pct"), float, default=0, lo=0)
        if len(phone) < 12 or pct > 90:
            flash("تأكد من الرقم (مثل 07xxxxxxxxx) وإن الخصم بين 0 و90%.")
            return redirect(request.url)
        ex = one("SELECT id FROM vip_customers WHERE business_id=? AND phone=?", b["id"], phone)
        if ex:
            run("UPDATE vip_customers SET name=?, discount_pct=? WHERE id=?", f["name"][:60], pct, ex["id"])
            vid = ex["id"]
        else:
            vid = run("INSERT INTO vip_customers(business_id,phone,name,discount_pct) VALUES(?,?,?,?)",
                      b["id"], phone, f["name"][:60], pct)
        audit(b["id"], session.get(f"name:{slug}"), "زبون مميز", f"{f['name'][:60]} — خصم {pct:g}%")
        return redirect(url_for("smart.vip_prices", slug=slug, vid=vid))
    rows = many("""SELECT v.*, (SELECT COUNT(*) FROM vip_prices p WHERE p.vip_id=v.id) fixed
                   FROM vip_customers v WHERE business_id=? ORDER BY name""", b["id"])
    return render_template("vip.html", b=b, rows=rows)

@bp.route("/b/<slug>/vip/<int:vid>", methods=["GET", "POST"])
@login_required(owner_only=True)
def vip_prices(slug, vid):
    b = biz(slug)
    v = one("SELECT * FROM vip_customers WHERE id=? AND business_id=?", vid, b["id"]) or abort(404)
    if request.method == "POST":
        if request.form.get("delete"):
            run("DELETE FROM vip_prices WHERE vip_id=?", vid); run("DELETE FROM vip_customers WHERE id=?", vid)
            audit(b["id"], session.get(f"name:{slug}"), "حذف زبون مميز", v["name"])
            return redirect(url_for("smart.vip", slug=slug))
        run("DELETE FROM vip_prices WHERE vip_id=?", vid)
        valid = {p["id"] for p in many("SELECT id FROM products WHERE business_id=?", b["id"])}
        for k, val in request.form.items():
            pid, price = (num(k[2:]), num(val, lo=1)) if k.startswith("p_") else (None, None)
            if pid in valid and price:
                run("INSERT INTO vip_prices(vip_id,product_id,price) VALUES(?,?,?)", vid, pid, price)
        audit(b["id"], session.get(f"name:{slug}"), "أسعار خاصة", f"{v['name']}: {len([k for k in request.form if k.startswith('p_') and request.form[k].strip()])} صنف")
        flash(f"انحفظت أسعار {v['name']}.")
        return redirect(url_for("smart.vip", slug=slug))
    products = many("SELECT * FROM products WHERE business_id=? AND active=1 ORDER BY sort, category, name", b["id"])
    fixed = {r["product_id"]: r["price"] for r in many("SELECT * FROM vip_prices WHERE vip_id=?", vid)}
    return render_template("vip_prices.html", b=b, v=v, products=products, fixed=fixed)

# ---------- reports ----------
@bp.route("/b/<slug>/reports")
@login_required(owner_only=True)
def report_view(slug):
    b = biz(slug)
    kind = request.args.get("kind", "week")
    return render_template("reports.html", b=b, r=rp.build(b["id"], kind), kind=kind)

# ---------- cron (hit from cron-job.org) ----------
@bp.route("/cron/<task>")
def cron(task):
    if not CRON_KEY or request.args.get("key") != CRON_KEY:
        abort(403)
    if task == "late":
        return {"late_alerts": od.check_late_orders()}
    if task == "backup":
        return bk.run_backup()
    if task in ("week", "month"):
        rp.send_reports(task)
        return {"sent": task}
    abort(404)

@bp.route("/admin/backup")
def backup_download():
    """Download the newest backup (for keeping a copy on your own computer)."""
    if not ADMIN_KEY or request.args.get("key") != ADMIN_KEY:
        abort(403)
    path = bk.latest() or bk.snapshot()
    return send_file(path, as_attachment=True)
