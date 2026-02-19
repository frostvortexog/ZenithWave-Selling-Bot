import os
import time
import json
import requests
from datetime import datetime
from fastapi import FastAPI, Request
import psycopg2
from psycopg2.pool import ThreadedConnectionPool

# =========================
# CONFIG (PUT IN FILE)
# =========================
BOT_TOKEN = "8585926679:AAFT_CWzHMm7YQif0xeaDweYQcbXN6XwMjc"
ADMIN_IDS = {8537079657}  # add more admins like: {111,222,333}
DATABASE_URL = "postgresql://postgres.nuxdkcfngtmbdrracmtu:RadheyRadhe@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres"  # postgres://... from Supabase

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

MIN_DIAMONDS = 30
COUPON_TYPES = ["500", "1K", "2K", "4K"]

app = FastAPI()

# =========================
# DB POOL
# =========================
pool = ThreadedConnectionPool(
    minconn=1,
    maxconn=6,
    dsn=DATABASE_URL,
)

def db_fetchone(q, p=None):
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(q, p)
            row = cur.fetchone()
        conn.commit()
        return row
    finally:
        pool.putconn(conn)

def db_fetchall(q, p=None):
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(q, p)
            rows = cur.fetchall()
        conn.commit()
        return rows
    finally:
        pool.putconn(conn)

def db_exec(q, p=None):
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(q, p)
        conn.commit()
    finally:
        pool.putconn(conn)

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# =========================
# TELEGRAM HELPERS
# =========================
def tg(method: str, payload: dict):
    try:
        requests.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=8)
    except Exception:
        pass

def send_msg(chat_id: int, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg("sendMessage", payload)

def send_photo(chat_id: int, file_id: str, caption: str = None, reply_markup=None):
    payload = {"chat_id": chat_id, "photo": file_id}
    if caption:
        payload["caption"] = caption
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg("sendPhoto", payload)

def answer_callback(callback_id: str):
    tg("answerCallbackQuery", {"callback_query_id": callback_id})

# =========================
# MENUS
# =========================
def user_menu():
    return {
        "keyboard": [
            ["💎 Add Diamonds", "🛒 Buy Coupon"],
            ["📦 My Orders", "💰 Balance"],
        ],
        "resize_keyboard": True
    }

def admin_menu():
    return {
        "keyboard": [
            ["📊 Stock", "➕ Add Coupon", "➖ Remove Coupon"],
            ["💰 Change Price", "🎁 Free Coupon"],
            ["🔄 Update QR"],
        ],
        "resize_keyboard": True
    }

def pay_method_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "Amazon Gift Card", "callback_data": "pay_amazon"}],
            [{"text": "UPI", "callback_data": "pay_upi"}],
        ]
    }

def coupon_types_keyboard(prefix: str):
    # prefix examples: "buytype_", "admin_add_", "admin_remove_", "admin_price_", "admin_free_"
    rows = []
    for t in COUPON_TYPES:
        rows.append([{"text": t, "callback_data": f"{prefix}{t}"}])
    return {"inline_keyboard": rows}

# =========================
# USER STATE
# =========================
def ensure_user(uid: int, username: str):
    db_exec(
        "INSERT INTO users (user_id, username) VALUES (%s,%s) "
        "ON CONFLICT (user_id) DO UPDATE SET username=EXCLUDED.username",
        (uid, username),
    )

def get_user(uid: int):
    row = db_fetchone("SELECT diamonds, state, temp FROM users WHERE user_id=%s", (uid,))
    if not row:
        return (0, None, None)
    return row

def set_state(uid: int, state: str | None, temp: str | None = None):
    db_exec("UPDATE users SET state=%s, temp=%s WHERE user_id=%s", (state, temp, uid))

def add_diamonds(uid: int, amount: int):
    db_exec("UPDATE users SET diamonds = diamonds + %s WHERE user_id=%s", (amount, uid))

