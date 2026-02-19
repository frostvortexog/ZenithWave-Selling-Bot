<?php
// =========================================================
// ✅ SINGLE FILE Production Telegram Selling Bot (Webhook)
// ✅ ENV: BOT_TOKEN, ADMIN_IDS, DATABASE_URL (Render-friendly)
// ✅ Coins Add (UPI with Admin-updated QR + manual submit)
// ✅ Admin Accept/Decline -> auto credit diamonds
// ✅ Buy Coupons (500/1K/2K/4K) with stock + prices
// ✅ My Orders (coupon purchases + deposit history)
// ✅ Admin Panel: View stock, Add/Remove coupons, Change prices, Free coupon
// ✅ NEW: Update UPI QR (admin uploads image; stored file_id in DB)
// =========================================================

// ------------------- CONFIG (ENV) -------------------
$BOT_TOKEN = getenv("BOT_TOKEN");
$ADMIN_IDS_RAW = getenv("ADMIN_IDS") ?: "";
$DATABASE_URL = getenv("DATABASE_URL");

$ADMIN_IDS = array_values(array_filter(array_map(function($x){
  $x = trim($x);
  return ctype_digit($x) ? (int)$x : null;
}, explode(",", $ADMIN_IDS_RAW))));

if (!$BOT_TOKEN) { http_response_code(500); exit("BOT_TOKEN missing"); }
if (!$DATABASE_URL) { http_response_code(500); exit("DATABASE_URL missing"); }
if (count($ADMIN_IDS) === 0) { http_response_code(500); exit("ADMIN_IDS missing or invalid"); }

// ---------------- DB CONNECT ----------------
$db = parse_url($DATABASE_URL);
$host = $db["host"] ?? "";
$port = $db["port"] ?? "5432";
$user = $db["user"] ?? "";
$pass = $db["pass"] ?? "";
$dbname = ltrim($db["path"] ?? "", "/");
$dsn = "pgsql:host=$host;port=$port;dbname=$dbname;sslmode=require";

$pdo = new PDO($dsn, $user, $pass, [
  PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
  PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
]);

// ---------------- TELEGRAM API ----------------
function bot($method, $data = []) {
  global $BOT_TOKEN;
  $url = "https://api.telegram.org/bot{$BOT_TOKEN}/{$method}";
  $ch = curl_init($url);
  curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
  curl_setopt($ch, CURLOPT_POSTFIELDS, $data);
  $res = curl_exec($ch);
  curl_close($ch);
  return json_decode($res, true);
}

function is_admin($id) {
  global $ADMIN_IDS;
  return in_array((int)$id, array_map('intval', $ADMIN_IDS), true);
}

function now_time() {
  return date("d M Y, h:i A");
}

