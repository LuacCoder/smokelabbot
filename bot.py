"""
SMOKELAB Telegram Bot — aiogram 3.x
Хостинг: Koyeb / Render (бесплатный worker)
Функции: приём заказов из Mini App, управление заказами админом, SQLite.
"""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    WebAppInfo,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    KeyboardButton,
    ReplyKeyboardMarkup,
    CallbackQuery,
    BotCommand,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

# ─── КОНФИГУРАЦИЯ (переменные окружения) ──────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
ADMIN_ID     = int(os.getenv("ADMIN_ID", "0"))
MINI_APP_URL = os.getenv("MINI_APP_URL", "")  # GitHub Pages URL или аналогичный

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан! Добавьте его в переменные окружения.")

BASE_DIR = Path(__file__).parent
DB_FILE = BASE_DIR / "orders.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())

# ─── БАЗА ДАННЫХ SQLITE3 ─────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_num TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            full_name TEXT,
            items TEXT NOT NULL,
            total REAL NOT NULL,
            payment TEXT NOT NULL,
            customer TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def save_order(order_data: dict):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO orders (order_num, user_id, username, full_name, items, total, payment, customer, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, datetime('now'))
    """, (
        order_data["order_num"],
        order_data["user_id"],
        order_data.get("username"),
        order_data.get("full_name"),
        json.dumps(order_data["items"], ensure_ascii=False),
        order_data["total"],
        order_data["payment"],
        json.dumps(order_data["customer"], ensure_ascii=False),
        order_data["created_at"],
    ))
    conn.commit()
    conn.close()

def get_order(order_num: str) -> dict | None:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE order_num = ?", (order_num,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    order = dict(row)
    order["items"] = json.loads(order["items"])
    order["customer"] = json.loads(order["customer"])
    return order

def get_user_orders(user_id: int, limit: int = 10) -> list[dict]:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    rows = cur.fetchall()
    conn.close()
    orders = []
    for row in rows:
        order = dict(row)
        order["items"] = json.loads(order["items"])
        order["customer"] = json.loads(order["customer"])
        orders.append(order)
    return orders

def update_order_status(order_num: str, new_status: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "UPDATE orders SET status = ?, updated_at = datetime('now') WHERE order_num = ?",
        (new_status, order_num)
    )
    conn.commit()
    conn.close()

def get_active_orders():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE status != 'done' AND status != 'cancelled' ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# Счётчик заказов (глобальный) – загружается при старте
def get_next_order_number():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM orders")
    count = cur.fetchone()[0]
    conn.close()
    return f"#{count + 1:06d}"

# ─── КЛАВИАТУРЫ ──────────────────────────────────────────────────────────────
def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(
                text="🛒 Открыть магазин",
                web_app=WebAppInfo(url=MINI_APP_URL),
            )],
            [
                KeyboardButton(text="📦 Мои заказы"),
                KeyboardButton(text="ℹ️ О нас"),
            ],
        ],
        resize_keyboard=True,
    )

def kb_admin_order(order_num: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принят",    callback_data=f"st:{order_num}:accepted"),
            InlineKeyboardButton(text="🚚 В пути",    callback_data=f"st:{order_num}:shipping"),
        ],
        [
            InlineKeyboardButton(text="✔️ Выполнен", callback_data=f"st:{order_num}:done"),
            InlineKeyboardButton(text="❌ Отменён",   callback_data=f"st:{order_num}:cancelled"),
        ],
    ])

# ─── ВСПОМОГАТЕЛЬНЫЕ ─────────────────────────────────────────────────────────
PAY_LABELS = {
    "cash":   "💵 Наличными при получении",
    "card":   "💳 Банковская карта",
    "crypto": "₿ Криптовалюта",
}

STATUS_INFO = {
    "accepted":  ("✅ Принят",    "Ваш заказ <b>{num}</b> <b>принят</b> и готовится к отправке! ✅"),
    "shipping":  ("🚚 В пути",    "Ваш заказ <b>{num}</b> уже <b>в пути</b>! 🚚"),
    "done":      ("✔️ Выполнен",  "Заказ <b>{num}</b> <b>выполнен</b>. Спасибо за покупку! 🎉"),
    "cancelled": ("❌ Отменён",   "Заказ <b>{num}</b> <b>отменён</b>. Вопросы: @smokelab_support"),
}

def format_items(items: list) -> str:
    return "\n".join(
        f"  • {it['name']} × {it['qty']} = {it['price'] * it['qty']:.2f} BYN"
        for it in items
    )

# ─── КОМАНДЫ БОТА ────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(msg: Message):
    name = msg.from_user.first_name or "друг"
    await msg.answer(
        f"👋 Привет, <b>{name}</b>!\n\n"
        f"Добро пожаловать в <b>SMOKELAB</b> — ваш вейп-магазин в Беларуси.\n\n"
        f"Нажмите кнопку ниже, чтобы открыть каталог 👇",
        reply_markup=kb_main(),
    )

@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        "<b>Помощь</b>\n\n"
        "/start — Главное меню\n"
        "/orders — История заказов\n"
        "/help — Эта справка\n\n"
        "Поддержка: @smokelab_support",
    )

@dp.message(Command("orders"))
async def cmd_orders(msg: Message):
    await show_orders(msg)

@dp.message(F.text == "📦 Мои заказы")
async def btn_orders(msg: Message):
    await show_orders(msg)

@dp.message(F.text == "ℹ️ О нас")
async def btn_about(msg: Message):
    await msg.answer(
        "<b>SMOKELAB</b> 💨\n\n"
        "Лучший выбор вейп-продуктов в Беларуси.\n\n"
        "🕐 9:00 – 22:00, ежедневно\n"
        "📍 Минск и доставка по РБ\n"
        "💬 @smokelab_support",
    )

async def show_orders(msg: Message):
    uid = msg.from_user.id
    user_orders = get_user_orders(uid)
    if not user_orders:
        await msg.answer(
            "У вас пока нет заказов.\nОткройте магазин и сделайте первую покупку! 🛒",
            reply_markup=kb_main(),
        )
        return
    icons = {"pending":"⏳","accepted":"✅","shipping":"🚚","done":"✔️","cancelled":"❌"}
    text = "<b>Ваши заказы:</b>\n\n"
    for o in user_orders[:10]:
        text += (
            f"{icons.get(o['status'],'⏳')} <b>{o['order_num']}</b>\n"
            f"   {o['total']:.2f} BYN · {PAY_LABELS.get(o['payment'], o['payment'])}\n"
            f"   {o['created_at']}\n\n"
        )
    await msg.answer(text)

# ─── ПРИЁМ ЗАКАЗА ИЗ MINI APP ────────────────────────────────────────────────
@dp.message(F.web_app_data)
async def handle_order(msg: Message):
    try:
        data = json.loads(msg.web_app_data.data)
    except json.JSONDecodeError:
        await msg.answer("⚠️ Ошибка при обработке заказа. Попробуйте ещё раз.")
        return

    if data.get("type") != "order":
        return

    items = data.get("items", [])
    if not items:
        await msg.answer("❌ Ваша корзина пуста.")
        return

    total = float(data.get("total", 0))
    payment = data.get("payment", "cash")
    customer = data.get("customer", {})
    user = msg.from_user
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")

    # Генерируем номер заказа на сервере
    order_num = get_next_order_number()

    order_data = {
        "order_num":  order_num,
        "user_id":    user.id,
        "username":   user.username,
        "full_name":  user.full_name,
        "items":      items,
        "total":      total,
        "payment":    payment,
        "customer":   customer,
        "created_at": now_str,
    }
    save_order(order_data)  # сохраняем в БД

    items_text   = format_items(items)
    comment_line = f"💬 {customer.get('comment', '')}\n" if customer.get("comment") else ""

    # Подтверждение пользователю
    await msg.answer(
        f"🎉 <b>Заказ {order_num} оформлен!</b>\n\n"
        f"<b>Состав:</b>\n{items_text}\n\n"
        f"<b>Итого:</b> {total:.2f} BYN\n"
        f"<b>Оплата:</b> {PAY_LABELS.get(payment, payment)}\n\n"
        f"<b>Доставка:</b>\n"
        f"👤 {customer.get('name','—')}\n"
        f"📱 {customer.get('phone','—')}\n"
        f"🏠 {customer.get('address','—')}\n"
        f"{comment_line}\n"
        f"⏳ <i>Мы скоро свяжемся с вами. "
        f"Статус можно узнать командой /orders.</i>",
        reply_markup=kb_main(),
    )

    # Уведомление администратору
    if ADMIN_ID:
        tg_ref = f"@{user.username}" if user.username else f"<a href='tg://user?id={user.id}'>{user.full_name}</a>"
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🔔 <b>НОВЫЙ ЗАКАЗ {order_num}</b>\n"
                f"🕐 {now_str}\n\n"
                f"<b>Клиент:</b> {tg_ref} ({user.full_name})\n"
                f"👤 {customer.get('name','—')}\n"
                f"📱 {customer.get('phone','—')}\n"
                f"🏠 {customer.get('address','—')}\n"
                f"{comment_line}\n"
                f"<b>Товары:</b>\n{items_text}\n\n"
                f"<b>Итого:</b> <code>{total:.2f} BYN</code>\n"
                f"<b>Оплата:</b> {PAY_LABELS.get(payment, payment)}",
                reply_markup=kb_admin_order(order_num),
            )
            log.info(f"Новый заказ {order_num} от пользователя {user.id}")
        except Exception as e:
            log.error("Не удалось уведомить администратора: %s", e)

# ─── СМЕНА СТАТУСА (только для ADMIN_ID) ─────────────────────────────────────
@dp.callback_query(F.data.startswith("st:"))
async def handle_status(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Нет доступа", show_alert=True)
        return

    _, order_num, new_status = call.data.split(":")
    order = get_order(order_num)

    if not order:
        await call.answer("Заказ не найден в базе данных.", show_alert=True)
        return

    if order["status"] == new_status:
        await call.answer("Статус уже установлен")
        return

    update_order_status(order_num, new_status)
    label, user_text = STATUS_INFO[new_status]

    # Обновляем сообщение админу
    await call.message.edit_text(
        call.message.html_text + f"\n\n<b>Статус →</b> {label}",
        reply_markup=None,
    )
    await call.answer(f"Статус: {label}")

    # Уведомляем клиента
    try:
        await bot.send_message(order["user_id"], user_text.format(num=order_num))
    except Exception as e:
        log.error("Не удалось уведомить пользователя: %s", e)

# ─── АДМИН-КОМАНДЫ ───────────────────────────────────────────────────────────
@dp.message(Command("admin"))
async def cmd_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("⛔ У вас нет доступа к панели администратора.")
    await msg.answer(
        "🛠️ <b>Панель администратора</b>\n\n"
        "/orders_active - активные заказы\n"
        "/order_status - изменить статус заказа\n"
        "Также используйте кнопки в сообщении с новым заказом.",
    )

@dp.message(Command("orders_active"))
async def cmd_orders_active(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("⛔ Нет доступа.")
    active = get_active_orders()
    if not active:
        return await msg.answer("Нет активных заказов.")
    icons = {"pending":"⏳","accepted":"✅","shipping":"🚚"}
    text = "<b>Активные заказы:</b>\n\n"
    for o in active:
        text += (
            f"{icons.get(o['status'], '⏳')} <b>{o['order_num']}</b> - {o['total']} BYN\n"
            f"   Статус: {o['status']} | {o['created_at']}\n"
        )
    await msg.answer(text)

@dp.message(Command("order_status"))
async def cmd_order_status(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("⛔ Нет доступа.")
    # Простейшая реализация: просим ввести номер заказа и новый статус
    await msg.answer(
        "Формат: <code>/set_status #000001 accepted</code>\n\n"
        "Доступные статусы: accepted, shipping, done, cancelled",
    )

@dp.message(Command("set_status"))
async def cmd_set_status(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("⛔ Нет доступа.")
    parts = msg.text.strip().split()
    if len(parts) != 3:
        return await msg.answer("Неверный формат. Пример: <code>/set_status #000001 accepted</code>")
    _, order_num, new_status = parts
    if new_status not in STATUS_INFO:
        return await msg.answer(f"Неизвестный статус. Допустимые: {', '.join(STATUS_INFO.keys())}")
    order = get_order(order_num)
    if not order:
        return await msg.answer(f"Заказ {order_num} не найден.")
    if order["status"] == new_status:
        return await msg.answer("Статус уже установлен.")
    update_order_status(order_num, new_status)
    label, user_text = STATUS_INFO[new_status]
    await msg.answer(f"Статус заказа {order_num} изменён на {label}")
    try:
        await bot.send_message(order["user_id"], user_text.format(num=order_num))
    except Exception as e:
        log.error("Не удалось уведомить пользователя: %s", e)

# ─── ЗАПУСК ──────────────────────────────────────────────────────────────────
async def main():
    init_db()
    # Установим список команд в интерфейсе бота
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="orders", description="Мои заказы"),
        BotCommand(command="help", description="Помощь"),
    ])
    log.info("SMOKELAB Bot запускается | ADMIN_ID=%s", ADMIN_ID)
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())