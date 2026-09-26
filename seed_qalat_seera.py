"""Creates the first client: شركة قلعة صيرة للتجارة العامة (Najaf).
Run once: python seed_qalat_seera.py  (safe to re-run: updates instead of duplicating)

Every price here is an ESTIMATE (price_confirmed=0). The bot tells customers the price
is confirmed by management, and the public page hides it, until the owner saves it once
from 'المنيو والأسعار'. Basis: Iraqi CSO average for commercial basmati (~2,150 IQD/kg
retail, Mar 2026) scaled up for premium 1121/mandi grades; bag weights from the posts
where visible, otherwise marked 'يُؤكد'."""
import os, json
from db import init_db, one, run

SLUG = "qalat-seera"
IMG = "/static/clients/qalat-seera/"
BUSINESS = dict(
    name="شركة قلعة صيرة للتجارة العامة", vertical="wholesale", city="النجف",
    phone=os.getenv("QS_BOT_PHONE", "9647739342546"),        # the bot's WhatsApp number (digits, 964...)
    phone_number_id=os.getenv("WHATSAPP_PHONE_ID") or None,   # links incoming WhatsApp messages to this client
    contact_phones="07739342546,07519835545", facebook="https://www.facebook.com/share/1DLmvjTgL5/",
    about=("تستورد شركة قلعة صيرة للتجارة العامة الأرز البسمتي والشاي السيلاني، وتوزّعها بالجملة لمطاعم المندي "
           "والمواكب الحسينية ومحلات المواد الغذائية في العراق. ماركتنا الخاصة «الخوجة أنور»، ونوّرد لسلاسل مطاعم "
           "معروفة أرزاً يحمل شعار ضمان الجودة الخاص بشركتنا."),
    owner_phone=os.getenv("QS_OWNER_PHONE", "9647739342546"),
    owner_pin=os.getenv("QS_OWNER_PIN"), staff_pin=os.getenv("QS_STAFF_PIN"),
    address="النجف الأشرف", tagline="بسمتي درجة أولى، الأرز اللي تعتمده مطاعم المندي بالعراق.",
    volume_tiers=json.dumps([[4_000_000, 5], [1_500_000, 3]]), late_after_min=240,
    palette="ivory",   # corporate white, chosen by the client
)

# (sort, name, category, price, unit, image, orderable, note)
PRODUCTS = [
 (1, "أرز المندي اليمني — الخوجة أنور", "الرز", 57000, "كيس 20 كغم (الوزن يُؤكد)", "mandi_khojah.jpg", 1,
  "بسمتي درجة أولى. الأرز الرئيسي لمطاعم المندي."),
 (2, "أرز الخوجة أنور سيلا 1121", "الرز", 52000, "كيس 20 كغم (الوزن يُؤكد)", "sella1121.jpg", 1,
  "بسمتي سيلا 1121 طويل الحبة. مطلوب للمواكب والولائم."),
 (3, "أرز حليمة الفاخر", "الرز", 78000, "كيس 30 كغم", "haleema.jpg", 1,
  "هندي، مخصص لمطاعم المندي."),
 (4, "أرز العابدي ذهب", "الرز", 45000, "كيس (الوزن يُؤكد)", "abidi.jpg", 1,
  "حبة طويلة، الخيار الاقتصادي للكميات الكبيرة."),
 (5, "شاي المناسبات إيرل غراي", "الشاي", 50000, "كيس 5 كغم", "tea_kraft.jpg", 1,
  "سيلاني 100%، درجة STD 158 OP، معطّر بإيرل غراي. تعبئة كرافت أو بيضاء."),
 (20, "أرز حضرموت اليمني البسمتي", "توريد الوكالات", 0, "", "hadramout.jpg", 0,
  "الوكالة الخاصة بمطاعم حضرموت. إذا طلبه زبون من خارج السلسلة حوّله للإدارة."),
 (21, "أرز صنعاء اليمن سيلا بسمتي", "توريد الوكالات", 0, "", "sanaa.jpg", 0,
  "الوكالة الخاصة بمطاعم صنعاء اليمن. إذا طلبه زبون من خارج السلسلة حوّله للإدارة."),
]
PRICE_RANGES = {1: "54–60 ألف", 2: "50–55 ألف", 3: "74–82 ألف", 4: "40–48 ألف", 5: "45–60 ألف"}

def seed():
    init_db()
    b = one("SELECT id FROM businesses WHERE slug=?", SLUG)
    data = {k: v for k, v in BUSINESS.items() if v is not None}   # unset env vars never overwrite saved values
    if b:
        run(f"UPDATE businesses SET {', '.join(k + '=?' for k in data)} WHERE id=?", *data.values(), b["id"])
        bid = b["id"]
    else:
        import secrets
        for k in ("owner_pin", "staff_pin"):
            if not data.get(k):
                data[k] = str(secrets.randbelow(900000) + 100000)
                print(f"GENERATED {k.upper()} for {SLUG}: {data[k]}  (set QS_{k.split('_')[0].upper()}_PIN to choose your own)", flush=True)
        bid = run(f"INSERT INTO businesses(slug,{','.join(data)}) VALUES(?{',?' * len(data)})", SLUG, *data.values())
    for sort, name, cat, price, unit, img, orderable, note in PRODUCTS:
        ex = one("SELECT id, price_confirmed FROM products WHERE business_id=? AND name=?", bid, name)
        if ex and ex["price_confirmed"]:
            continue  # never overwrite a price the owner already confirmed
        vals = (cat, price, unit, IMG + img, orderable, note, sort)
        if ex:
            run("UPDATE products SET category=?,price=?,unit=?,image_url=?,orderable=?,note=?,sort=?,price_confirmed=0,active=1 WHERE id=?", *vals, ex["id"])
        else:
            run("INSERT INTO products(business_id,name,category,price,unit,image_url,orderable,note,sort,price_confirmed,active) VALUES(?,?,?,?,?,?,?,?,?,0,1)",
                bid, name, *vals)
    return bid

if __name__ == "__main__":
    print("business id:", seed(), "-> /c/" + SLUG, "and /b/" + SLUG + "/login")
