<?php
// =========================================================
// ✅ FAST PRODUCTION SINGLE FILE Telegram Selling Bot (PHP)
// ✅ Render ENV: BOT_TOKEN, ADMIN_IDS, DATABASE_URL
// ✅ Fixes: Health GET, fast 200 response, safe handlers
// =========================================================

// ---------- FAST RESPONSE FOR HEALTH CHECK ----------
if (($_SERVER['REQUEST_METHOD'] ?? '') === 'GET') {
  http_response_code(200);
  echo "OK";
  exit;
}

// ---------- GLOBAL ERROR HANDLING (prevents silent death) ----------
ini_set('display_errors', '0');
error_reporting(E_ALL);
set_exception_handler(function($e){
  error_log("EXCEPTION: ".$e->getMessage()." | ".$e->getFile().":".$e->getLine());
  http_response_code(200); // Telegram expects 200, otherwise it retries
  echo "OK";
  exit;
});
set_error_handler(function($severity, $message, $file, $line){
  error_log("PHP_ERROR: $message | $file:$line");
  return true; // prevent default handler
});

// ---------- ENV CONFIG ----------
$BOT_TOKEN = getenv("BOT_TOKEN");
$ADMIN_IDS_RAW = getenv("ADMIN_IDS") ?: "";
$DATABASE_URL = getenv("DATABASE_URL");

// Optional SSL override (rare cases)
$PGSSLMODE = getenv("PGSSLMODE"); // e.g. "require" or "disable"

$ADMIN_IDS = array_values(array_filter(array_map(function($x){
  $x = trim($x);
  return ctype_digit($x) ? (int)$x : null;
}, explode(",", $ADMIN_IDS_RAW))));

if (!$BOT_TOKEN || !$DATABASE_URL || count($ADMIN_IDS) === 0) {
  error_log("Missing ENV: BOT_TOKEN or DATABASE_URL or ADMIN_IDS");
  http_response_code(200);
  echo "OK";
  exit;
}

// ---------- TELEGRAM API ----------
function bot($method, $data = []) {
  global $BOT_TOKEN;
  $url = "https://api.telegram.org/bot{$BOT_TOKEN}/{$method}";
  $ch = curl_init($url);
  curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
  curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, 3);
  curl_setopt($ch, CURLOPT_TIMEOUT, 8);
  curl_setopt($ch, CURLOPT_POSTFIELDS, $data);
  $res = curl_exec($ch);
  if ($res === false) {
    error_log("CURL_ERROR: ".curl_error($ch));
  }
  curl_close($ch);
  $j = json_decode($res ?: "{}", true);
  return is_array($j) ? $j : [];
}
function is_admin($id) {
  global $ADMIN_IDS;
  return in_array((int)$id, $ADMIN_IDS, true);
}
function now_time() {
  return date("d M Y, h:i A");
}

// ---------- DB CONNECT (lazy connect for speed) ----------
$pdo = null;
function db() {
  global $pdo, $DATABASE_URL, $PGSSLMODE;

  if ($pdo) return $pdo;

  $db = parse_url($DATABASE_URL);
  $host = $db["host"] ?? "";
  $port = $db["port"] ?? "5432";
  $user = $db["user"] ?? "";
  $pass = $db["pass"] ?? "";
  $dbname = ltrim($db["path"] ?? "", "/");

  // SSL mode: Supabase usually requires 'require'
  $ssl = $PGSSLMODE ?: "require";

  $dsn = "pgsql:host=$host;port=$port;dbname=$dbname;sslmode=$ssl";
  $pdo = new PDO($dsn, $user, $pass, [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
  ]);
  return $pdo;
}

