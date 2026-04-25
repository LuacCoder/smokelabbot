"""
SMOKELAB Telegram Bot — aiogram 3.x
Хостинг: Render (бесплатный Worker)
Функции: приём заказов из Mini App, управление заказами админом (inline-кнопки),
SQLite, рассылка, отслеживание заказа через /order.
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

# ─── КОНФИГУРАЦИЯ ─────────────────────────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN", "8738864601:AAGvTSRtkU-LBe-b7HREagxhbfo6g0miFXU")
ADMIN_ID     = int(os.getenv("ADMIN_ID", "8160958113"))
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://luaccoder.github.io/smokelabbot/")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан!")

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

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ─── БАЗА ДАННЫХ ──────────────────────────────────────────────────────────
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
            user_message_id INTEGER DEFAULT 0,
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
        INSERT INTO orders (order_num, user_id, username, full_name, items, total, payment, customer, status, user_message_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, datetime('now'))
    """, (
        order_data["order_num"],
        order_data["user_id"],
        order_data.get("username"),
        order_data.get("full_name"),
        json.dumps(order_data["items"], ensure_ascii=False),
        order_data["total"],
        order_data["payment"],
        json.dumps(order_data["customer"], ensure_ascii=False),
        order_data.get("user_message_id", 0),
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
    return [dict(r) for r in rows]

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
    cur.execute("SELECT DISTINCT user_id FROM orders")
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]

# ─── КЛАВИАТУРЫ ──────────────────────────────────────────────────────────
def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛒 Открыть магазин", web_app=WebAppInfo(url=MINI_APP_URL))],
            [KeyboardButton(text="📦 Мои заказы"), KeyboardButton(text="ℹ️ О нас")],
        ],
        resize_keyboard=True,
    )

def kb_admin_order(order_num: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принят", callback_data=f"st:{order_num}:accepted"),
         InlineKeyboardButton(text="🚚 В пути", callback_data=f"st:{order_num}:shipping")],
        [InlineKeyboardButton(text="✔️ Выполнен", callback_data=f"st:{order_num}:done"),
         InlineKeyboardButton(text="❌ Отменён", callback_data=f"st:{order_num}:cancelled")],
    ])

# ─── ВСПОМОГАТЕЛЬНЫЕ ─────────────────────────────────────────────────────
PAY_LABELS = {
    "cash":   "💵 Наличными при получении",
    "card":   "💳 Банковская карта",
    "crypto": "₿ Криптовалюта",
    "pickup": "🏪 Самовывоз",
}

STATUS_INFO = {
    "accepted":  "✅ Принят",
    "shipping":  "🚚 В пути",
    "done":      "✔️ Выполнен",
    "cancelled": "❌ Отменён",
}

def format_items(items: list) -> str:
    return "\n".join(
        f"  • {it['name']} × {it['qty']} = {it['price'] * it['qty']:.2f} BYN"
        for it in items
    )

def user_order_text(order: dict) -> str:
    items_text = format_items(order["items"])
    customer = order["customer"]
    comment_line = f"💬 {customer['comment']}\n" if customer.get("comment") else ""
    payment_label = PAY_LABELS.get(order["payment"], order["payment"])
    status = STATUS_INFO.get(order["status"], "⏳ Ожидает")
    return (
        f"🎉 <b>Заказ {order['order_num']}</b>\n\n"
        f"<b>Состав:</b>\n{items_text}\n\n"
        f"<b>Итого:</b> {order['total']:.2f} BYN\n"
        f"<b>Оплата:</b> {payment_label}\n\n"
        f"<b>Доставка:</b>\n"
        f"👤 {customer.get('name','—')}\n"
        f"📱 {customer.get('phone','—')}\n"
        f"🏠 {customer.get('address','—')}\n"
        f"{comment_line}\n"
        f"<b>Статус:</b> {status}"
    )

# ─── КОМАНДЫ ─────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(msg: Message):
    name = msg.from_user.first_name or "друг"
    await msg.answer(
        f"👋 Привет, <b>{name}</b>!\n\nДобро пожаловать в <b>SMOKELAB</b> — ваш вейп-магазин.\nНажмите кнопку ниже, чтобы открыть каталог 👇",
        reply_markup=kb_main(),
    )

@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        "<b>Помощь</b>\n\n/start — Главное меню\n/orders — История заказов\n/order НОМЕР — Информация о заказе\n/help — Эта справка\nПоддержка: @smokelab_support"
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
        "<b>SMOKELAB</b> 💨\n\nЛучший выбор вейп-продуктов в Беларуси.\n🕐 9:00 – 22:00\n📍 Минск и доставка по РБ\n💬 @smokelab_support"
    )

async def show_orders(msg: Message):
    uid = msg.from_user.id
    user_orders = get_user_orders(uid)
    if not user_orders:
        await msg.answer("У вас пока нет заказов.", reply_markup=kb_main())
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

