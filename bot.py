"""
SMOKELAB Telegram Bot — aiogram 3.x
Хостинг: Render (бесплатный Worker)
Функции: приём заказов, рассылка, управление статусами, SQLite.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
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

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN", "8738864601:AAGvTSRtkU-LBe-b7HREagxhbfo6g0miFXU")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8160958113"))
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://luaccoder.github.io/smokelabbot/")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан!")

BASE_DIR = Path(__file__).parent
DB_FILE = BASE_DIR / "orders.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(BASE_DIR / "bot.log", encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# --- База данных ---
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
            delivery_cost REAL DEFAULT 0,
            payment TEXT NOT NULL,
            customer TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            user_message_id INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            last_active TEXT
        )
    """)
    conn.commit()
    conn.close()

def add_user(user):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO users (user_id, first_name, username, last_active)
        VALUES (?, ?, ?, datetime('now'))
    """, (user.id, user.first_name, user.username))
    conn.commit()
    conn.close()

def save_order(order_data: dict):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO orders (order_num, user_id, username, full_name, items, total, delivery_cost, payment, customer, status, user_message_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, datetime('now'))
    """, (
        order_data["order_num"], order_data["user_id"], order_data.get("username"),
        order_data.get("full_name"), json.dumps(order_data["items"], ensure_ascii=False),
        order_data["total"], order_data.get("delivery_cost", 0), order_data["payment"],
        json.dumps(order_data["customer"], ensure_ascii=False), order_data.get("user_message_id", 0),
        order_data["created_at"],
    ))
    conn.commit()
    conn.close()

def get_order(order_num: str):
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

def get_user_orders(user_id: int, limit=10):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def update_order_status(order_num, new_status):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("UPDATE orders SET status = ?, updated_at = datetime('now') WHERE order_num = ?", (new_status, order_num))
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

def get_next_order_number():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM orders")
    count = cur.fetchone()[0]
    conn.close()
    return f"#{count + 1:06d}"

def get_all_user_ids():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]

# --- Клавиатуры ---
def kb_main():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛒 Открыть магазин", web_app=WebAppInfo(url=MINI_APP_URL))],
            [KeyboardButton(text="📦 Мои заказы"), KeyboardButton(text="ℹ️ О нас")],
        ],
        resize_keyboard=True,
    )

def kb_admin_order(order_num):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принят", callback_data=f"st:{order_num}:accepted"),
         InlineKeyboardButton(text="🚚 В пути", callback_data=f"st:{order_num}:shipping")],
        [InlineKeyboardButton(text="✔️ Выполнен", callback_data=f"st:{order_num}:done"),
         InlineKeyboardButton(text="❌ Отменён", callback_data=f"st:{order_num}:cancelled")],
    ])

# --- Текст заказа ---
PAY_LABELS = {
    "cash": "💵 Наличными при получении",
    "card": "💳 Банковская карта",
    "crypto": "₿ Криптовалюта",
    "pickup": "🏪 Самовывоз",
}

STATUS_INFO = {
    "accepted": "✅ Принят",
    "shipping": "🚚 В пути",
    "done": "✔️ Выполнен",
    "cancelled": "❌ Отменён",
}

def format_items(items):
    return "\n".join(f"  • {it['name']} × {it['qty']} = {it['price'] * it['qty']:.2f} BYN" for it in items)

def build_address_link(address: str) -> str:
    encoded = address.replace(" ", "%20")
    return f'<a href="https://yandex.ru/maps/?text={encoded}">{address}</a>'

def user_order_text(order):
    items_text = format_items(order["items"])
    customer = order["customer"]
    delivery_cost = order.get("delivery_cost", 0)
    total = order["total"] + delivery_cost
    status = STATUS_INFO.get(order["status"], "⏳ Ожидает")
    payment_label = PAY_LABELS.get(order["payment"], order["payment"])
    comment_line = f"💬 {customer['comment']}\n" if customer.get("comment") else ""
    addr_str = build_address_link(customer.get("address", "—"))
    return (
        f"🎉 <b>Заказ {order['order_num']}</b>\n\n"
        f"<b>Состав:</b>\n{items_text}\n\n"
        f"<b>Товары:</b> {order['total']:.2f} BYN\n"
        f"<b>Доставка:</b> {delivery_cost:.2f} BYN\n"
        f"<b>Итого:</b> {total:.2f} BYN\n"
        f"<b>Оплата:</b> {payment_label}\n\n"
        f"<b>Доставка:</b>\n"
        f"👤 {customer.get('name','—')}\n"
        f"📱 {customer.get('phone','—')}\n"
        f"🏠 {addr_str}\n"
        f"{comment_line}\n"
        f"<b>Статус:</b> {status}"
    )