// ---------------- STATE HELPERS ----------------
function set_state($tid, $step, $dataArr = []) {
  global $pdo;
  $data = json_encode($dataArr, JSON_UNESCAPED_UNICODE);
  $q = $pdo->prepare("INSERT INTO user_state(telegram_id, step, data, updated_at)
                      VALUES(?,?,?,NOW())
                      ON CONFLICT (telegram_id) DO UPDATE SET step=EXCLUDED.step, data=EXCLUDED.data, updated_at=NOW()");
  $q->execute([$tid, $step, $data]);
}

function get_state($tid) {
  global $pdo;
  $q = $pdo->prepare("SELECT step, data FROM user_state WHERE telegram_id=?");
  $q->execute([$tid]);
  $row = $q->fetch();
  if (!$row) return ["step"=>null, "data"=>[]];
  $data = [];
  if (!empty($row["data"])) {
    $tmp = json_decode($row["data"], true);
    if (is_array($tmp)) $data = $tmp;
  }
  return ["step"=>$row["step"], "data"=>$data];
}

function clear_state($tid) {
  global $pdo;
  $q = $pdo->prepare("DELETE FROM user_state WHERE telegram_id=?");
  $q->execute([$tid]);
}

// ---------------- USER HELPERS ----------------
function ensure_user($tid, $username) {
  global $pdo;
  $q = $pdo->prepare("INSERT INTO users(telegram_id, username) VALUES(?,?)
                      ON CONFLICT (telegram_id) DO UPDATE SET username=EXCLUDED.username");
  $q->execute([$tid, $username]);
}

function get_balance($tid) {
  global $pdo;
  $q = $pdo->prepare("SELECT diamonds FROM users WHERE telegram_id=?");
  $q->execute([$tid]);
  $v = $q->fetchColumn();
  return (int)($v ?? 0);
}

function add_balance($tid, $amount) {
  global $pdo;
  $q = $pdo->prepare("UPDATE users SET diamonds = diamonds + ? WHERE telegram_id=?");
  $q->execute([(int)$amount, (int)$tid]);
}

// ---------------- SETTINGS ----------------
function get_setting($k) {
  global $pdo;
  $q = $pdo->prepare("SELECT v FROM bot_settings WHERE k=?");
  $q->execute([$k]);
  $v = $q->fetchColumn();
  return $v === false ? "" : (string)$v;
}
function set_setting($k, $v) {
  global $pdo;
  $q = $pdo->prepare("INSERT INTO bot_settings(k,v) VALUES(?,?)
                      ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v");
  $q->execute([$k, (string)$v]);
}

// ---------------- COUPONS HELPERS ----------------
function coupon_stock_count($type) {
  global $pdo;
  $q = $pdo->prepare("SELECT COUNT(*) FROM coupons_stock WHERE type=? AND is_used=FALSE");
  $q->execute([$type]);
  return (int)$q->fetchColumn();
}

function get_price($type) {
  global $pdo;
  $q = $pdo->prepare("SELECT price FROM coupon_prices WHERE type=?");
  $q->execute([$type]);
  $p = $q->fetchColumn();
  return (int)($p ?? 0);
}

function add_coupons($type, $codes) {
  global $pdo;
  $pdo->beginTransaction();
  try {
    $q = $pdo->prepare("INSERT INTO coupons_stock(type, code, is_used) VALUES(?,?,FALSE)");
    foreach ($codes as $c) {
      $c = trim($c);
      if ($c === "") continue;
      $q->execute([$type, $c]);
    }
    $pdo->commit();
    return true;
  } catch (Exception $e) {
    $pdo->rollBack();
    return false;
  }
}

function remove_coupons($type, $count) {
  global $pdo;
  $count = (int)$count;
  if ($count <= 0) return 0;

  $pdo->beginTransaction();
  try {
    $q = $pdo->prepare("SELECT id FROM coupons_stock WHERE type=? AND is_used=FALSE ORDER BY id ASC LIMIT $count FOR UPDATE");
    $q->execute([$type]);
    $ids = $q->fetchAll();
    $n = count($ids);
    if ($n > 0) {
      $del = $pdo->prepare("DELETE FROM coupons_stock WHERE id=?");
      foreach ($ids as $r) $del->execute([$r["id"]]);
    }
    $pdo->commit();
    return $n;
  } catch (Exception $e) {
    $pdo->rollBack();
    return 0;
  }
}

// ---------------- UI ----------------
function main_menu_kb() {
  return json_encode([
    "keyboard" => [
      [["💰 Add Diamonds"], ["💎 Balance"]],
      [["🛒 Buy Coupon"]],
      [["📦 My Orders"]],
    ],
    "resize_keyboard" => true
  ]);
}

function admin_menu_kb() {
  return json_encode([
    "keyboard" => [
      [["📊 View Stock"], ["🧾 Update UPI QR"]],
      [["➕ Add Coupon"], ["➖ Remove Coupon"]],
      [["🎁 Free Coupon"], ["💰 Change Price"]],
    ],
    "resize_keyboard" => true
  ]);
}

// ---------------- UPDATE IN ----------------
$update = json_decode(file_get_contents("php://input"), true);
if (!$update) exit;

$message  = $update["message"] ?? null;
$callback = $update["callback_query"] ?? null;

// ================== MESSAGE HANDLER ==================
if ($message) {
  $from_id = (int)($message["from"]["id"] ?? 0);
  $text    = trim($message["text"] ?? "");
  $username = $message["from"]["username"] ?? "NoUsername";
  $photo = $message["photo"] ?? null;

  ensure_user($from_id, $username);
  $st = get_state($from_id);
  $step = $st["step"];
  $data = $st["data"];

  if ($text === "/start") {
    bot("sendMessage", [
      "chat_id" => $from_id,
      "text"    => "Welcome ✅\n\nUse the menu below.",
      "reply_markup" => main_menu_kb()
    ]);
    clear_state($from_id);
    exit;
  }

  if ($text === "/admin" && is_admin($from_id)) {
    bot("sendMessage", [
      "chat_id" => $from_id,
      "text" => "👑 Admin Panel",
      "reply_markup" => admin_menu_kb()
    ]);
    clear_state($from_id);
    exit;
  }

  if ($text === "💎 Balance") {
    $bal = get_balance($from_id);
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"💎 Your Balance: {$bal} Diamonds"]);
    exit;
  }

  if ($text === "💰 Add Diamonds") {
    bot("sendMessage", [
      "chat_id" => $from_id,
      "text" => "💳 Select Payment Method:\n\n⚠️ Under Maintenance:\n\nPlease use other methods for deposit.",
      "reply_markup" => json_encode([
        "inline_keyboard" => [
          [["text"=>"🏦 UPI", "callback_data"=>"dep_upi_start"]],
        ]
      ])
    ]);
    clear_state($from_id);
    exit;
  }

  if ($text === "📦 My Orders") {
    global $pdo;
    $q1 = $pdo->prepare("SELECT coupon_type, quantity, total_price, created_at FROM orders WHERE telegram_id=? ORDER BY id DESC LIMIT 10");
    $q1->execute([$from_id]);
    $orders = $q1->fetchAll();

    $q2 = $pdo->prepare("SELECT method, diamonds_requested, status, created_at FROM deposits WHERE telegram_id=? ORDER BY id DESC LIMIT 10");
    $q2->execute([$from_id]);
    $deps = $q2->fetchAll();

    $msg = "📦 Your Activity (last 10)\n\n🛒 Coupon Orders:\n";
    if (!$orders) $msg .= "• No coupon orders yet.\n";
    else foreach ($orders as $o) $msg .= "• {$o['coupon_type']} x{$o['quantity']} — {$o['total_price']}💎 ({$o['created_at']})\n";

    $msg .= "\n💰 Deposits:\n";
    if (!$deps) $msg .= "• No deposits yet.\n";
    else foreach ($deps as $d) $msg .= "• {$d['method']} — {$d['diamonds_requested']}💎 — {$d['status']} ({$d['created_at']})\n";

    bot("sendMessage", ["chat_id"=>$from_id, "text"=>$msg]);
    exit;
  }

  if ($text === "🛒 Buy Coupon") {
    $types = ["500","1000","2000","4000"];
    $buttons = [];
    foreach ($types as $t) {
      $price = get_price($t);
      $stock = coupon_stock_count($t);
      $label = ($t==="1000"?"1K":($t==="2000"?"2K":($t==="4000"?"4K":"500")));
      $buttons[] = [["text"=>"$label ({$price} 💎) | Stock: {$stock}", "callback_data"=>"buy_type_$t"]];
    }
    bot("sendMessage", [
      "chat_id" => $from_id,
      "text" => "Select a coupon type:",
      "reply_markup" => json_encode(["inline_keyboard"=>$buttons])
    ]);
    clear_state($from_id);
    exit;
  }

  // -------- ADMIN: view stock --------
  if (is_admin($from_id) && $text === "📊 View Stock") {
    $types = ["500","1000","2000","4000"];
    $msg = "📊 Stock & Prices\n\n";
    foreach ($types as $t) {
      $msg .= "• $t: Stock=".coupon_stock_count($t).", Price=".get_price($t)."💎\n";
    }
    $qr = get_setting("upi_qr_file_id");
    $msg .= "\n🧾 UPI QR: " . ($qr ? "✅ Set" : "❌ Not set");
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>$msg]);
    exit;
  }

  // -------- ADMIN: update QR --------
  if (is_admin($from_id) && $text === "🧾 Update UPI QR") {
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"🧾 Send the NEW UPI QR image now (as a photo)."]);
    set_state($from_id, "admin_wait_qr_photo", []);
    exit;
  }

  if (is_admin($from_id) && $step === "admin_wait_qr_photo") {
    if (!$photo) { bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Please send the QR as a photo."]); exit; }
    $largest = end($photo);
    $file_id = $largest["file_id"] ?? "";
    if (!$file_id) { bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Could not read file_id. Try again."]); exit; }
    set_setting("upi_qr_file_id", $file_id);
    clear_state($from_id);
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"✅ UPI QR updated successfully."]);
    exit;
  }

  // -------- USER: UPI diamonds input --------
  if ($step === "upi_wait_diamonds" && $text !== "") {
    if (!ctype_digit($text)) { bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Send diamonds as a number (minimum 30)."]); exit; }
    $diamonds = (int)$text;
    if ($diamonds < 30) { bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Minimum is 30 diamonds. Send again:"]); exit; }

    $qr = get_setting("upi_qr_file_id");
    if (!$qr) { bot("sendMessage", ["chat_id"=>$from_id, "text"=>"⚠️ UPI QR is not set yet. Please try later."]); clear_state($from_id); exit; }

    $amount = $diamonds;
    $q = $pdo->prepare("INSERT INTO deposits(telegram_id, method, diamonds_requested, payment_amount, status)
                        VALUES(?,?,?,?, 'pending') RETURNING id");
    $q->execute([$from_id, "upi", $diamonds, $amount]);
    $dep_id = (int)$q->fetchColumn();

    $summary =
"📝 Order Summary:
━━━━━━━━━━━━━━━
💹 Rate: 1 Rs = 1 Diamond 💎
💵 Amount: {$amount} Rs
💎 Diamonds to Receive: {$diamonds} 💎
💳 Method: UPI
📅 Time: ".now_time()."
━━━━━━━━━━━━━━━
Scan QR and pay, then click below.";

    bot("sendPhoto", [
      "chat_id"=>$from_id,
      "photo"=>$qr,
      "caption"=>$summary,
      "reply_markup"=>json_encode([
        "inline_keyboard"=>[
          [["text"=>"✅ I have done the payment", "callback_data"=>"upi_done_$dep_id"]],
        ]
      ])
    ]);

    clear_state($from_id);
    exit;
  }

  // -------- USER: payer name --------
  if ($step === "upi_wait_payer_name" && $text !== "") {
    $payer = trim($text);
    if (mb_strlen($payer) < 2) { bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Enter a valid payer name:"]); exit; }
    $dep_id = (int)($data["dep_id"] ?? 0);
    if ($dep_id <= 0) { clear_state($from_id); exit; }

    $q = $pdo->prepare("UPDATE deposits SET payer_name=? WHERE id=? AND telegram_id=?");
    $q->execute([$payer, $dep_id, $from_id]);

    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"📸 Now send the payment screenshot (photo)."]);
    set_state($from_id, "upi_wait_screenshot", ["dep_id"=>$dep_id]);
    exit;
  }

  // -------- USER: screenshot --------
  if ($step === "upi_wait_screenshot") {
    if (!$photo) { bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Please send the screenshot as a photo."]); exit; }
    $dep_id = (int)($data["dep_id"] ?? 0);
    if ($dep_id <= 0) { clear_state($from_id); exit; }

    $largest = end($photo);
    $file_id = $largest["file_id"] ?? "";
    if (!$file_id) { bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Could not read screenshot. Try again."]); exit; }

    $q = $pdo->prepare("UPDATE deposits SET screenshot_file_id=?, status='submitted' WHERE id=? AND telegram_id=?");
    $q->execute([$file_id, $dep_id, $from_id]);

    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"⏳ Payment submitted.\nAdmin will review and approve soon."]);

    $dep = $pdo->prepare("SELECT * FROM deposits WHERE id=?");
    $dep->execute([$dep_id]);
    $row = $dep->fetch();

    if ($row) {
      $u = $pdo->prepare("SELECT username FROM users WHERE telegram_id=?");
      $u->execute([$from_id]);
      $uname = $u->fetchColumn() ?: "NoUsername";

      $admin_text =
"💰 NEW UPI PAYMENT SUBMISSION

👤 User: @$uname
🆔 ID: {$from_id}
💵 Amount: {$row['payment_amount']} Rs
💎 Diamonds: {$row['diamonds_requested']}
👤 Payer Name: {$row['payer_name']}
📅 Time: {$row['created_at']}
🧾 Deposit ID: {$dep_id}";

      foreach ($ADMIN_IDS as $aid) {
        bot("sendPhoto", [
          "chat_id" => $aid,
          "photo" => $file_id,
          "caption" => $admin_text,
          "reply_markup" => json_encode([
            "inline_keyboard" => [
              [["text"=>"✅ Accept", "callback_data"=>"admin_dep_accept_$dep_id"]],
              [["text"=>"❌ Decline", "callback_data"=>"admin_dep_decline_$dep_id"]],
            ]
          ])
        ]);
      }
    }

    clear_state($from_id);
    exit;
  }
}

// ================== CALLBACK HANDLER ==================
if ($callback) {
  $from_id = (int)($callback["from"]["id"] ?? 0);
  $data = $callback["data"] ?? "";

  if ($data === "dep_upi_start") {
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"Enter diamonds amount you want to add (Minimum 30):"]);
    set_state($from_id, "upi_wait_diamonds", []);
    exit;
  }

  if (strpos($data, "upi_done_") === 0) {
    $dep_id = (int)str_replace("upi_done_", "", $data);
    $q = $pdo->prepare("SELECT id FROM deposits WHERE id=? AND telegram_id=? AND method='upi'");
    $q->execute([$dep_id, $from_id]);
    if (!$q->fetch()) {
      bot("answerCallbackQuery", ["callback_query_id"=>$callback["id"], "text"=>"Invalid request."]);
      exit;
    }
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"What is the payer name? (Name used in UPI payment)"]);
    set_state($from_id, "upi_wait_payer_name", ["dep_id"=>$dep_id]);
    exit;
  }

  if (is_admin($from_id) && strpos($data, "admin_dep_accept_") === 0) {
    $dep_id = (int)str_replace("admin_dep_accept_", "", $data);

    $pdo->beginTransaction();
    try {
      $q = $pdo->prepare("SELECT * FROM deposits WHERE id=? FOR UPDATE");
      $q->execute([$dep_id]);
      $dep = $q->fetch();
      if (!$dep) { $pdo->rollBack(); exit; }
      if ($dep["status"] === "approved") { $pdo->rollBack(); exit; }

      $u = (int)$dep["telegram_id"];
      $amt = (int)$dep["diamonds_requested"];

      $q2 = $pdo->prepare("UPDATE deposits SET status='approved' WHERE id=?");
      $q2->execute([$dep_id]);

      add_balance($u, $amt);
      $pdo->commit();

      bot("sendMessage", ["chat_id"=>$u, "text"=>"✅ Payment Approved!\n{$amt} Diamonds added to your balance."]);
      bot("answerCallbackQuery", ["callback_query_id"=>$callback["id"], "text"=>"Approved ✅"]);
    } catch (Exception $e) {
      $pdo->rollBack();
      bot("answerCallbackQuery", ["callback_query_id"=>$callback["id"], "text"=>"Error"]);
    }
    exit;
  }

  if (is_admin($from_id) && strpos($data, "admin_dep_decline_") === 0) {
    $dep_id = (int)str_replace("admin_dep_decline_", "", $data);
    $q = $pdo->prepare("SELECT telegram_id, status FROM deposits WHERE id=?");
    $q->execute([$dep_id]);
    $dep = $q->fetch();
    if (!$dep) exit;

    if ($dep["status"] !== "approved") {
      $q2 = $pdo->prepare("UPDATE deposits SET status='declined' WHERE id=?");
      $q2->execute([$dep_id]);
    }

    bot("sendMessage", ["chat_id"=>(int)$dep["telegram_id"], "text"=>"❌ Payment Declined."]);
    bot("answerCallbackQuery", ["callback_query_id"=>$callback["id"], "text"=>"Declined ❌"]);
    exit;
  }
}

http_response_code(200);
echo "OK";
