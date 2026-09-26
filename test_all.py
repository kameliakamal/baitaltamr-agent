"""python test_all.py -> runs every feature + the security fixes with WhatsApp mocked."""
import os, io, datetime as dt, re
os.environ["DB_PATH"] = "/tmp/t.db"
for f in ("/tmp/t.db", "/tmp/t.db-wal", "/tmp/t.db-shm"):
    if os.path.exists(f): os.remove(f)
import whatsapp as wa
sent = []
wa.send_text = lambda to, body: (sent.append(("text", to, body)) or (True, ""))
wa.send_template = lambda to, name, params, lang="ar": (sent.append(("tpl", to, name)) or (True, ""))
import demo_app, orders as od, reports as rp, pricing, hooks, menu_ocr
from db import one, run, many, num
app = demo_app.app

def client(pin=None, name=""):
    c = app.test_client(); c.get("/b/demo/login")
    if pin: c.post("/b/demo/login", data={"pin": pin, "name": name, "_csrf": tok(c)})
    return c
def tok(c):
    with c.session_transaction() as s: return s.get("_csrf")

b = one("SELECT * FROM businesses WHERE slug='demo'")
# --- parsing
assert num("٥٠٠٠") == 5000 and num("5,000") == 5000 and num("abc") is None and num("-3", lo=1) is None
# --- pricing
vid = run("INSERT INTO vip_customers(business_id,phone,name,discount_pct) VALUES(?,?,?,?)", b["id"], "9647901234567", "أبو علي", 5)
run("INSERT INTO vip_prices VALUES(?,?,?)", vid, 2, 15000)
q = pricing.quote(b["id"], "07901234567", [{"product_id": 2, "qty": 4}, {"product_id": 4, "qty": 2}])
assert q["items"][0]["unit_price"] == 15000 and q["items"][1]["unit_price"] == 1900 and q["discount"] == 0
assert pricing.quote(b["id"], None, [{"product_id": 2, "qty": 4}])["discount"] == 6800
# --- security
c = app.test_client(); c.get("/b/demo/login")
assert c.post("/b/demo/login", data={"pin": "222222"}).status_code == 400, "CSRF missing must fail"
r = c.post("/b/demo/login?next=https://evil.io", data={"pin": "222222", "_csrf": tok(c)})
assert r.headers["Location"].startswith("/b/demo/"), "open redirect"
bad = app.test_client(); bad.get("/b/demo/login")
for _ in range(5): bad.post("/b/demo/login", data={"pin": "000000", "_csrf": tok(bad)})
r = bad.post("/b/demo/login", data={"pin": "222222", "_csrf": tok(bad)})
assert "انتظر" in r.get_data(as_text=True), "lockout after 5 fails"
run("DELETE FROM login_attempts")
staff = client("222222", "حيدر")
# --- staff phone order + XSS-proof name
r = staff.post("/b/demo/orders/new", data={"_csrf": tok(staff), "source": "phone", "phone": "07901234567",
    "cname": "x');alert(1);('", "address": "حي الأمير", "q_1": "٢", "q_5": "3", "q_999": "1", "q_4": "abc"})
assert r.status_code == 302
o = one("SELECT * FROM orders ORDER BY id DESC LIMIT 1")
assert o["entered_by"] == "حيدر" and o["total"] == 2 * 8550 + 3 * 950
assert sent[-1][0] == "tpl", "customer never messaged us -> must use template"
html = staff.get("/b/demo/orders").get_data(as_text=True)
_own = client("111111"); ohtml = _own.get("/b/demo/orders").get_data(as_text=True)
assert 'onclick="return askReason(this)"' in ohtml and "x&#39;);alert(1);(&#39;" in ohtml, "name only inside escaped data attribute"
for p in ["/b/demo/orders/new", f"/track/{o['public_id']}", f"/d/{o['driver_token']}"]:
    assert staff.get(p).status_code == 200, p