def admin_notification_text(order):
    items_text = format_items(order["items"])
    customer = order["customer"]
    delivery_cost = order.get("delivery_cost", 0)
    total = order["total"] + delivery_cost
    payment_label = PAY_LABELS.get(order["payment"], order["payment"])
    addr_str = build_address_link(customer.get("address", "—"))
    return (
        f"🔔 <b>НОВЫЙ ЗАКАЗ {order['order_num']}</b>\n"
        f"🕐 {order['created_at']}\n\n"
        f"<b>Клиент:</b> {order['full_name']} (ID: {order['user_id']})\n"
        f"👤 {customer.get('name','—')}\n"
        f"📱 {customer.get('phone','—')}\n"
        f"🏠 {addr_str}\n"
        f"<b>Товары:</b>\n{items_text}\n"
        f"<b>Товары:</b> {order['total']:.2f} BYN\n"
        f"<b>Доставка:</b> {delivery_cost:.2f} BYN\n"
        f"<b>Итого:</b> {total:.2f} BYN\n"
        f"<b>Оплата:</b> {payment_label}"
    )

# --- Команды ---
@dp.message(CommandStart())
async def cmd_start(msg: Message):
    add_user(msg.from_user)
    name = msg.from_user.first_name or "друг"
    await msg.answer(f"👋 Привет, <b>{name}</b>!\n\nДобро пожаловать в <b>SMOKELAB</b> — ваш вейп-магазин в Витебске.\nНажмите кнопку ниже, чтобы открыть каталог 👇", reply_markup=kb_main())

@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer("<b>Помощь</b>\n\n/start — Главное меню\n/orders — История заказов\n/order НОМЕР — Информация о заказе\n/help — Эта справка\nПоддержка: @smokelab_support")

@dp.message(Command("orders"))
@dp.message(F.text == "📦 Мои заказы")
async def show_orders(msg: Message):
    uid = msg.from_user.id
    orders = get_user_orders(uid)
    if not orders:
        await msg.answer("У вас пока нет заказов.", reply_markup=kb_main())
        return
    icons = {"pending":"⏳","accepted":"✅","shipping":"🚚","done":"✔️","cancelled":"❌"}
    text = "<b>Ваши заказы:</b>\n\n"
    for o in orders[:10]:
        total = o["total"] + o.get("delivery_cost", 0)
        text += f"{icons.get(o['status'],'⏳')} <b>{o['order_num']}</b>\n   {total:.2f} BYN · {PAY_LABELS.get(o['payment'], o['payment'])}\n   {o['created_at']}\n\n"
    await msg.answer(text)

@dp.message(F.text == "ℹ️ О нас")
async def btn_about(msg: Message):
    await msg.answer("<b>SMOKELAB</b> 💨\n\nЛучший выбор вейп-продуктов в Беларуси.\n📍 г. Витебск, ул. Генерала Ивановского, 34\n🕐 9:00 – 22:00\n💬 @smokelab_support")

# --- Обработка данных из Mini App ---
@dp.message(F.web_app_data)
async def handle_web_app_data(msg: Message):
    try:
        data = json.loads(msg.web_app_data.data)
    except json.JSONDecodeError:
        await msg.answer("⚠️ Ошибка при обработке данных.")
        return

    if data.get("type") == "order":
        await process_order(msg, data)
    elif data.get("type") == "broadcast":
        await process_broadcast(msg, data)