def deduct_diamonds(uid: int, amount: int):
    db_exec("UPDATE users SET diamonds = diamonds - %s WHERE user_id=%s", (amount, uid))

# =========================
# SETTINGS
# =========================
def get_upi_qr():
    row = db_fetchone("SELECT value FROM settings WHERE key='upi_qr'")
    return row[0] if row else ""

def set_upi_qr(file_id: str):
    db_exec("UPDATE settings SET value=%s WHERE key='upi_qr'", (file_id,))

def get_price(t: str) -> int:
    row = db_fetchone("SELECT price FROM prices WHERE type=%s", (t,))
    return int(row[0]) if row else 0

def set_price(t: str, price: int):
    db_exec("UPDATE prices SET price=%s WHERE type=%s", (price, t))

def stock_count(t: str) -> int:
    row = db_fetchone("SELECT COUNT(*) FROM coupons WHERE type=%s", (t,))
    return int(row[0]) if row else 0

# =========================
# ORDER HELPERS
# =========================
def create_order(user_id: int, kind: str, method: str, amount_int: int, diamonds: int, details: str, screenshot_file_id: str | None):
    db_exec(
        "INSERT INTO orders (user_id, kind, method, amount_int, diamonds, details, screenshot_file_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (user_id, kind, method, amount_int, diamonds, details, screenshot_file_id),
    )
    row = db_fetchone("SELECT MAX(id) FROM orders WHERE user_id=%s", (user_id,))
    return int(row[0]) if row and row[0] else 0

def set_order_status(order_id: int, status: str):
    db_exec("UPDATE orders SET status=%s WHERE id=%s", (status, order_id))

def get_order(order_id: int):
    return db_fetchone(
        "SELECT id, user_id, kind, method, amount_int, diamonds, details, screenshot_file_id, status, created_at "
        "FROM orders WHERE id=%s",
        (order_id,),
    )

# =========================
# ADMIN NOTIFY (ACCEPT/DECLINE)
# =========================
def notify_admin_for_order(order_id: int):
    order = get_order(order_id)
    if not order:
        return

    _, user_id, kind, method, amount_int, diamonds, details, ss, status, created_at = order
    created_str = created_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(created_at, "strftime") else str(created_at)

    text = (
        f"🧾 New Order #{order_id}\n"
        f"👤 User: {user_id}\n"
        f"📦 Kind: {kind}\n"
        f"💳 Method: {method}\n"
        f"💵 Amount: {amount_int}\n"
        f"💎 Diamonds: {diamonds}\n"
        f"📝 Details: {details}\n"
        f"📅 Time: {created_str}\n"
        f"📌 Status: {status}"
    )

    k = {
        "inline_keyboard": [[
            {"text": "✅ Accept", "callback_data": f"admin_acc_{order_id}"},
            {"text": "❌ Decline", "callback_data": f"admin_dec_{order_id}"},
        ]]
    }

    # Send screenshot first if exists
    for admin_id in ADMIN_IDS:
        if ss:
            send_photo(admin_id, ss, caption=text, reply_markup=k)
        else:
            send_msg(admin_id, text, reply_markup=k)