assert staff.get("/b/demo/menu").status_code == 302, "staff blocked from owner pages"
# --- status rules
od.set_status(o["id"], "out"); assert one("SELECT status FROM orders WHERE id=?", o["id"])["status"] == "received", "can't skip preparing"
od.set_status(o["id"], "preparing", "حيدر"); od.set_status(o["id"], "out", "حيدر", driver_name="كرار")
drv = app.test_client(); drv.get(f"/d/{o['driver_token']}")
drv.post(f"/d/{o['driver_token']}", data={"status": "delivered", "_csrf": tok(drv)})
assert one("SELECT status FROM orders WHERE id=?", o["id"])["status"] == "delivered"
od.set_status(o["id"], "cancelled"); assert one("SELECT status FROM orders WHERE id=?", o["id"])["status"] == "delivered", "delivered is final"
# --- receipt confirmation: whole words only
assert not od.handle_delivery_reply(b["id"], "9647901234567", "ايش صار بالطلب")
assert not od.handle_delivery_reply(b["id"], "9647901234567", "نعم بس ناقص بيبسي")
assert od.handle_delivery_reply(b["id"], "9647901234567", "اي وصل")
# --- pickup order closes without a driver
p = od.create_order(b["id"], [{"product_id": 3, "qty": 1}], None, "", "", source="walkin", entered_by="حيدر")
od.set_status(p["id"], "preparing"); od.set_status(p["id"], "delivered")
assert one("SELECT status FROM orders WHERE id=?", p["id"])["status"] == "delivered"
# --- webhook: retries are ignored, voice/text pass through, owner gets report on request
got = []
payload = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "x"},
           "messages": [{"id": "wamid.1", "from": "9647711111111", "type": "text", "text": {"body": "اريد مندي"}}]}}]}]}
import threading
hooks.handle_webhook(payload, lambda bz, who, text: got.append(text)); hooks.handle_webhook(payload, lambda *a: got.append("DUP"))
for t in threading.enumerate():
    if t is not threading.current_thread() and t.daemon: t.join(5)
assert got == ["اريد مندي"], got
assert wa.in_window(b["id"], "9647711111111"), "inbound message opens the 24h window"
n = len(sent); hooks.preprocess(b, {"from": b["owner_phone"], "type": "text", "text": {"body": "تقرير"}})
assert sent[n][0] == "text" and "التقرير" in sent[n][2]
# --- owner pages + menu review with bad input
owner = client("111111")
for pth in ["/b/demo/menu", "/b/demo/vip", f"/b/demo/vip/{vid}", "/b/demo/reports", "/b/demo/reports?kind=month"]:
    assert owner.get(pth).status_code == 200, pth
did = menu_ocr.save_draft(b["id"], [{"name": "كبة", "category": "المقبلات", "price": 3000, "unit": ""}, {"name": "لبن", "category": "", "price": None, "unit": ""}])
owner.post(f"/b/demo/menu/review/{did}", data={"_csrf": tok(owner), "count": "2", "mode": "merge", "keep_0": "1", "name_0": "كبة", "price_0": "٣٠٠٠", "keep_1": "1", "name_1": "لبن", "price_1": "مو معروف"})
assert one("SELECT price FROM products WHERE name='كبة'")["price"] == 3000 and not one("SELECT id FROM products WHERE name='لبن'")
assert owner.post("/b/demo/menu/1", data={"_csrf": tok(owner), "price": ""}).status_code == 302  # no crash
assert owner.post(f"/b/demo/vip/{vid}", data={"_csrf": tok(owner), "p_2": "x", "p_1": "8000"}).status_code == 302
# --- menu photo shrink
from PIL import Image
big = io.BytesIO(); Image.new("RGB", (6000, 4000), "white").save(big, "PNG")
small, mime = menu_ocr.prepare(big.getvalue()); assert mime == "image/jpeg" and max(Image.open(io.BytesIO(small)).size) == 2000
try: menu_ocr.extract(b"not an image"); raise SystemExit("should fail")
except menu_ocr.MenuReadError as e: assert str(e) == "not_an_image"
# --- setup duplicate slug + weak pins
os.environ["ADMIN_KEY"] = ""; import admin; admin.ADMIN_KEY = "k"
s = app.test_client(); s.get("/setup?key=k")
form = {"_csrf": tok(s), "name": "x", "slug": "demo", "vertical": "grocery", "phone": "07700000000", "owner_phone": "07711111111", "owner_pin": "123456", "staff_pin": "654321"}
assert "مستخدم" in s.post("/setup?key=k", data=form).get_data(as_text=True)
assert "6 أرقام" in s.post("/setup?key=k", data={**form, "slug": "new", "owner_pin": "1"}).get_data(as_text=True)
# --- reports + late
last_week = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat()
run("UPDATE orders SET created_at=?", last_week); print(rp.as_whatsapp(b, rp.build(b["id"], "week"))[:120])
o2 = od.create_order(b["id"], [{"product_id": 3, "qty": 1}], "07700000001", "تست", "النجف")
run("UPDATE orders SET created_at=? WHERE id=?", last_week, o2["id"]); assert od.check_late_orders() == 1
print("ALL OK")

