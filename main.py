"""
LRoulette — Telegram Bot
Requires: aiogram>=3.0, aiohttp
Environment variables: BOT_TOKEN, CRYPTOBOT_TOKEN, ADMIN_ID
"""

import os
import asyncio
import random
import sqlite3
import logging
from datetime import datetime
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ─────────────────────────── CONFIG ───────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "YOUR_CRYPTOBOT_TOKEN_HERE")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

USDT_TO_RUB = 90          # Фиксированный курс
MIN_BET = 50
MAX_BET = 2000
MIN_PLAYERS = 5
PRIZE_PERCENT = 0.80       # Победитель
OWNER_PERCENT = 0.20       # Владелец
ROULETTE_DELAY = 300       # 5 минут после /start_roulette
ROOM_TIMEOUT = 3600        # 1 час до авто-сброса комнаты
ROOM_TIMEOUT_BONUS = 0.05  # 5% бонус за ожидание при сбросе

CRYPTOBOT_API = "https://pay.crypt.bot/api"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────── DATABASE ───────────────────────────
DB_PATH = "lroulette.db"

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id   INTEGER PRIMARY KEY,
            username  TEXT,
            balance   INTEGER DEFAULT 0,
            total_won INTEGER DEFAULT 0,
            total_bet INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS current_room (
            user_id    INTEGER PRIMARY KEY,
            bet_amount INTEGER,
            bet_time   DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS history (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            winner_id        INTEGER,
            winner_username  TEXT,
            prize            INTEGER,
            bank             INTEGER,
            players_count    INTEGER,
            timestamp        DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS withdrawals (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            amount     INTEGER,
            method     TEXT,
            details    TEXT,
            status     TEXT DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS owner_stats (
            id           INTEGER PRIMARY KEY DEFAULT 1,
            total_profit INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS paid_invoices (
            invoice_id TEXT PRIMARY KEY
        );
        INSERT OR IGNORE INTO owner_stats (id, total_profit) VALUES (1, 0);
        """)
    log.info("Database initialised.")

# ─────────────────── DB HELPERS ───────────────────────────────
def ensure_user(user_id: int, username: Optional[str]):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
            (user_id, username or "")
        )
        if username:
            conn.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
        conn.commit()

def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def get_balance(user_id: int) -> int:
    u = get_user(user_id)
    return u["balance"] if u else 0

def change_balance(user_id: int, delta: int):
    with get_conn() as conn:
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (delta, user_id))
        conn.commit()

def get_room_participants():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM current_room ORDER BY bet_time").fetchall()

def get_room_bet(user_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM current_room WHERE user_id=?", (user_id,)).fetchone()

def place_bet(user_id: int, amount: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO current_room (user_id, bet_amount) VALUES (?, ?)",
            (user_id, amount)
        )
        conn.execute("UPDATE users SET balance = balance - ?, total_bet = total_bet + ? WHERE user_id=?",
                     (amount, amount, user_id))
        conn.commit()

def clear_room():
    with get_conn() as conn:
        conn.execute("DELETE FROM current_room")
        conn.commit()

def get_owner_profit() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT total_profit FROM owner_stats WHERE id=1").fetchone()
        return row["total_profit"] if row else 0

def add_owner_profit(amount: int):
    with get_conn() as conn:
        conn.execute("UPDATE owner_stats SET total_profit = total_profit + ? WHERE id=1", (amount,))
        conn.commit()

def add_history(winner_id: int, winner_username: str, prize: int, bank: int, players_count: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO history (winner_id, winner_username, prize, bank, players_count) VALUES (?,?,?,?,?)",
            (winner_id, winner_username, prize, bank, players_count)
        )
        conn.commit()

def get_history(limit=10):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM history ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()

def get_pending_withdrawals():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM withdrawals WHERE status='pending' ORDER BY created_at").fetchall()

def get_withdrawal(wid: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()

def update_withdrawal_status(wid: int, status: str):
    with get_conn() as conn:
        conn.execute("UPDATE withdrawals SET status=? WHERE id=?", (status, wid))
        conn.commit()

def create_withdrawal(user_id: int, amount: int, method: str, details: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO withdrawals (user_id, amount, method, details) VALUES (?,?,?,?)",
            (user_id, amount, method, details)
        )
        conn.commit()
        return cur.lastrowid

def is_invoice_paid(invoice_id: str) -> bool:
    with get_conn() as conn:
        return bool(conn.execute("SELECT 1 FROM paid_invoices WHERE invoice_id=?", (invoice_id,)).fetchone())

def mark_invoice_paid(invoice_id: str):
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO paid_invoices (invoice_id) VALUES (?)", (invoice_id,))
        conn.commit()

def get_all_user_ids():
    with get_conn() as conn:
        rows = conn.execute("SELECT user_id FROM users").fetchall()
        return [r["user_id"] for r in rows]

def get_stats():
    with get_conn() as conn:
        players = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        total_balance = conn.execute("SELECT COALESCE(SUM(balance),0) as s FROM users").fetchone()["s"]
        profit = get_owner_profit()
        return players, total_balance, profit

# ─────────────────── CRYPTOBOT ────────────────────────────────
async def cryptobot_request(method: str, params: dict) -> Optional[dict]:
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    url = f"{CRYPTOBOT_API}/{method}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=params, headers=headers) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return data["result"]
                log.warning(f"CryptoBot error: {data}")
    except Exception as e:
        log.error(f"CryptoBot request failed: {e}")
    return None

async def create_invoice(amount_usdt: float) -> Optional[dict]:
    return await cryptobot_request("createInvoice", {
        "asset": "USDT",
        "amount": str(round(amount_usdt, 2)),
        "description": "Пополнение баланса LRoulette",
        "expires_in": 3600
    })

async def check_invoice(invoice_id: str) -> Optional[dict]:
    result = await cryptobot_request("getInvoices", {"invoice_ids": str(invoice_id)})
    if result and result.get("items"):
        return result["items"][0]
    return None

# ─────────────────── KEYBOARDS ────────────────────────────────
def main_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎲 Сделать ставку", callback_data="bet")
    kb.button(text="💰 Мой баланс", callback_data="balance")
    kb.button(text="📊 Текущая рулетка", callback_data="room_info")
    kb.button(text="📜 История победителей", callback_data="history")
    kb.button(text="💸 Вывести средства", callback_data="withdraw")
    kb.button(text="🆘 Помощь", callback_data="help")
    kb.adjust(2, 2, 2)
    return kb.as_markup()

def back_to_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Главное меню", callback_data="main_menu")
    return kb.as_markup()

def admin_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Заявки на вывод", callback_data="admin_withdrawals")
    kb.button(text="📊 Статистика", callback_data="admin_stats")
    kb.button(text="🔄 Сбросить комнату", callback_data="admin_reset")
    kb.button(text="📢 Рассылка", callback_data="admin_broadcast")
    kb.button(text="▶️ Запустить рулетку", callback_data="admin_start_roulette")
    kb.adjust(2, 2, 1)
    return kb.as_markup()

# ─────────────────── STATES ───────────────────────────────────
class BetState(StatesGroup):
    waiting_amount = State()

class TopupState(StatesGroup):
    waiting_amount = State()
    waiting_check = State()

class WithdrawState(StatesGroup):
    waiting_amount = State()
    waiting_method = State()
    waiting_details = State()

class BroadcastState(StatesGroup):
    waiting_text = State()

# ─────────────────── GLOBALS ──────────────────────────────────
bot: Bot = None
dp: Dispatcher = None
router = Router()

# Background task handles
_room_timer_task: Optional[asyncio.Task] = None
_roulette_task: Optional[asyncio.Task] = None
_roulette_running = False   # Guard to avoid double-launch

# ─────────────────── HELPERS ──────────────────────────────────
def uname(row) -> str:
    """Format @username or ID fallback."""
    if isinstance(row, dict) or hasattr(row, "keys"):
        un = row.get("username") or row["user_id"] if "username" in row.keys() else str(row["user_id"])
        return f"@{un}" if (un and not str(un).isdigit()) else str(un)
    return str(row)

def fmt_user(user_id: int, username: Optional[str]) -> str:
    if username:
        return f"@{username}"
    return str(user_id)

def escape_md(text: str) -> str:
    """Escape MarkdownV2 special chars."""
    special = r"\_*[]()~`>#+-=|{}.!"
    for ch in special:
        text = text.replace(ch, f"\\{ch}")
    return text

# ─────────────────── ROOM TIMER ──────────────────────────────
async def room_timeout_job():
    """Run once — waits ROOM_TIMEOUT, then resets room with refund+bonus."""
    global _room_timer_task
    await asyncio.sleep(ROOM_TIMEOUT)
    participants = get_room_participants()
    if not participants or len(participants) >= MIN_PLAYERS:
        return
    log.info("Room timeout reached, refunding bets with bonus.")
    for p in participants:
        uid = p["user_id"]
        bet = p["bet_amount"]
        bonus = int(bet * ROOM_TIMEOUT_BONUS)
        change_balance(uid, bet + bonus)
        try:
            await bot.send_message(
                uid,
                f"⏰ К сожалению, за 1 час не набралось {MIN_PLAYERS} игроков\\.\n"
                f"Твоя ставка *{bet} ₽* возвращена \\+ бонус *{bonus} ₽* за ожидание\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            log.warning(f"Could not notify {uid}: {e}")
    clear_room()
    _room_timer_task = None

def start_room_timer():
    global _room_timer_task
    if _room_timer_task and not _room_timer_task.done():
        return
    _room_timer_task = asyncio.create_task(room_timeout_job())

def cancel_room_timer():
    global _room_timer_task
    if _room_timer_task and not _room_timer_task.done():
        _room_timer_task.cancel()
    _room_timer_task = None

# ─────────────────── ROULETTE LOGIC ───────────────────────────
async def run_roulette():
    """Called after ROULETTE_DELAY seconds. Picks winner, pays out."""
    global _roulette_running
    try:
        await asyncio.sleep(ROULETTE_DELAY)
        participants = get_room_participants()
        if not participants:
            _roulette_running = False
            return

        cancel_room_timer()

        # Weighted random
        pool = []
        for p in participants:
            pool.extend([p["user_id"]] * p["bet_amount"])

        winner_id = random.choice(pool)
        bank = sum(p["bet_amount"] for p in participants)
        prize = int(bank * PRIZE_PERCENT)
        owner_cut = int(bank * OWNER_PERCENT)

        winner_row = get_user(winner_id)
        winner_uname = winner_row["username"] if winner_row else None
        winner_display = fmt_user(winner_id, winner_uname)

        change_balance(winner_id, prize)
        with get_conn() as conn:
            conn.execute("UPDATE users SET total_won = total_won + ? WHERE user_id=?", (prize, winner_id))
            conn.commit()
        add_owner_profit(owner_cut)
        add_history(winner_id, winner_display, prize, bank, len(participants))

        bets_map = {p["user_id"]: p["bet_amount"] for p in participants}

        for p in participants:
            uid = p["user_id"]
            user_bet = bets_map.get(uid, 0)
            try:
                await bot.send_message(
                    uid,
                    f"🏆 *Результат рулетки\\!*\n\n"
                    f"Победитель: *{escape_md(winner_display)}*\n"
                    f"Выигрыш: *{prize} ₽*\n"
                    f"Банк: *{bank} ₽*\n"
                    f"Твоя ставка: *{user_bet} ₽*",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except Exception as e:
                log.warning(f"Could not notify {uid}: {e}")

        clear_room()
        log.info(f"Roulette done. Winner: {winner_id}, prize: {prize}")
    except asyncio.CancelledError:
        log.info("Roulette task cancelled.")
    finally:
        _roulette_running = False

def start_roulette_task():
    global _roulette_running, _roulette_task
    if _roulette_running:
        return False
    _roulette_running = True
    _roulette_task = asyncio.create_task(run_roulette())
    return True

# ─────────────────── /start ───────────────────────────────────
@router.message(CommandStart())
async def cmd_start(msg: Message):
    uid = msg.from_user.id
    uname_str = msg.from_user.username
    ensure_user(uid, uname_str)

    # 1) Отправить превью
    try:
        photo = FSInputFile("preview.jpg")
        await bot.send_photo(uid, photo=photo, caption="")
    except Exception as e:
        log.warning(f"Could not send preview.jpg: {e}")

    # 2) Приветственное сообщение
    text = (
        "┌─────────────────────────────────────┐\n"
        "│      🎰 *LRoulette* 🎰              │\n"
        "│   Ставки\\. Банк\\. Победа\\.           │\n"
        "└─────────────────────────────────────┘\n\n"
        "*Как играть?*\n"
        "• Ставка от *50* до *2000* ₽\n"
        "• Нужно *5\\+* игроков для запуска\n"
        "• Твой шанс \\= \\(твоя ставка / общий банк\\) × 100%\n"
        "• Победитель получает *80%* от банка\n"
        "• Владелец забирает *20%* на развитие\n\n"
        "*Вывод средств*\n"
        "• СБП \\(по номеру телефона\\)\n"
        "• TRC20 \\(USDT\\)\n\n"
        "*Вопросы* — @admin\n\n"
        "👇 *Выбирай действие:* 👇"
    )
    await msg.answer(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=main_keyboard())

# ─────────────────── BALANCE ──────────────────────────────────
@router.callback_query(F.data == "balance")
async def cb_balance(cq: CallbackQuery):
    uid = cq.from_user.id
    ensure_user(uid, cq.from_user.username)
    bal = get_balance(uid)

    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Пополнить баланс", callback_data="topup")
    kb.button(text="🏠 Главное меню", callback_data="main_menu")
    kb.adjust(1)

    await cq.message.edit_text(
        f"💰 *Твой баланс:* *{bal} ₽*\n\nДля пополнения нажми кнопку ниже\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb.as_markup()
    )
    await cq.answer()

# ─────────────────── TOP-UP ───────────────────────────────────
@router.callback_query(F.data == "topup")
async def cb_topup(cq: CallbackQuery, state: FSMContext):
    await state.set_state(TopupState.waiting_amount)
    await cq.message.edit_text(
        "💳 *Пополнение баланса*\n\nВведи сумму пополнения в *рублях* \\(минимум 90 ₽ = 1 USDT\\):",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=back_to_menu_kb()
    )
    await cq.answer()

@router.message(TopupState.waiting_amount)
async def topup_amount(msg: Message, state: FSMContext):
    try:
        amount_rub = int(msg.text.strip())
        if amount_rub < 90:
            await msg.answer("Минимум 90 ₽ \\(1 USDT\\)\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return
    except ValueError:
        await msg.answer("Введи целое число рублей\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    amount_usdt = round(amount_rub / USDT_TO_RUB, 2)
    invoice = await create_invoice(amount_usdt)
    if not invoice:
        await msg.answer("❌ Ошибка создания инвойса\\. Попробуй позже\\.", parse_mode=ParseMode.MARKDOWN_V2)
        await state.clear()
        return

    invoice_id = invoice.get("invoice_id") or invoice.get("id")
    pay_url = invoice.get("pay_url") or invoice.get("bot_invoice_url", "")

    await state.update_data(invoice_id=str(invoice_id), amount_rub=amount_rub)
    await state.set_state(TopupState.waiting_check)

    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оплатить через CryptoBot", url=pay_url)
    kb.button(text="✅ Я оплатил — проверить", callback_data=f"check_pay:{invoice_id}:{amount_rub}")
    kb.button(text="🏠 Главное меню", callback_data="main_menu")
    kb.adjust(1)

    await msg.answer(
        f"📄 *Инвойс создан\\!*\n\n"
        f"Сумма: *{amount_usdt} USDT* \\({amount_rub} ₽\\)\n"
        f"Курс: 1 USDT = {USDT_TO_RUB} ₽\n\n"
        f"После оплаты нажми *«Я оплатил — проверить»*\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb.as_markup()
    )

@router.callback_query(F.data.startswith("check_pay:"))
async def cb_check_pay(cq: CallbackQuery, state: FSMContext):
    _, invoice_id, amount_rub = cq.data.split(":")
    amount_rub = int(amount_rub)
    uid = cq.from_user.id

    if is_invoice_paid(invoice_id):
        await cq.answer("Этот инвойс уже был зачислен.", show_alert=True)
        await state.clear()
        return

    invoice = await check_invoice(invoice_id)
    if not invoice:
        await cq.answer("❌ Не удалось получить статус. Попробуй позже.", show_alert=True)
        return

    status = invoice.get("status", "")
    if status == "paid":
        mark_invoice_paid(invoice_id)
        change_balance(uid, amount_rub)
        await state.clear()

        kb = InlineKeyboardBuilder()
        kb.button(text="🎲 Сделать ставку", callback_data="bet")
        kb.button(text="🏠 Главное меню", callback_data="main_menu")
        kb.adjust(1)

        await cq.message.edit_text(
            f"✅ *Оплата подтверждена\\!*\n\n"
            f"Начислено: *{amount_rub} ₽*\n"
            f"Баланс: *{get_balance(uid)} ₽*",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb.as_markup()
        )
    elif status == "expired":
        await cq.answer("Инвойс истёк. Создай новый.", show_alert=True)
        await state.clear()
    else:
        await cq.answer("Оплата ещё не поступила. Подожди и попробуй снова.", show_alert=True)

    await cq.answer()

# ─────────────────── BET ──────────────────────────────────────
@router.callback_query(F.data == "bet")
async def cb_bet(cq: CallbackQuery, state: FSMContext):
    uid = cq.from_user.id
    ensure_user(uid, cq.from_user.username)
    bal = get_balance(uid)

    existing = get_room_bet(uid)
    note = ""
    if existing:
        note = f"\n\n⚠️ У тебя уже есть ставка *{existing['bet_amount']} ₽*\\. Если введёшь новую — старая вернётся на баланс\\."

    await state.set_state(BetState.waiting_amount)
    await cq.message.edit_text(
        f"🎲 *Сделать ставку*\n\n"
        f"Твой баланс: *{bal} ₽*\n"
        f"Диапазон ставок: *{MIN_BET}–{MAX_BET} ₽*"
        f"{note}\n\n"
        f"Введи сумму ставки:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=back_to_menu_kb()
    )
    await cq.answer()

@router.message(BetState.waiting_amount)
async def bet_amount(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    try:
        amount = int(msg.text.strip())
    except ValueError:
        await msg.answer("Введи целое число рублей\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    if amount < MIN_BET or amount > MAX_BET:
        await msg.answer(
            f"Ставка должна быть от *{MIN_BET}* до *{MAX_BET}* ₽\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    bal = get_balance(uid)
    if bal < amount:
        await msg.answer(
            f"Недостаточно средств\\. Баланс: *{bal} ₽*\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    # Вернуть старую ставку если есть
    existing = get_room_bet(uid)
    if existing:
        old_bet = existing["bet_amount"]
        with get_conn() as conn:
            conn.execute("UPDATE users SET balance = balance + ?, total_bet = total_bet - ? WHERE user_id=?",
                         (old_bet, old_bet, uid))
            conn.commit()

    was_empty = len(get_room_participants()) == 0
    place_bet(uid, amount)

    participants = get_room_participants()
    count = len(participants)
    bank = sum(p["bet_amount"] for p in participants)

    if was_empty:
        start_room_timer()

    await state.clear()

    chance = round((amount / bank) * 100, 2) if bank > 0 else 0
    await msg.answer(
        f"✅ *Ставка принята\\!*\n\n"
        f"Твоя ставка: *{amount} ₽*\n"
        f"Шанс победы: *{escape_md(str(chance))}%*\n"
        f"Участников: *{count}*\n"
        f"Банк: *{bank} ₽*",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_keyboard()
    )

    # Уведомить админа если впервые >=5
    if count >= MIN_PLAYERS:
        owner_cut = int(bank * OWNER_PERCENT)
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🎲 *Новая рулетка готова к запуску\\!*\n\n"
                f"Участников: *{count}*\n"
                f"Банк: *{bank} ₽*\n"
                f"Твой профит \\(20%\\): *{owner_cut} ₽*\n\n"
                f"Нажми /start\\_roulette чтобы запустить\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            log.warning(f"Admin notify failed: {e}")

# ─────────────────── ROOM INFO ────────────────────────────────
@router.callback_query(F.data == "room_info")
async def cb_room_info(cq: CallbackQuery):
    uid = cq.from_user.id
    participants = get_room_participants()
    count = len(participants)
    bank = sum(p["bet_amount"] for p in participants)
    my_bet = get_room_bet(uid)

    lines = [f"📊 *Текущая рулетка*\n\nУчастников: *{count}*\nБанк: *{bank} ₽*"]
    if my_bet:
        bet_val = my_bet["bet_amount"]
        chance = round((bet_val / bank) * 100, 2) if bank > 0 else 0
        lines.append(f"Твоя ставка: *{bet_val} ₽*\nТвой шанс: *{escape_md(str(chance))}%*")
    else:
        lines.append("Ты ещё не сделал ставку\\.")

    lines.append(f"\nДля победы нужно минимум *{MIN_PLAYERS}* игроков\\.")

    await cq.message.edit_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=back_to_menu_kb()
    )
    await cq.answer()

# ─────────────────── HISTORY ──────────────────────────────────
@router.callback_query(F.data == "history")
async def cb_history(cq: CallbackQuery):
    rows = get_history(10)
    if not rows:
        text = "📜 *История побед пока пуста\\.*"
    else:
        parts = ["📜 *История победителей*\n"]
        for i, r in enumerate(rows, 1):
            ts = r["timestamp"][:16].replace("T", " ")
            parts.append(
                f"🏆 *\\#{i}* \\({escape_md(ts)}\\)\n"
                f"Победитель: {escape_md(r['winner_username'])}\n"
                f"Выигрыш: *{r['prize']} ₽*\n"
                f"Банк: *{r['bank']} ₽*\n"
                f"Игроков: *{r['players_count']}*\n"
            )
        text = "\n".join(parts)

    await cq.message.edit_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_to_menu_kb())
    await cq.answer()

# ─────────────────── WITHDRAW ─────────────────────────────────
@router.callback_query(F.data == "withdraw")
async def cb_withdraw(cq: CallbackQuery, state: FSMContext):
    uid = cq.from_user.id
    ensure_user(uid, cq.from_user.username)
    bal = get_balance(uid)

    if bal < 100:
        await cq.answer("Минимальный баланс для вывода: 100 ₽.", show_alert=True)
        return

    await state.set_state(WithdrawState.waiting_amount)
    await cq.message.edit_text(
        f"💸 *Вывод средств*\n\n"
        f"Твой баланс: *{bal} ₽*\n"
        f"Минимум: *100 ₽*, сумма кратна 50\n\n"
        f"Введи сумму вывода:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=back_to_menu_kb()
    )
    await cq.answer()

@router.message(WithdrawState.waiting_amount)
async def withdraw_amount(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    try:
        amount = int(msg.text.strip())
    except ValueError:
        await msg.answer("Введи целое число рублей\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    bal = get_balance(uid)
    if amount < 100 or amount % 50 != 0 or amount > bal:
        await msg.answer(
            f"Сумма должна быть от *100 ₽*, кратна *50* и не превышать баланс *{bal} ₽*\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    await state.update_data(amount=amount)
    await state.set_state(WithdrawState.waiting_method)

    kb = InlineKeyboardBuilder()
    kb.button(text="📱 СБП (номер телефона)", callback_data="wmethod:sbp")
    kb.button(text="🔗 TRC20 (USDT адрес)", callback_data="wmethod:trc20")
    kb.button(text="🏠 Главное меню", callback_data="main_menu")
    kb.adjust(1)

    await msg.answer(
        f"Сумма: *{amount} ₽*\n\nВыбери метод вывода:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb.as_markup()
    )

@router.callback_query(F.data.startswith("wmethod:"), WithdrawState.waiting_method)
async def withdraw_method(cq: CallbackQuery, state: FSMContext):
    method = cq.data.split(":")[1]
    await state.update_data(method=method)
    await state.set_state(WithdrawState.waiting_details)

    prompt = "Введи номер телефона \\(в формате +7XXXXXXXXXX\\):" if method == "sbp" \
        else "Введи TRC20 адрес кошелька:"

    await cq.message.edit_text(prompt, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_to_menu_kb())
    await cq.answer()

@router.message(WithdrawState.waiting_details)
async def withdraw_details(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    data = await state.get_data()
    amount = data["amount"]
    method = data["method"]
    details = msg.text.strip()

    bal = get_balance(uid)
    if bal < amount:
        await msg.answer("Недостаточно средств на балансе\\.", parse_mode=ParseMode.MARKDOWN_V2)
        await state.clear()
        return

    change_balance(uid, -amount)
    wid = create_withdrawal(uid, amount, method, details)
    await state.clear()

    await msg.answer(
        f"✅ *Заявка №{wid} создана\\!*\n\n"
        f"Сумма: *{amount} ₽*\n"
        f"Метод: *{escape_md(method.upper())}*\n"
        f"Реквизиты: `{escape_md(details)}`\n\n"
        f"Администратор рассмотрит её в ближайшее время\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_keyboard()
    )

    # Уведомить админа
    method_name = "СБП" if method == "sbp" else "TRC20"
    try:
        await bot.send_message(
            ADMIN_ID,
            f"💸 *Новая заявка на вывод \\#{wid}*\n\n"
            f"Пользователь: {fmt_user(uid, msg.from_user.username)}\n"
            f"Сумма: *{amount} ₽*\n"
            f"Метод: *{escape_md(method_name)}*\n"
            f"Реквизиты: `{escape_md(details)}`",
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        log.warning(f"Admin notify failed: {e}")

# ─────────────────── HELP ─────────────────────────────────────
@router.callback_query(F.data == "help")
async def cb_help(cq: CallbackQuery):
    await cq.message.edit_text(
        "🆘 *Помощь*\n\nПо всем вопросам писать: @admin\n\nВозврат к меню ниже\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=back_to_menu_kb()
    )
    await cq.answer()

# ─────────────────── MAIN MENU ────────────────────────────────
@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.message.edit_text(
        "🎰 *LRoulette* — Главное меню\n\nВыбирай действие:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_keyboard()
    )
    await cq.answer()

# ─────────────────── /start_roulette ─────────────────────────
@router.message(Command("start_roulette"))
async def cmd_start_roulette(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    participants = get_room_participants()
    count = len(participants)
    if count < MIN_PLAYERS:
        await msg.answer(f"Недостаточно участников: {count}/{MIN_PLAYERS}")
        return
    if _roulette_running:
        await msg.answer("Рулетка уже запущена!")
        return

    bank = sum(p["bet_amount"] for p in participants)
    ok = start_roulette_task()
    if not ok:
        await msg.answer("Рулетка уже запущена!")
        return

    for p in participants:
        try:
            await bot.send_message(
                p["user_id"],
                f"🎲 *Рулетка запущена\\!* Победитель будет объявлен через 5 минут\\.\\.\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            log.warning(f"Notify {p['user_id']}: {e}")

    await msg.answer(
        f"▶️ Рулетка запущена!\nУчастников: {count}\nБанк: {bank} ₽\nОбъявление через 5 минут."
    )

# ─────────────────── /reset_roulette ─────────────────────────
@router.message(Command("reset_roulette"))
async def cmd_reset_roulette(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await _do_reset(msg)

async def _do_reset(msg: Message):
    cancel_room_timer()
    participants = get_room_participants()
    for p in participants:
        change_balance(p["user_id"], p["bet_amount"])
        try:
            await bot.send_message(
                p["user_id"],
                "🔄 *Комната была сброшена администратором\\.*\nСтавка возвращена на баланс\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            log.warning(f"Notify {p['user_id']}: {e}")
    clear_room()
    await msg.answer(f"✅ Комната сброшена. Возвращено {len(participants)} ставок.")

# ─────────────────── /admin ───────────────────────────────────
@router.message(Command("admin"))
async def cmd_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer("🔧 *Админ\\-панель*", parse_mode=ParseMode.MARKDOWN_V2,
                     reply_markup=admin_keyboard())

@router.callback_query(F.data == "admin_withdrawals")
async def cb_admin_withdrawals(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    pending = get_pending_withdrawals()
    if not pending:
        await cq.message.edit_text("📋 Нет активных заявок на вывод.", reply_markup=back_to_menu_kb())
        await cq.answer()
        return

    for w in pending:
        method_name = "СБП" if w["method"] == "sbp" else "TRC20"
        text = (
            f"📋 *Заявка №{w['id']}*\n"
            f"Пользователь: {w['user_id']}\n"
            f"Сумма: *{w['amount']} ₽*\n"
            f"Метод: *{method_name}*\n"
            f"Реквизиты: `{w['details']}`\n"
            f"Создана: {str(w['created_at'])[:16]}"
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Выполнено", callback_data=f"wadmin:complete:{w['id']}")
        kb.button(text="❌ Отказать", callback_data=f"wadmin:reject:{w['id']}")
        kb.adjust(2)
        try:
            await cq.message.answer(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb.as_markup())
        except Exception as e:
            log.warning(f"Could not send withdrawal info: {e}")

    await cq.answer()

@router.callback_query(F.data.startswith("wadmin:"))
async def cb_wadmin(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    _, action, wid_str = cq.data.split(":")
    wid = int(wid_str)
    w = get_withdrawal(wid)
    if not w:
        await cq.answer("Заявка не найдена.", show_alert=True)
        return
    if w["status"] != "pending":
        await cq.answer(f"Уже обработана: {w['status']}", show_alert=True)
        return

    if action == "complete":
        update_withdrawal_status(wid, "completed")
        try:
            await bot.send_message(
                w["user_id"],
                f"✅ Ваша заявка №{wid} выполнена, средства отправлены\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            log.warning(e)
        await cq.message.edit_text(f"✅ Заявка №{wid} отмечена как выполненная.")
    elif action == "reject":
        update_withdrawal_status(wid, "rejected")
        # Вернуть деньги
        change_balance(w["user_id"], w["amount"])
        try:
            await bot.send_message(
                w["user_id"],
                f"❌ Заявка №{wid} отклонена\\. Отказано, обратитесь в поддержку\\. Средства возвращены на баланс\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            log.warning(e)
        await cq.message.edit_text(f"❌ Заявка №{wid} отклонена.")

    await cq.answer()

@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    players, total_bal, profit = get_stats()
    room = get_room_participants()
    await cq.message.edit_text(
        f"📊 *Статистика*\n\n"
        f"Игроков зарегистрировано: *{players}*\n"
        f"Суммарный баланс пользователей: *{total_bal} ₽*\n"
        f"Профит владельца: *{profit} ₽*\n"
        f"В текущей комнате: *{len(room)}* чел\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=back_to_menu_kb()
    )
    await cq.answer()

@router.callback_query(F.data == "admin_reset")
async def cb_admin_reset(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    await _do_reset(cq.message)
    await cq.answer()

@router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(BroadcastState.waiting_text)
    await cq.message.edit_text("📢 Введи текст рассылки:", reply_markup=back_to_menu_kb())
    await cq.answer()

@router.message(BroadcastState.waiting_text)
async def broadcast_send(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        await state.clear()
        return
    text = msg.text.strip()
    user_ids = get_all_user_ids()
    sent = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception:
            pass
    await state.clear()
    await msg.answer(f"✅ Рассылка отправлена {sent}/{len(user_ids)} пользователям.")

@router.callback_query(F.data == "admin_start_roulette")
async def cb_admin_start_roulette(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    # Эмулируем команду /start_roulette
    participants = get_room_participants()
    count = len(participants)
    if count < MIN_PLAYERS:
        await cq.answer(f"Недостаточно участников: {count}/{MIN_PLAYERS}", show_alert=True)
        return
    if _roulette_running:
        await cq.answer("Рулетка уже запущена!", show_alert=True)
        return

    bank = sum(p["bet_amount"] for p in participants)
    ok = start_roulette_task()
    if not ok:
        await cq.answer("Рулетка уже запущена!", show_alert=True)
        return

    for p in participants:
        try:
            await bot.send_message(
                p["user_id"],
                "🎲 *Рулетка запущена\\!* Победитель будет объявлен через 5 минут\\.\\.\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            log.warning(f"Notify {p['user_id']}: {e}")

    await cq.message.edit_text(
        f"▶️ Рулетка запущена!\nУчастников: {count}\nБанк: {bank} ₽\nОбъявление через 5 минут."
    )
    await cq.answer()

# ─────────────────── MAIN ─────────────────────────────────────
async def main():
    global bot, dp
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    log.info("Bot starting...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