async def process_order(msg: Message, data: dict):
    items = data.get("items", [])
    if not items:
        await msg.answer("❌ Корзина пуста.")
        return

    total = float(data.get("total", 0))
    payment = data.get("payment", "cash")
    customer = data.get("customer", {})
    user = msg.from_user
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")

    add_user(user)

    order_num = get_next_order_number()
    order_data = {
        "order_num": order_num,
        "user_id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "items": items,
        "total": total,
        "delivery_cost": float(data.get("deliveryCost", 0)),
        "payment": payment,
        "customer": customer,
        "created_at": now_str,
        "user_message_id": 0,
    }
    save_order(order_data)

    order = get_order(order_num)
    await msg.answer(user_order_text(order), reply_markup=kb_main())

    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                admin_notification_text(order),
                reply_markup=kb_admin_order(order_num),
            )
            log.info(f"Новый заказ {order_num} от пользователя {user.id}")
        except Exception as e:
            log.error("Не удалось уведомить администратора: %s", e)

async def process_broadcast(msg: Message, data: dict):
    if msg.from_user.id != ADMIN_ID:
        return
    text = data.get("text", "")
    if not text:
        await bot.send_message(ADMIN_ID, "Текст рассылки пуст.")
        return
    user_ids = get_all_user_ids()
    if not user_ids:
        await bot.send_message(ADMIN_ID, "Нет пользователей для рассылки.")
        return
    success = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text)
            success += 1
        except Exception as e:
            log.warning(f"Не удалось отправить сообщение пользователю {uid}: {e}")
    await bot.send_message(ADMIN_ID, f"Рассылка завершена: {success}/{len(user_ids)} пользователей получили сообщение.")

# --- Смена статуса ---
@dp.callback_query(F.data.startswith("st:"))
async def handle_status(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    _, order_num, new_status = call.data.split(":")
    order = get_order(order_num)
    if not order:
        await call.answer("Заказ не найден.", show_alert=True)
        return
    if order["status"] == new_status:
        await call.answer("Статус уже установлен")
        return
    update_order_status(order_num, new_status)
    updated_order = get_order(order_num)

    # Всегда отправляем новое сообщение пользователю
    try:
        await bot.send_message(updated_order["user_id"], user_order_text(updated_order))
    except Exception as e:
        log.error("Не удалось уведомить пользователя: %s", e)

    # Обновляем сообщение админу
    final_statuses = ["done", "cancelled"]
    reply_markup = None if new_status in final_statuses else call.message.reply_markup
    await call.message.edit_text(
        call.message.html_text + f"\n\n<b>Статус изменён на:</b> {STATUS_INFO[new_status]}",
        reply_markup=reply_markup
    )
    await call.answer(f"Статус изменён на «{STATUS_INFO[new_status]}»")

# --- Админ-команды ---
@dp.message(Command("admin"))
async def cmd_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("⛔ Нет доступа.")
    await msg.answer("🛠️ Панель администратора\n/orders_active - активные заказы\n/broadcast - рассылка\n/set_status - изменить статус")

@dp.message(Command("broadcast"))
async def cmd_broadcast(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer("Введите текст для рассылки (или /cancel):")
    @dp.message(F.text, F.from_user.id == ADMIN_ID)
    async def get_text(m: Message):
        if m.text == "/cancel":
            await m.answer("Отменено.")
            return
        user_ids = get_all_user_ids()
        if not user_ids:
            await m.answer("Нет пользователей.")
            return
        ok = 0
        for uid in user_ids:
            try:
                await bot.send_message(uid, m.text)
                ok += 1
            except:
                pass
        await m.answer(f"Рассылка завершена: {ok}/{len(user_ids)} пользователей.")

@dp.message(Command("set_status"))
async def cmd_set_status(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    parts = msg.text.strip().split()
    if len(parts) != 3:
        return await msg.answer("Формат: /set_status #000001 accepted")
    _, order_num, status = parts
    if status not in STATUS_INFO:
        return await msg.answer("Неверный статус.")
    order = get_order(order_num)
    if not order:
        return await msg.answer("Заказ не найден.")
    update_order_status(order_num, status)
    await msg.answer(f"Статус заказа {order_num} изменён на {STATUS_INFO[status]}")

# --- Запуск ---
async def main():
    init_db()
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="orders", description="Мои заказы"),
        BotCommand(command="help", description="Помощь"),
    ])
    log.info("SMOKELAB Bot запускается | ADMIN_ID=%s", ADMIN_ID)
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    time.sleep(2)  # небольшая задержка для корректного перезапуска на Render
    asyncio.run(main())