# ================= new features =================
import backup as bk, glob
sent.clear()
staff = client("222222", "حيدر"); owner = client("111111", "أبو مهدي")
e = od.create_order(b["id"], [{"product_id": 1, "qty": 1}], "07705550000", "مصطفى", "حي السعد", source="phone", entered_by="حيدر")
# --- edit
r = staff.post(f"/b/demo/orders/{e['id']}/edit", data={"_csrf": tok(staff), "q_1": "2", "q_5": "1", "cname": "مصطفى", "address": "حي السعد", "notes": "حار"})
e2 = one("SELECT * FROM orders WHERE id=?", e["id"])
assert e2["total"] == 2 * 9000 + 1000 and e2["notes"] == "حار", e2
ev = one("SELECT detail FROM order_events WHERE order_id=? AND status='edited'", e["id"])["detail"]
assert "بيبسي" in ev and "9,000 ← 19,000" in ev, ev
assert sent[-1][0] == "tpl", "amount changed -> customer told (template: outside window)"
assert staff.get(f"/b/demo/orders/{e['id']}/edit").status_code == 200
od.set_status(e["id"], "preparing"); od.set_status(e["id"], "out")
staff.post(f"/b/demo/orders/{e['id']}/edit", data={"_csrf": tok(staff), "q_1": "5"})
assert one("SELECT total FROM orders WHERE id=?", e["id"])["total"] == 19000, "no edits after it left"
# --- owner-only cancel with reason
c1 = od.create_order(b["id"], [{"product_id": 3, "qty": 1}], None, "حضوري", "", source="walkin", entered_by="حيدر")
assert staff.post(f"/b/demo/orders/{c1['id']}/status", data={"_csrf": tok(staff), "status": "cancelled", "reason": "x"}).status_code == 403
owner.post(f"/b/demo/orders/{c1['id']}/status", data={"_csrf": tok(owner), "status": "cancelled", "reason": ""})
assert one("SELECT status FROM orders WHERE id=?", c1["id"])["status"] == "received", "reason required"
owner.post(f"/b/demo/orders/{c1['id']}/status", data={"_csrf": tok(owner), "status": "cancelled", "reason": "الزبون غيّر رأيه"})
assert one("SELECT cancel_reason FROM orders WHERE id=?", c1["id"])["cancel_reason"] == "الزبون غيّر رأيه"
assert "cancelled" not in staff.get("/b/demo/orders").get_data(as_text=True).split('<div class="board">')[1].split("<script>")[0].replace('class="ticket cancelled','')  # staff sees no cancel button
# --- print
k = staff.get(f"/b/demo/orders/{e['id']}/print").get_data(as_text=True)
cu = staff.get(f"/b/demo/orders/{e['id']}/print?copy=customer").get_data(as_text=True)
assert "نسخة المطبخ" in k and "18,000" not in k and "18,000" in cu and "حار" in k
# --- rating via WhatsApp
assert od.parse_rating("2 مندي") is None and od.parse_rating("٤") == 4 and od.parse_rating("⭐⭐⭐") == 3 and od.parse_rating("4 من 5") == 4
od.set_status(e["id"], "delivered")
assert od.handle_after_delivery(b["id"], "07705550000", "تمام")
assert "من 1 إلى 5" in sent[-1][2]
assert not od.handle_after_delivery(b["id"], "07705550000", "2 مندي دجاج"), "new order must reach the bot"
n = len(sent)
assert od.handle_after_delivery(b["id"], "07705550000", "٢")
assert any(s[1] == b["owner_phone"] for s in sent[n:]), "owner alerted on 2/5"
assert od.handle_after_delivery(b["id"], "07705550000", "الأكل وصل بارد")
row = one("SELECT rating, rating_comment, rating_state FROM orders WHERE id=?", e["id"])
assert row == {"rating": 2, "rating_comment": "الأكل وصل بارد", "rating_state": "done"}, row
assert not od.handle_after_delivery(b["id"], "07705550000", "اريد اطلب مرة ثانية")
# --- rating via tracking page
t1 = od.create_order(b["id"], [{"product_id": 4, "qty": 1}], "07706660000", "زينب", "الحنانة")
for s_ in ("preparing", "out", "delivered"): od.set_status(t1["id"], s_)
pub = app.test_client(); page = pub.get(f"/track/{t1['public_id']}").get_data(as_text=True)
assert "شكد تقيّم" in page
pub.post(f"/track/{t1['public_id']}/rate", data={"_csrf": tok(pub), "stars": "5"})
pub.post(f"/track/{t1['public_id']}/rate", data={"_csrf": tok(pub), "stars": "1"})
assert one("SELECT rating FROM orders WHERE id=?", t1["id"])["rating"] == 5, "first rating sticks"
# --- audit log
owner.post("/b/demo/menu/3", data={"_csrf": tok(owner), "price": "15000"})
log = owner.get("/b/demo/log").get_data(as_text=True)
for w in ("تغيير سعر", "14,000 ← 15,000", "إلغاء طلب", "الزبون غيّر رأيه", "تعديل طلب", "أبو مهدي"):
    assert w in log, w
