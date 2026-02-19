import os
import re
import requests
from datetime import datetime
from typing import Optional, List, Dict, Any

import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor

from fastapi import FastAPI, Request

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL missing")
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS missing (comma-separated Telegram user IDs)")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Minimums
MIN_AMAZON = 30
MIN_UPI = 30

COUPON_TYPES = ["500", "1k", "2k", "4k"]

app = FastAPI()

# =========================
# DB POOL
# =========================
pool = ThreadedConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=DATABASE_URL,
    sslmode="require",
)

def db_fetchall(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        pool.putconn(conn)

def db_fetchone(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    rows = db_fetchall(sql, params)
    return rows[0] if rows else None

def db_execute(sql: str, params: tuple = ()) -> None:
    conn = pool.getconn()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params)
    finally:
        pool.putconn(conn)

def db_transaction(fn):
    conn = pool.getconn()
    try:
        conn.autocommit = False
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result = fn(conn, cur)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True
        pool.putconn(conn)

# =========================
# TELEGRAM HELPERS
# =========================
def tg_send_message(chat_id: int, text: str, reply_markup: dict = None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(f"{API}/sendMessage", json=payload, timeout=10)

def tg_send_photo(chat_id: int, file_id: str, caption: str, reply_markup: dict = None):
    payload = {"chat_id": chat_id, "photo": file_id, "caption": caption, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(f"{API}/sendPhoto", json=payload, timeout=10)

def tg_answer_callback(callback_id: str, text: str = ""):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = False
    requests.post(f"{API}/answerCallbackQuery", json=payload, timeout=10)

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def safe_int(s: str) -> Optional[int]:
    try:
        s = (s or "").strip()
        if not re.fullmatch(r"\d+", s):
            return None
        return int(s)
    except Exception:
        return None

# =========================
# MENUS
# =========================
def main_menu():
    return {
        "keyboard": [
            ["💰 Add Coins", "🛒 Buy Coupon"],
            ["📦 My Orders", "💎 Balance"],
        ],
        "resize_keyboard": True,
    }

def payment_menu():
    return {
        "keyboard": [
            ["Amazon Gift Card", "UPI"],
            ["⬅️ Back"]
        ],
        "resize_keyboard": True
    }

def admin_menu():
    return {
        "keyboard": [
            ["📊 Stock", "💲 Change Prices"],
            ["🎁 Get Code Free"],
            ["➕ Add Coupon", "➖ Remove Coupon"],
            ["⬅️ Back"],
        ],
        "resize_keyboard": True,
    }

# =========================
# IN-MEMORY STATES
# =========================
states: Dict[int, Dict[str, Any]] = {}

def set_state(chat_id: int, **kwargs):
    states[chat_id] = {**states.get(chat_id, {}), **kwargs}

def clear_state(chat_id: int):
    if chat_id in states:
        del states[chat_id]

# =========================
# DB USER
# =========================
def ensure_user(telegram_id: int, username: str):
    db_execute(
        "INSERT INTO users (telegram_id, username) VALUES (%s, %s) "
        "ON CONFLICT (telegram_id) DO UPDATE SET username=EXCLUDED.username",
        (telegram_id, username or "")
    )

def get_balance(telegram_id: int) -> int:
    row = db_fetchone("SELECT diamonds FROM users WHERE telegram_id=%s", (telegram_id,))
    return int(row["diamonds"]) if row else 0

# =========================
# COUPON / PRICE HELPERS
# =========================
def get_price_map() -> Dict[str, int]:
    rows = db_fetchall("SELECT type, price FROM coupon_prices")
    return {r["type"]: int(r["price"]) for r in rows}

def get_stock_count(t: str) -> int:
    row = db_fetchone("SELECT COUNT(*) AS c FROM coupons WHERE type=%s AND is_used=FALSE", (t,))
    return int(row["c"]) if row else 0

def coupon_menu():
    prices = get_price_map()
    keyboard = []
    for t in COUPON_TYPES:
        price = prices.get(t, 0)
        stock = get_stock_count(t)
        keyboard.append([f"{t} (💎{price} | Stock:{stock})"])
    keyboard.append(["⬅️ Back"])
    return {"keyboard": keyboard, "resize_keyboard": True}

def extract_coupon_type_from_button(text: str) -> Optional[str]:
    if not text:
        return None
    first = text.strip().split()[0]
    return first if first in COUPON_TYPES else None

# =========================
# HEALTH ROUTES
# =========================
@app.get("/")
def home():
    return {"ok": True, "message": "Webhook is running. Telegram must POST updates here."}

@app.get("/health")
def health():
    return {"ok": True}

# =========================
# WEBHOOK
# =========================
@app.post("/")
async def webhook(request: Request):
    data = await request.json()

    # -------------------------
    # CALLBACKS
    # -------------------------
    if "callback_query" in data:
        cq = data["callback_query"]
        callback_id = cq["id"]
        from_id = cq["from"]["id"]
        msg_chat_id = cq["message"]["chat"]["id"]
        cb_data = cq.get("data", "")

        # Amazon "Submit Gift Card"
        if cb_data.startswith("amazon_submit:"):
            uid = int(cb_data.split(":")[1])
            if msg_chat_id != uid:
                tg_answer_callback(callback_id, "Not for you.")
                return {"ok": True}

            st = states.get(uid, {})
            if st.get("step") != "amazon_wait_submit":
                tg_answer_callback(callback_id, "Expired.")
                return {"ok": True}

            set_state(uid, step="amazon_gift_amount")
            tg_answer_callback(callback_id, "OK")
            tg_send_message(uid, f"Enter your Amazon Gift Card Amount / Code for {st['diamonds']} Diamonds:")
            return {"ok": True}

        # Admin Approve / Reject deposit
        if cb_data.startswith("dep_"):
            if from_id not in ADMIN_IDS:
                tg_answer_callback(callback_id, "Admins only.")
                return {"ok": True}

            try:
                action, dep_id = cb_data.split(":")
                dep_id = int(dep_id)
            except Exception:
                tg_answer_callback(callback_id, "Bad callback.")
                return {"ok": True}

            deposit = db_fetchone("SELECT * FROM deposits WHERE id=%s", (dep_id,))
            if not deposit:
                tg_answer_callback(callback_id, "Deposit not found.")
                return {"ok": True}

            if deposit["status"] != "pending":
                tg_answer_callback(callback_id, "Already processed.")
                return {"ok": True}

            user_id = int(deposit["telegram_id"])
            diamonds = int(deposit["diamonds"])

            if action == "dep_approve":
                def _tx(conn, cur):
                    cur.execute("UPDATE deposits SET status='approved' WHERE id=%s AND status='pending'", (dep_id,))
                    if cur.rowcount != 1:
                        return False
                    cur.execute("UPDATE users SET diamonds = diamonds + %s WHERE telegram_id=%s", (diamonds, user_id))
                    return True

                ok = db_transaction(_tx)
                if ok:
                    tg_answer_callback(callback_id, "Approved ✅")
                    tg_send_message(user_id, f"✅ Deposit Approved!\n💎 {diamonds} Diamonds added.")
                else:
                    tg_answer_callback(callback_id, "Could not approve.")
                return {"ok": True}

            if action == "dep_reject":
                def _tx(conn, cur):
                    cur.execute("UPDATE deposits SET status='rejected' WHERE id=%s AND status='pending'", (dep_id,))
                    return cur.rowcount == 1

                ok = db_transaction(_tx)
                if ok:
                    tg_answer_callback(callback_id, "Rejected ❌")
                    tg_send_message(user_id, "❌ Deposit Rejected.")
                else:
                    tg_answer_callback(callback_id, "Could not reject.")
                return {"ok": True}

            tg_answer_callback(callback_id, "Unknown action.")
            return {"ok": True}

        tg_answer_callback(callback_id, "")
        return {"ok": True}

    # -------------------------
    # MESSAGES
    # -------------------------
    if "message" not in data:
        return {"ok": True}

    msg = data["message"]
    chat_id = msg["chat"]["id"]
    username = msg.get("from", {}).get("username", "") or ""
    text = msg.get("text", "")

    ensure_user(chat_id, username)

    # reset/back
    if text in ("/reset", "/cancel"):
        clear_state(chat_id)
        tg_send_message(chat_id, "✅ Reset done.", main_menu())
        return {"ok": True}

    if text == "⬅️ Back":
        clear_state(chat_id)
        tg_send_message(chat_id, "Main menu:", main_menu())
        return {"ok": True}

    if text == "/start":
        clear_state(chat_id)
        tg_send_message(chat_id, "Welcome 💎", main_menu())
        return {"ok": True}

    if text == "/admin" and chat_id in ADMIN_IDS:
        clear_state(chat_id)
        tg_send_message(chat_id, "Admin Panel 👑", admin_menu())
        return {"ok": True}

    if text == "💎 Balance":
        bal = get_balance(chat_id)
        tg_send_message(chat_id, f"💎 Your Balance: <b>{bal}</b> Diamonds")
        return {"ok": True}

    if text == "📦 My Orders":
        orders = db_fetchall(
            "SELECT coupon_type, quantity, total_spent, created_at FROM orders WHERE telegram_id=%s ORDER BY id DESC LIMIT 20",
            (chat_id,)
        )
        if not orders:
            tg_send_message(chat_id, "📦 No orders yet.")
            return {"ok": True}

        lines = ["📦 <b>Your Orders</b> (last 20):\n"]
        for o in orders:
            dt = o["created_at"].strftime("%Y-%m-%d %H:%M") if o["created_at"] else ""
            lines.append(f"• {o['coupon_type']} x{o['quantity']} — 💎{o['total_spent']} — {dt}")
        tg_send_message(chat_id, "\n".join(lines))
        return {"ok": True}

    # =========================
    # USER: ADD COINS
    # =========================
    if text == "💰 Add Coins":
        clear_state(chat_id)
        tg_send_message(
            chat_id,
            "💳 <b>Select Payment Method:</b>\n\n⚠️ Under Maintenance:\n🛠️ Google Play Redeem Code\n\nPlease use other methods for deposit.",
            payment_menu()
        )
        return {"ok": True}

    if text == "Amazon Gift Card":
        clear_state(chat_id)
        set_state(chat_id, step="amazon_coins")
        tg_send_message(chat_id, f"Enter the number of coins to add (Method: Amazon)\nMinimum: {MIN_AMAZON}")
        return {"ok": True}

    if text == "UPI":
        clear_state(chat_id)
        set_state(chat_id, step="upi_coins")
        tg_send_message(chat_id, f"Enter the number of coins to add (Method: UPI)\nMinimum: {MIN_UPI}")
        return {"ok": True}

    # =========================
    # USER: BUY COUPON
    # =========================
    if text == "🛒 Buy Coupon":
        clear_state(chat_id)
        set_state(chat_id, step="await_coupon_type")
        tg_send_message(chat_id, "Select a coupon type:", coupon_menu())
        return {"ok": True}

    # =========================
    # ADMIN PANEL ACTIONS
    # =========================
    if chat_id in ADMIN_IDS:
        if text == "📊 Stock":
            lines = ["📊 <b>Stock</b>:"]
            for t in COUPON_TYPES:
                lines.append(f"• {t}: {get_stock_count(t)}")
            tg_send_message(chat_id, "\n".join(lines))
            return {"ok": True}

        if text == "💲 Change Prices":
            clear_state(chat_id)
            set_state(chat_id, step="admin_price_type")
            tg_send_message(chat_id, "Select Type: 500 / 1k / 2k / 4k")
            return {"ok": True}

        if text == "🎁 Get Code Free":
            clear_state(chat_id)
            set_state(chat_id, step="admin_free_type")
            tg_send_message(chat_id, "Select Type: 500 / 1k / 2k / 4k")
            return {"ok": True}

        if text == "➕ Add Coupon":
            clear_state(chat_id)
            set_state(chat_id, step="admin_add_type")
            tg_send_message(chat_id, "Select Type: 500 / 1k / 2k / 4k")
            return {"ok": True}

        if text == "➖ Remove Coupon":
            clear_state(chat_id)
            set_state(chat_id, step="admin_remove_type")
            tg_send_message(chat_id, "Select Type: 500 / 1k / 2k / 4k")
            return {"ok": True}

    # =========================
    # STATE MACHINE
    # =========================
    st = states.get(chat_id, {})

    # -------- AMAZON: coins input --------
    if st.get("step") == "amazon_coins":
        amt = safe_int(text)
        if amt is None:
            tg_send_message(chat_id, "❌ Please send a valid number.")
            return {"ok": True}
        if amt < MIN_AMAZON:
            tg_send_message(chat_id, f"❌ Minimum is {MIN_AMAZON}.")
            return {"ok": True}

        set_state(chat_id, step="amazon_wait_submit", diamonds=amt)

        summary = (
            "📝 <b>Order Summary:</b>\n"
            "━━━━━━━━━━━━━━━\n"
            "💹 Rate: 1 Rs = 1 Diamond 💎\n"
            f"💵 Amount: <b>{amt}</b>\n"
            f"💎 Diamonds to Receive: <b>{amt}</b>\n"
            "💳 Method: <b>Amazon Gift Card</b>\n"
            f"📅 Time: <b>{now_str()}</b>\n"
            "━━━━━━━━━━━━━━━\n"
            "Click below to proceed."
        )

        tg_send_message(
            chat_id,
            summary,
            {"inline_keyboard": [[{"text": "✅ Submit a Gift Card", "callback_data": f"amazon_submit:{chat_id}"}]]}
        )
        return {"ok": True}

    # -------- AMAZON: gift amount/code ANY TEXT (no validation) --------
    if st.get("step") == "amazon_gift_amount":
        gift_text = (text or "").strip()
        set_state(chat_id, step="amazon_screenshot", gift_amount=gift_text)
        tg_send_message(chat_id, "📸 Now upload a screenshot of the gift card:")
        return {"ok": True}

    # -------- UPI: coins input --------
    if st.get("step") == "upi_coins":
        amt = safe_int(text)
        if amt is None:
            tg_send_message(chat_id, "❌ Please send a valid number.")
            return {"ok": True}
        if amt < MIN_UPI:
            tg_send_message(chat_id, f"❌ Minimum is {MIN_UPI}.")
            return {"ok": True}

        set_state(chat_id, step="upi_screenshot", diamonds=amt)
        tg_send_message(chat_id, "📸 Now upload a screenshot of the UPI payment:")
        return {"ok": True}

    # -------- SCREENSHOT handler (Amazon/UPI) --------
    if "photo" in msg and st.get("step") in ("amazon_screenshot", "upi_screenshot"):
        file_id = msg["photo"][-1]["file_id"]
        method = "Amazon Gift Card" if st["step"] == "amazon_screenshot" else "UPI"
        diamonds = int(st.get("diamonds", 0))

        # gift_amount can be TEXT now (code or amount)
        gift_amount = st.get("gift_amount", str(diamonds))

        def _tx(conn, cur):
            cur.execute(
                """
                INSERT INTO deposits (telegram_id, method, diamonds, amount, screenshot_file_id, status)
                VALUES (%s,%s,%s,%s,%s,'pending')
                RETURNING id
                """,
                (chat_id, method, diamonds, str(gift_amount), file_id)
            )
            row = cur.fetchone()
            return int(row["id"])

        dep_id = db_transaction(_tx)

        tg_send_message(chat_id, "✅ Admin is checking your deposit. Please wait for approval.", main_menu())

        admin_caption = (
            "📥 <b>New Deposit Request</b>\n\n"
            f"👤 User: <b>@{username}</b>\n"
            f"🆔 TG ID: <code>{chat_id}</code>\n"
            f"💳 Method: <b>{method}</b>\n"
            f"💎 Diamonds: <b>{diamonds}</b>\n"
            f"🧾 Gift Amount/Code: <code>{gift_amount}</code>\n"
            f"📅 Time: <b>{now_str()}</b>\n"
            f"🧾 Deposit ID: <code>{dep_id}</code>"
        )

        admin_kb = {
            "inline_keyboard": [[
                {"text": "✅ Accept", "callback_data": f"dep_approve:{dep_id}"},
                {"text": "❌ Decline", "callback_data": f"dep_reject:{dep_id}"}
            ]]
        }

        for admin_id in ADMIN_IDS:
            tg_send_photo(admin_id, file_id, admin_caption, admin_kb)

        clear_state(chat_id)
        return {"ok": True}

    # -------- BUY: awaiting coupon type --------
    if st.get("step") == "await_coupon_type":
        ctype = extract_coupon_type_from_button(text)
        if ctype is None:
            tg_send_message(chat_id, "❌ Please select a coupon type using the buttons.", coupon_menu())
            return {"ok": True}
        set_state(chat_id, step="await_coupon_qty", coupon_type=ctype)
        tg_send_message(chat_id, f"How many <b>{ctype}</b> coupons do you want to buy?\nPlease send the quantity:")
        return {"ok": True}

    # -------- BUY: quantity --------
    if st.get("step") == "await_coupon_qty":
        qty = safe_int(text)
        if qty is None or qty <= 0:
            tg_send_message(chat_id, "❌ Send a valid quantity (example: 1, 2, 3).")
            return {"ok": True}

        ctype = st.get("coupon_type")
        prices = get_price_map()
        price = int(prices.get(ctype, 0))
        total_cost = price * qty

        if price <= 0:
            tg_send_message(chat_id, "❌ Price not set for this type. Contact admin.")
            clear_state(chat_id)
            return {"ok": True}

        def _buy_tx(conn, cur):
            cur.execute("SELECT diamonds FROM users WHERE telegram_id=%s FOR UPDATE", (chat_id,))
            u = cur.fetchone()
            if not u:
                return ("ERR", "User not found")
            balance = int(u["diamonds"])
            if balance < total_cost:
                return ("NO_BAL", balance)

            cur.execute(
                """
                SELECT id, code FROM coupons
                WHERE type=%s AND is_used=FALSE
                ORDER BY id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (ctype, qty)
            )
            picked = cur.fetchall()
            if len(picked) < qty:
                return ("NO_STOCK", len(picked))

            picked_ids = [p["id"] for p in picked]
            picked_codes = [p["code"] for p in picked]

            cur.execute("UPDATE users SET diamonds = diamonds - %s WHERE telegram_id=%s", (total_cost, chat_id))
            cur.execute("UPDATE coupons SET is_used=TRUE WHERE id = ANY(%s)", (picked_ids,))
            cur.execute("INSERT INTO orders (telegram_id, coupon_type, quantity, total_spent) VALUES (%s,%s,%s,%s)",
                        (chat_id, ctype, qty, total_cost))
            return ("OK", picked_codes, balance - total_cost)

        result = db_transaction(_buy_tx)

        if result[0] == "NO_STOCK":
            tg_send_message(chat_id, f"❌ Not enough stock!\nAvailable: <b>{result[1]}</b>", coupon_menu())
            return {"ok": True}

        if result[0] == "NO_BAL":
            bal = result[1]
            tg_send_message(chat_id, f"❌ Not enough diamonds!\nNeeded: <b>{total_cost}</b>\nYou have: <b>{bal}</b>")
            return {"ok": True}

        if result[0] != "OK":
            tg_send_message(chat_id, "❌ Something went wrong. Try again.")
            clear_state(chat_id)
            return {"ok": True}

        codes = result[1]
        new_balance = result[2]

        tg_send_message(
            chat_id,
            "🎉 <b>Purchase Successful!</b>\n\nHere are your coupon codes:\n\n"
            + "\n".join([f"<code>{c}</code>" for c in codes])
            + f"\n\n💎 New Balance: <b>{new_balance}</b>",
            main_menu()
        )
        clear_state(chat_id)
        return {"ok": True}

    # =========================
    # ADMIN STATE MACHINE
    # =========================
    if chat_id in ADMIN_IDS:
        if st.get("step") == "admin_price_type":
            if text not in COUPON_TYPES:
                tg_send_message(chat_id, "❌ Select: 500 / 1k / 2k / 4k")
                return {"ok": True}
            set_state(chat_id, step="admin_price_value", admin_type=text)
            tg_send_message(chat_id, f"Enter new price for <b>{text}</b>:")
            return {"ok": True}

        if st.get("step") == "admin_price_value":
            new_price = safe_int(text)
            if new_price is None or new_price <= 0:
                tg_send_message(chat_id, "❌ Enter a valid price (number > 0).")
                return {"ok": True}
            t = st.get("admin_type")
            db_execute("UPDATE coupon_prices SET price=%s WHERE type=%s", (new_price, t))
            tg_send_message(chat_id, f"✅ Price updated: <b>{t}</b> = 💎<b>{new_price}</b>", admin_menu())
            clear_state(chat_id)
            return {"ok": True}

        if st.get("step") == "admin_free_type":
            if text not in COUPON_TYPES:
                tg_send_message(chat_id, "❌ Select: 500 / 1k / 2k / 4k")
                return {"ok": True}

            def _free_tx(conn, cur):
                cur.execute(
                    """
                    SELECT id, code FROM coupons
                    WHERE type=%s AND is_used=FALSE
                    ORDER BY id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """,
                    (text,)
                )
                row = cur.fetchone()
                if not row:
                    return None
                cur.execute("UPDATE coupons SET is_used=TRUE WHERE id=%s", (row["id"],))
                return row["code"]

            code = db_transaction(_free_tx)
            if not code:
                tg_send_message(chat_id, "❌ No stock for that type.")
                return {"ok": True}

            tg_send_message(chat_id, f"🎁 Free Code ({text}):\n<code>{code}</code>", admin_menu())
            clear_state(chat_id)
            return {"ok": True}

        if st.get("step") == "admin_add_type":
            if text not in COUPON_TYPES:
                tg_send_message(chat_id, "❌ Select: 500 / 1k / 2k / 4k")
                return {"ok": True}
            set_state(chat_id, step="admin_add_codes", admin_type=text)
            tg_send_message(chat_id, f"Send {text} coupons line-by-line (one per line):")
            return {"ok": True}

        if st.get("step") == "admin_add_codes":
            t = st.get("admin_type")
            codes = [c.strip() for c in (text or "").splitlines() if c.strip()]
            if not codes:
                tg_send_message(chat_id, "❌ No codes found. Send again line-by-line.")
                return {"ok": True}

            def _add_tx(conn, cur):
                for c in codes:
                    cur.execute("INSERT INTO coupons (type, code, is_used) VALUES (%s,%s,FALSE)", (t, c))
                return len(codes)

            n = db_transaction(_add_tx)
            tg_send_message(chat_id, f"✅ Added <b>{n}</b> coupons to <b>{t}</b>.", admin_menu())
            clear_state(chat_id)
            return {"ok": True}

        if st.get("step") == "admin_remove_type":
            if text not in COUPON_TYPES:
                tg_send_message(chat_id, "❌ Select: 500 / 1k / 2k / 4k")
                return {"ok": True}
            set_state(chat_id, step="admin_remove_qty", admin_type=text)
            tg_send_message(chat_id, f"How many unused <b>{text}</b> coupons to remove?")
            return {"ok": True}

        if st.get("step") == "admin_remove_qty":
            qty = safe_int(text)
            if qty is None or qty <= 0:
                tg_send_message(chat_id, "❌ Enter a valid number.")
                return {"ok": True}
            t = st.get("admin_type")

            def _rm_tx(conn, cur):
                cur.execute(
                    """
                    DELETE FROM coupons
                    WHERE id IN (
                        SELECT id FROM coupons
                        WHERE type=%s AND is_used=FALSE
                        ORDER BY id
                        LIMIT %s
                    )
                    """,
                    (t, qty)
                )
                return cur.rowcount

            removed = db_transaction(_rm_tx)
            tg_send_message(chat_id, f"✅ Removed <b>{removed}</b> coupons from <b>{t}</b>.", admin_menu())
            clear_state(chat_id)
            return {"ok": True}

    # If user sent photo but not expected
    if "photo" in msg:
        tg_send_message(chat_id, "❌ I wasn't expecting a screenshot right now. Use 💰 Add Coins first.")
        return {"ok": True}

    tg_send_message(chat_id, "Choose an option:", main_menu())
    return {"ok": True}