# ─── ИНФОРМАЦИЯ О ЗАКАЗЕ ─────────────────────────────────────────────────
@dp.message(Command("order"))
async def cmd_order_info(msg: Message):
    parts = msg.text.strip().split()
    if len(parts) != 2:
        return await msg.answer("Укажите номер заказа, например: <code>/order #000001</code>")
    order_num = parts[1]
    order = get_order(order_num)
    if not order:
        return await msg.answer("❌ Заказ не найден.")
    text = user_order_text(order)
    if msg.from_user.id == ADMIN_ID:
        await msg.answer(text, reply_markup=kb_admin_order(order_num))
    else:
        await msg.answer(text)

# ─── ПРИЁМ ЗАКАЗА ────────────────────────────────────────────────────────
@dp.message(F.web_app_data)
async def handle_order(msg: Message):
    try:
        data = json.loads(msg.web_app_data.data)
    except json.JSONDecodeError:
        await msg.answer("⚠️ Ошибка при обработке заказа.")
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

    order_num = get_next_order_number()

    order_data = {
        "order_num": order_num,
        "user_id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "items": items,
        "total": total,
        "payment": payment,
        "customer": customer,
        "created_at": now_str,
        "user_message_id": 0,
    }
    save_order(order_data)

    sent_msg = await msg.answer(user_order_text(get_order(order_num)), reply_markup=kb_main())
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("UPDATE orders SET user_message_id = ? WHERE order_num = ?", (sent_msg.message_id, order_num))
    conn.commit()
    conn.close()

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
                f"{'💬 ' + customer.get('comment', '') if customer.get('comment') else ''}\n"
                f"<b>Товары:</b>\n{format_items(items)}\n\n"
                f"<b>Итого:</b> <code>{total:.2f} BYN</code>\n"
                f"<b>Оплата:</b> {PAY_LABELS.get(payment, payment)}",
                reply_markup=kb_admin_order(order_num),
            )
            log.info(f"Новый заказ {order_num} от пользователя {user.id}")
        except Exception as e:
            log.error("Не удалось уведомить администратора: %s", e)

# ─── СМЕНА СТАТУСА ───────────────────────────────────────────────────────
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

    if order["user_message_id"]:
        try:
            await bot.edit_message_text(
                user_order_text(get_order(order_num)),
                chat_id=order["user_id"],
                message_id=order["user_message_id"]
            )
        except Exception as e:
            log.warning("Не удалось отредактировать сообщение пользователю: %s", e)

    if new_status == "cancelled":
        await call.message.edit_text(
            call.message.html_text + f"\n\n<b>Статус: {STATUS_INFO[new_status]}</b>",
            reply_markup=None
        )
    else:
        await call.message.edit_text(
            call.message.html_text + f"\n\n<b>Статус: {STATUS_INFO[new_status]}</b>",
            reply_markup=call.message.reply_markup
        )

    await call.answer(f"Статус изменён на «{STATUS_INFO[new_status]}»")

# ─── АДМИН-КОМАНДЫ ───────────────────────────────────────────────────────
@dp.message(Command("admin"))
async def cmd_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("⛔ Нет доступа.")
    await msg.answer(
        "🛠️ <b>Панель администратора</b>\n\n"
        "/orders_active - активные заказы\n"
        "/broadcast - рассылка\n"
        "/set_status - изменить статус вручную"
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

@dp.message(Command("broadcast"))
async def cmd_broadcast(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("⛔ Нет доступа.")
    await msg.answer("Введите текст для рассылки (или /cancel для отмены):")
    @dp.message(F.text, F.from_user.id == ADMIN_ID)
    async def get_broadcast_text(m: Message):
        if m.text == "/cancel":
            await m.answer("Рассылка отменена.")
            return
        user_ids = get_all_user_ids()
        success = 0
        for uid in user_ids:
            try:
                await bot.send_message(uid, m.text)
                success += 1
            except:
                pass
        await m.answer(f"Рассылка завершена: {success}/{len(user_ids)} пользователей получили сообщение.")

@dp.message(Command("set_status"))
async def cmd_set_status(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("⛔ Нет доступа.")
    parts = msg.text.strip().split()
    if len(parts) != 3:
        return await msg.answer("Формат: <code>/set_status #000001 accepted</code>")
    _, order_num, new_status = parts
    if new_status not in STATUS_INFO:
        return await msg.answer(f"Неизвестный статус. Допустимые: {', '.join(STATUS_INFO.keys())}")
    order = get_order(order_num)
    if not order:
        return await msg.answer(f"Заказ {order_num} не найден.")
    if order["status"] == new_status:
        return await msg.answer("Статус уже установлен.")
    update_order_status(order_num, new_status)
    if order["user_message_id"]:
        try:
            await bot.edit_message_text(
                user_order_text(get_order(order_num)),
                chat_id=order["user_id"],
                message_id=order["user_message_id"]
            )
        except Exception as e:
            log.error("Не удалось отредактировать сообщение пользователю: %s", e)
    await msg.answer(f"Статус заказа {order_num} изменён на {STATUS_INFO[new_status]}")

# ─── ЗАПУСК ──────────────────────────────────────────────────────────────
async def main():
    init_db()
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