# =========================
# COUPON PURCHASE (TRANSACTION SAFE)
# =========================
def purchase_coupons(user_id: int, coupon_type: str, qty: int):
    """
    Returns: (ok:bool, message:str, codes:list[str])
    Transaction:
      - lock user row
      - check diamonds
      - lock coupon rows (FOR UPDATE SKIP LOCKED)
      - delete those coupon rows
      - deduct diamonds
      - create approved order
    """
    if coupon_type not in COUPON_TYPES:
        return (False, "Invalid coupon type.", [])
    if qty <= 0:
        return (False, "Invalid quantity.", [])

    price_each = get_price(coupon_type)
    if price_each <= 0:
        return (False, "Price not set by admin.", [])

    total_cost = price_each * qty

    conn = pool.getconn()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            # Lock user
            cur.execute("SELECT diamonds FROM users WHERE user_id=%s FOR UPDATE", (user_id,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return (False, "User not found.", [])
            user_diamonds = int(row[0])

            if user_diamonds < total_cost:
                conn.rollback()
                return (False, f"❌ Not enough diamonds! Needed: {total_cost} | You have: {user_diamonds}", [])

            # Lock coupons
            cur.execute(
                "SELECT id, code FROM coupons WHERE type=%s ORDER BY id ASC LIMIT %s FOR UPDATE SKIP LOCKED",
                (coupon_type, qty),
            )
            coupons = cur.fetchall()
            if len(coupons) < qty:
                available = len(coupons)
                conn.rollback()
                return (False, f"❌ Not enough stock! Available: {available}", [])

            ids = [c[0] for c in coupons]
            codes = [c[1] for c in coupons]

            # Delete claimed coupons
            cur.execute("DELETE FROM coupons WHERE id = ANY(%s)", (ids,))

            # Deduct diamonds
            cur.execute("UPDATE users SET diamonds = diamonds - %s WHERE user_id=%s", (total_cost, user_id))

            # Create approved order (coupon)
            details = f"type={coupon_type}, qty={qty}, price_each={price_each}"
            cur.execute(
                "INSERT INTO orders (user_id, kind, method, amount_int, diamonds, details, status) "
                "VALUES (%s,'coupon','BuyCoupon',%s,%s,%s,'approved')",
                (user_id, total_cost, total_cost, details),
            )

        conn.commit()
        return (True, f"✅ Purchase successful! Spent {total_cost} 💎", codes)

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return (False, f"❌ Purchase failed: {str(e)}", [])
    finally:
        try:
            conn.autocommit = True
        except Exception:
            pass
        pool.putconn(conn)

# =========================
# HELPERS
# =========================
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def format_order_summary(method: str, diamonds: int):
    return (
        "📝 Order Summary:\n"
        "━━━━━━━━━━━━━━━\n"
        "💹 Rate: 1 Rs = 1 Diamond 💎\n"
        f"💵 Amount: {diamonds}\n"
        f"💎 Diamonds to Receive: {diamonds}\n"
        f"💳 Method: {method}\n"
        f"📅 Time: {datetime.now().strftime('%H:%M:%S')}\n"
        "━━━━━━━━━━━━━━━\n"
        "Click below to proceed."
    )

# =========================
# WEBHOOK
# =========================
@app.get("/health")
def health():
    return {"ok": True}

@app.post("/")
async def telegram_webhook(req: Request):
    update = await req.json()

    # Messages
    msg = update.get("message")
    if msg:
        chat_id = msg["chat"]["id"]
        uid = msg["from"]["id"]
        username = msg["from"].get("username", "") or ""

        ensure_user(uid, username)

        text = msg.get("text", "")
        diamonds, state, temp = get_user(uid)

        # Admin entry shortcut
        if text == "/admin" and is_admin(uid):
            send_msg(chat_id, "🛠️ Admin Panel", admin_menu())
            return {"ok": True}

        # Start
        if text == "/start":
            if is_admin(uid):
                send_msg(chat_id, "Welcome Admin ✅", admin_menu())
            else:
                send_msg(chat_id, "Welcome 💎", user_menu())
            set_state(uid, None, None)
            return {"ok": True}

        # User menu buttons
        if text == "💰 Balance":
            diamonds, _, _ = get_user(uid)
            send_msg(chat_id, f"💎 Your Balance: {diamonds}")
            return {"ok": True}

        if text == "📦 My Orders":
            rows = db_fetchall(
                "SELECT id, kind, method, diamonds, status, created_at "
                "FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT 10",
                (uid,)
            )
            if not rows:
                send_msg(chat_id, "📦 No orders yet.")
                return {"ok": True}

            out = ["📦 Your Orders (last 10):\n"]
            for oid, kind, method, dia, status, created_at in rows:
                t = created_at.strftime("%d-%m %H:%M") if hasattr(created_at, "strftime") else str(created_at)
                out.append(f"#{oid} | {kind}/{method} | {dia} 💎 | {status} | {t}")
            send_msg(chat_id, "\n".join(out))
            return {"ok": True}

        if text == "💎 Add Diamonds":
            send_msg(
                chat_id,
                "💳 Select Payment Method:\n\n⚠️ Under Maintenance:\n\nPlease use other methods for deposit.",
                pay_method_keyboard()
            )
            return {"ok": True}

        if text == "🛒 Buy Coupon":
            rows = []
            for t in COUPON_TYPES:
                price = get_price(t)
                stock = stock_count(t)
                rows.append([{"text": f"{t} (💎{price}) Stock:{stock}", "callback_data": f"buytype_{t}"}])
            send_msg(chat_id, "Select a coupon type:", {"inline_keyboard": rows})
            return {"ok": True}

        # =========================
        # STATE HANDLERS (USER)
        # =========================

        # Amazon: ask diamonds amount
        if state == "amazon_amount":
            if not text.isdigit() or int(text) < MIN_DIAMONDS:
                send_msg(chat_id, f"❌ Minimum diamonds is {MIN_DIAMONDS}. Send a number.")
                return {"ok": True}

            dia = int(text)
            set_state(uid, None, str(dia))

            summary = format_order_summary("Amazon Gift Card", dia)
            k = {"inline_keyboard": [[{"text": "Submit a Gift Card", "callback_data": "amazon_submit"}]]}
            send_msg(chat_id, summary, k)
            return {"ok": True}

        # Amazon: gift card amount/code text (accept anything)
        if state == "amazon_gift_text":
            # store gift text in temp as JSON: {"dia":X,"gift":"..."}
            try:
                payload = json.loads(temp) if temp else {}
            except Exception:
                payload = {}
            payload["gift"] = text
            set_state(uid, "amazon_wait_ss", json.dumps(payload, ensure_ascii=False))
            send_msg(chat_id, "📸 Now upload a screenshot of the gift card:")
            return {"ok": True}

        # Amazon: screenshot
        if state == "amazon_wait_ss":
            photos = msg.get("photo")
            if not photos:
                send_msg(chat_id, "📸 Please upload a screenshot image.")
                return {"ok": True}

            file_id = photos[-1]["file_id"]
            try:
                payload = json.loads(temp) if temp else {}
            except Exception:
                payload = {}
            dia = int(payload.get("dia", 0))
            gift_text = payload.get("gift", "")

            # create pending order
            order_id = create_order(
                user_id=uid,
                kind="deposit",
                method="Amazon",
                amount_int=dia,
                diamonds=dia,
                details=f"gift={gift_text}",
                screenshot_file_id=file_id
            )

            set_state(uid, None, None)
            send_msg(chat_id, "⏳ Admin is checking your proof. Please wait for approval.")
            notify_admin_for_order(order_id)
            return {"ok": True}

        # UPI: ask diamonds amount
        if state == "upi_amount":
            if not text.isdigit() or int(text) < MIN_DIAMONDS:
                send_msg(chat_id, f"❌ Minimum diamonds is {MIN_DIAMONDS}. Send a number.")
                return {"ok": True}

            dia = int(text)
            qr = get_upi_qr()
            if not qr:
                send_msg(chat_id, "⚠️ UPI is not available right now. Admin has not set QR.")
                set_state(uid, None, None)
                return {"ok": True}

            summary = format_order_summary("UPI", dia)
            caption = summary + "\n\n✅ Pay the exact amount, then tap: Done Payment"
            k = {"inline_keyboard": [[{"text": "✅ Done Payment", "callback_data": "upi_done"}]]}

            # store dia in temp
            set_state(uid, None, str(dia))
            send_photo(chat_id, qr, caption=caption, reply_markup=k)
            return {"ok": True}

        # UPI: payer name
        if state == "upi_payer":
            # temp contains diamonds int
            dia = int(temp or "0")
            payload = {"dia": dia, "payer": text}
            set_state(uid, "upi_wait_ss", json.dumps(payload, ensure_ascii=False))
            send_msg(chat_id, "📸 Send screenshot of the payment:")
            return {"ok": True}

        # UPI: screenshot
        if state == "upi_wait_ss":
            photos = msg.get("photo")
            if not photos:
                send_msg(chat_id, "📸 Please upload a screenshot image.")
                return {"ok": True}

            file_id = photos[-1]["file_id"]
            try:
                payload = json.loads(temp) if temp else {}
            except Exception:
                payload = {}
            dia = int(payload.get("dia", 0))
            payer = payload.get("payer", "")

            order_id = create_order(
                user_id=uid,
                kind="deposit",
                method="UPI",
                amount_int=dia,
                diamonds=dia,
                details=f"payer={payer}",
                screenshot_file_id=file_id
            )

            set_state(uid, None, None)
            send_msg(chat_id, "⏳ Admin is checking your payment. Please wait for approval.")
            notify_admin_for_order(order_id)
            return {"ok": True}

        # Coupon buy: quantity
        if state == "buy_qty":
            ctype = temp or ""
            if not text.isdigit() or int(text) <= 0:
                send_msg(chat_id, "❌ Please send quantity as a number.")
                return {"ok": True}

            qty = int(text)
            ok, message, codes = purchase_coupons(uid, ctype, qty)
            set_state(uid, None, None)

            if not ok:
                send_msg(chat_id, message)
                return {"ok": True}

            # Send codes (split if long)
            send_msg(chat_id, message)
            chunk = []
            for code in codes:
                chunk.append(code)
                if len(chunk) >= 25:
                    send_msg(chat_id, "🎟️ Your Coupons:\n" + "\n".join(chunk))
                    chunk = []
            if chunk:
                send_msg(chat_id, "🎟️ Your Coupons:\n" + "\n".join(chunk))
            return {"ok": True}

        # =========================
        # ADMIN STATE HANDLERS
        # =========================
        if is_admin(uid):
            # Update QR photo
            if state == "admin_update_qr":
                photos = msg.get("photo")
                if not photos:
                    send_msg(chat_id, "📸 Please send QR as photo.")
                    return {"ok": True}
                file_id = photos[-1]["file_id"]
                set_upi_qr(file_id)
                set_state(uid, None, None)
                send_msg(chat_id, "✅ UPI QR updated.", admin_menu())
                return {"ok": True}

            # Admin add coupons (expects text lines)
            if state == "admin_add_codes":
                ctype = temp or ""
                codes = [x.strip() for x in (text or "").splitlines() if x.strip()]
                if not codes:
                    send_msg(chat_id, "❌ Send coupon codes line by line.")
                    return {"ok": True}

                conn = pool.getconn()
                try:
                    with conn.cursor() as cur:
                        for code in codes:
                            cur.execute("INSERT INTO coupons (type, code) VALUES (%s,%s)", (ctype, code))
                    conn.commit()
                finally:
                    pool.putconn(conn)

                set_state(uid, None, None)
                send_msg(chat_id, f"✅ Added {len(codes)} coupons to {ctype}.", admin_menu())
                return {"ok": True}

            # Admin remove coupons (expects number)
            if state == "admin_remove_qty":
                ctype = temp or ""
                if not text.isdigit() or int(text) <= 0:
                    send_msg(chat_id, "❌ Send remove quantity as number.")
                    return {"ok": True}
                qty = int(text)

                conn = pool.getconn()
                try:
                    conn.autocommit = False
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT id FROM coupons WHERE type=%s ORDER BY id ASC LIMIT %s FOR UPDATE SKIP LOCKED",
                            (ctype, qty),
                        )
                        rows = cur.fetchall()
                        ids = [r[0] for r in rows]
                        if not ids:
                            conn.rollback()
                            send_msg(chat_id, f"❌ No coupons available to remove in {ctype}.")
                            return {"ok": True}
                        cur.execute("DELETE FROM coupons WHERE id = ANY(%s)", (ids,))
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    send_msg(chat_id, f"❌ Remove failed: {e}")
                    return {"ok": True}
                finally:
                    try:
                        conn.autocommit = True
                    except Exception:
                        pass
                    pool.putconn(conn)

                set_state(uid, None, None)
                send_msg(chat_id, f"✅ Removed {len(ids)} coupons from {ctype}.", admin_menu())
                return {"ok": True}

            # Admin change price (expects number)
            if state == "admin_new_price":
                ctype = temp or ""
                if not text.isdigit() or int(text) <= 0:
                    send_msg(chat_id, "❌ Send new price as a number.")
                    return {"ok": True}
                set_price(ctype, int(text))
                set_state(uid, None, None)
                send_msg(chat_id, f"✅ Price updated: {ctype} = {text} 💎", admin_menu())
                return {"ok": True}

        # Default fallback
        return {"ok": True}

    # Callbacks
    cb = update.get("callback_query")
    if cb:
        cb_id = cb["id"]
        uid = cb["from"]["id"]
        chat_id = cb["message"]["chat"]["id"]
        data = cb.get("data", "")

        answer_callback(cb_id)

        # =========================
        # USER CALLBACKS
        # =========================
        if data == "pay_amazon":
            set_state(uid, "amazon_amount", None)
            send_msg(chat_id, f"Enter the number of coins to add (Method: Amazon). Minimum {MIN_DIAMONDS}:")
            return {"ok": True}

        if data == "amazon_submit":
            # temp currently holds diamonds as string
            _, _, temp = get_user(uid)
            dia = int(temp or "0")
            payload = {"dia": dia}
            set_state(uid, "amazon_gift_text", json.dumps(payload, ensure_ascii=False))
            send_msg(chat_id, f"Enter your Amazon Gift Card Amount / Code for They Enter:")
            return {"ok": True}

        if data == "pay_upi":
            set_state(uid, "upi_amount", None)
            send_msg(chat_id, f"How many diamonds you need to buy? Minimum {MIN_DIAMONDS}:")
            return {"ok": True}

        if data == "upi_done":
            # temp holds diamonds
            _, _, temp = get_user(uid)
            set_state(uid, "upi_payer", temp)
            send_msg(chat_id, "What is the payer name?")
            return {"ok": True}

        if data.startswith("buytype_"):
            ctype = data.split("_", 1)[1]
            set_state(uid, "buy_qty", ctype)
            send_msg(chat_id, f"How many {ctype} coupons do you want to buy?\n\nPlease send the quantity:")
            return {"ok": True}

        # =========================
        # ADMIN CALLBACKS
        # =========================
        if is_admin(uid):
            if data.startswith("admin_acc_"):
                order_id = int(data.split("_")[-1])
                order = get_order(order_id)
                if not order:
                    send_msg(chat_id, "Order not found.")
                    return {"ok": True}

                _, user_id, kind, method, amount_int, diamonds, details, ss, status, created_at = order
                if status != "pending":
                    send_msg(chat_id, f"⚠️ Order already {status}.")
                    return {"ok": True}

                # Approve deposit -> add diamonds
                if kind == "deposit":
                    add_diamonds(user_id, diamonds)

                set_order_status(order_id, "approved")
                send_msg(chat_id, f"✅ Approved Order #{order_id}")
                send_msg(user_id, f"✅ Approved! {diamonds} Diamonds added to your balance.")
                return {"ok": True}

            if data.startswith("admin_dec_"):
                order_id = int(data.split("_")[-1])
                order = get_order(order_id)
                if not order:
                    send_msg(chat_id, "Order not found.")
                    return {"ok": True}
                if order[8] != "pending":
                    send_msg(chat_id, f"⚠️ Order already {order[8]}.")
                    return {"ok": True}

                set_order_status(order_id, "declined")
                send_msg(chat_id, f"❌ Declined Order #{order_id}")
                send_msg(order[1], "❌ Your deposit was declined by admin.")
                return {"ok": True}

            # Admin panel buttons
            if data == "admin_stock":
                # not used; admin uses keyboard "📊 Stock"
                return {"ok": True}

            if data.startswith("admin_add_"):
                ctype = data.split("_", 2)[2]
                set_state(uid, "admin_add_codes", ctype)
                send_msg(chat_id, f"Send {ctype} coupon codes line by line:")
                return {"ok": True}

            if data.startswith("admin_remove_"):
                ctype = data.split("_", 2)[2]
                set_state(uid, "admin_remove_qty", ctype)
                send_msg(chat_id, f"How many {ctype} coupons you want to remove? Send a number:")
                return {"ok": True}

            if data.startswith("admin_price_"):
                ctype = data.split("_", 2)[2]
                set_state(uid, "admin_new_price", ctype)
                send_msg(chat_id, f"Send new price (diamonds) for {ctype}:")
                return {"ok": True}

            if data.startswith("admin_free_"):
                ctype = data.split("_", 2)[2]

                # give 1 coupon for free
                conn = pool.getconn()
                try:
                    conn.autocommit = False
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT id, code FROM coupons WHERE type=%s ORDER BY id ASC LIMIT 1 FOR UPDATE SKIP LOCKED",
                            (ctype,),
                        )
                        row = cur.fetchone()
                        if not row:
                            conn.rollback()
                            send_msg(chat_id, f"❌ No stock for {ctype}.")
                            return {"ok": True}
                        cid, code = row
                        cur.execute("DELETE FROM coupons WHERE id=%s", (cid,))
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    send_msg(chat_id, f"❌ Failed: {e}")
                    return {"ok": True
}
                finally:
                    try:
                        conn.autocommit = True
                    except Exception:
                        pass
                    pool.putconn(conn)

                send_msg(chat_id, f"🎁 Free Coupon ({ctype}):\n{code}")
                return {"ok": True}

        return {"ok": True}

    # Admin keyboard actions (handled here as message text)
    if msg:
        chat_id = msg["chat"]["id"]
        uid = msg["from"]["id"]
        text = msg.get("text", "")

        if is_admin(uid):
            if text == "📊 Stock":
                out = ["📊 Stock:\n"]
                for t in COUPON_TYPES:
                    out.append(f"{t}: {stock_count(t)}")
                send_msg(chat_id, "\n".join(out))
                return {"ok": True}

            if text == "➕ Add Coupon":
                send_msg(chat_id, "Select type to add coupons:", coupon_types_keyboard("admin_add_"))
                return {"ok": True}

            if text == "➖ Remove Coupon":
                send_msg(chat_id, "Select type to remove coupons:", coupon_types_keyboard("admin_remove_"))
                return {"ok": True}

            if text == "💰 Change Price":
                # show current prices
                out = ["💰 Current Prices:\n"]
                for t in COUPON_TYPES:
                    out.append(f"{t}: {get_price(t)} 💎")
                send_msg(chat_id, "\n".join(out))
                send_msg(chat_id, "Select type to change price:", coupon_types_keyboard("admin_price_"))
                return {"ok": True}

            if text == "🎁 Free Coupon":
                send_msg(chat_id, "Select type to get free coupon:", coupon_types_keyboard("admin_free_"))
                return {"ok": True}

            if text == "🔄 Update QR":
                set_state(uid, "admin_update_qr", None)
                send_msg(chat_id, "📸 Send the new UPI QR photo:")
                return {"ok": True}

    return {"ok": True}
