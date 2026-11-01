import asyncio
import html
import os
import re
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.token import TokenValidationError
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PROXY = os.getenv("PROXY", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
if ADMIN_ID_RAW and not ADMIN_ID_RAW.isdigit():
    raise SystemExit(
        f"ADMIN_ID должен быть числом — без букв, кавычек и пробелов. Сейчас в .env: {ADMIN_ID_RAW!r}"
    )
ADMIN_ID = int(ADMIN_ID_RAW or 0)

# на хостинге (Bothost и похожие) постоянным обычно является только отдельный каталог,
# который приходит переменной DATA_DIR; локально база как и раньше лежит рядом с bot.py
DATA_DIR = os.getenv("DATA_DIR", "").strip()
if DATA_DIR:
    os.makedirs(DATA_DIR, exist_ok=True)
    DB_FILE = os.path.join(DATA_DIR, "orders.db")
else:
    DB_FILE = os.path.join(HERE, "orders.db")

PENDING = "pending"
NEED_PRICE = "need_price"
SENT = "sent"
NEGOTIATING = "negotiating"
OFFERED = "offered"
ACCEPTED = "accepted"
DONE = "done"
REJECTED = "rejected"
DECLINED = "declined"
CANCELLED = "cancelled"

STATUS_RU = {
    PENDING: "ожидает оценки",
    NEED_PRICE: "принят, ждет цены",
    SENT: "ждет ответа клиента",
    NEGOTIATING: "клиент торгуется",
    OFFERED: "клиент предложил цену",
    ACCEPTED: "согласован, в работе",
    DONE: "выполнен",
    REJECTED: "отклонен мастером",
    DECLINED: "клиента не устроила цена",
    CANCELLED: "отменен клиентом",
}

OPEN_STATUSES = (PENDING, NEED_PRICE, SENT, NEGOTIATING, OFFERED, ACCEPTED)

# кнопки мастера и клиента: по ним проверяем, кому разрешено нажимать
ADMIN_ACTIONS = {"acc", "rej", "fin", "okp", "nop", "take", "menu", "card", "blk", "unblk"}
CLIENT_ACTIONS = {"yes", "no", "bid", "bidno"}

# разделы меню мастера: подпись и какие статусы в них лежат
MENU_SECTIONS = {
    "new": ("📥 Новые", (PENDING,)),
    "price": ("💬 Ждут цену", (NEED_PRICE,)),
    "wait": ("📨 Ждут ответа клиента", (SENT, NEGOTIATING)),
    "offer": ("🤔 Предложили цену", (OFFERED,)),
    "work": ("🎉 Согласованы, в работе", (ACCEPTED,)),
    "done": ("✅ Выполнены", (DONE,)),
}
MENU_LIMIT = 8

SQL_CREATE = """
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    user_name    TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    description  TEXT    NOT NULL,
    price        TEXT,
    client_price TEXT,
    attach_type  TEXT,
    attach_id    TEXT,
    file_name    TEXT,
    created_at   TEXT    NOT NULL,
    await_offer  INTEGER NOT NULL DEFAULT 0,
    await_price  INTEGER NOT NULL DEFAULT 0,
    await_budget INTEGER NOT NULL DEFAULT 0
)
"""

SQL_CREATE_BLOCKED = """
CREATE TABLE IF NOT EXISTS blocked (
    user_id    INTEGER PRIMARY KEY,
    user_name  TEXT    NOT NULL,
    reason     TEXT,
    blocked_at TEXT    NOT NULL
)
"""

router = Router()


# ---------------- база ----------------


def init_db():
    con = sqlite3.connect(DB_FILE)
    con.execute(SQL_CREATE)
    con.execute(SQL_CREATE_BLOCKED)
    # старые базы были без этих колонок — догоняем на месте, данные не трогаем
    cols = {row[1] for row in con.execute("PRAGMA table_info(orders)")}
    if "client_price" not in cols:
        con.execute("ALTER TABLE orders ADD COLUMN client_price TEXT")
    if "await_budget" not in cols:
        con.execute("ALTER TABLE orders ADD COLUMN await_budget INTEGER NOT NULL DEFAULT 0")
    con.commit()
    con.close()


def run(sql, args=()):
    con = sqlite3.connect(DB_FILE)
    cur = con.execute(sql, args)
    con.commit()
    last_id = cur.lastrowid
    con.close()
    return last_id


def rows(sql, args=()):
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    found = [dict(r) for r in con.execute(sql, args).fetchall()]
    con.close()
    return found


def one(sql, args=()):
    found = rows(sql, args)
    return found[0] if found else None


def add_order(user_id, user_name, description):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return run(
        "INSERT INTO orders (user_id, user_name, status, description, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (user_id, user_name, PENDING, description, now),
    )


def get_order(order_id):
    return one("SELECT * FROM orders WHERE id = ?", (order_id,))


def set_status(order_id, status):
    run("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))


def set_price(order_id, price):
    run("UPDATE orders SET price = ?, await_price = 0 WHERE id = ?", (price, order_id))


def set_offer_wait(order_id, value):
    run("UPDATE orders SET await_offer = ? WHERE id = ?", (value, order_id))


def set_budget_wait(order_id, value):
    run("UPDATE orders SET await_budget = ? WHERE id = ?", (value, order_id))


def set_client_price(order_id, price):
    run("UPDATE orders SET client_price = ?, await_budget = 0 WHERE id = ?", (price, order_id))


def set_price_wait(order_id):
    run("UPDATE orders SET await_price = 1 WHERE id = ?", (order_id,))


def focus_price(order_id):
    """Цена, которую мастер напишет следующим, уйдёт именно в этот заказ."""
    run("UPDATE orders SET await_price = 0")
    run("UPDATE orders SET await_price = 1 WHERE id = ?", (order_id,))


def clear_flags(order_id):
    run("UPDATE orders SET await_price = 0, await_offer = 0, await_budget = 0 WHERE id = ?", (order_id,))


def set_attach(order_id, attach_type, attach_id, file_name):
    run(
        "UPDATE orders SET attach_type = ?, attach_id = ?, file_name = ? WHERE id = ?",
        (attach_type, attach_id, file_name, order_id),
    )


def last_order_of_user(user_id):
    return one("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))


def orders_of_user(user_id):
    return rows("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 10", (user_id,))


def orders_in_statuses(*statuses):
    marks = ",".join("?" for _ in statuses)
    return rows(f"SELECT * FROM orders WHERE status IN ({marks}) ORDER BY id", statuses)


def orders_waiting_price():
    return rows("SELECT * FROM orders WHERE await_price = 1 ORDER BY id")


def order_waiting_offer(user_id):
    return one(
        "SELECT * FROM orders WHERE user_id = ? AND await_offer = 1 ORDER BY id DESC LIMIT 1",
        (user_id,),
    )


def order_waiting_budget(user_id):
    return one(
        "SELECT * FROM orders WHERE user_id = ? AND await_budget = 1 ORDER BY id DESC LIMIT 1",
        (user_id,),
    )


# ---------------- чёрный список ----------------


def is_blocked(user_id):
    return one("SELECT 1 FROM blocked WHERE user_id = ?", (user_id,)) is not None


def block_user(user_id, user_name, reason=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    run(
        "INSERT OR REPLACE INTO blocked (user_id, user_name, reason, blocked_at) VALUES (?, ?, ?, ?)",
        (user_id, user_name, reason, now),
    )


def unblock_user(user_id):
    """True, если пользователь там и был — иначе /unblock просто ничего не меняет."""
    was = is_blocked(user_id)
    run("DELETE FROM blocked WHERE user_id = ?", (user_id,))
    return was


def blocked_list():
    return rows("SELECT * FROM blocked ORDER BY blocked_at DESC, user_id LIMIT 20")


# ---------------- текст сообщений ----------------


def esc(text):
    return html.escape(text or "", quote=False)


def kb(*buttons):
    row = [InlineKeyboardButton(text=text, callback_data=data) for text, data in buttons]
    return InlineKeyboardMarkup(inline_keyboard=[row])


def kbm(*rows_of_buttons):
    """Клавиатура из нескольких рядов: kbm(("✅", "acc:1"), ("❌", "rej:1"), ...)"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows_of_buttons
            if row
        ]
    )


def order_text(order):
    text = (
        f"<b>Заказ #{order['id']}</b> от {esc(order['user_name'])}\n"
        f"{esc(order['description'])}\n\n"
        f"<i>Прислан: {order['created_at']} · статус: {STATUS_RU[order['status']]}</i>"
    )
    if order["client_price"]:
        text += f"\n💰 Клиент сам предложил: <b>{esc(order['client_price'])}</b>"
    if order["price"]:
        text += f"\n💶 Цена: <b>{esc(order['price'])}</b>"
    return text


def order_line(order, for_client=False):
    line = f"#{order['id']} ({STATUS_RU[order['status']]})"
    if order["price"]:
        line += f", цена: {esc(order['price'])}"
    if order["client_price"]:
        line += f", {('твоя цена' if for_client else 'клиент предложил')}: {esc(order['client_price'])}"
    line += f"\n{esc(order['description'][:120])}"
    return line


def short_label(order):
    """Подпись кнопки в списке меню: номер заказа и начало описания в одну строку."""
    brief = re.sub(r"\s+", " ", order["description"]).strip()[:22]
    return f"#{order['id']} {brief}".strip()


def parse_price(text):
    found = re.search(r"\d[\d\s\u00a0.,]*", text or "")
    if not found:
        return None
    value = re.sub(r"[^\d.,]", "", found.group()).replace(",", ".")
    while ".." in value:
        value = value.replace("..", ".")
    value = value.strip(".")
    if value.count(".") > 1:
        first = value.index(".")
        value = value[: first + 1] + value[first + 1 :].replace(".", "")
    return value or None


def parse_only_price(text):
    """Сообщение = одна цена: «2000», «2 000», «1500,50», «3500 руб». Иначе None."""
    clean = (text or "").replace("\u00a0", " ").strip().lower()
    if not re.fullmatch(r"[0-9][0-9\s.,]*(\s*(руб|рублей|руб\.*|rub|₽|грн|тг|сум|₸|₼))?", clean):
        return None
    return parse_price(clean)


def display_name(message: Message):
    user = message.from_user
    name = user.full_name or user.username or str(user.id)
    if user.username:
        return f"{name} (@{user.username})"
    return name


async def to_admin(bot: Bot, text, keyboard=None):
    await bot.send_message(ADMIN_ID, text, reply_markup=keyboard)


async def send_order_to(bot: Bot, chat_id, order, text, keyboard=None):
    if order["attach_type"] == "photo":
        await bot.send_photo(chat_id, order["attach_id"], caption=text, reply_markup=keyboard)
    elif order["attach_type"] == "document":
        await bot.send_document(chat_id, order["attach_id"], caption=text, reply_markup=keyboard)
    else:
        await bot.send_message(chat_id, text, reply_markup=keyboard)


async def remove_buttons(message: Message):
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:
        # сообщение мог удалить клиент или оно слишком старое — не критично
        pass


# ---------------- меню мастера ----------------


SECTION_HINT = {
    "new": "✅ — принять и назначить цену, ❌ — отклонить, 🚫 — в чёрный список.",
    "price": "Напиши цену числом (например 1500) — она уйдёт клиенту. "
             "Если заказов несколько: /price 12 1500 или нажми на номер заказа.",
    "wait": "Ждём ответа клиента: он сам нажмёт «Согласен» или предложит свою цену.",
    "offer": "Клиент назвал свою цену. ✅ — согласиться, ❌ — отказать.",
    "work": "Согласовано. Нажми «✔️ Готово», когда выполнишь заказ.",
    "done": "Выполненные заказы. Нажми на номер — откроется карточка.",
}


def order_row(order, section):
    """Строка меню: номер с описанием (это карточка) + кнопки того статуса, в котором заказ."""
    oid = order["id"]
    tag = f":{section}" if section else ""
    row = [(short_label(order), f"card:{oid}{tag}")]
    status = order["status"]
    if status == PENDING:
        row += [("✅", f"acc:{oid}{tag}"), ("❌", f"rej:{oid}{tag}")]
    elif status == NEED_PRICE and order["client_price"]:
        row.append((f"💰 {order['client_price']}", f"take:{oid}{tag}"))
    elif status == OFFERED:
        row += [("✅", f"okp:{oid}{tag}"), ("❌", f"nop:{oid}{tag}")]
    elif status == ACCEPTED:
        row.append(("✔️ Готово", f"fin:{oid}{tag}"))
    if status not in (DONE, REJECTED, DECLINED, CANCELLED):
        row.append(("🚫", f"blk:{oid}{tag}"))
    return tuple(row)


def menu_nav(section):
    return (("🏠 Меню", "menu:home"), ("🔄 Обновить", f"menu:{section}"))


def menu_home():
    def n(*statuses):
        return len(orders_in_statuses(*statuses))

    text = (
        "🛠 <b>Меню мастера</b>\n\n"
        f"📥 Новые: {n(PENDING)} · 💬 Ждут цену: {n(NEED_PRICE)} · 📨 Ждут ответа: {n(SENT, NEGOTIATING)}\n"
        f"🤔 Своя цена: {n(OFFERED)} · 🎉 В работе: {n(ACCEPTED)} · ✅ Готово: {n(DONE)}\n"
        f"🚫 В чёрном списке: {len(blocked_list())}"
    )
    markup = kbm(
        (("📥 Новые", "menu:new"), ("💬 Ждут цену", "menu:price")),
        (("📨 Ждут ответа", "menu:wait"), ("🤔 Торгуются", "menu:offer")),
        (("🎉 В работе", "menu:work"), ("✅ Выполнены", "menu:done")),
        (("🚫 Блок-лист", "menu:blocked"), ("🔄 Обновить", "menu:home")),
    )
    return text, markup


def menu_section(section):
    title, statuses = MENU_SECTIONS[section]
    found = orders_in_statuses(*statuses)
    shown = found[:MENU_LIMIT]
    lines = [f"🛠 <b>{title}</b>", SECTION_HINT[section]]
    if not shown:
        lines.append("\nПусто.")
    else:
        lines.append("")
        for order in shown:
            block = " 🚫 в чёрном списке" if is_blocked(order["user_id"]) else ""
            price = order["price"] or order["client_price"]
            summa = f" · {esc(price)}" if price else ""
            lines.append(f"• #{order['id']} {esc(order['user_name'][:40])}{summa}{block}")
        if len(found) > len(shown):
            lines.append(f"\nПоказаны {len(shown)} из {len(found)}.")
    rows = [order_row(order, section) for order in shown]
    rows.append(menu_nav(section))
    return "\n".join(lines), kbm(*rows)


def menu_blocked():
    found = blocked_list()
    lines = ["🚫 <b>Чёрный список</b>"]
    if not found:
        lines.append("\nНикого нет. Кнопка 🚫 в карточке заказа заносит клиента сюда.")
    else:
        lines.append(
            "\nЗаказы этих людей бот не принимает и мастеру не пересылает.\n"
            "Нажми на строку, чтобы разблокировать:"
        )
        for user in found:
            reason = f" — {esc(user['reason'][:80])}" if user["reason"] else ""
            lines.append(f"• {esc(user['user_name'][:40])} ({user['user_id']}){reason}")
    rows = [[(f"↩️ {user['user_name'][:24]} · {user['user_id']}", f"unblk:{user['user_id']}")] for user in found]
    rows.append(list(menu_nav("blocked")))
    return "\n".join(lines), kbm(*rows)


def build_menu(section):
    if section == "blocked":
        return menu_blocked()
    if section in MENU_SECTIONS:
        return menu_section(section)
    return menu_home()


async def render_menu(message: Message, bot: Bot, section):
    if message is None:
        return
    text, markup = build_menu(section)
    try:
        await message.edit_text(text, reply_markup=markup)
    except Exception:
        # Telegram не даёт редактировать то, и без того уже показывает эти кнопки
        pass


async def refresh_after(callback: CallbackQuery, bot: Bot, section):
    """Кнопка из меню — перерисовать раздел; кнопка из отдельного сообщения — убрать её."""
    if section:
        await render_menu(callback.message, bot, section)
    else:
        await remove_buttons(callback.message)


# ---------------- доступ ----------------


BLOCK_TEXT = (
    "Тебя внесли в чёрный список этого бота: заказы не принимаются.\n"
    "Если это ошибка — напиши мастеру напрямую."
)


async def denied(message: Message) -> bool:
    """True, если пишет занесённый в чёрный список: уже ответили, дальше не пускаем."""
    if not is_blocked(message.from_user.id):
        return False
    await message.answer(BLOCK_TEXT)
    return True


# ---------------- команды клиента ----------------


@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot):
    if await denied(message):
        return
    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "<b>Ты мастер этого бота.</b>\n\n"
            "Новые заказы приходят сюда текстом или с фото — под ними кнопки «Принять» / «Отклонить».\n"
            "• /menu — меню со всеми заказами по разделам\n"
            "• /orders — активные заказы\n"
            "• /price 12 1500 — цена для заказа №12\n"
            "• /block 12345678 — внести в чёрный список, /unblock 12345678 — убрать\n\n"
            f"<code>ADMIN_ID = {ADMIN_ID}</code>"
        )
        return
    active = last_order_of_user(message.from_user.id)
    hint = ""
    if active and active["status"] in OPEN_STATUSES:
        hint = f"\n\nУ тебя открытый заказ #{active['id']} — {STATUS_RU[active['status']]}."
    await message.answer(
        "Привет! Опиши заказ обычным сообщением — я передам мастеру, он назовёт цену.\n"
        "Свою цену можно предложить сразу: кнопка «💰 Моя цена» появится после заказа."
        f"{hint}\n\n"
        "/my — твои заказы, /cancel — отменить последний.\n"
        f"<i>Твой ID: {message.from_user.id}</i>"
    )


@router.message(Command("help"))
async def cmd_help(message: Message, bot: Bot):
    if await denied(message):
        return
    await message.answer(
        "Напиши обычным сообщением, что нужно сделать, — это и есть заказ.\n"
        "Сразу после заказа бот спросит, хочешь ли ты предложить свою цену: кнопка «💰 Моя цена» "
        "и сумма одним числом. Мастер увидит её в карточке.\n"
        "Фото или файл можно прислать следующим сообщением, он прикрепится к заказу.\n"
        "/my — статусы твоих заказов\n"
        "/cancel — отменить последний заказ\n"
        "Когда мастер пришлёт цену, нажми «Согласен» или «Дорого». "
        "После «Дорого» тоже можно написать свою цену — мастер увидит её."
    )


@router.message(Command("my"))
async def cmd_my(message: Message, bot: Bot):
    if await denied(message):
        return
    found = orders_of_user(message.from_user.id)
    if not found:
        await message.answer("Заказов пока нет. Опиши, что нужно сделать, обычным сообщением.")
        return
    await message.answer("Твои заказы:\n\n" + "\n".join(order_line(o, True) for o in found))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, bot: Bot):
    if await denied(message):
        return
    order = last_order_of_user(message.from_user.id)
    if not order:
        await message.answer("Отменять нечего.")
        return
    if order["status"] not in (PENDING, SENT, NEGOTIATING):
        await message.answer(f"Заказ #{order['id']} уже {STATUS_RU[order['status']]} — отменить нельзя.")
        return
    set_status(order["id"], CANCELLED)
    clear_flags(order["id"])
    await message.answer(f"Заказ #{order['id']} отменён.")
    await to_admin(
        bot,
        f"🚫 Клиент {esc(order['user_name'])} отменил заказ #{order['id']}.\n"
        f"«{esc(order['description'][:200])}»",
    )


# ---------------- команды мастера ----------------


@router.message(Command("menu"))
async def cmd_menu(message: Message, bot: Bot):
    if await denied(message):
        return
    if message.from_user.id != ADMIN_ID:
        await message.answer("Меню мастера. Тебе тут нечего решать — опиши заказ обычным сообщением.")
        return
    text, markup = build_menu("home")
    await message.answer(text, reply_markup=markup)


@router.message(Command("block"))
async def cmd_block(message: Message, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Чёрный список ведёт мастер.")
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: /block 12345678 причина (причину можно не писать).\n"
                            "Проще нажать 🚫 под заказом этого человека.")
        return
    user_id = int(parts[1])
    if user_id == ADMIN_ID:
        await message.answer("Себя блокировать нельзя.")
        return
    reason = parts[2].strip() if len(parts) > 2 else None
    if is_blocked(user_id):
        await message.answer(f"{user_id} уже в чёрном списке.")
        return
    order = one("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))
    name = order["user_name"] if order else f"ID {user_id}"
    block_user(user_id, name, reason)
    await message.answer(f"🚫 {esc(name)} внесён в чёрный список. Разблокировать: /menu → Блок-лист")


@router.message(Command("unblock"))
async def cmd_unblock(message: Message, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Чёрный список ведёт мастер.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: /unblock 12345678")
        return
    user_id = int(parts[1])
    if unblock_user(user_id):
        await message.answer(f"{user_id} больше не в чёрном списке — может писать заказы.")
    else:
        await message.answer(f"{user_id} и не был в чёрном списке.")


@router.message(Command("orders"))
async def cmd_orders(message: Message, bot: Bot):
    if await denied(message):
        return
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда только для мастера.")
        return
    active = orders_in_statuses(*OPEN_STATUSES)
    if not active:
        await message.answer("Активных заказов нет.")
        return
    await message.answer("Активные заказы:\n\n" + "\n\n".join(order_line(o) for o in active))


@router.message(Command("price"))
async def cmd_price(message: Message, bot: Bot):
    if await denied(message):
        return
    if message.from_user.id != ADMIN_ID:
        await message.answer("Цену назначает только мастер.")
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Формат: /price 12 1500")
        return
    try:
        order_id = int(parts[1])
    except ValueError:
        await message.answer("Номер заказа — целое число. Пример: /price 12 1500")
        return
    order = get_order(order_id)
    if not order:
        await message.answer(f"Заказа #{order_id} нет.")
        return
    if order["status"] not in (PENDING, NEED_PRICE, OFFERED):
        await message.answer(f"Заказ #{order['id']} уже {STATUS_RU[order['status']]} — цену менять нельзя.")
        return
    await send_price_to_client(bot, order, " ".join(parts[2:]))


async def send_price_to_client(bot: Bot, order, price):
    set_price(order["id"], price)
    set_status(order["id"], SENT)
    note = ""
    if order["client_price"] and order["client_price"] != price:
        note = f"Ты предлагал {esc(order['client_price'])}.\n\n"
    await bot.send_message(
        order["user_id"],
        f"Мастер оценил заказ #{order['id']} в <b>{esc(price)}</b>.\n"
        f"«{esc(order['description'][:200])}»\n\n"
        f"{note}Согласны? Нажмите кнопку ниже.",
        reply_markup=kb(("👍 Согласен", f"yes:{order['id']}"), ("👎 Дорого", f"no:{order['id']}")),
    )
    await to_admin(bot, f"💶 Заказ #{order['id']}: отправил клиенту цену {esc(price)}. Жду его ответа.")


# ---------------- обычные сообщения ----------------


@router.message()
async def handle_message(message: Message, bot: Bot):
    if await denied(message):
        return
    user_id = message.from_user.id
    is_admin = user_id == ADMIN_ID
    text = (message.text or "").strip()
    photo = message.photo[-1] if message.photo else None
    document = message.document
    attach_type = "photo" if photo else ("document" if document else None)
    attach_id = photo.file_id if photo else (document.file_id if document else None)
    file_name = document.file_name if document else None

    if text.startswith("/"):
        await message.answer("Такой команды нет. Опиши заказ обычным сообщением, либо /help.")
        return

    if not text and not message.caption and not attach_type:
        await message.answer("Пустое сообщение. Опиши заказ текстом.")
        return

    # фото/файл без подписи = дополнение к последнему заказу клиента
    if attach_type and not message.caption:
        if is_admin:
            await message.answer("Файлы от клиента приходят мне автоматически. Опиши заказ текстом.")
            return
        order = last_order_of_user(user_id)
        if not order or order["status"] not in (PENDING, NEED_PRICE, SENT, NEGOTIATING, OFFERED):
            await message.answer("Сначала опиши заказ текстом, потом пришли фото или файл.")
            return
        set_attach(order["id"], attach_type, attach_id, file_name)
        order = get_order(order["id"])
        note = f"📎 Дополнение к заказу #{order['id']} от {esc(order['user_name'])}:"
        await send_order_to(bot, ADMIN_ID, order, note)
        await message.answer(f"Прикрепил это к заказу #{order['id']} и отправил мастеру.")
        return

    description = (message.caption or text).strip()

    if is_admin:
        await master_free_text(message, bot, description)
        return

    # клиент отвечает ценой после кнопки «Дорого»
    waiting = order_waiting_offer(user_id)
    if waiting:
        offer = parse_price(description)
        if not offer:
            await message.answer("Напишите вашу цену числом, например: 2000")
            return
        set_offer_wait(waiting["id"], 0)
        set_price(waiting["id"], offer)
        set_status(waiting["id"], OFFERED)
        await to_admin(
            bot,
            f"🤔 Клиент {esc(waiting['user_name'])} предложил свою цену <b>{esc(offer)}</b> "
            f"за заказ #{waiting['id']}.\n«{esc(waiting['description'][:200])}»",
            kb((f"✅ Принять {esc(offer)}", f"okp:{waiting['id']}"), ("❌ Отказать", f"nop:{waiting['id']}")),
        )
        await message.answer(f"Передал мастеру: {offer}. Он решит и ответит.")
        return

    # клиент отвечает ценой после «💰 Моя цена» сразу после создания заказа
    bidding = order_waiting_budget(user_id)
    if bidding:
        budget = parse_only_price(description)
        if budget:
            set_client_price(bidding["id"], budget)
            await to_admin(
                bot,
                f"💰 Клиент {esc(bidding['user_name'])} сам предложил <b>{esc(budget)}</b> "
                f"за заказ #{bidding['id']}.\n«{esc(bidding['description'][:200])}»\n\n"
                f"Видно в карточке заказа и в /menu.",
                kb((f"✅ Принять {esc(budget)}", f"okp:{bidding['id']}"), ("❌ Отклонить", f"rej:{bidding['id']}")),
            )
            await message.answer(
                f"Передал мастеру твою цену: {budget}. Он ответит — свою цену или согласие."
            )
            return
        # написано не похоже на цену — считаем это новым заказом, вопрос закрываем
        set_budget_wait(bidding["id"], 0)

    order_id = add_order(user_id, display_name(message), description)
    order = get_order(order_id)
    if attach_type:
        set_attach(order_id, attach_type, attach_id, file_name)
        order = get_order(order_id)
    await send_order_to(
        bot,
        ADMIN_ID,
        order,
        order_text(order),
        kb(("✅ Принять", f"acc:{order_id}"), ("❌ Отклонить", f"rej:{order_id}"), ("🚫", f"blk:{order_id}")),
    )
    await message.answer(
        f"Заказ #{order_id} отправлен мастеру. Как только оценит — напишу сюда.\n\n"
        f"Хочешь предложить свою цену? Нажми «💰 Моя цена» и напиши сумму одним числом, например 2000. "
        f"Если напишешь обычным текстом — это будет новый заказ.\n"
        f"Нужно добавить фото или файл? Пришли его следующим сообщением.",
        kb(("💰 Моя цена", f"bid:{order_id}"), ("✖️ Не надо", f"bidno:{order_id}")),
    )


async def master_free_text(message: Message, bot: Bot, text):
    waiting = orders_waiting_price()
    if len(waiting) == 1:
        price = parse_price(text)
        if not price:
            await message.answer("Нужна цена числом, например: 1500")
            return
        await send_price_to_client(bot, waiting[0], price)
        return
    if len(waiting) > 1:
        ids = ", ".join(f"#{o['id']}" for o in waiting)
        await message.answer(f"Жду цены для нескольких заказов: {ids}. "
                            f"Напиши цену в карточке нужного заказа (/menu) или: /price 12 1500")
        return
    await message.answer("Напоминаю команды мастера: /menu — все заказы по разделам, "
                        "/price 12 1500 — цена для заказа №12.")


# ---------------- нажатия кнопок ----------------


@router.callback_query(F.data.contains(":"))
async def on_button(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split(":")
    action = parts[0]
    raw_id = parts[1] if len(parts) > 1 else ""
    section = parts[2] if len(parts) > 2 else ""
    user_id = callback.from_user.id

    if action in ADMIN_ACTIONS and user_id != ADMIN_ID:
        await callback.answer("Это кнопка для мастера", show_alert=True)
        return
    if is_blocked(user_id):
        await callback.answer("Тебе закрыт доступ к боту", show_alert=True)
        return

    if action == "menu":
        await callback.answer("Готово")
        await render_menu(callback.message, bot, raw_id or "home")
        return

    if action == "unblk":
        if not raw_id.isdigit():
            await callback.answer("Кнопка устарела", show_alert=True)
            return
    else:
        # все остальные кнопки привязаны к заказу
        order = get_order(int(raw_id)) if raw_id.isdigit() else None
        if not order:
            await callback.answer("Этот заказ уже не найден", show_alert=True)
            return
        if action in CLIENT_ACTIONS and user_id != order["user_id"]:
            await callback.answer("Это сообщение не вам", show_alert=True)
            return

    if action == "card":
        # открываем карточку отдельным сообщением: в меню она осталась на месте
        note = "Открыл карточку"
        if order["status"] == NEED_PRICE:
            focus_price(order["id"])
            note = "Цена, которую ты напишешь, уйдёт в этот заказ"
        await send_order_to(bot, ADMIN_ID, order, order_text(order), kbm(order_row(order, "")))
        await callback.answer(note)

    elif action == "acc":
        if order["status"] != PENDING:
            await callback.answer(f"Заказ уже {STATUS_RU[order['status']]}", show_alert=True)
            return
        set_price_wait(order["id"])
        set_status(order["id"], NEED_PRICE)
        await refresh_after(callback, bot, section)
        if order["client_price"]:
            await to_admin(
                bot,
                f"💰 Заказ #{order['id']} принят. Клиент сам хочет {esc(order['client_price'])}.\n"
                f"Напиши свою цену числом — или согласись с его.",
                kb((f"💰 Взяю {esc(order['client_price'])}", f"take:{order['id']}")),
            )
        else:
            await to_admin(bot, f"💰 Заказ #{order['id']} принят. Пришли цену, например: 1500")
        await callback.answer("Принят, жду цену")

    elif action == "take":
        if not order["client_price"]:
            await callback.answer("Клиент не предлагал свою цену", show_alert=True)
            return
        if order["status"] not in (PENDING, NEED_PRICE, OFFERED):
            await callback.answer(f"Заказ уже {STATUS_RU[order['status']]}", show_alert=True)
            return
        await send_price_to_client(bot, order, order["client_price"])
        await refresh_after(callback, bot, section)
        await callback.answer("Отправил клиенту его цену")

    elif action == "bid":
        if order["client_price"]:
            await callback.answer(f"Твоя цена {order['client_price']} уже у мастера", show_alert=True)
            return
        if order["status"] not in (PENDING, NEED_PRICE):
            await callback.answer("Цену уже согласуем", show_alert=True)
            return
        set_budget_wait(order["id"], 1)
        await remove_buttons(callback.message)
        await bot.send_message(
            order["user_id"],
            "Напиши сумму одним числом, например: 2000\n"
            "Обычный текст будет считаться новым заказом.",
        )
        await callback.answer("Жду сумму")

    elif action == "bidno":
        set_budget_wait(order["id"], 0)
        await remove_buttons(callback.message)
        await bot.send_message(order["user_id"], "Хорошо, ждём оценку мастера.")
        await callback.answer("Ок")

    elif action == "blk":
        block_user(order["user_id"], order["user_name"])
        await refresh_after(callback, bot, section)
        await to_admin(
            bot,
            f"🚫 {esc(order['user_name'])} ({order['user_id']}) в чёрном списке: "
            f"его сообщения бот не принимает и заказов от него не шлёт.\n"
            f"Разблокировать: /menu → 🚫 Блок-лист",
        )
        await callback.answer("Внёс в чёрный список")

    elif action == "unblk":
        if unblock_user(int(raw_id)):
            await to_admin(bot, f"✅ {raw_id} убрал из чёрного списка — снова может писать заказы.")
        await render_menu(callback.message, bot, "blocked")
        await callback.answer("Готово")

    elif action == "yes":
        if order["status"] != SENT:
            await callback.answer("Эта цена уже не актуальна", show_alert=True)
            return
        set_status(order["id"], ACCEPTED)
        await remove_buttons(callback.message)
        await bot.send_message(
            order["user_id"],
            f"Отлично, заказ #{order['id']} за {esc(order['price'])} подтверждён — мастер начинает работу.\n"
            f"Статус: /my",
        )
        await to_admin(
            bot,
            f"🎉 Клиент {esc(order['user_name'])} согласен на {esc(order['price'])}!\n"
            f"Заказ #{order['id']}: «{esc(order['description'][:300])}»\n\n"
            f"Сделайте заказ и нажмите «Выполнено», когда закончите.",
            kb(("✅ Заказ выполнен", f"fin:{order['id']}")),
        )
        await callback.answer("Спасибо!")

    elif action == "no":
        if order["status"] != SENT:
            await callback.answer("Эта цена уже не актуальна", show_alert=True)
            return
        set_status(order["id"], NEGOTIATING)
        set_offer_wait(order["id"], 1)
        await remove_buttons(callback.message)
        await bot.send_message(
            order["user_id"],
            "Понял. Напишите вашу цену обычным сообщением — передам мастеру.\n"
            "Или /cancel, чтобы отменить заказ.",
        )
        await to_admin(bot, f"🤔 Клиент не согласен на {esc(order['price'])} по заказу #{order['id']}. Жду его цену.")
        await callback.answer("Передал мастеру")

    elif action == "okp":
        summa = order["price"] or order["client_price"]
        if order["status"] not in (PENDING, OFFERED):
            await callback.answer(f"Заказ уже {STATUS_RU[order['status']]}", show_alert=True)
            return
        if not summa:
            await callback.answer("Клиент не предлагал свою цену", show_alert=True)
            return
        set_price(order["id"], summa)
        set_status(order["id"], ACCEPTED)
        await refresh_after(callback, bot, section)
        await bot.send_message(
            order["user_id"],
            f"Мастер согласен на вашу цену — {esc(summa)}. Заказ #{order['id']} подтверждён.",
        )
        await to_admin(
            bot,
            f"🎉 Согласовали {esc(summa)} по заказу #{order['id']}.",
            kb(("✅ Заказ выполнен", f"fin:{order['id']}")),
        )
        await callback.answer("Согласовано")

    elif action == "nop":
        if order["status"] != OFFERED:
            await callback.answer(f"Заказ уже {STATUS_RU[order['status']]}", show_alert=True)
            return
        set_status(order["id"], DECLINED)
        await refresh_after(callback, bot, section)
        await bot.send_message(
            order["user_id"],
            f"Мастер не может сделать заказ #{order['id']} за {esc(order['price'])}.\n"
            f"Можно попробовать другой объём — опишите новый заказ.",
        )
        await callback.answer("Отказ передан клиенту")

    elif action == "rej":
        if order["status"] not in (PENDING, OFFERED):
            await callback.answer(f"Заказ уже {STATUS_RU[order['status']]}", show_alert=True)
            return
        set_status(order["id"], REJECTED)
        clear_flags(order["id"])
        await refresh_after(callback, bot, section)
        await bot.send_message(
            order["user_id"],
            f"Мастер не смог взять заказ #{order['id']}.\n"
            f"Если хотите, уточните детали или отправьте новый заказ — я передам.",
        )
        await callback.answer("Отклонен")

    elif action == "fin":
        if order["status"] != ACCEPTED:
            await callback.answer("Заказ ещё не подтвержден клиентом", show_alert=True)
            return
        set_status(order["id"], DONE)
        await refresh_after(callback, bot, section)
        await bot.send_message(order["user_id"], f"Заказ #{order['id']} выполнен ✅ Спасибо за работу!")
        await callback.answer("Готово")

    else:
        await callback.answer("Кнопка устарела", show_alert=True)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN. Скопируйте .env.example в .env и впишите токен от @BotFather")
    if not ADMIN_ID:
        raise SystemExit("Нет ADMIN_ID. Напишите боту /start — ваш ID в конце приветствия, впишите его в .env")
    init_db()
    try:
        bot = Bot(
            BOT_TOKEN,
            session=AiohttpSession(proxy=PROXY) if PROXY else None,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
    except TokenValidationError:
        raise SystemExit(
            "BOT_TOKEN некорректный. Нужен токен целиком из @BotFather, формат 123456789:AAHbp3...\n"
            f"Начало того, что передано: {BOT_TOKEN[:10]}..."
        )
    dp = Dispatcher()
    dp.include_router(router)
    async with bot:
        if PROXY:
            print(f"Работаю через прокси {PROXY}", flush=True)
        try:
            me = await bot.get_me()
            # переключаем бота в режим polling; заказы, пришедшие пока бот лежал, не выбрасываем
            await bot.delete_webhook(drop_pending_updates=False)
        except TelegramUnauthorizedError:
            raise SystemExit(
                "Telegram не принял BOT_TOKEN (401). Свежий токен: @BotFather → /mybots → выбрать бота → API Token"
            )
        except Exception as err:  # ошибки транспорта, включая недоступность прокси
            where = f"прокси {PROXY}" if PROXY else "Telegram API напрямую"
            if PROXY:
                hint = (
                    "Проверь адрес и порт в строке PROXY — приложение-прокси должно быть запущено.\n"
                    "Если прокси не нужен, удали строку PROXY из .env."
                )
            else:
                hint = (
                    "Скорее всего API недоступен без прокси. Добавь в .env одну строку:\n"
                    "  PROXY=http://127.0.0.1:8118   (http-прокси)\n"
                    "  PROXY=socks5://127.0.0.1:1080 (socks5)"
                )
            raise SystemExit(f"Бот не смог достучаться до {where}.\n{type(err).__name__}: {err}\n\n{hint}")
        print(f"Бот @{me.username} слушает. Заказы мастера приходят в chat_id {ADMIN_ID}. База: {DB_FILE}",
              flush=True)
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
