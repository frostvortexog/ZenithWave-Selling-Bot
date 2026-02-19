import json
import time
from datetime import datetime

import requests
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from fastapi import FastAPI, Request

# =========================
# CONFIG (PUT INSIDE FILE)
# =========================
BOT_TOKEN = "8585926679:AAFT_CWzHMm7YQif0xeaDweYQcbXN6XwMjc"
DATABASE_URL = "postgresql://postgres.nuxdkcfngtmbdrracmtu:RadheyRadhe@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres"  # Supabase Postgres connection string
ADMIN_IDS = {8537079657}  # add more admins: {111,222,333}

MIN_DEPOSIT = 30
COUPON_TYPES = ["500", "1K", "2K", "4K"]

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = FastAPI()

# =========================
# DB POOL (FAST)
# =========================
pool = ThreadedConnectionPool(minconn=1, maxconn=8, dsn=DATABASE_URL)


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
        requests.post(f"{TG}/{method}", json=payload, timeout=10)
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


def answer_cb(cb_id: str):
    tg("answerCallbackQuery", {"callback_query_id": cb_id})


# =========================
# MENUS
# =========================
def user_menu():
    return {
        "keyboard": [
            ["💎 Add Diamonds", "🛒 Buy Coupon"],
            ["📦 My Orders", "💰 Balance"],
        ],
        "resize_keyboard": True,
    }


def admin_menu():
    return {
        "keyboard": [
            ["🛠 Admin Panel"],  # one button to open panel
            ["💎 Add Diamonds", "🛒 Buy Coupon"],
            ["📦 My Orders", "💰 Balance"],
        ],
        "resize_keyboard": True,
    }


def admin_panel_menu():
    return {
        "keyboard": [
            ["📊 Stock", "💰 Change Prices"],
            ["➕ Add Coupon", "➖ Remove Coupon"],
            ["🎁 Free Coupon", "🔄 Update QR"],
            ["⬅️ Back to User Menu"],
        ],
        "resize_keyboard": True,
    }


def pay_method_kb():
    return {
        "inline_keyboard": [
            [{"text": "Amazon Gift Card", "callback_data": "pay_amazon"}],
            [{"text": "UPI", "callback_data": "pay_upi"}],
        ]
    }


def coupon_select_kb(prefix: str):
    # prefix examples: buytype_, admin_add_, admin_remove_, admin_price_, admin_free_
    rows = []
    for t in COUPON_TYPES:
        rows.append([{"text": t, "callback_data": f"{prefix}{t}"}])
    return {"inline_keyboard": rows}


# =========================
# USER + SETTINGS
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
        return 0, None, None
    return int(row[0]), row[1], row[2]


def set_state(uid: int, state: str | None, temp: str | None = None):
    db_exec("UPDATE users SET state=%s, temp=%s WHERE user_id=%s", (state, temp, uid))


def add_diamonds(uid: int, amount: int):
    db_exec("UPDATE users SET diamonds = diamonds + %s WHERE user_id=%s", (amount, uid))


def get_upi_qr() -> str:
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
# ORDERS
# =========================
def create_order(user_id: int, kind: str, method: str, amount_int: int, diamonds: int, details: str, screenshot_id: str | None):
    db_exec(
        "INSERT INTO orders (user_id, kind, method, amount_int, diamonds, details, screenshot_file_id, status) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,'pending')",
        (user_id, kind, method, amount_int, diamonds, details, screenshot_id),
    )
    row = db_fetchone("SELECT id FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT 1", (user_id,))
    return int(row[0]) if row else 0


def get_order(order_id: int):
    return db_fetchone(
        "SELECT id, user_id, kind, method, amount_int, diamonds, details, screenshot_file_id, status, created_at "
        "FROM orders WHERE id=%s",
        (order_id,),
    )


def set_order_status(order_id: int, status: str):
    db_exec("UPDATE orders SET status=%s WHERE id=%s", (status, order_id))


def notify_admin(order_id: int):
    order = get_order(order_id)
    if not order:
        return
    oid, user_id, kind, method, amount_int, diamonds, details, screenshot, status, created_at = order
    t = created_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(created_at, "strftime") else str(created_at)

    text = (
        f"🧾 Order #{oid}\n"
        f"👤 User: {user_id}\n"
        f"📦 Kind: {kind}\n"
        f"💳 Method: {method}\n"
        f"💵 Amount: {amount_int}\n"
        f"💎 Diamonds: {diamonds}\n"
        f"📝 Details: {details}\n"
        f"📅 Time: {t}\n"
        f"📌 Status: {status}"
    )

    kb = {
        "inline_keyboard": [[
            {"text": "✅ Accept", "callback_data": f"admin_acc_{oid}"},
            {"text": "❌ Decline", "callback_data": f"admin_dec_{oid}"},
        ]]
    }

    for admin_id in ADMIN_IDS:
        if screenshot:
            send_photo(admin_id, screenshot, caption=text, reply_markup=kb)
        else:
            send_msg(admin_id, text, reply_markup=kb)