assert staff.get("/b/demo/log").status_code == 302
# --- reports include ratings
rep = rp.summary(b["id"], dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc), dt.datetime(2100, 1, 1, tzinfo=dt.timezone.utc))
assert rep["rated"] == 2 and rep["rating"] == 3.5 and rep["low"][0]["comment"] == "الأكل وصل بارد" and rep["cancel_reasons"]
assert owner.get("/b/demo/reports").status_code == 200
# --- backup
bk.BACKUP_DIR = "/tmp/bk"; import shutil; shutil.rmtree("/tmp/bk", ignore_errors=True); bk.KEEP = 3
res = bk.run_backup(); assert res["offsite"] == "skipped" and res["kb"] >= 1
import gzip, sqlite3
raw = gzip.decompress(open(bk.latest(), "rb").read()); open("/tmp/restored.db", "wb").write(raw)
assert sqlite3.connect("/tmp/restored.db").execute("SELECT COUNT(*) FROM orders").fetchone()[0] == one("SELECT COUNT(*) n FROM orders")["n"]
for i in range(5):
    open(f"/tmp/bk/smart-2000010{i}-0000.db.gz", "wb").write(b"x")
bk.snapshot(); assert len(glob.glob("/tmp/bk/*.gz")) == 3, "rotation keeps newest 3"
admin.CRON_KEY = "c"; assert app.test_client().get("/cron/backup?key=c").get_json()["offsite"] == "skipped"
assert app.test_client().get("/admin/backup?key=k").status_code == 200 and app.test_client().get("/admin/backup").status_code == 403
print("NEW FEATURES OK")

# ================= first client: قلعة صيرة =================
os.environ["QS_OWNER_PIN"] = "739342"; os.environ["QS_STAFF_PIN"] = "519835"
import seed_qalat_seera as qs
qid = qs.seed(); qs.seed()
assert one("SELECT COUNT(*) n FROM products WHERE business_id=?", qid)["n"] == 7, "re-seeding must not duplicate"
pub = app.test_client().get("/c/qalat-seera").get_data(as_text=True)
assert "أرز المندي اليمني" in pub and "حسب الكمية" in pub and "57,000" not in pub, "estimated prices stay private"
assert "نوّرد لسلاسل المطاعم" in pub and 'data-id="6"' not in pub.split('class="agency"')[0], "agency rice not orderable"
cat = pricing.catalog_for_prompt(qid)
assert "سعر غير مؤكد" in cat and "5% فوق 4,000,000" in cat
q = pricing.quote(qid, None, [{"product_id": 1 + 0, "qty": 30}])
mandi = one("SELECT id FROM products WHERE business_id=? AND sort=1", qid)["id"]
agency_id = one("SELECT id FROM products WHERE business_id=? AND orderable=0", qid)["id"]
q = pricing.quote(qid, None, [{"product_id": mandi, "qty": 30}, {"product_id": agency_id, "qty": 5}])
assert len(q["items"]) == 1 and q["discount"] == round(30 * 57000 * .03) and q["unconfirmed"]
reply, order = od.extract_order_block('تم تثبيت طلبك ✅\n<order>{"items":[{"id": %d, "qty": 30}], "name": "مطعم الريم", "address": "النجف، حي الأمير"}</order>' % mandi)
assert "<order>" not in reply and order["lines"] == [{"product_id": mandi, "qty": 30.0}] and order["name"] == "مطعم الريم"
assert od.extract_order_block("<order>{broken</order>") == ("", None)
# owner confirms a price once -> it goes public and the bot stops hedging
qo = app.test_client(); qo.get("/b/qalat-seera/login")
qo.post("/b/qalat-seera/login", data={"pin": "739342", "_csrf": tok(qo)})
qo.post(f"/b/qalat-seera/menu/{mandi}", data={"_csrf": tok(qo), "price": "57000"})
assert one("SELECT price_confirmed FROM products WHERE id=?", mandi)["price_confirmed"] == 1
assert "57,000" in app.test_client().get("/c/qalat-seera").get_data(as_text=True)
assert "تأكيد سعر" in qo.get("/b/qalat-seera/log").get_data(as_text=True)
qs.seed(); assert one("SELECT price_confirmed FROM products WHERE id=?", mandi)["price_confirmed"] == 1, "seed never overwrites a confirmed price"
assert "تقريبي" in qo.get("/b/qalat-seera/menu").get_data(as_text=True)
print("FIRST CLIENT OK")
# palette: the client's choice is the default, previews still work, junk falls back safely
assert 'data-palette="ivory"' in app.test_client().get("/c/qalat-seera").get_data(as_text=True)
assert 'data-palette="navy"' in app.test_client().get("/c/qalat-seera?palette=navy").get_data(as_text=True)
assert 'data-palette="tobacco"' in app.test_client().get("/c/qalat-seera?palette=<script>").get_data(as_text=True)
print("PALETTE OK")
