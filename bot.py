"""
SMOKELAB Telegram Bot — aiogram 3.x
Фиксированная доставка 5 BYN, управление остатками, логирование API.
База данных: data/orders.db
"""

import asyncio, hashlib, hmac, json, logging, os, sqlite3, time, urllib.parse
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton,
    KeyboardButton, ReplyKeyboardMarkup, CallbackQuery, BotCommand,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

# ── Конфигурация ──────────────────────────────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN", "8738864601:AAGvTSRtkU-LBe-b7HREagxhbfo6g0miFXU")
ADMIN_ID     = int(os.getenv("ADMIN_ID", "8160958113"))
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://luaccoder.github.io/smokelabbot/")
PORT         = int(os.getenv("PORT", "8080"))

BASE_DIR = Path(__file__).parent
DB_DIR   = BASE_DIR / "data"
DB_DIR.mkdir(exist_ok=True)
DB_FILE  = DB_DIR / "orders.db"

logging.basicConfig(
    level=logging.INFO,   # Для продакшена INFO, при отладке можно DEBUG
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher(storage=MemoryStorage())

CORS_HEADERS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, PUT, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Telegram-Init-Data",
}

# ── Инициализация базы данных ─────────────────────────────────────────────────
def init_db() -> None:
    log.info("Инициализация БД: %s", DB_FILE)
    with sqlite3.connect(DB_FILE) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                order_num       TEXT    UNIQUE NOT NULL,
                user_id         INTEGER NOT NULL,
                username        TEXT,
                full_name       TEXT,
                items           TEXT    NOT NULL,
                total           REAL    NOT NULL,
                delivery_cost   REAL    DEFAULT 0,
                payment         TEXT    NOT NULL,
                customer        TEXT    NOT NULL,
                status          TEXT    DEFAULT 'pending',
                user_message_id INTEGER DEFAULT 0,
                created_at      TEXT    NOT NULL,
                updated_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                first_name  TEXT,
                username    TEXT,
                last_active TEXT
            );

            CREATE TABLE IF NOT EXISTS categories (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                key   TEXT    UNIQUE NOT NULL,
                name  TEXT    NOT NULL,
                icon  TEXT    DEFAULT '📦',
                image TEXT    DEFAULT '',
                sort  INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS products (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                cat_key  TEXT    NOT NULL,
                name     TEXT    NOT NULL,
                desc     TEXT    DEFAULT '',
                price    REAL    NOT NULL,
                emoji    TEXT    DEFAULT '📦',
                image    TEXT    DEFAULT '',
                variants TEXT    DEFAULT '[]',
                sort     INTEGER DEFAULT 0
            );
        """)
        # Дефолтные категории
        count = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        if count == 0:
            log.info("Создание дефолтных категорий")
            conn.executemany(
                "INSERT OR IGNORE INTO categories (key, name, icon) VALUES (?,?,?)",
                [
                    ("vapes",       "Вейпы",     "💨"),
                    ("liquids",     "Жидкости",  "🧪"),
                    ("snus",        "Снюс",      "🍃"),
                    ("accessories", "Расходники", "🔧"),
                ],
            )
        # Дефолтные товары с полем stock
        pcount = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        if pcount == 0:
            log.info("Создание дефолтных товаров")
            conn.executemany(
                "INSERT OR IGNORE INTO products (cat_key,name,desc,price,emoji,variants) VALUES (?,?,?,?,?,?)",
                [
                    ("vapes","ELFBAR 5000","5000 затяжек, вкусы",19.90,"💨",
                     '[{"name":"Чёрный","price":19.90,"stock":10},{"name":"Белый","price":19.90,"stock":5}]'),
                    ("vapes","LOST MARY OS5000","Компактный",21.50,"🌊",
                     '[{"name":"Чёрный","price":21.50,"stock":7},{"name":"Красный","price":21.50,"stock":0}]'),
                    ("liquids","Liquid Salt Nic 30мл","Солевая жидкость",9.90,"🧪",
                     '[{"name":"Манго","price":9.90,"stock":12},{"name":"Клубника","price":9.90,"stock":8}]'),
                    ("liquids","Big Tasty Mango","Фруктовая",12.50,"🍋",
                     '[{"name":"Манго","price":12.50,"stock":6},{"name":"Ананас","price":12.50,"stock":4}]'),
                    ("snus","Killa Cola Ice","Снюс с охлаждением",6.90,"❄️",
                     '[{"name":"Кола","price":6.90,"stock":20},{"name":"Мята","price":6.90,"stock":15}]'),
                    ("snus","Lyft Mint","Мятный снюс",5.50,"🍃",
                     '[{"name":"Мята","price":5.50,"stock":25}]'),
                    ("accessories","Испаритель Smok V8","Совместим с Smok TFV8",4.90,"🔧",
                     '[{"name":"0.15 Ohm","price":4.90,"stock":30},{"name":"0.2 Ohm","price":4.90,"stock":30}]'),
                    ("accessories","Ватные фитили","Японский хлопок",3.50,"🌸",
                     '[{"name":"Стандарт","price":3.50,"stock":40}]'),
                ],
            )
        conn.commit()
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        log.info("Таблицы в БД: %s", [t[0] for t in tables])

# ── Вспомогательные функции работы с БД ──────────────────────────────────────
def add_user(user) -> None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (user_id, first_name, username, last_active) "
            "VALUES (?, ?, ?, datetime('now'))",
            (user.id, user.first_name, user.username),
        )
        conn.commit()

def save_order(d: dict) -> None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT INTO orders (order_num,user_id,username,full_name,items,total,"
            "delivery_cost,payment,customer,status,user_message_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?,datetime('now'))",
            (d["order_num"],d["user_id"],d.get("username"),d.get("full_name"),
             json.dumps(d["items"],ensure_ascii=False),d["total"],d.get("delivery_cost",0),
             d["payment"],json.dumps(d["customer"],ensure_ascii=False),
             d.get("user_message_id",0),d["created_at"]),
        )
        conn.commit()

def get_order(order_num: str) -> dict | None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM orders WHERE order_num=?",(order_num,)).fetchone()
    if not row: return None
    o = dict(row); o["items"] = json.loads(o["items"]); o["customer"] = json.loads(o["customer"])
    return o

def get_user_orders(user_id: int, limit=10) -> list:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?",(user_id,limit)
        ).fetchall()
    result = []
    for r in rows:
        o = dict(r); o["items"] = json.loads(o["items"]); o["customer"] = json.loads(o["customer"])
        result.append(o)
    return result

def update_order_status(order_num: str, status: str) -> None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "UPDATE orders SET status=?,updated_at=datetime('now') WHERE order_num=?",
            (status,order_num)
        ); conn.commit()

def get_active_orders() -> list:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM orders WHERE status NOT IN ('done','cancelled') ORDER BY id DESC"
        ).fetchall()
    result = []
    for r in rows:
        o = dict(r); o["items"] = json.loads(o["items"]); o["customer"] = json.loads(o["customer"])
        result.append(o)
    return result

def get_next_order_number() -> str:
    with sqlite3.connect(DB_FILE) as conn:
        count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    return f"#{count + 1:06d}"

def get_all_user_ids() -> list[int]:
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute("SELECT user_id FROM users").fetchall()
    return [r[0] for r in rows]

# ── CRUD категорий ────────────────────────────────────────────────────────────
def db_get_categories() -> list:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM categories ORDER BY sort,id").fetchall()
    return [dict(r) for r in rows]

def db_save_category(key, name, icon, image, cat_id=None) -> int:
    with sqlite3.connect(DB_FILE) as conn:
        if cat_id:
            conn.execute(
                "UPDATE categories SET key=?,name=?,icon=?,image=? WHERE id=?",
                (key,name,icon,image,cat_id)
            )
            rowid = cat_id
        else:
            cur = conn.execute(
                "INSERT INTO categories (key,name,icon,image) VALUES (?,?,?,?)",
                (key,name,icon,image)
            )
            rowid = cur.lastrowid
        conn.commit()
    return rowid

def db_delete_category(cat_id: int) -> None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM categories WHERE id=?", (cat_id,))
        conn.commit()

# ── CRUD товаров (с поддержкой stock) ─────────────────────────────────────────
def db_get_products() -> list:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM products ORDER BY sort,id").fetchall()
    result = []
    for r in rows:
        p = dict(r)
        variants = json.loads(p.get("variants") or "[]")
        for v in variants:
            if "stock" not in v:
                v["stock"] = 0
        p["variants"] = variants
        result.append(p)
    return result

def db_save_product(cat_key, name, desc, price, emoji, image, variants, prod_id=None) -> int:
    v_json = json.dumps(variants, ensure_ascii=False)
    with sqlite3.connect(DB_FILE) as conn:
        if prod_id:
            conn.execute(
                "UPDATE products SET cat_key=?,name=?,desc=?,price=?,emoji=?,image=?,variants=? WHERE id=?",
                (cat_key, name, desc, price, emoji, image, v_json, prod_id)
            )
            rowid = prod_id
        else:
            cur = conn.execute(
                "INSERT INTO products (cat_key,name,desc,price,emoji,image,variants) VALUES (?,?,?,?,?,?,?)",
                (cat_key, name, desc, price, emoji, image, v_json)
            )
            rowid = cur.lastrowid
        conn.commit()
    return rowid

def db_delete_product(prod_id: int) -> None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM products WHERE id=?", (prod_id,))
        conn.commit()

# ── Telegram initData верификация ─────────────────────────────────────────────
def verify_init_data(init_data: str) -> dict | None:
    if not init_data:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
    except Exception:
        return None
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        return None
    return json.loads(parsed.get("user", "{}"))

# ── Web API (aiohttp) с логированием ──────────────────────────────────────────
async def handle_options(request: web.Request) -> web.Response:
    return web.Response(headers=CORS_HEADERS)

async def api_get_data(request: web.Request) -> web.Response:
    log.info("GET /api/data")
    cats = db_get_categories()
    prods = db_get_products()
    return web.json_response({"categories": cats, "products": prods}, headers=CORS_HEADERS)

def _check_admin(request: web.Request) -> bool:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user = verify_init_data(init_data)
    return bool(user and int(user.get("id", 0)) == ADMIN_ID)

async def api_save_category(request: web.Request) -> web.Response:
    if not _check_admin(request):
        return web.json_response({"error": "Unauthorized"}, status=403, headers=CORS_HEADERS)
    try:
        data = await request.json()
        log.info("POST /api/admin/category: %s", data.get("name"))
        cat_id = data.get("id")
        key = str(data.get("key","")).strip()
        name = str(data.get("name","")).strip()
        icon = str(data.get("icon","📦")).strip()
        image = str(data.get("image","")).strip()
        if not key or not name:
            return web.json_response({"error":"key and name required"}, status=400, headers=CORS_HEADERS)
        new_id = db_save_category(key, name, icon, image, cat_id)
        return web.json_response({"ok": True, "id": new_id}, headers=CORS_HEADERS)
    except Exception as e:
        log.exception("api_save_category error")
        return web.json_response({"error": str(e)}, status=500, headers=CORS_HEADERS)

async def api_delete_category(request: web.Request) -> web.Response:
    if not _check_admin(request):
        return web.json_response({"error": "Unauthorized"}, status=403, headers=CORS_HEADERS)
    cat_id = int(request.match_info["id"])
    log.info("DELETE /api/admin/category/%d", cat_id)
    db_delete_category(cat_id)
    return web.json_response({"ok": True}, headers=CORS_HEADERS)

async def api_save_product(request: web.Request) -> web.Response:
    if not _check_admin(request):
        return web.json_response({"error": "Unauthorized"}, status=403, headers=CORS_HEADERS)
    try:
        d = await request.json()
        prod_id = d.get("id")
        name = str(d.get("name","")).strip()
        log.info("POST /api/admin/product: %s (id=%s)", name, prod_id)
        if not name:
            return web.json_response({"error":"name required"}, status=400, headers=CORS_HEADERS)

        variants = d.get("variants", [])
        clean_variants = []
        for v in variants:
            if isinstance(v, dict) and v.get("name"):
                clean_variants.append({
                    "name": str(v["name"]),
                    "price": float(v.get("price", 0)),
                    "stock": int(v.get("stock", 0))
                })

        image_len = len(str(d.get("image", "")))
        log.info("Изображение: %d символов", image_len)

        new_id = db_save_product(
            cat_key  = str(d.get("cat","")).strip(),
            name     = name,
            desc     = str(d.get("desc","")).strip(),
            price    = float(d.get("price", 0)),
            emoji    = str(d.get("emoji","📦")).strip(),
            image    = str(d.get("image","")).strip(),
            variants = clean_variants,
            prod_id  = prod_id,
        )
        log.info("Товар сохранён, id=%d", new_id)
        return web.json_response({"ok": True, "id": new_id}, headers=CORS_HEADERS)
    except Exception as e:
        log.exception("api_save_product error")
        return web.json_response({"error": str(e)}, status=500, headers=CORS_HEADERS)

async def api_delete_product(request: web.Request) -> web.Response:
    if not _check_admin(request):
        return web.json_response({"error": "Unauthorized"}, status=403, headers=CORS_HEADERS)
    prod_id = int(request.match_info["id"])
    log.info("DELETE /api/admin/product/%d", prod_id)
    db_delete_product(prod_id)
    return web.json_response({"ok": True}, headers=CORS_HEADERS)

def create_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get   ("/api/data",                api_get_data)
    app.router.add_post  ("/api/admin/category",      api_save_category)
    app.router.add_delete("/api/admin/category/{id}", api_delete_category)
    app.router.add_post  ("/api/admin/product",       api_save_product)
    app.router.add_delete("/api/admin/product/{id}",  api_delete_product)
    app.router.add_route ("OPTIONS", "/{path_info:.*}", handle_options)
    app.router.add_get("/", lambda r: web.Response(text="SMOKELAB bot is running ✓"))
    return app

# ── Клавиатуры и форматирование сообщений ────────────────────────────────────
def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🛒 Открыть магазин", web_app=WebAppInfo(url=MINI_APP_URL))],
        [KeyboardButton(text="📦 Мои заказы"), KeyboardButton(text="ℹ️ О нас")],
    ], resize_keyboard=True)

def kb_admin_order(order_num: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принят",   callback_data=f"st:{order_num}:accepted"),
            InlineKeyboardButton(text="🚚 В пути",   callback_data=f"st:{order_num}:shipping"),
        ],
        [
            InlineKeyboardButton(text="✔️ Выполнен", callback_data=f"st:{order_num}:done"),
            InlineKeyboardButton(text="❌ Отменён",  callback_data=f"st:{order_num}:cancelled"),
        ],
    ])

PAY_LABELS = {
    "cash":   "💵 Наличными",
    "card":   "💳 Банковская карта",
}
STATUS_INFO = {
    "pending":   "⏳ Ожидает",
    "accepted":  "✅ Принят",
    "shipping":  "🚚 В пути",
    "done":      "✔️ Выполнен",
    "cancelled": "❌ Отменён",
}

def format_items(items: list) -> str:
    return "\n".join(
        f"  • {it['name']} × {it['qty']} = {it['price']*it['qty']:.2f} BYN"
        for it in items
    )

def build_address_link(address: str) -> str:
    encoded = address.replace(" ", "%20")
    return f'<a href="https://yandex.ru/maps/?text={encoded}">{address}</a>'

def user_order_text(order: dict) -> str:
    items_text    = format_items(order["items"])
    customer      = order["customer"]
    delivery_cost = order.get("delivery_cost", 0)
    total         = order["total"] + delivery_cost
    status        = STATUS_INFO.get(order["status"], "⏳")
    payment_label = PAY_LABELS.get(order["payment"], order["payment"])
    comment_line  = f"💬 {customer['comment']}\n" if customer.get("comment") else ""
    receipt_line  = f"📋 Чек: {customer['receiptNote']}\n" if customer.get("receiptNote") else ""
    if customer.get("delivery") == "pickup":
        addr_str = customer.get("address", "—")
    else:
        addr_str = build_address_link(customer.get("address", "—"))
    phone_line = f"📱 {customer['phone']}\n" if customer.get("phone") else ""
    return (
        f"🎉 <b>Заказ {order['order_num']}</b>\n\n"
        f"<b>Состав:</b>\n{items_text}\n\n"
        f"<b>Сумма товаров:</b> {order['total']:.2f} BYN\n"
        f"<b>Доставка:</b> {delivery_cost:.2f} BYN\n"
        f"<b>Итого:</b> {total:.2f} BYN\n\n"
        f"<b>Оплата:</b> {payment_label}\n\n"
        f"<b>Получатель:</b>\n"
        f"👤 {customer.get('name', '—')}\n"
        f"{phone_line}"
        f"🏠 {addr_str}\n"
        f"{comment_line}"
        f"{receipt_line}\n"
        f"<b>Статус:</b> {status}"
    )

def admin_notification_text(order: dict) -> str:
    items_text    = format_items(order["items"])
    customer      = order["customer"]
    delivery_cost = order.get("delivery_cost", 0)
    total         = order["total"] + delivery_cost
    payment_label = PAY_LABELS.get(order["payment"], order["payment"])
    comment_line  = f"💬 {customer['comment']}\n" if customer.get("comment") else ""
    receipt_line  = f"📋 Чек: {customer['receiptNote']}\n" if customer.get("receiptNote") else ""
    if customer.get("delivery") == "pickup":
        addr_str = customer.get("address", "—")
    else:
        addr_str = build_address_link(customer.get("address", "—"))
    phone_line = f"📱 {customer['phone']}\n" if customer.get("phone") else ""
    username_info = f" (@{order['username']})" if order.get('username') else ""
    return (
        f"🔔 <b>НОВЫЙ ЗАКАЗ {order['order_num']}</b>\n"
        f"🕐 {order['created_at']}\n\n"
        f"<b>Клиент:</b> {order['full_name']}{username_info} (ID: {order['user_id']})\n"
        f"👤 {customer.get('name', '—')}\n"
        f"{phone_line}"
        f"🏠 {addr_str}\n"
        f"{comment_line}"
        f"{receipt_line}\n"
        f"<b>Состав:</b>\n{items_text}\n\n"
        f"<b>Сумма товаров:</b> {order['total']:.2f} BYN\n"
        f"<b>Доставка:</b> {delivery_cost:.2f} BYN\n"
        f"<b>Итого:</b> {total:.2f} BYN\n"
        f"<b>Оплата:</b> {payment_label}"
    )

# ── Хэндлеры бота ─────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(msg: Message) -> None:
    add_user(msg.from_user)
    name = msg.from_user.first_name or "друг"
    await msg.answer(
        f"👋 Привет, <b>{name}</b>!\n\n"
        "Добро пожаловать в <b>SMOKELAB</b> — ваш вейп-магазин в Витебске.\n"
        "Нажмите кнопку ниже, чтобы открыть каталог 👇",
        reply_markup=kb_main(),
    )

@dp.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    await msg.answer(
        "<b>Помощь</b>\n\n"
        "/start — Главное меню\n"
        "/orders — История заказов\n"
        "/help — Эта справка\n\n"
        "Поддержка: @smokelab_support"
    )

@dp.message(Command("orders"))
@dp.message(F.text == "📦 Мои заказы")
async def show_orders(msg: Message) -> None:
    orders = get_user_orders(msg.from_user.id)
    if not orders:
        await msg.answer("У вас пока нет заказов.", reply_markup=kb_main())
        return
    icons = {"pending":"⏳","accepted":"✅","shipping":"🚚","done":"✔️","cancelled":"❌"}
    lines = ["<b>Ваши заказы:</b>\n"]
    for o in orders:
        total = o["total"] + o.get("delivery_cost", 0)
        lines.append(
            f"{icons.get(o['status'],'⏳')} <b>{o['order_num']}</b>\n"
            f"   {total:.2f} BYN · {PAY_LABELS.get(o['payment'],o['payment'])}\n"
            f"   {o['created_at']}\n"
        )
    await msg.answer("\n".join(lines))

@dp.message(F.text == "ℹ️ О нас")
async def btn_about(msg: Message) -> None:
    await msg.answer(
        "<b>SMOKELAB</b> 💨\n\n"
        "Лучший выбор вейп-продуктов в Беларуси.\n"
        "📍 г. Витебск, ул. Генерала Ивановского, 34\n"
        "🕐 9:00 – 22:00\n"
        "💬 @smokelab_support"
    )

@dp.message(F.web_app_data)
async def handle_web_app_data(msg: Message) -> None:
    try:
        data = json.loads(msg.web_app_data.data)
    except json.JSONDecodeError:
        await msg.answer("⚠️ Ошибка при обработке данных.")
        return
    t = data.get("type")
    if t == "order":
        await process_order(msg, data)
    elif t == "broadcast":
        await process_broadcast(msg, data)

async def process_order(msg: Message, data: dict) -> None:
    items = data.get("items", [])
    if not items:
        await msg.answer("❌ Корзина пуста.")
        return
    user = msg.from_user
    add_user(user)
    now_str   = datetime.now().strftime("%d.%m.%Y %H:%M")
    order_num = get_next_order_number()
    order_data = {
        "order_num":     order_num,
        "user_id":       user.id,
        "username":      user.username,
        "full_name":     user.full_name,
        "items":         items,
        "total":         float(data.get("total", 0)),
        "delivery_cost": float(data.get("deliveryCost", 0)),
        "payment":       data.get("payment", "cash"),
        "customer":      data.get("customer", {}),
        "created_at":    now_str,
        "user_message_id": 0,
    }
    save_order(order_data)
    order = get_order(order_num)
    await msg.answer(user_order_text(order), reply_markup=kb_main())
    try:
        await bot.send_message(ADMIN_ID, admin_notification_text(order),
                               reply_markup=kb_admin_order(order_num))
        log.info("Новый заказ %s от %s", order_num, user.id)
    except Exception as exc:
        log.error("Не удалось уведомить администратора: %s", exc)

async def process_broadcast(msg: Message, data: dict) -> None:
    if msg.from_user.id != ADMIN_ID:
        return
    text = data.get("text", "").strip()
    if not text:
        await bot.send_message(ADMIN_ID, "⚠️ Текст рассылки пуст.")
        return
    user_ids = get_all_user_ids()
    success = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text)
            success += 1
        except Exception as exc:
            log.warning("Ошибка отправки %s: %s", uid, exc)
    await bot.send_message(ADMIN_ID,
                           f"✅ Рассылка завершена: {success}/{len(user_ids)} получили сообщение.")

@dp.callback_query(F.data.startswith("st:"))
async def handle_status(call: CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Нет доступа", show_alert=True); return
    parts = call.data.split(":", 2)
    if len(parts) != 3:
        await call.answer("❌ Некорректные данные", show_alert=True); return
    _, order_num, new_status = parts
    order = get_order(order_num)
    if not order:
        await call.answer("Заказ не найден.", show_alert=True); return
    if order["status"] == new_status:
        await call.answer("Статус уже установлен"); return
    update_order_status(order_num, new_status)
    updated = get_order(order_num)
    try:
        await bot.send_message(updated["user_id"], user_order_text(updated))
    except Exception as exc:
        log.error("Не удалось уведомить пользователя: %s", exc)
    final = {"done","cancelled"}
    new_markup = None if new_status in final else call.message.reply_markup
    await call.message.edit_text(
        call.message.html_text + f"\n\n<b>Статус изменён:</b> {STATUS_INFO[new_status]}",
        reply_markup=new_markup,
    )
    await call.answer(f"Готово: {STATUS_INFO[new_status]}")

@dp.message(Command("admin"))
async def cmd_admin(msg: Message) -> None:
    if msg.from_user.id != ADMIN_ID: return
    await msg.answer(
        "🛠️ <b>Панель администратора</b>\n\n"
        "/orders_active — Активные заказы\n"
        "/users_count — Количество пользователей\n"
        "/set_status НОМЕР СТАТУС — Изменить статус\n\n"
        "📱 Управление товарами — через кнопку Магазин → вкладка Админ"
    )

@dp.message(Command("orders_active"))
async def cmd_orders_active(msg: Message) -> None:
    if msg.from_user.id != ADMIN_ID: return
    orders = get_active_orders()
    if not orders:
        await msg.answer("✅ Активных заказов нет."); return
    await msg.answer(f"📋 Активных заказов: <b>{len(orders)}</b>")
    for order in orders[:20]:
        await msg.answer(admin_notification_text(order),
                         reply_markup=kb_admin_order(order["order_num"]))

@dp.message(Command("users_count"))
async def cmd_users_count(msg: Message) -> None:
    if msg.from_user.id != ADMIN_ID: return
    uids = get_all_user_ids()
    await msg.answer(f"👥 Пользователей в базе: <b>{len(uids)}</b>")

@dp.message(Command("set_status"))
async def cmd_set_status(msg: Message) -> None:
    if msg.from_user.id != ADMIN_ID: return
    parts = msg.text.strip().split()
    if len(parts) != 3:
        await msg.answer("Формат: /set_status #000001 accepted"); return
    _, order_num, status = parts
    if status not in STATUS_INFO:
        await msg.answer(f"❌ Статусы: {', '.join(k for k in STATUS_INFO if k!='pending')}"); return
    order = get_order(order_num)
    if not order:
        await msg.answer(f"❌ Заказ {order_num} не найден."); return
    update_order_status(order_num, status)
    await msg.answer(f"✅ Статус заказа {order_num} → {STATUS_INFO[status]}")

# ── Запуск бота и веб-сервера ────────────────────────────────────────────────
async def main() -> None:
    init_db()
    await bot.set_my_commands([
        BotCommand(command="start",  description="Главное меню"),
        BotCommand(command="orders", description="Мои заказы"),
        BotCommand(command="help",   description="Помощь"),
    ])
    app = create_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("SMOKELAB Bot запущен | ADMIN_ID=%s | PORT=%s", ADMIN_ID, PORT)
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    time.sleep(2)
    asyncio.run(main())