// ---------- STATE ----------
function set_state($tid, $step, $dataArr = []) {
  $pdo = db();
  static $stmt = null;
  if (!$stmt) {
    $stmt = $pdo->prepare("
      INSERT INTO user_state(telegram_id, step, data, updated_at)
      VALUES(?,?,?,NOW())
      ON CONFLICT (telegram_id)
      DO UPDATE SET step=EXCLUDED.step, data=EXCLUDED.data, updated_at=NOW()
    ");
  }
  $stmt->execute([$tid, $step, json_encode($dataArr, JSON_UNESCAPED_UNICODE)]);
}
function get_state($tid) {
  $pdo = db();
  static $stmt = null;
  if (!$stmt) $stmt = $pdo->prepare("SELECT step, data FROM user_state WHERE telegram_id=?");
  $stmt->execute([$tid]);
  $row = $stmt->fetch();
  if (!$row) return ["step"=>null, "data"=>[]];
  $data = [];
  if (!empty($row["data"])) {
    $tmp = json_decode($row["data"], true);
    if (is_array($tmp)) $data = $tmp;
  }
  return ["step"=>$row["step"], "data"=>$data];
}
function clear_state($tid) {
  $pdo = db();
  static $stmt = null;
  if (!$stmt) $stmt = $pdo->prepare("DELETE FROM user_state WHERE telegram_id=?");
  $stmt->execute([$tid]);
}

// ---------- USERS ----------
function ensure_user($tid, $username) {
  $pdo = db();
  static $stmt = null;
  if (!$stmt) {
    $stmt = $pdo->prepare("
      INSERT INTO users(telegram_id, username)
      VALUES(?,?)
      ON CONFLICT (telegram_id) DO UPDATE SET username=EXCLUDED.username
    ");
  }
  $stmt->execute([$tid, $username]);
}
function get_balance($tid) {
  $pdo = db();
  static $stmt = null;
  if (!$stmt) $stmt = $pdo->prepare("SELECT diamonds FROM users WHERE telegram_id=?");
  $stmt->execute([$tid]);
  return (int)($stmt->fetchColumn() ?? 0);
}
function add_balance($tid, $amount) {
  $pdo = db();
  static $stmt = null;
  if (!$stmt) $stmt = $pdo->prepare("UPDATE users SET diamonds=diamonds+? WHERE telegram_id=?");
  $stmt->execute([(int)$amount, (int)$tid]);
}

// ---------- SETTINGS ----------
function get_setting($k) {
  $pdo = db();
  static $stmt = null;
  if (!$stmt) $stmt = $pdo->prepare("SELECT v FROM bot_settings WHERE k=?");
  $stmt->execute([$k]);
  $v = $stmt->fetchColumn();
  return $v === false ? "" : (string)$v;
}
function set_setting($k, $v) {
  $pdo = db();
  static $stmt = null;
  if (!$stmt) {
    $stmt = $pdo->prepare("
      INSERT INTO bot_settings(k,v) VALUES(?,?)
      ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v
    ");
  }
  $stmt->execute([$k, (string)$v]);
}

// ---------- COUPONS ----------
function coupon_stock_count($type) {
  $pdo = db();
  static $stmt = null;
  if (!$stmt) $stmt = $pdo->prepare("SELECT COUNT(*) FROM coupons_stock WHERE type=? AND is_used=FALSE");
  $stmt->execute([$type]);
  return (int)$stmt->fetchColumn();
}
function get_price($type) {
  $pdo = db();
  static $stmt = null;
  if (!$stmt) $stmt = $pdo->prepare("SELECT price FROM coupon_prices WHERE type=?");
  $stmt->execute([$type]);
  return (int)($stmt->fetchColumn() ?? 0);
}
function add_coupons($type, $codes) {
  $pdo = db();
  $pdo->beginTransaction();
  try {
    $stmt = $pdo->prepare("INSERT INTO coupons_stock(type, code, is_used) VALUES(?,?,FALSE)");
    foreach ($codes as $c) {
      $c = trim($c);
      if ($c !== "") $stmt->execute([$type, $c]);
    }
    $pdo->commit();
    return true;
  } catch (Exception $e) {
    $pdo->rollBack();
    error_log("add_coupons error: ".$e->getMessage());
    return false;
  }
}
function remove_coupons($type, $count) {
  $pdo = db();
  $count = (int)$count;
  if ($count <= 0) return 0;
  $pdo->beginTransaction();
  try {
    $q = $pdo->prepare("SELECT id FROM coupons_stock WHERE type=? AND is_used=FALSE ORDER BY id ASC LIMIT $count FOR UPDATE");
    $q->execute([$type]);
    $rows = $q->fetchAll();
    $n = count($rows);
    if ($n > 0) {
      $del = $pdo->prepare("DELETE FROM coupons_stock WHERE id=?");
      foreach ($rows as $r) $del->execute([$r["id"]]);
    }
    $pdo->commit();
    return $n;
  } catch (Exception $e) {
    $pdo->rollBack();
    error_log("remove_coupons error: ".$e->getMessage());
    return 0;
  }
}

// ---------- UI ----------
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

// ---------- READ UPDATE ----------
$raw = file_get_contents("php://input");
$update = json_decode($raw ?: "{}", true);
if (!is_array($update) || !$update) {
  http_response_code(200);
  echo "OK";
  exit;
}

// Support different Telegram update types
$message  = $update["message"] ?? ($update["edited_message"] ?? null);
$callback = $update["callback_query"] ?? null;

// =========================================================
// MESSAGE FLOW
// =========================================================
if ($message) {
  $from_id = (int)($message["from"]["id"] ?? 0);
  $text    = trim($message["text"] ?? "");
  $username = $message["from"]["username"] ?? "NoUsername";
  $photo = $message["photo"] ?? null;

  // Always ensure user exists (fast, cached stmt)
  try { ensure_user($from_id, $username); } catch(Exception $e){ error_log("ensure_user: ".$e->getMessage()); }

  $st = ["step"=>null,"data"=>[]];
  try { $st = get_state($from_id); } catch(Exception $e){ error_log("get_state: ".$e->getMessage()); }
  $step = $st["step"];
  $data = $st["data"];

  // /start
  if ($text === "/start") {
    bot("sendMessage", [
      "chat_id" => $from_id,
      "text" => "Welcome ✅\n\nUse the menu below.",
      "reply_markup" => main_menu_kb()
    ]);
    try { clear_state($from_id); } catch(Exception $e){}
    http_response_code(200); echo "OK"; exit;
  }

  // /admin
  if ($text === "/admin" && is_admin($from_id)) {
    bot("sendMessage", [
      "chat_id" => $from_id,
      "text" => "👑 Admin Panel",
      "reply_markup" => admin_menu_kb()
    ]);
    try { clear_state($from_id); } catch(Exception $e){}
    http_response_code(200); echo "OK"; exit;
  }

  // Balance
  if ($text === "💎 Balance") {
    $bal = 0; try { $bal = get_balance($from_id); } catch(Exception $e){ error_log("balance err: ".$e->getMessage()); }
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"💎 Your Balance: {$bal} Diamonds"]);
    http_response_code(200); echo "OK"; exit;
  }

  // Add Diamonds menu
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
    try { clear_state($from_id); } catch(Exception $e){}
    http_response_code(200); echo "OK"; exit;
  }

  // Buy Coupon menu
  if ($text === "🛒 Buy Coupon") {
    $types = ["500","1000","2000","4000"];
    $buttons = [];
    foreach ($types as $t) {
      $price = 0; $stock = 0;
      try { $price = get_price($t); $stock = coupon_stock_count($t); } catch(Exception $e){ error_log("buy menu: ".$e->getMessage()); }
      $label = ($t==="1000"?"1K":($t==="2000"?"2K":($t==="4000"?"4K":"500")));
      $buttons[] = [["text"=>"$label ({$price} 💎) | Stock: {$stock}", "callback_data"=>"buy_type_$t"]];
    }
    bot("sendMessage", [
      "chat_id" => $from_id,
      "text" => "Select a coupon type:",
      "reply_markup" => json_encode(["inline_keyboard"=>$buttons])
    ]);
    try { clear_state($from_id); } catch(Exception $e){}
    http_response_code(200); echo "OK"; exit;
  }

  // My Orders
  if ($text === "📦 My Orders") {
    $msg = "📦 Your Activity (last 10)\n\n";

    try {
      $pdo = db();
      $q1 = $pdo->prepare("SELECT coupon_type, quantity, total_price, created_at FROM orders WHERE telegram_id=? ORDER BY id DESC LIMIT 10");
      $q1->execute([$from_id]);
      $orders = $q1->fetchAll();

      $msg .= "🛒 Coupon Orders:\n";
      if (!$orders) $msg .= "• No coupon orders yet.\n";
      else foreach ($orders as $o) $msg .= "• {$o['coupon_type']} x{$o['quantity']} — {$o['total_price']}💎 ({$o['created_at']})\n";

      $q2 = $pdo->prepare("SELECT method, diamonds_requested, status, created_at FROM deposits WHERE telegram_id=? ORDER BY id DESC LIMIT 10");
      $q2->execute([$from_id]);
      $deps = $q2->fetchAll();

      $msg .= "\n💰 Deposits:\n";
      if (!$deps) $msg .= "• No deposits yet.\n";
      else foreach ($deps as $d) $msg .= "• {$d['method']} — {$d['diamonds_requested']}💎 — {$d['status']} ({$d['created_at']})\n";
    } catch(Exception $e) {
      error_log("MyOrders DB err: ".$e->getMessage());
      $msg .= "⚠️ Database not ready or tables missing.\n";
    }

    bot("sendMessage", ["chat_id"=>$from_id, "text"=>$msg]);
    http_response_code(200); echo "OK"; exit;
  }

  // ---------------- ADMIN BUTTONS (message) ----------------
  if (is_admin($from_id) && $text === "📊 View Stock") {
    $types = ["500","1000","2000","4000"];
    $msg = "📊 Stock & Prices\n\n";
    foreach ($types as $t) {
      $msg .= "• $t: Stock=".coupon_stock_count($t).", Price=".get_price($t)."💎\n";
    }
    $qr = get_setting("upi_qr_file_id");
    $msg .= "\n🧾 UPI QR: " . ($qr ? "✅ Set" : "❌ Not set");
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>$msg]);
    http_response_code(200); echo "OK"; exit;
  }

  if (is_admin($from_id) && $text === "🧾 Update UPI QR") {
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"🧾 Send the NEW UPI QR image now (as a photo)."]);
    set_state($from_id, "admin_wait_qr_photo", []);
    http_response_code(200); echo "OK"; exit;
  }

  if (is_admin($from_id) && $step === "admin_wait_qr_photo") {
    if (!$photo) {
      bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Please send the QR as a photo."]);
      http_response_code(200); echo "OK"; exit;
    }
    $largest = end($photo);
    $file_id = $largest["file_id"] ?? "";
    if (!$file_id) {
      bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Could not read file_id. Try again."]);
      http_response_code(200); echo "OK"; exit;
    }
    set_setting("upi_qr_file_id", $file_id);
    clear_state($from_id);
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"✅ UPI QR updated successfully."]);
    http_response_code(200); echo "OK"; exit;
  }

  // Admin Add/Remove/Price/Free (menus via callbacks)
  if (is_admin($from_id) && $text === "➕ Add Coupon") {
    bot("sendMessage", [
      "chat_id"=>$from_id,
      "text"=>"Choose coupon type to ADD:",
      "reply_markup"=>json_encode([
        "inline_keyboard"=>[
          [["text"=>"500","callback_data"=>"admin_add_type_500"],["text"=>"1K","callback_data"=>"admin_add_type_1000"]],
          [["text"=>"2K","callback_data"=>"admin_add_type_2000"],["text"=>"4K","callback_data"=>"admin_add_type_4000"]],
        ]
      ])
    ]);
    clear_state($from_id);
    http_response_code(200); echo "OK"; exit;
  }

  if (is_admin($from_id) && $text === "➖ Remove Coupon") {
    bot("sendMessage", [
      "chat_id"=>$from_id,
      "text"=>"Choose coupon type to REMOVE:",
      "reply_markup"=>json_encode([
        "inline_keyboard"=>[
          [["text"=>"500","callback_data"=>"admin_rm_type_500"],["text"=>"1K","callback_data"=>"admin_rm_type_1000"]],
          [["text"=>"2K","callback_data"=>"admin_rm_type_2000"],["text"=>"4K","callback_data"=>"admin_rm_type_4000"]],
        ]
      ])
    ]);
    clear_state($from_id);
    http_response_code(200); echo "OK"; exit;
  }

  if (is_admin($from_id) && $text === "💰 Change Price") {
    bot("sendMessage", [
      "chat_id"=>$from_id,
      "text"=>"Choose coupon type to CHANGE price:",
      "reply_markup"=>json_encode([
        "inline_keyboard"=>[
          [["text"=>"500","callback_data"=>"admin_price_type_500"],["text"=>"1K","callback_data"=>"admin_price_type_1000"]],
          [["text"=>"2K","callback_data"=>"admin_price_type_2000"],["text"=>"4K","callback_data"=>"admin_price_type_4000"]],
        ]
      ])
    ]);
    clear_state($from_id);
    http_response_code(200); echo "OK"; exit;
  }

  if (is_admin($from_id) && $text === "🎁 Free Coupon") {
    bot("sendMessage", [
      "chat_id"=>$from_id,
      "text"=>"Choose coupon type to GET for free:",
      "reply_markup"=>json_encode([
        "inline_keyboard"=>[
          [["text"=>"500","callback_data"=>"admin_free_type_500"],["text"=>"1K","callback_data"=>"admin_free_type_1000"]],
          [["text"=>"2K","callback_data"=>"admin_free_type_2000"],["text"=>"4K","callback_data"=>"admin_free_type_4000"]],
        ]
      ])
    ]);
    clear_state($from_id);
    http_response_code(200); echo "OK"; exit;
  }

  // ---------------- USER UPI STEPS ----------------
  if ($step === "upi_wait_diamonds" && $text !== "") {
    if (!ctype_digit($text)) {
      bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Send diamonds as a number (minimum 30)."]);
      http_response_code(200); echo "OK"; exit;
    }
    $diamonds = (int)$text;
    if ($diamonds < 30) {
      bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Minimum is 30 diamonds. Send again:"]);
      http_response_code(200); echo "OK"; exit;
    }

    $qr = get_setting("upi_qr_file_id");
    if (!$qr) {
      bot("sendMessage", ["chat_id"=>$from_id, "text"=>"⚠️ UPI QR is not set yet. Please try later."]);
      clear_state($from_id);
      http_response_code(200); echo "OK"; exit;
    }

    $amount = $diamonds;

    try {
      $pdo = db();
      $q = $pdo->prepare("INSERT INTO deposits(telegram_id, method, diamonds_requested, payment_amount, status)
                          VALUES(?,?,?,?, 'pending') RETURNING id");
      $q->execute([$from_id, "upi", $diamonds, $amount]);
      $dep_id = (int)$q->fetchColumn();
    } catch(Exception $e) {
      error_log("deposit insert err: ".$e->getMessage());
      bot("sendMessage", ["chat_id"=>$from_id, "text"=>"⚠️ Server DB error. Try later."]);
      clear_state($from_id);
      http_response_code(200); echo "OK"; exit;
    }

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
    http_response_code(200); echo "OK"; exit;
  }

  if ($step === "upi_wait_payer_name" && $text !== "") {
    $payer = trim($text);
    if (mb_strlen($payer) < 2) {
      bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Enter a valid payer name:"]);
      http_response_code(200); echo "OK"; exit;
    }
    $dep_id = (int)($data["dep_id"] ?? 0);
    if ($dep_id <= 0) { clear_state($from_id); http_response_code(200); echo "OK"; exit; }

    $pdo = db();
    $q = $pdo->prepare("UPDATE deposits SET payer_name=? WHERE id=? AND telegram_id=?");
    $q->execute([$payer, $dep_id, $from_id]);

    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"📸 Now send the payment screenshot (photo)."]);
    set_state($from_id, "upi_wait_screenshot", ["dep_id"=>$dep_id]);
    http_response_code(200); echo "OK"; exit;
  }

  if ($step === "upi_wait_screenshot") {
    if (!$photo) {
      bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Please send the screenshot as a photo."]);
      http_response_code(200); echo "OK"; exit;
    }
    $dep_id = (int)($data["dep_id"] ?? 0);
    if ($dep_id <= 0) { clear_state($from_id); http_response_code(200); echo "OK"; exit; }

    $largest = end($photo);
    $file_id = $largest["file_id"] ?? "";
    if (!$file_id) {
      bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Could not read screenshot. Try again."]);
      http_response_code(200); echo "OK"; exit;
    }

    $pdo = db();
    $q = $pdo->prepare("UPDATE deposits SET screenshot_file_id=?, status='submitted' WHERE id=? AND telegram_id=?");
    $q->execute([$file_id, $dep_id, $from_id]);

    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"⏳ Payment submitted.\nAdmin will review and approve soon."]);

    // notify admins
    $dep = $pdo->prepare("SELECT * FROM deposits WHERE id=?");
    $dep->execute([$dep_id]);
    $row = $dep->fetch();

    if ($row) {
      $uname = $username ?: "NoUsername";
      $admin_text =
"💰 NEW UPI PAYMENT SUBMISSION

👤 User: @$uname
🆔 ID: {$from_id}
💵 Amount: {$row['payment_amount']} Rs
💎 Diamonds: {$row['diamonds_requested']}
👤 Payer Name: {$row['payer_name']}
📅 Time: {$row['created_at']}
🧾 Deposit ID: {$dep_id}";

      global $ADMIN_IDS;
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
    http_response_code(200); echo "OK"; exit;
  }

  // ---------------- BUY QTY STEP ----------------
  if ($step === "buy_wait_qty" && $text !== "") {
    if (!ctype_digit($text) || (int)$text <= 0) {
      bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Send a valid quantity number."]);
      http_response_code(200); echo "OK"; exit;
    }
    $qty = (int)$text;
    $type = $data["type"] ?? "";

    if (!in_array($type, ["500","1000","2000","4000"], true)) {
      clear_state($from_id);
      http_response_code(200); echo "OK"; exit;
    }

    $pdo = db();
    $price = get_price($type);
    $total = $price * $qty;

    // fast checks
    $stock = coupon_stock_count($type);
    if ($stock < $qty) {
      bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Not enough stock! Available: {$stock}"]);
      clear_state($from_id);
      http_response_code(200); echo "OK"; exit;
    }

    $pdo->beginTransaction();
    try {
      // lock user
      $bq = $pdo->prepare("SELECT diamonds FROM users WHERE telegram_id=? FOR UPDATE");
      $bq->execute([$from_id]);
      $bal = (int)($bq->fetchColumn() ?? 0);

      if ($bal < $total) {
        $pdo->rollBack();
        bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Not enough diamonds!\nNeeded: {$total} | You have: {$bal}"]);
        clear_state($from_id);
        http_response_code(200); echo "OK"; exit;
      }

      // lock coupons
      $cq = $pdo->prepare("SELECT id, code FROM coupons_stock WHERE type=? AND is_used=FALSE ORDER BY id ASC LIMIT $qty FOR UPDATE");
      $cq->execute([$type]);
      $rows = $cq->fetchAll();
      if (count($rows) < $qty) {
        $pdo->rollBack();
        bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Stock changed. Try again."]);
        clear_state($from_id);
        http_response_code(200); echo "OK"; exit;
      }

      // deduct
      $du = $pdo->prepare("UPDATE users SET diamonds=diamonds-? WHERE telegram_id=?");
      $du->execute([$total, $from_id]);

      // mark used
      $ids = array_map(fn($r)=>$r["id"], $rows);
      $codes = array_map(fn($r)=>$r["code"], $rows);
      $in = implode(",", array_fill(0, count($ids), "?"));
      $uu = $pdo->prepare("UPDATE coupons_stock SET is_used=TRUE WHERE id IN ($in)");
      $uu->execute($ids);

      $codes_text = implode("\n", $codes);
      $io = $pdo->prepare("INSERT INTO orders(telegram_id,coupon_type,quantity,total_price,codes) VALUES(?,?,?,?,?)");
      $io->execute([$from_id, $type, $qty, $total, $codes_text]);

      $pdo->commit();

      bot("sendMessage", ["chat_id"=>$from_id, "text"=>"✅ Purchase Successful!\n\nHere are your coupons:\n\n{$codes_text}"]);
      clear_state($from_id);
      http_response_code(200); echo "OK"; exit;

    } catch(Exception $e) {
      $pdo->rollBack();
      error_log("buy error: ".$e->getMessage());
      bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Error. Try again."]);
      clear_state($from_id);
      http_response_code(200); echo "OK"; exit;
    }
  }

  // ---------------- ADMIN ADD/REMOVE/PRICE STEPS ----------------
  if (is_admin($from_id) && $step === "admin_wait_add_codes" && $text !== "") {
    $type = $data["type"] ?? "";
    if (!in_array($type, ["500","1000","2000","4000"], true)) { clear_state($from_id); http_response_code(200); echo "OK"; exit; }
    $lines = preg_split("/\r\n|\n|\r/", $text);
    $lines = array_values(array_filter(array_map("trim", $lines), fn($x)=>$x!==""));
    if (count($lines) === 0) {
      bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Send codes line-by-line."]);
      http_response_code(200); echo "OK"; exit;
    }
    $ok = add_coupons($type, $lines);
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>$ok ? "✅ Added ".count($lines)." coupons to {$type}." : "❌ Failed to add coupons."]);
    clear_state($from_id);
    http_response_code(200); echo "OK"; exit;
  }

  if (is_admin($from_id) && $step === "admin_wait_rm_count" && $text !== "") {
    if (!ctype_digit($text) || (int)$text <= 0) {
      bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Send a valid number to remove."]);
      http_response_code(200); echo "OK"; exit;
    }
    $type = $data["type"] ?? "";
    $removed = remove_coupons($type, (int)$text);
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"✅ Removed {$removed} unused coupons from {$type}."]);
    clear_state($from_id);
    http_response_code(200); echo "OK"; exit;
  }

  if (is_admin($from_id) && $step === "admin_wait_new_price" && $text !== "") {
    if (!ctype_digit($text) || (int)$text <= 0) {
      bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Send a valid price number."]);
      http_response_code(200); echo "OK"; exit;
    }
    $type = $data["type"] ?? "";
    $newp = (int)$text;
    $pdo = db();
    $q = $pdo->prepare("INSERT INTO coupon_prices(type,price) VALUES(?,?) ON CONFLICT (type) DO UPDATE SET price=EXCLUDED.price");
    $q->execute([$type, $newp]);
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"✅ Price updated: {$type} => {$newp}💎"]);
    clear_state($from_id);
    http_response_code(200); echo "OK"; exit;
  }
}