# =========================
# COUPON PURCHASE (SAFE TX)
# =========================
def purchase_coupons(user_id: int, ctype: str, qty: int):
    """
    Transaction-safe:
    - lock user row
    - check diamonds
    - lock coupon rows (SKIP LOCKED)
    - delete those coupons
    - deduct diamonds
    - create approved order
    """
    if ctype not in COUPON_TYPES:
        return False, "Invalid coupon type.", []

    if qty <= 0:
        return False, "❌ Please send a valid quantity.", []

    price_each = get_price(ctype)
    if price_each <= 0:
        return False, "❌ Price not set by admin.", []

    total = price_each * qty

    conn = pool.getconn()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            # lock user
            cur.execute("SELECT diamonds FROM users WHERE user_id=%s FOR UPDATE", (user_id,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return False, "User not found.", []
            bal = int(row[0])

            if bal < total:
                conn.rollback()
                return False, f"❌ Not enough diamonds! Needed: {total} | You have: {bal}", []

            # lock coupons
            cur.execute(
                "SELECT id, code FROM coupons WHERE type=%s ORDER BY id ASC LIMIT %s FOR UPDATE SKIP LOCKED",
                (ctype, qty),
            )
            rows = cur.fetchall()
            if len(rows) < qty:
                conn.rollback()
                return False, f"❌ Not enough stock! Available: {len(rows)}", []

            ids = [r[0] for r in rows]
            codes = [r[1] for r in rows]

            # delete selected coupons
            cur.execute("DELETE FROM coupons WHERE id = ANY(%s)", (ids,))

            # deduct diamonds
            cur.execute("UPDATE users SET diamonds = diamonds - %s WHERE user_id=%s", (total, user_id))

            # approved order
            details = f"type={ctype}, qty={qty}, price_each={price_each}"
            cur.execute(
                "INSERT INTO orders (user_id, kind, method, amount_int, diamonds, details, status) "
                "VALUES (%s,'coupon','BuyCoupon',%s,%s,%s,'approved')",
                (user_id, total, total, details),
            )

        conn.commit()
        return True, f"✅ Purchase successful! Spent {total} 💎", codes
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return False, f"❌ Purchase failed: {e}", []
    finally:
        try:
            conn.autocommit = True
        except Exception:
            pass
        pool.putconn(conn)


# =========================
# FORMATTING
# =========================
def order_summary(method: str, diamonds: int):
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
# ROUTES (GET fix for browser)
# =========================
@app.get("/")
def root():
    return {"ok": True, "message": "Webhook is running. Telegram will POST updates here."}


@app.post("/")
async def webhook(req: Request):
    upd = await req.json()

    msg = upd.get("message")
    cb = upd.get("callback_query")

    # -------------------------
    # MESSAGE HANDLER
    # -------------------------
    if msg:
        chat_id = msg["chat"]["id"]
        uid = msg["from"]["id"]
        username = msg["from"].get("username", "") or ""
        text = msg.get("text")

        ensure_user(uid, username)

        diamonds, state, temp = get_user(uid)

        # /start
        if text == "/start":
            if is_admin(uid):
                send_msg(chat_id, "Welcome ✅", admin_menu())
            else:
                send_msg(chat_id, "Welcome 💎", user_menu())
            set_state(uid, None, None)
            return {"ok": True}

        # Admin open panel
        if is_admin(uid) and text == "🛠 Admin Panel":
            send_msg(chat_id, "🛠 Admin Panel", admin_panel_menu())
            return {"ok": True}

        # Back to user menu (admin)
        if is_admin(uid) and text == "⬅️ Back to User Menu":
            send_msg(chat_id, "Back ✅", admin_menu())
            return {"ok": True}

        # Balance
        if text == "💰 Balance":
            diamonds, _, _ = get_user(uid)
            send_msg(chat_id, f"💎 Your Balance: {diamonds}")
            return {"ok": True}

        # My Orders
        if text == "📦 My Orders":
            rows = db_fetchall(
                "SELECT id, kind, method, diamonds, status, created_at "
                "FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT 10",
                (uid,),
            )
            if not rows:
                send_msg(chat_id, "📦 No orders yet.")
                return {"ok": True}
            out = ["📦 Your Orders (last 10):\n"]
            for oid, kind, method, dia, st, ct in rows:
                ts = ct.strftime("%d-%m %H:%M") if hasattr(ct, "strftime") else str(ct)
                out.append(f"#{oid} | {kind}/{method} | {dia}💎 | {st} | {ts}")
            send_msg(chat_id, "\n".join(out))
            return {"ok": True}

        # Add Diamonds
        if text == "💎 Add Diamonds":
            send_msg(
                chat_id,
                "💳 Select Payment Method:\n\n⚠️ Under Maintenance:\n\nPlease use other methods for deposit.",
                pay_method_kb(),
            )
            return {"ok": True}

        # Buy Coupon
        if text == "🛒 Buy Coupon":
            rows = []
            for t in COUPON_TYPES:
                p = get_price(t)
                s = stock_count(t)
                rows.append([{"text": f"{t} (💎{p}) Stock:{s}", "callback_data": f"buytype_{t}"}])
            send_msg(chat_id, "Select a coupon type:", {"inline_keyboard": rows})
            return {"ok": True}

        # -------------------------
        # ADMIN PANEL (message buttons)
        # -------------------------
        if is_admin(uid):
            if text == "📊 Stock":
                out = ["📊 Stock:\n"]
                for t in COUPON_TYPES:
                    out.append(f"{t}: {stock_count(t)}")
                send_msg(chat_id, "\n".join(out))
                return {"ok": True}

            if text == "➕ Add Coupon":
                send_msg(chat_id, "Select type to add coupons:", coupon_select_kb("admin_add_"))
                return {"ok": True}

            if text == "➖ Remove Coupon":
                send_msg(chat_id, "Select type to remove coupons:", coupon_select_kb("admin_remove_"))
                return {"ok": True}

            if text == "💰 Change Prices":
                cur = ["💰 Current Prices:\n"]
                for t in COUPON_TYPES:
                    cur.append(f"{t}: {get_price(t)} 💎")
                send_msg(chat_id, "\n".join(cur))
                send_msg(chat_id, "Select type to change price:", coupon_select_kb("admin_price_"))
                return {"ok": True}

            if text == "🎁 Free Coupon":
                send_msg(chat_id, "Select type to get a free coupon:", coupon_select_kb("admin_free_"))
                return {"ok": True}

            if text == "🔄 Update QR":
                set_state(uid, "admin_update_qr", None)
                send_msg(chat_id, "📸 Send the new UPI QR photo now:")
                return {"ok": True}

        # -------------------------
        # STATE MACHINE (USER FLOWS)
        # -------------------------

        # Amazon: amount
        if state == "amazon_amount":
            if not (text and text.isdigit()) or int(text) < MIN_DEPOSIT:
                send_msg(chat_id, f"❌ Minimum coin is {MIN_DEPOSIT}. Send a number.")
                return {"ok": True}
            dia = int(text)
            set_state(uid, None, str(dia))
            kb = {"inline_keyboard": [[{"text": "Submit a Gift Card", "callback_data": "amazon_submit"}]]}
            send_msg(chat_id, order_summary("Amazon Gift Card", dia), kb)
            return {"ok": True}

        # Amazon: gift card text
        if state == "amazon_gift_text":
            # temp is diamonds string
            dia = int(temp or "0")
            payload = {"dia": dia, "gift_text": text or ""}
            set_state(uid, "amazon_wait_ss", json.dumps(payload, ensure_ascii=False))
            send_msg(chat_id, "📸 Now upload a screenshot of the gift card:")
            return {"ok": True}

        # Amazon: screenshot
        if state == "amazon_wait_ss":
            photos = msg.get("photo")
            if not photos:
                send_msg(chat_id, "📸 Please upload screenshot image.")
                return {"ok": True}
            file_id = photos[-1]["file_id"]
            payload = json.loads(temp or "{}")
            dia = int(payload.get("dia", 0))
            gift_text = payload.get("gift_text", "")

            order_id = create_order(
                user_id=uid,
                kind="deposit",
                method="Amazon",
                amount_int=dia,
                diamonds=dia,
                details=f"gift={gift_text}",
                screenshot_id=file_id,
            )
            set_state(uid, None, None)
            send_msg(chat_id, "⏳ Admin is checking your code. Wait for approval.")
            notify_admin(order_id)
            return {"ok": True}

        # UPI: amount
        if state == "upi_amount":
            if not (text and text.isdigit()) or int(text) < MIN_DEPOSIT:
                send_msg(chat_id, f"❌ Minimum coin is {MIN_DEPOSIT}. Send a number.")
                return {"ok": True}

            dia = int(text)
            qr = get_upi_qr()
            if not qr:
                set_state(uid, None, None)
                send_msg(chat_id, "⚠️ UPI is not available right now. Admin has not updated QR.")
                return {"ok": True}

            set_state(uid, None, str(dia))
            kb = {"inline_keyboard": [[{"text": "✅ Done the Payment", "callback_data": "upi_done"}]]}
            send_photo(
                chat_id,
                qr,
                caption=order_summary("UPI", dia) + "\n\n✅ Pay and then click: Done the Payment",
                reply_markup=kb,
            )
            return {"ok": True}

        # UPI: payer name
        if state == "upi_payer":
            dia = int(temp or "0")
            payload = {"dia": dia, "payer": text or ""}
            set_state(uid, "upi_wait_ss", json.dumps(payload, ensure_ascii=False))
            send_msg(chat_id, "📸 Send screenshot of the payment:")
            return {"ok": True}

        # UPI: screenshot
        if state == "upi_wait_ss":
            photos = msg.get("photo")
            if not photos:
                send_msg(chat_id, "📸 Please upload payment screenshot image.")
                return {"ok": True}
            file_id = photos[-1]["file_id"]
            payload = json.loads(temp or "{}")
            dia = int(payload.get("dia", 0))
            payer = payload.get("payer", "")

            order_id = create_order(
                user_id=uid,
                kind="deposit",
                method="UPI",
                amount_int=dia,
                diamonds=dia,
                details=f"payer={payer}",
                screenshot_id=file_id,
            )
            set_state(uid, None, None)
            send_msg(chat_id, "⏳ Wait for admin approval.")
            notify_admin(order_id)
            return {"ok": True}

        # Buy coupon: quantity
        if state == "buy_qty":
            ctype = temp or ""
            if not (text and text.isdigit()) or int(text) <= 0:
                send_msg(chat_id, "❌ Please send the quantity as a number.")
                return {"ok": True}
            qty = int(text)

            ok, message, codes = purchase_coupons(uid, ctype, qty)
            set_state(uid, None, None)
            if not ok:
                send_msg(chat_id, message)
                return {"ok": True}

            send_msg(chat_id, message)
            # send codes in chunks to avoid telegram limits
            chunk = []
            for code in codes:
                chunk.append(code)
                if len(chunk) >= 25:
                    send_msg(chat_id, "🎟️ Your Coupons:\n" + "\n".join(chunk))
                    chunk = []
            if chunk:
                send_msg(chat_id, "🎟️ Your Coupons:\n" + "\n".join(chunk))
            return {"ok": True}

        # -------------------------
        # STATE MACHINE (ADMIN)
        # -------------------------
        if is_admin(uid):
            # Update QR photo
            if state == "admin_update_qr":
                photos = msg.get("photo")
                if not photos:
                    send_msg(chat_id, "📸 Send QR as photo only.")
                    return {"ok": True}
                file_id = photos[-1]["file_id"]
                set_upi_qr(file_id)
                set_state(uid, None, None)
                send_msg(chat_id, "✅ UPI QR Updated.", admin_panel_menu())
                return {"ok": True}

            # Add coupons (bulk)
            if state == "admin_add_codes":
                ctype = temp or ""
                lines = (text or "").splitlines()
                codes = [x.strip() for x in lines if x.strip()]
                if not codes:
                    send_msg(chat_id, "❌ Send coupon codes line-by-line.")
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
                send_msg(chat_id, f"✅ Added {len(codes)} coupons to {ctype}.", admin_panel_menu())
                return {"ok": True}

            # Remove coupons (qty)
            if state == "admin_remove_qty":
                ctype = temp or ""
                if not (text and text.isdigit()) or int(text) <= 0:
                    send_msg(chat_id, "❌ Send remove quantity as a number.")
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
                            send_msg(chat_id, f"❌ No stock to remove for {ctype}.")
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
                send_msg(chat_id, f"✅ Removed {len(ids)} coupons from {ctype}.", admin_panel_menu())
                return {"ok": True}

            # Change price (new price)
            if state == "admin_new_price":
                ctype = temp or ""
                if not (text and text.isdigit()) or int(text) <= 0:
                    send_msg(chat_id, "❌ Send new price as a number.")
                    return {"ok": True}
                set_price(ctype, int(text))
                set_state(uid, None, None)
                send_msg(chat_id, f"✅ Updated price: {ctype} = {text} 💎", admin_panel_menu())
                return {"ok": True}

        return {"ok": True}

    # -------------------------
    # CALLBACK HANDLER
    # -------------------------
    if cb:
        cb_id = cb["id"]
        uid = cb["from"]["id"]
        chat_id = cb["message"]["chat"]["id"]
        data = cb.get("data", "")

        answer_cb(cb_id)

        # USER: choose payment methods
        if data == "pay_amazon":
            set_state(uid, "amazon_amount", None)
            send_msg(chat_id, f"Enter the number of coins to add (Method: Amazon):\nMinimum is {MIN_DEPOSIT}")
            return {"ok": True}

        if data == "amazon_submit":
            # temp currently contains diamonds
            _, _, temp = get_user(uid)
            set_state(uid, "amazon_gift_text", temp)  # temp = diamonds
            send_msg(chat_id, "Enter your Amazon Gift Card Amount / Code for They Enter:")
            return {"ok": True}

        if data == "pay_upi":
            set_state(uid, "upi_amount", None)
            send_msg(chat_id, f"How many diamonds you need to buy? Minimum is {MIN_DEPOSIT}:")
            return {"ok": True}

        if data == "upi_done":
            # temp contains diamonds
            _, _, temp = get_user(uid)
            set_state(uid, "upi_payer", temp)
            send_msg(chat_id, "What Is The Payer Name?")
            return {"ok": True}

        # USER: buy coupon type
        if data.startswith("buytype_"):
            ctype = data.split("_", 1)[1]
            set_state(uid, "buy_qty", ctype)
            send_msg(chat_id, f"How many {ctype} coupons do you want to buy?\nPlease send the quantity:")
            return {"ok": True}

        # ADMIN: accept/decline deposits
        if is_admin(uid) and data.startswith("admin_acc_"):
            order_id = int(data.split("_")[-1])
            order = get_order(order_id)
            if not order:
                send_msg(chat_id, "❌ Order not found.")
                return {"ok": True}
            if order[8] != "pending":
                send_msg(chat_id, f"⚠️ Already {order[8]}.")
                return {"ok": True}

            # if deposit -> add diamonds
            kind = order[2]
            user_id = order[1]
            dia = int(order[5])

            if kind == "deposit":
                add_diamonds(user_id, dia)

            set_order_status(order_id, "approved")
            send_msg(chat_id, f"✅ Approved Order #{order_id}")
            send_msg(user_id, f"✅ Approved! {dia} Diamonds added.")
            return {"ok": True}

        if is_admin(uid) and data.startswith("admin_dec_"):
            order_id = int(data.split("_")[-1])
            order = get_order(order_id)
            if not order:
                send_msg(chat_id, "❌ Order not found.")
                return {"ok": True}
            if order[8] != "pending":
                send_msg(chat_id, f"⚠️ Already {order[8]}.")
                return {"ok": True}

            set_order_status(order_id, "declined")
            send_msg(chat_id, f"❌ Declined Order #{order_id}")
            send_msg(order[1], "❌ Your request was declined by admin.")
            return {"ok": True}

        # ADMIN PANEL callbacks
        if is_admin(uid) and data.startswith("admin_add_"):
            ctype = data.split("_", 2)[2]
            set_state(uid, "admin_add_codes", ctype)
            send_msg(chat_id, f"Send {ctype} coupon codes line-by-line:")
            return {"ok": True}

        if is_admin(uid) and data.startswith("admin_remove_"):
            ctype = data.split("_", 2)[2]
            set_state(uid, "admin_remove_qty", ctype)
            send_msg(chat_id, f"How many {ctype} coupons do you want to remove? Send a number:")
            return {"ok": True}

        if is_admin(uid) and data.startswith("admin_price_"):
            ctype = data.split("_", 2)[2]
            set_state(uid, "admin_new_price", ctype)
            send_msg(chat_id, f"Send new price (diamonds) for {ctype}:")
            return {"ok": True}

        if is_admin(uid) and data.startswith("admin_free_"):
            ctype = data.split("_", 2)[2]

            # give one coupon for free (remove from DB)
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
                return {"ok": True}
            finally:
                try:
                    conn.autocommit = True
                except Exception:
                    pass
                pool.putconn(conn)

            send_msg(chat_id, f"🎁 Free Coupon ({ctype}):\n{code}")
            return {"ok": True}

        return {"ok": True}

    return {"ok": True}
