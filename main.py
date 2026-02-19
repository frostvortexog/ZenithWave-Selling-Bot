import os
import psycopg2
import requests
from fastapi import FastAPI, Request
from psycopg2.extras import RealDictCursor
from datetime import datetime

app = FastAPI()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS").split(",")))
DATABASE_URL = os.getenv("DATABASE_URL")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
conn = psycopg2.connect(DATABASE_URL, sslmode="require")
conn.autocommit = True

user_states = {}

# ---------------- DATABASE ---------------- #

def query(sql, params=None, fetch=False):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        if fetch:
            return cur.fetchall()

def transaction(actions):
    with conn:
        with conn.cursor() as cur:
            for sql, params in actions:
                cur.execute(sql, params)

# ---------------- TELEGRAM ---------------- #

def send(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = keyboard
    requests.post(f"{API}/sendMessage", json=payload)

def send_photo(chat_id, file_id, caption, keyboard=None):
    payload = {"chat_id": chat_id, "photo": file_id, "caption": caption}
    if keyboard:
        payload["reply_markup"] = keyboard
    requests.post(f"{API}/sendPhoto", json=payload)

# ---------------- MENUS ---------------- #

def main_menu():
    return {
        "keyboard": [
            ["💰 Add Coins", "🛒 Buy Coupon"],
            ["📦 My Orders", "💎 Balance"]
        ],
        "resize_keyboard": True
    }

def admin_menu():
    return {
        "keyboard": [
            ["📊 Stock", "💲 Change Prices"],
            ["🎁 Get Code Free"],
            ["➕ Add Coupon", "➖ Remove Coupon"]
        ],
        "resize_keyboard": True
    }

# ---------------- WEBHOOK ---------------- #

@app.post("/")
async def webhook(request: Request):
    data = await request.json()

    # -------- MESSAGE -------- #
    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text")
        username = msg["from"].get("username","")

        query("INSERT INTO users (telegram_id, username) VALUES (%s,%s) ON CONFLICT DO NOTHING",
              (chat_id, username))

        if text == "/start":
            send(chat_id,"Welcome 💎",main_menu())
            return {"ok":True}

        if chat_id in ADMIN_IDS and text == "/admin":
            send(chat_id,"Admin Panel 👑",admin_menu())
            return {"ok":True}

        if text == "💎 Balance":
            bal = query("SELECT diamonds FROM users WHERE telegram_id=%s",
                        (chat_id,),True)[0]["diamonds"]
            send(chat_id,f"💎 Your Balance: {bal}")
            return {"ok":True}

        # ---------------- ADD COINS ---------------- #
        if text == "💰 Add Coins":
            send(chat_id,
                 "💳 Select Payment Method:\n\n⚠️ Under Maintenance:\n🛠️ Google Play Redeem Code\n\nUse other methods.",
                 {"keyboard":[["Amazon Gift Card","UPI"]],
                  "resize_keyboard":True})
            return {"ok":True}

        if text == "Amazon Gift Card":
            user_states[chat_id]={"step":"amazon_amount"}
            send(chat_id,"Enter Diamonds (Min 20):")
            return {"ok":True}

        if text == "UPI":
            user_states[chat_id]={"step":"upi_amount"}
            send(chat_id,"Enter Diamonds (Min 30):")
            return {"ok":True}

        if chat_id in user_states:
            state=user_states[chat_id]

            if state["step"]=="amazon_amount":
                amt=int(text)
                if amt<20:
                    send(chat_id,"Minimum 20")
                    return {"ok":True}
                state.update({"amount":amt,"method":"Amazon","step":"screenshot"})
                send(chat_id,f"Send Gift Card Screenshot for {amt} Diamonds")
                return {"ok":True}

            if state["step"]=="upi_amount":
                amt=int(text)
                if amt<30:
                    send(chat_id,"Minimum 30")
                    return {"ok":True}
                state.update({"amount":amt,"method":"UPI","step":"screenshot"})
                send(chat_id,f"Send UPI Screenshot for {amt} Diamonds")
                return {"ok":True}

        # SCREENSHOT
        if "photo" in msg:
            file_id=msg["photo"][-1]["file_id"]
            if chat_id in user_states:
                state=user_states[chat_id]
                amt=state["amount"]
                method=state["method"]

                query("INSERT INTO deposits (telegram_id,method,diamonds,amount,screenshot_file_id) VALUES (%s,%s,%s,%s,%s)",
                      (chat_id,method,amt,amt,file_id))

                send(chat_id,"Admin reviewing your deposit...")

                for admin in ADMIN_IDS:
                    send_photo(admin,file_id,
                               f"Deposit Request\nUser:@{username}\nMethod:{method}\nDiamonds:{amt}",
                               {"inline_keyboard":[[
                                   {"text":"✅ Accept","callback_data":f"approve_{chat_id}_{amt}"},
                                   {"text":"❌ Decline","callback_data":f"reject_{chat_id}"}
                               ]]})

                del user_states[chat_id]
                return {"ok":True}

        # ---------------- BUY COUPON ---------------- #
        if text=="🛒 Buy Coupon":
            prices=query("SELECT * FROM coupon_prices",fetch=True)
            buttons=[]
            for p in prices:
                stock=query("SELECT COUNT(*) FROM coupons WHERE type=%s AND is_used=FALSE",
                            (p["type"],),True)[0]["count"]
                buttons.append([f"{p['type']} (💎{p['price']} | Stock:{stock})"])
            send(chat_id,"Select Coupon:",{"keyboard":buttons,"resize_keyboard":True})
            return {"ok":True}

        # SELECT TYPE
        if any(t in text for t in ["500","1k","2k","4k"]):
            ctype=text.split()[0]
            user_states[chat_id]={"step":"buy_qty","type":ctype}
            send(chat_id,"Enter Quantity:")
            return {"ok":True}

        # QUANTITY
        if chat_id in user_states and user_states[chat_id]["step"]=="buy_qty":
            qty=int(text)
            ctype=user_states[chat_id]["type"]

            price=query("SELECT price FROM coupon_prices WHERE type=%s",
                        (ctype,),True)[0]["price"]

            stock=query("SELECT id FROM coupons WHERE type=%s AND is_used=FALSE LIMIT %s",
                        (ctype,qty),True)

            if len(stock)<qty:
                send(chat_id,f"❌ Not enough stock! Available: {len(stock)}")
                return {"ok":True}

            user=query("SELECT diamonds FROM users WHERE telegram_id=%s",
                       (chat_id,),True)[0]["diamonds"]

            total=price*qty
            if user<total:
                send(chat_id,f"❌ Not enough diamonds!\nNeeded:{total}\nYou have:{user}")
                return {"ok":True}

            actions=[]
            actions.append(("UPDATE users SET diamonds=diamonds-%s WHERE telegram_id=%s",
                            (total,chat_id)))

            for s in stock:
                actions.append(("UPDATE coupons SET is_used=TRUE WHERE id=%s",(s["id"],)))

            actions.append(("INSERT INTO orders (telegram_id,coupon_type,quantity,total_spent) VALUES (%s,%s,%s,%s)",
                            (chat_id,ctype,qty,total)))

            transaction(actions)

            codes="\n".join([query("SELECT code FROM coupons WHERE id=%s",(s["id"],),True)[0]["code"] for s in stock])

            send(chat_id,f"🎉 Purchase Successful!\n\n{codes}",main_menu())
            del user_states[chat_id]
            return {"ok":True}

        # ---------------- MY ORDERS ---------------- #
        if text=="📦 My Orders":
            orders=query("SELECT * FROM orders WHERE telegram_id=%s ORDER BY id DESC LIMIT 10",
                         (chat_id,),True)
            if not orders:
                send(chat_id,"No orders yet.")
                return {"ok":True}
            msg="📦 Your Orders:\n"
            for o in orders:
                msg+=f"{o['coupon_type']} x{o['quantity']} (💎{o['total_spent']})\n"
            send(chat_id,msg)
            return {"ok":True}

        # ---------------- ADMIN FEATURES ---------------- #
        if chat_id in ADMIN_IDS:

            if text=="📊 Stock":
                msg="📊 Stock:\n"
                for t in ["500","1k","2k","4k"]:
                    count=query("SELECT COUNT(*) FROM coupons WHERE type=%s AND is_used=FALSE",
                                (t,),True)[0]["count"]
                    msg+=f"{t}: {count}\n"
                send(chat_id,msg)
                return {"ok":True}

            if text=="🎁 Get Code Free":
                user_states[chat_id]={"step":"free_select"}
                send(chat_id,"Select Type: 500 / 1k / 2k / 4k")
                return {"ok":True}

            if chat_id in user_states and user_states[chat_id]["step"]=="free_select":
                ctype=text
                code=query("SELECT id,code FROM coupons WHERE type=%s AND is_used=FALSE LIMIT 1",
                           (ctype,),True)
                if not code:
                    send(chat_id,"No stock.")
                    return {"ok":True}
                transaction([
                    ("UPDATE coupons SET is_used=TRUE WHERE id=%s",(code[0]["id"],))
                ])
                send(chat_id,f"🎁 Code:\n{code[0]['code']}")
                del user_states[chat_id]
                return {"ok":True}

            if text=="➕ Add Coupon":
                user_states[chat_id]={"step":"add_type"}
                send(chat_id,"Select Type: 500 / 1k / 2k / 4k")
                return {"ok":True}

            if chat_id in user_states and user_states[chat_id]["step"]=="add_type":
                user_states[chat_id]={"step":"add_codes","type":text}
                send(chat_id,"Send codes line by line:")
                return {"ok":True}

            if chat_id in user_states and user_states[chat_id]["step"]=="add_codes":
                codes=text.split("\n")
                for c in codes:
                    query("INSERT INTO coupons (type,code) VALUES (%s,%s)",
                          (user_states[chat_id]["type"],c.strip()))
                send(chat_id,"Coupons Added.")
                del user_states[chat_id]
                return {"ok":True}

            if text=="➖ Remove Coupon":
                user_states[chat_id]={"step":"remove_type"}
                send(chat_id,"Select Type: 500 / 1k / 2k / 4k")
                return {"ok":True}

            if chat_id in user_states and user_states[chat_id]["step"]=="remove_type":
                user_states[chat_id]={"step":"remove_qty","type":text}
                send(chat_id,"How many to remove?")
                return {"ok":True}

            if chat_id in user_states and user_states[chat_id]["step"]=="remove_qty":
                qty=int(text)
                ctype=user_states[chat_id]["type"]
                query("DELETE FROM coupons WHERE id IN (SELECT id FROM coupons WHERE type=%s AND is_used=FALSE LIMIT %s)",
                      (ctype,qty))
                send(chat_id,"Removed.")
                del user_states[chat_id]
                return {"ok":True}

            if text=="💲 Change Prices":
                user_states[chat_id]={"step":"price_type"}
                send(chat_id,"Select Type: 500 / 1k / 2k / 4k")
                return {"ok":True}

            if chat_id in user_states and user_states[chat_id]["step"]=="price_type":
                user_states[chat_id]={"step":"price_new","type":text}
                send(chat_id,"Enter New Price:")
                return {"ok":True}

            if chat_id in user_states and user_states[chat_id]["step"]=="price_new":
                query("UPDATE coupon_prices SET price=%s WHERE type=%s",
                      (int(text),user_states[chat_id]["type"]))
                send(chat_id,"Price Updated.")
                del user_states[chat_id]
                return {"ok":True}

    # -------- CALLBACK -------- #
    if "callback_query" in data:
        call=data["callback_query"]
        admin=call["from"]["id"]
        data_text=call["data"]

        if admin not in ADMIN_IDS:
            return {"ok":True}

        if data_text.startswith("approve_"):
            parts=data_text.split("_")
            uid=int(parts[1])
            amt=int(parts[2])
            query("UPDATE users SET diamonds=diamonds+%s WHERE telegram_id=%s",
                  (amt,uid))
            send(uid,f"✅ Deposit Approved\n{amt} Diamonds added.")

        if data_text.startswith("reject_"):
            uid=int(data_text.split("_")[1])
            send(uid,"❌ Deposit Rejected.")

    return {"ok":True}