// =========================================================
// CALLBACK FLOW
// =========================================================
if ($callback) {
  $from_id = (int)($callback["from"]["id"] ?? 0);
  $cbid = $callback["id"] ?? "";
  $data = $callback["data"] ?? "";

  // Start UPI
  if ($data === "dep_upi_start") {
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"Enter diamonds amount you want to add (Minimum 30):"]);
    set_state($from_id, "upi_wait_diamonds", []);
    bot("answerCallbackQuery", ["callback_query_id"=>$cbid, "text"=>""]);
    http_response_code(200); echo "OK"; exit;
  }

  // user clicked done payment
  if (strpos($data, "upi_done_") === 0) {
    $dep_id = (int)str_replace("upi_done_", "", $data);
    $pdo = db();
    $q = $pdo->prepare("SELECT id FROM deposits WHERE id=? AND telegram_id=? AND method='upi'");
    $q->execute([$dep_id, $from_id]);
    if (!$q->fetch()) {
      bot("answerCallbackQuery", ["callback_query_id"=>$cbid, "text"=>"Invalid request."]);
      http_response_code(200); echo "OK"; exit;
    }
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"What is the payer name? (Name used in UPI payment)"]);
    set_state($from_id, "upi_wait_payer_name", ["dep_id"=>$dep_id]);
    bot("answerCallbackQuery", ["callback_query_id"=>$cbid, "text"=>""]);
    http_response_code(200); echo "OK"; exit;
  }

  // buy type selected
  if (strpos($data, "buy_type_") === 0) {
    $type = str_replace("buy_type_", "", $data);
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"How many {$type} coupons do you want to buy?\nPlease send the quantity:"]);
    set_state($from_id, "buy_wait_qty", ["type"=>$type]);
    bot("answerCallbackQuery", ["callback_query_id"=>$cbid, "text"=>""]);
    http_response_code(200); echo "OK"; exit;
  }

  // ADMIN: add type
  if (is_admin($from_id) && strpos($data, "admin_add_type_") === 0) {
    $type = str_replace("admin_add_type_", "", $data);
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"Send coupon codes for {$type} (one per line):"]);
    set_state($from_id, "admin_wait_add_codes", ["type"=>$type]);
    bot("answerCallbackQuery", ["callback_query_id"=>$cbid, "text"=>""]);
    http_response_code(200); echo "OK"; exit;
  }

  // ADMIN: remove type
  if (is_admin($from_id) && strpos($data, "admin_rm_type_") === 0) {
    $type = str_replace("admin_rm_type_", "", $data);
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"How many unused {$type} coupons do you want to remove? Send number:"]);
    set_state($from_id, "admin_wait_rm_count", ["type"=>$type]);
    bot("answerCallbackQuery", ["callback_query_id"=>$cbid, "text"=>""]);
    http_response_code(200); echo "OK"; exit;
  }

  // ADMIN: price type
  if (is_admin($from_id) && strpos($data, "admin_price_type_") === 0) {
    $type = str_replace("admin_price_type_", "", $data);
    bot("sendMessage", ["chat_id"=>$from_id, "text"=>"Send NEW price (diamonds) for {$type}:"]);
    set_state($from_id, "admin_wait_new_price", ["type"=>$type]);
    bot("answerCallbackQuery", ["callback_query_id"=>$cbid, "text"=>""]);
    http_response_code(200); echo "OK"; exit;
  }

  // ADMIN: free coupon
  if (is_admin($from_id) && strpos($data, "admin_free_type_") === 0) {
    $type = str_replace("admin_free_type_", "", $data);
    $pdo = db();
    $pdo->beginTransaction();
    try {
      $q = $pdo->prepare("SELECT id, code FROM coupons_stock WHERE type=? AND is_used=FALSE ORDER BY id ASC LIMIT 1 FOR UPDATE");
      $q->execute([$type]);
      $row = $q->fetch();
      if (!$row) {
        $pdo->rollBack();
        bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ No stock available for {$type}."]);
      } else {
        $u = $pdo->prepare("UPDATE coupons_stock SET is_used=TRUE WHERE id=?");
        $u->execute([$row["id"]]);
        $pdo->commit();
        bot("sendMessage", ["chat_id"=>$from_id, "text"=>"🎁 Free {$type} Coupon:\n\n{$row['code']}"]);
      }
    } catch(Exception $e) {
      $pdo->rollBack();
      error_log("free coupon err: ".$e->getMessage());
      bot("sendMessage", ["chat_id"=>$from_id, "text"=>"❌ Error."]);
    }
    bot("answerCallbackQuery", ["callback_query_id"=>$cbid, "text"=>""]);
    http_response_code(200); echo "OK"; exit;
  }

  // ADMIN: accept/decline deposit
  if (is_admin($from_id) && strpos($data, "admin_dep_accept_") === 0) {
    $dep_id = (int)str_replace("admin_dep_accept_", "", $data);
    $pdo = db();
    $pdo->beginTransaction();
    try {
      $q = $pdo->prepare("SELECT * FROM deposits WHERE id=? FOR UPDATE");
      $q->execute([$dep_id]);
      $dep = $q->fetch();
      if (!$dep) { $pdo->rollBack(); bot("answerCallbackQuery", ["callback_query_id"=>$cbid, "text"=>"Not found"]); http_response_code(200); echo "OK"; exit; }
      if ($dep["status"] === "approved") { $pdo->rollBack(); bot("answerCallbackQuery", ["callback_query_id"=>$cbid, "text"=>"Already approved"]); http_response_code(200); echo "OK"; exit; }

      $u = (int)$dep["telegram_id"];
      $amt = (int)$dep["diamonds_requested"];

      $q2 = $pdo->prepare("UPDATE deposits SET status='approved' WHERE id=?");
      $q2->execute([$dep_id]);

      add_balance($u, $amt);
      $pdo->commit();

      bot("sendMessage", ["chat_id"=>$u, "text"=>"✅ Payment Approved!\n{$amt} Diamonds added to your balance."]);
      bot("answerCallbackQuery", ["callback_query_id"=>$cbid, "text"=>"Approved ✅"]);
    } catch(Exception $e) {
      $pdo->rollBack();
      error_log("approve dep err: ".$e->getMessage());
      bot("answerCallbackQuery", ["callback_query_id"=>$cbid, "text"=>"Error"]);
    }
    http_response_code(200); echo "OK"; exit;
  }

  if (is_admin($from_id) && strpos($data, "admin_dep_decline_") === 0) {
    $dep_id = (int)str_replace("admin_dep_decline_", "", $data);
    $pdo = db();
    try {
      $q = $pdo->prepare("SELECT telegram_id, status FROM deposits WHERE id=?");
      $q->execute([$dep_id]);
      $dep = $q->fetch();
      if ($dep) {
        if ($dep["status"] !== "approved") {
          $q2 = $pdo->prepare("UPDATE deposits SET status='declined' WHERE id=?");
          $q2->execute([$dep_id]);
        }
        bot("sendMessage", ["chat_id"=>(int)$dep["telegram_id"], "text"=>"❌ Payment Declined."]);
      }
    } catch(Exception $e) {
      error_log("decline dep err: ".$e->getMessage());
    }
    bot("answerCallbackQuery", ["callback_query_id"=>$cbid, "text"=>"Declined ❌"]);
    http_response_code(200); echo "OK"; exit;
  }

  // default cb answer
  bot("answerCallbackQuery", ["callback_query_id"=>$cbid, "text"=>""]);
}

// Always answer 200
http_response_code(200);
echo "OK";
