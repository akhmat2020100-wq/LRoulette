"""
LRoulette — Telegram Bot
Requires: aiogram>=3.0, aiohttp
Environment variables: BOT_TOKEN, CRYPTOBOT_TOKEN, ADMIN_ID
"""

import os, asyncio, random, sqlite3, logging
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, FSInputFile, WebAppInfo, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ─────────────────────────── CONFIG ───────────────────────────
BOT_TOKEN       = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "YOUR_CRYPTOBOT_TOKEN_HERE")
ADMIN_ID        = int(os.getenv("ADMIN_ID", "0"))

USDT_TO_RUB     = 90
MIN_BET         = 50
MAX_BET         = 2000
PRIZE_PERCENT   = 0.80
OWNER_PERCENT   = 0.20
ROULETTE_DELAY  = 300
ROOM_TIMEOUT    = 3600
DEFAULT_ROOM_DELAY      = 600  # 10 min for default room
CUSTOM_ROOM_DELAY       = 300  # 5 min for user-created rooms
DEFAULT_ROOM_ID         = 1
COMMISSION_SBP          = 0.05  # 5% commission for card withdrawal
COMMISSION_CRYPTO       = 0.00  # 0% for crypto

CRYPTOBOT_API = "https://pay.crypt.bot/api"
WEBAPP_URL    = os.getenv("WEBAPP_URL", f"https://{os.getenv('REPLIT_DEV_DOMAIN','localhost')}")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────── DATABASE ───────────────────────────
DB_PATH = "lroulette.db"

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            balance    INTEGER DEFAULT 0,
            total_won  INTEGER DEFAULT 0,
            total_bet  INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS rooms (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT,
            creator_id  INTEGER DEFAULT 0,
            min_players INTEGER DEFAULT 3,
            max_players INTEGER DEFAULT 0,
            status      TEXT DEFAULT 'waiting',
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS room_participants (
            room_id    INTEGER,
            user_id    INTEGER,
            bet_amount INTEGER,
            joined_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (room_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS history (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            room_name        TEXT DEFAULT 'Стандартная',
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
        INSERT OR IGNORE INTO rooms (id, name, creator_id, min_players, max_players, status)
            VALUES (1, '🎰 Стандартная', 0, 3, 0, 'waiting');
        """)
    # Migrations
    try:
        with get_conn() as conn:
            conn.execute("DROP TABLE IF EXISTS current_room")
            conn.commit()
    except Exception:
        pass
    for col_sql in [
        "ALTER TABLE owner_stats ADD COLUMN withdraw_commission INTEGER DEFAULT 0",
        "ALTER TABLE owner_stats ADD COLUMN total_deposited     INTEGER DEFAULT 0",
    ]:
        try:
            with get_conn() as conn:
                conn.execute(col_sql)
                conn.commit()
        except Exception:
            pass
    # Migrate balance/total_won/total_bet columns to REAL for kopek support
    try:
        with get_conn() as conn:
            col_info = conn.execute("PRAGMA table_info(users)").fetchall()
            bal_type = next((c["type"] for c in col_info if c["name"] == "balance"), "REAL")
            if bal_type != "REAL":
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS users_v2 (
                        user_id    INTEGER PRIMARY KEY,
                        username   TEXT,
                        balance    REAL DEFAULT 0.0,
                        total_won  REAL DEFAULT 0.0,
                        total_bet  REAL DEFAULT 0.0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );
                    INSERT OR IGNORE INTO users_v2
                        SELECT user_id, username,
                               CAST(balance   AS REAL),
                               CAST(total_won AS REAL),
                               CAST(total_bet AS REAL),
                               created_at
                        FROM users;
                    DROP TABLE users;
                    ALTER TABLE users_v2 RENAME TO users;
                """)
                conn.commit()
    except Exception:
        pass
    log.info("Database initialised.")

# ─── USER HELPERS ──────────────────────────────────────────────
def ensure_user(user_id: int, username: Optional[str]):
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?,?)", (user_id, username or ""))
        if username:
            conn.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
        conn.commit()

def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def get_balance(user_id: int) -> float:
    u = get_user(user_id)
    return float(u["balance"]) if u else 0.0

def change_balance(user_id: int, delta: float):
    with get_conn() as conn:
        conn.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (delta, user_id))
        conn.commit()

def get_all_user_ids():
    with get_conn() as conn:
        return [r["user_id"] for r in conn.execute("SELECT user_id FROM users").fetchall()]

def get_stats():
    with get_conn() as conn:
        players       = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        total_balance = conn.execute("SELECT COALESCE(SUM(balance),0) as s FROM users").fetchone()["s"]
        profit        = get_owner_profit()
        return players, total_balance, profit

def get_extended_stats() -> dict:
    with get_conn() as conn:
        players       = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        total_balance = conn.execute("SELECT COALESCE(SUM(balance),0) as s FROM users").fetchone()["s"]
        total_bet     = conn.execute("SELECT COALESCE(SUM(total_bet),0) as s FROM users").fetchone()["s"]
        total_won     = conn.execute("SELECT COALESCE(SUM(total_won),0) as s FROM users").fetchone()["s"]
        games_played  = conn.execute("SELECT COUNT(*) as c FROM history").fetchone()["c"]
        row           = conn.execute("SELECT * FROM owner_stats WHERE id=1").fetchone()
        game_profit   = row["total_profit"] if row else 0
        commission    = row["withdraw_commission"] if row and "withdraw_commission" in row.keys() else 0
        deposited     = row["total_deposited"]     if row and "total_deposited"     in row.keys() else 0
        w_done_row    = conn.execute(
            "SELECT COUNT(*) as c, COALESCE(SUM(amount),0) as s FROM withdrawals WHERE status='completed'"
        ).fetchone()
        w_pend_row    = conn.execute(
            "SELECT COUNT(*) as c, COALESCE(SUM(amount),0) as s FROM withdrawals WHERE status='pending'"
        ).fetchone()
        active_rooms  = conn.execute(
            "SELECT COUNT(*) as c FROM rooms WHERE status IN ('waiting','countdown')"
        ).fetchone()["c"]
        in_play       = conn.execute(
            "SELECT COUNT(*) as c FROM room_participants rp "
            "JOIN rooms r ON r.id=rp.room_id WHERE r.status IN ('waiting','countdown')"
        ).fetchone()["c"]
    return {
        "players":        players,
        "total_balance":  total_balance,
        "total_bet":      total_bet,
        "total_won":      total_won,
        "games_played":   games_played,
        "game_profit":    game_profit,
        "commission":     commission,
        "deposited":      deposited,
        "w_done_count":   w_done_row["c"],
        "w_done_sum":     w_done_row["s"],
        "w_pend_count":   w_pend_row["c"],
        "w_pend_sum":     w_pend_row["s"],
        "active_rooms":   active_rooms,
        "in_play":        in_play,
    }

def add_withdraw_commission(amount: int):
    with get_conn() as conn:
        conn.execute("UPDATE owner_stats SET withdraw_commission=withdraw_commission+? WHERE id=1", (amount,))
        conn.commit()

def add_total_deposited(amount: int):
    with get_conn() as conn:
        conn.execute("UPDATE owner_stats SET total_deposited=total_deposited+? WHERE id=1", (amount,))
        conn.commit()

# ─── ROOM HELPERS ──────────────────────────────────────────────
def get_room(room_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()

def get_active_rooms():
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM rooms WHERE status IN ('waiting','countdown') ORDER BY id"
        ).fetchall()

def get_room_participants(room_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT rp.*, u.username FROM room_participants rp "
            "LEFT JOIN users u ON rp.user_id=u.user_id "
            "WHERE rp.room_id=? ORDER BY rp.joined_at", (room_id,)
        ).fetchall()

def get_room_participant(room_id: int, user_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM room_participants WHERE room_id=? AND user_id=?",
            (room_id, user_id)
        ).fetchone()

def get_user_active_room(user_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT r.* FROM rooms r JOIN room_participants rp ON r.id=rp.room_id "
            "WHERE rp.user_id=? AND r.status IN ('waiting','countdown')",
            (user_id,)
        ).fetchone()

def join_room(room_id: int, user_id: int, amount: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO room_participants (room_id, user_id, bet_amount) VALUES (?,?,?)",
            (room_id, user_id, amount)
        )
        conn.execute(
            "UPDATE users SET balance=balance-?, total_bet=total_bet+? WHERE user_id=?",
            (amount, amount, user_id)
        )
        conn.commit()

def kick_from_room(room_id: int, user_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT bet_amount FROM room_participants WHERE room_id=? AND user_id=?",
            (room_id, user_id)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET balance=balance+?, total_bet=total_bet-? WHERE user_id=?",
                (row["bet_amount"], row["bet_amount"], user_id)
            )
        conn.execute("DELETE FROM room_participants WHERE room_id=? AND user_id=?", (room_id, user_id))
        conn.commit()

def create_room_db(name: str, creator_id: int, min_players: int, max_players: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO rooms (name, creator_id, min_players, max_players, status) VALUES (?,?,?,?,'waiting')",
            (name, creator_id, min_players, max_players)
        )
        conn.commit()
        return cur.lastrowid

def set_room_status(room_id: int, status: str):
    with get_conn() as conn:
        conn.execute("UPDATE rooms SET status=? WHERE id=?", (status, room_id))
        conn.commit()

def delete_room_db(room_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM room_participants WHERE room_id=?", (room_id,))
        conn.execute("DELETE FROM rooms WHERE id=?", (room_id,))
        conn.commit()

def reset_default_room():
    with get_conn() as conn:
        conn.execute("DELETE FROM room_participants WHERE room_id=?", (DEFAULT_ROOM_ID,))
        conn.execute("UPDATE rooms SET status='waiting' WHERE id=?", (DEFAULT_ROOM_ID,))
        conn.commit()

# ─── STATS / HISTORY / WITHDRAWAL HELPERS ─────────────────────
def get_owner_profit() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT total_profit FROM owner_stats WHERE id=1").fetchone()
        return row["total_profit"] if row else 0

def add_owner_profit(amount: int):
    with get_conn() as conn:
        conn.execute("UPDATE owner_stats SET total_profit=total_profit+? WHERE id=1", (amount,))
        conn.commit()

def add_history(room_name: str, winner_id: int, winner_username: str, prize: int, bank: int, players_count: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO history (room_name, winner_id, winner_username, prize, bank, players_count) VALUES (?,?,?,?,?,?)",
            (room_name, winner_id, winner_username, prize, bank, players_count)
        )
        conn.commit()

def get_history(limit=10):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM history ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()

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
    kb.button(text="🎲 Комнаты",         callback_data="rooms_list")
    kb.button(text="➕ Создать комнату",  callback_data="create_room")
    kb.button(text="💣 Мины",            callback_data="mines_start")
    kb.button(text="💰 Мой баланс",       callback_data="balance")
    kb.button(text="📜 История",          callback_data="history")
    kb.button(text="🆘 Помощь",           callback_data="help")
    kb.adjust(2, 1, 1, 2)
    return kb.as_markup()

def back_to_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Главное меню", callback_data="main_menu")
    return kb.as_markup()

def admin_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Заявки на вывод",  callback_data="admin_withdrawals")
    kb.button(text="📊 Статистика",        callback_data="admin_stats")
    kb.button(text="🔄 Сбросить комнату",  callback_data="admin_reset")
    kb.button(text="📢 Рассылка",          callback_data="admin_broadcast")
    kb.adjust(2, 2)
    return kb.as_markup()

# ─────────────────── STATES ───────────────────────────────────
class TopupState(StatesGroup):
    waiting_amount = State()
    waiting_check  = State()

class WithdrawState(StatesGroup):
    waiting_amount  = State()
    waiting_details = State()

class CreateRoomState(StatesGroup):
    waiting_name = State()
    waiting_min  = State()
    waiting_max  = State()
    waiting_bet  = State()

class JoinRoomState(StatesGroup):
    waiting_bet = State()

class BroadcastState(StatesGroup):
    waiting_text = State()

class MinesState(StatesGroup):
    waiting_bet = State()

# ─────────────────── GLOBALS ──────────────────────────────────
bot: Bot = None
dp: Dispatcher = None
router = Router()
_room_tasks:         dict = {}  # room_id -> countdown asyncio.Task
_room_timeout_tasks: dict = {}  # room_id -> timeout asyncio.Task
_mines_games:        dict = {}  # user_id -> active mines game state

# ─────────────────── HELPERS ──────────────────────────────────
def escape_md(text: str) -> str:
    special = r"\_*[]()~`>#+-=|{}.!"
    for ch in special:
        text = text.replace(ch, f"\\{ch}")
    return text

def fmt_user(user_id: int, username: Optional[str]) -> str:
    return f"@{username}" if username else str(user_id)

# ─────────────────── ROOM TIMEOUT ─────────────────────────────
async def room_timeout_job(room_id: int):
    await asyncio.sleep(ROOM_TIMEOUT)
    room = get_room(room_id)
    if not room or room["status"] != "waiting":
        return
    participants = get_room_participants(room_id)
    if len(participants) >= room["min_players"]:
        return
    log.info(f"Room {room_id} timed out, refunding.")
    for p in participants:
        uid = p["user_id"]
        bet = p["bet_amount"]
        change_balance(uid, bet)
        try:
            await bot.send_message(
                uid,
                f"⏰ Комната *{escape_md(room['name'])}* не набрала игроков за 1 час\\.\n"
                f"Ставка *{bet} ₽* возвращена\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            log.warning(e)
    if room_id == DEFAULT_ROOM_ID:
        reset_default_room()
    else:
        delete_room_db(room_id)
    _room_timeout_tasks.pop(room_id, None)

def start_room_timeout(room_id: int):
    if room_id in _room_timeout_tasks and not _room_timeout_tasks[room_id].done():
        return
    _room_timeout_tasks[room_id] = asyncio.create_task(room_timeout_job(room_id))

def cancel_room_timeout(room_id: int):
    task = _room_timeout_tasks.pop(room_id, None)
    if task and not task.done():
        task.cancel()

# ─────────────────── ROULETTE LOGIC ───────────────────────────
async def run_room_roulette(room_id: int, delay: int = ROULETTE_DELAY):
    try:
        await asyncio.sleep(delay)
        room         = get_room(room_id)
        participants = get_room_participants(room_id)
        if not participants or not room:
            return

        cancel_room_timeout(room_id)
        set_room_status(room_id, "finished")

        pool = []
        for p in participants:
            pool.extend([p["user_id"]] * p["bet_amount"])

        winner_id  = random.choice(pool)
        bank       = sum(p["bet_amount"] for p in participants)
        prize      = int(bank * PRIZE_PERCENT)
        owner_cut  = int(bank * OWNER_PERCENT)

        winner_row    = get_user(winner_id)
        winner_uname  = winner_row["username"] if winner_row else None
        winner_display = fmt_user(winner_id, winner_uname)

        change_balance(winner_id, prize)
        with get_conn() as conn:
            conn.execute("UPDATE users SET total_won=total_won+? WHERE user_id=?", (prize, winner_id))
            conn.commit()
        add_owner_profit(owner_cut)
        add_history(room["name"], winner_id, winner_display, prize, bank, len(participants))

        bets_map = {p["user_id"]: p["bet_amount"] for p in participants}
        for p in participants:
            uid = p["user_id"]
            try:
                await bot.send_message(
                    uid,
                    f"🏆 *Результат рулетки\\!*\n\n"
                    f"Комната: *{escape_md(room['name'])}*\n"
                    f"Победитель: *{escape_md(winner_display)}*\n"
                    f"Выигрыш: *{prize} ₽*\n"
                    f"Банк: *{bank} ₽*\n"
                    f"Твоя ставка: *{bets_map.get(uid, 0)} ₽*",
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=main_keyboard()
                )
            except Exception as e:
                log.warning(f"notify {uid}: {e}")

        if room_id == DEFAULT_ROOM_ID:
            reset_default_room()
        else:
            delete_room_db(room_id)
        log.info(f"Room {room_id} done. Winner: {winner_id}, prize: {prize}")
    except asyncio.CancelledError:
        log.info(f"Room {room_id} roulette cancelled.")
    finally:
        _room_tasks.pop(room_id, None)

def start_room_roulette(room_id: int, delay: int = ROULETTE_DELAY) -> bool:
    if room_id in _room_tasks and not _room_tasks[room_id].done():
        return False
    set_room_status(room_id, "countdown")
    cancel_room_timeout(room_id)
    _room_tasks[room_id] = asyncio.create_task(run_room_roulette(room_id, delay))
    return True

def cancel_room_roulette(room_id: int):
    task = _room_tasks.pop(room_id, None)
    if task and not task.done():
        task.cancel()

async def notify_room(room_id: int, text: str):
    for p in get_room_participants(room_id):
        try:
            await bot.send_message(p["user_id"], text, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            log.warning(e)

# ─────────────────── /start ───────────────────────────────────
@router.message(CommandStart())
async def cmd_start(msg: Message):
    uid = msg.from_user.id
    ensure_user(uid, msg.from_user.username)
    caption = (
        "🎰 *LRoulette* — Ставки\\. Банк\\. Победа\\.\n\n"
        "*Как играть?*\n"
        "• Зайди в комнату и сделай ставку \\(50–2000 ₽\\)\n"
        "• При наборе мин\\. игроков начинается отсчёт 5 мин\n"
        "• Победитель получает *80%* банка\n\n"
        "*Вывод:* СБП или TRC20 \\(USDT\\)\n"
        "*Вопросы* — @LRoulette\\_support"
    )
    try:
        photo = FSInputFile("preview.jpg")
        await bot.send_photo(uid, photo=photo, caption=caption, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        log.warning(f"preview: {e}")
    webapp_kb = InlineKeyboardBuilder()
    webapp_kb.button(text="🚀 Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL))
    await msg.answer("👇 *Выбирай действие:* 👇", parse_mode=ParseMode.MARKDOWN_V2, reply_markup=main_keyboard())
    await msg.answer("📱 Или открой мини-приложение:", reply_markup=webapp_kb.as_markup())

# ─────────────────── ROOMS LIST ───────────────────────────────
@router.callback_query(F.data == "rooms_list")
async def cb_rooms_list(cq: CallbackQuery):
    ensure_user(cq.from_user.id, cq.from_user.username)
    rooms = get_active_rooms()

    kb = InlineKeyboardBuilder()
    lines = ["🎲 *Активные комнаты*\n"]

    for room in rooms:
        parts = get_room_participants(room["id"])
        count = len(parts)
        bank  = sum(p["bet_amount"] for p in parts)
        max_s = f"/{room['max_players']}" if room["max_players"] > 0 else ""
        icon  = "⏳" if room["status"] == "countdown" else "🟢"
        lines.append(
            f"{icon} *{escape_md(room['name'])}*\n"
            f"  Игроков: {count}{escape_md(max_s)} \\| Мин\\.: {room['min_players']} \\| Банк: {bank} ₽\n"
        )
        kb.button(text=f"🚪 {room['name']}", callback_data=f"join_room:{room['id']}")

    if not rooms:
        lines.append("Нет активных комнат\\. Создай первую\\!")

    kb.button(text="➕ Создать комнату", callback_data="create_room")
    kb.button(text="🔄 Обновить",        callback_data="rooms_list")
    kb.button(text="🏠 Главное меню",    callback_data="main_menu")
    kb.adjust(1)

    await cq.message.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb.as_markup())
    await cq.answer()

# ─────────────────── JOIN ROOM ────────────────────────────────
@router.callback_query(F.data.startswith("join_room:"))
async def cb_join_room(cq: CallbackQuery, state: FSMContext):
    uid     = cq.from_user.id
    ensure_user(uid, cq.from_user.username)
    room_id = int(cq.data.split(":")[1])
    room    = get_room(room_id)

    if not room or room["status"] not in ("waiting", "countdown"):
        await cq.answer("Комната недоступна.", show_alert=True)
        return

    existing = get_user_active_room(uid)
    if existing:
        if existing["id"] == room_id:
            await _show_room_info(cq, room_id)
            await cq.answer()
            return
        await cq.answer(f"Ты уже в комнате «{existing['name']}».", show_alert=True)
        return

    parts = get_room_participants(room_id)
    if room["max_players"] > 0 and len(parts) >= room["max_players"]:
        await cq.answer("Комната заполнена.", show_alert=True)
        return

    bal = get_balance(uid)
    await state.update_data(join_room_id=room_id)
    await state.set_state(JoinRoomState.waiting_bet)
    await cq.message.edit_text(
        f"🚪 *Вход в комнату «{escape_md(room['name'])}»*\n\n"
        f"Твой баланс: *{bal} ₽*\n"
        f"Ставка: от *{MIN_BET}* до *{MAX_BET}* ₽\n\n"
        f"Введи сумму ставки:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=back_to_menu_kb()
    )
    await cq.answer()

@router.message(JoinRoomState.waiting_bet)
async def join_room_bet(msg: Message, state: FSMContext):
    uid  = msg.from_user.id
    data = await state.get_data()
    room_id = data.get("join_room_id")
    if not room_id:
        await state.clear()
        return

    try:
        amount = int(msg.text.strip())
    except ValueError:
        await msg.answer("Введи целое число рублей\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    if amount < MIN_BET or amount > MAX_BET:
        await msg.answer(f"Ставка от *{MIN_BET}* до *{MAX_BET}* ₽\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    bal = get_balance(uid)
    if bal < amount:
        await msg.answer(f"Недостаточно средств\\. Баланс: *{bal} ₽*\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    room = get_room(room_id)
    if not room or room["status"] not in ("waiting", "countdown"):
        await msg.answer("Комната уже недоступна\\.", parse_mode=ParseMode.MARKDOWN_V2)
        await state.clear()
        return

    parts = get_room_participants(room_id)
    if room["max_players"] > 0 and len(parts) >= room["max_players"]:
        await msg.answer("Комната уже заполнена\\.", parse_mode=ParseMode.MARKDOWN_V2)
        await state.clear()
        return

    join_room(room_id, uid, amount)
    await state.clear()

    parts = get_room_participants(room_id)
    count = len(parts)
    bank  = sum(p["bet_amount"] for p in parts)
    chance = round((amount / bank) * 100, 2) if bank > 0 else 0

    # Notify room creator
    if room["creator_id"] not in (0, uid):
        try:
            u = get_user(uid)
            uname = u["username"] if u else None
            await bot.send_message(
                room["creator_id"],
                f"👤 В комнату *{escape_md(room['name'])}* вошёл *{escape_md(fmt_user(uid, uname))}*\\!\n"
                f"Игроков: *{count}*, банк: *{bank} ₽*",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            log.warning(e)

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Моя комната",  callback_data=f"room_info:{room_id}")
    kb.button(text="🏠 Главное меню", callback_data="main_menu")
    kb.adjust(2)

    await msg.answer(
        f"✅ *Ставка принята\\!*\n\n"
        f"Комната: *{escape_md(room['name'])}*\n"
        f"Твоя ставка: *{amount} ₽*\n"
        f"Шанс победы: *{escape_md(str(chance))}%*\n"
        f"Участников: *{count}*, банк: *{bank} ₽*",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb.as_markup()
    )

    # Start timeout on first player
    if count == 1:
        start_room_timeout(room_id)

    # Auto countdown when min reached
    if count >= room["min_players"] and room["status"] == "waiting":
        delay = DEFAULT_ROOM_DELAY if room_id == DEFAULT_ROOM_ID else CUSTOM_ROOM_DELAY
        mins  = 10 if room_id == DEFAULT_ROOM_ID else 5
        if start_room_roulette(room_id, delay):
            await notify_room(
                room_id,
                f"🎲 *Набрано {room['min_players']} игроков в «{escape_md(room['name'])}»\\!*\n"
                f"Рулетка стартует через *{mins} минут*\\.\\.\\."
            )

    # Instant start when max reached
    if room["max_players"] > 0 and count >= room["max_players"]:
        if start_room_roulette(room_id, 5):
            await notify_room(
                room_id,
                f"🎲 *Комната «{escape_md(room['name'])}» заполнена\\!* Рулетка запускается\\!"
            )

# ─────────────────── ROOM INFO ────────────────────────────────
@router.callback_query(F.data.startswith("room_info:"))
async def cb_room_info(cq: CallbackQuery):
    room_id = int(cq.data.split(":")[1])
    await _show_room_info(cq, room_id)
    await cq.answer()

async def _show_room_info(cq: CallbackQuery, room_id: int):
    uid  = cq.from_user.id
    room = get_room(room_id)
    if not room:
        await cq.message.edit_text("Комната не найдена\\.", parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_to_menu_kb())
        return

    parts  = get_room_participants(room_id)
    count  = len(parts)
    bank   = sum(p["bet_amount"] for p in parts)
    my_bet = get_room_participant(room_id, uid)
    max_s  = f"/{room['max_players']}" if room["max_players"] > 0 else ""
    status = "⏳ Идёт отсчёт 5 мин" if room["status"] == "countdown" else "🟢 Ожидание"

    lines = [
        f"📊 *Комната: {escape_md(room['name'])}*\n",
        f"Статус: {status}",
        f"Игроков: *{count}{escape_md(max_s)}* \\(мин\\. {room['min_players']}\\)",
        f"Банк: *{bank} ₽*",
    ]
    if my_bet:
        chance = round((my_bet["bet_amount"] / bank) * 100, 2) if bank > 0 else 0
        lines.append(f"Твоя ставка: *{my_bet['bet_amount']} ₽* \\({escape_md(str(chance))}%\\)")

    kb = InlineKeyboardBuilder()
    if room["creator_id"] == uid:
        kb.button(text="⚙️ Управление",     callback_data=f"manage_room:{room_id}")
    if my_bet and room["status"] == "waiting":
        kb.button(text="🚪 Покинуть",        callback_data=f"leave_room:{room_id}")
    kb.button(text="🔄 Обновить",            callback_data=f"room_info:{room_id}")
    kb.button(text="🏠 Главное меню",        callback_data="main_menu")
    kb.adjust(1)

    await cq.message.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb.as_markup())

# ─────────────────── LEAVE ROOM ───────────────────────────────
@router.callback_query(F.data.startswith("leave_room:"))
async def cb_leave_room(cq: CallbackQuery):
    uid     = cq.from_user.id
    room_id = int(cq.data.split(":")[1])
    room    = get_room(room_id)

    if not get_room_participant(room_id, uid):
        await cq.answer("Ты не в этой комнате.", show_alert=True)
        return
    if room and room["status"] == "countdown":
        await cq.answer("Рулетка уже запущена, выйти нельзя.", show_alert=True)
        return

    kick_from_room(room_id, uid)
    await cq.answer("Ты покинул комнату. Ставка возвращена.", show_alert=True)

    if room and room_id != DEFAULT_ROOM_ID:
        if len(get_room_participants(room_id)) == 0:
            cancel_room_timeout(room_id)
            delete_room_db(room_id)

    await cq.message.edit_text("🏠 *Главное меню*", parse_mode=ParseMode.MARKDOWN_V2, reply_markup=main_keyboard())

# ─────────────────── MANAGE ROOM ──────────────────────────────
@router.callback_query(F.data.startswith("manage_room:"))
async def cb_manage_room(cq: CallbackQuery):
    uid     = cq.from_user.id
    room_id = int(cq.data.split(":")[1])
    room    = get_room(room_id)

    if not room or room["creator_id"] != uid:
        await cq.answer("Нет доступа.", show_alert=True)
        return

    parts = get_room_participants(room_id)
    count = len(parts)
    bank  = sum(p["bet_amount"] for p in parts)

    lines = [
        f"⚙️ *Управление: {escape_md(room['name'])}*\n",
        f"Игроков: *{count}*, банк: *{bank} ₽*\n",
        "*Участники:*"
    ]
    kb = InlineKeyboardBuilder()
    for p in parts:
        display = fmt_user(p["user_id"], p["username"])
        lines.append(f"• {escape_md(display)} — {p['bet_amount']} ₽")
        if p["user_id"] != uid:
            kb.button(
                text=f"❌ Выгнать {p['username'] or p['user_id']}",
                callback_data=f"kick:{room_id}:{p['user_id']}"
            )

    if count >= room["min_players"] and room["status"] == "waiting":
        kb.button(text="▶️ Запустить рулетку", callback_data=f"force_start:{room_id}")
    if room["status"] == "waiting":
        kb.button(text="🗑 Удалить комнату",    callback_data=f"delete_room:{room_id}")
    kb.button(text="🔙 Назад",                  callback_data=f"room_info:{room_id}")
    kb.adjust(1)

    await cq.message.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb.as_markup())
    await cq.answer()

# ─────────────────── KICK ─────────────────────────────────────
@router.callback_query(F.data.startswith("kick:"))
async def cb_kick(cq: CallbackQuery):
    uid = cq.from_user.id
    _, room_id_s, target_s = cq.data.split(":")
    room_id   = int(room_id_s)
    target_id = int(target_s)
    room      = get_room(room_id)

    if not room or room["creator_id"] != uid:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if room["status"] == "countdown":
        await cq.answer("Рулетка уже запущена.", show_alert=True)
        return

    kick_from_room(room_id, target_id)
    try:
        await bot.send_message(
            target_id,
            f"❌ Тебя выгнали из комнаты *{escape_md(room['name'])}*\\. Ставка возвращена\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        log.warning(e)

    await cq.answer("Игрок выгнан.", show_alert=True)
    await cb_manage_room(cq)

# ─────────────────── FORCE START ──────────────────────────────
@router.callback_query(F.data.startswith("force_start:"))
async def cb_force_start(cq: CallbackQuery):
    uid     = cq.from_user.id
    room_id = int(cq.data.split(":")[1])
    room    = get_room(room_id)

    if not room or room["creator_id"] != uid:
        await cq.answer("Нет доступа.", show_alert=True)
        return

    parts = get_room_participants(room_id)
    if len(parts) < room["min_players"]:
        await cq.answer(f"Нужно минимум {room['min_players']} игроков.", show_alert=True)
        return

    if not start_room_roulette(room_id, ROULETTE_DELAY):
        await cq.answer("Рулетка уже запущена.", show_alert=True)
        return

    await notify_room(
        room_id,
        f"▶️ *Создатель запустил рулетку в «{escape_md(room['name'])}»\\!*\nПобедитель через *5 минут*\\."
    )
    await cq.answer("Рулетка запущена!", show_alert=True)
    await cq.message.edit_text(
        f"▶️ Рулетка в комнате *{escape_md(room['name'])}* запущена\\!",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=back_to_menu_kb()
    )

# ─────────────────── DELETE ROOM ──────────────────────────────
@router.callback_query(F.data.startswith("delete_room:"))
async def cb_delete_room(cq: CallbackQuery):
    uid     = cq.from_user.id
    room_id = int(cq.data.split(":")[1])
    room    = get_room(room_id)

    if not room or room["creator_id"] != uid:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    if room_id == DEFAULT_ROOM_ID:
        await cq.answer("Стандартную комнату нельзя удалить.", show_alert=True)
        return

    for p in get_room_participants(room_id):
        kick_from_room(room_id, p["user_id"])
        try:
            await bot.send_message(
                p["user_id"],
                f"🗑 Комната *{escape_md(room['name'])}* удалена создателем\\. Ставка возвращена\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            log.warning(e)

    cancel_room_timeout(room_id)
    cancel_room_roulette(room_id)
    delete_room_db(room_id)

    await cq.answer("Комната удалена.", show_alert=True)
    await cq.message.edit_text("🏠 *Главное меню*", parse_mode=ParseMode.MARKDOWN_V2, reply_markup=main_keyboard())

# ─────────────────── CREATE ROOM ──────────────────────────────
@router.callback_query(F.data == "create_room")
async def cb_create_room(cq: CallbackQuery, state: FSMContext):
    uid = cq.from_user.id
    ensure_user(uid, cq.from_user.username)
    existing = get_user_active_room(uid)
    if existing:
        await cq.answer(f"Ты уже в комнате «{existing['name']}».", show_alert=True)
        return
    await state.set_state(CreateRoomState.waiting_name)
    await cq.message.edit_text(
        "➕ *Создание комнаты*\n\nВведи название \\(макс\\. 30 символов\\):",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=back_to_menu_kb()
    )
    await cq.answer()

@router.message(CreateRoomState.waiting_name)
async def create_room_name(msg: Message, state: FSMContext):
    name = msg.text.strip()[:30]
    if not name:
        await msg.answer("Название не может быть пустым\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    await state.update_data(room_name=name)
    await state.set_state(CreateRoomState.waiting_min)
    await msg.answer(
        f"Название: *{escape_md(name)}*\n\nВведи *минимальное* кол\\-во игроков \\(2–20\\):",
        parse_mode=ParseMode.MARKDOWN_V2
    )

@router.message(CreateRoomState.waiting_min)
async def create_room_min(msg: Message, state: FSMContext):
    try:
        min_p = int(msg.text.strip())
        if not 2 <= min_p <= 20:
            raise ValueError
    except ValueError:
        await msg.answer("Введи число от 2 до 20\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    await state.update_data(room_min=min_p)
    await state.set_state(CreateRoomState.waiting_max)
    await msg.answer(
        f"Мин\\. игроков: *{min_p}*\n\nВведи *максимальное* кол\\-во \\(или *0* — без ограничений\\):",
        parse_mode=ParseMode.MARKDOWN_V2
    )

@router.message(CreateRoomState.waiting_max)
async def create_room_max(msg: Message, state: FSMContext):
    data  = await state.get_data()
    min_p = data["room_min"]
    try:
        max_p = int(msg.text.strip())
        if max_p != 0 and max_p < min_p:
            raise ValueError
    except ValueError:
        await msg.answer(
            f"Введи число ≥ {min_p} или 0 для неограниченного\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return
    await state.update_data(room_max=max_p)
    await state.set_state(CreateRoomState.waiting_bet)
    bal = get_balance(msg.from_user.id)
    max_disp = escape_md("∞") if max_p == 0 else str(max_p)
    await msg.answer(
        f"Макс\\. игроков: *{max_disp}*\n\n"
        f"Твой баланс: *{bal} ₽*\n"
        f"Введи свою начальную ставку \\({MIN_BET}–{MAX_BET} ₽\\):",
        parse_mode=ParseMode.MARKDOWN_V2
    )

@router.message(CreateRoomState.waiting_bet)
async def create_room_bet(msg: Message, state: FSMContext):
    uid  = msg.from_user.id
    data = await state.get_data()
    try:
        amount = int(msg.text.strip())
        if not MIN_BET <= amount <= MAX_BET:
            raise ValueError
    except ValueError:
        await msg.answer(f"Ставка от *{MIN_BET}* до *{MAX_BET}* ₽\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    bal = get_balance(uid)
    if bal < amount:
        await msg.answer(f"Недостаточно средств\\. Баланс: *{bal} ₽*\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    room_name = data["room_name"]
    min_p     = data["room_min"]
    max_p     = data["room_max"]

    room_id = create_room_db(room_name, uid, min_p, max_p)
    join_room(room_id, uid, amount)
    start_room_timeout(room_id)
    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(text="⚙️ Управление комнатой", callback_data=f"manage_room:{room_id}")
    kb.button(text="🏠 Главное меню",         callback_data="main_menu")
    kb.adjust(1)

    max_disp = "∞" if max_p == 0 else str(max_p)
    await msg.answer(
        f"✅ *Комната создана\\!*\n\n"
        f"Название: *{escape_md(room_name)}*\n"
        f"Мин\\. игроков: *{min_p}*, макс\\.: *{escape_md(max_disp)}*\n"
        f"Твоя ставка: *{amount} ₽*\n\n"
        f"Жди игроков или поделись ссылкой на бота\\!",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb.as_markup()
    )

# ─────────────────── BALANCE ──────────────────────────────────
@router.callback_query(F.data == "balance")
async def cb_balance(cq: CallbackQuery):
    uid = cq.from_user.id
    ensure_user(uid, cq.from_user.username)
    bal = get_balance(uid)

    kb = InlineKeyboardBuilder()
    kb.button(text="💎 Пополнить через CryptoBot (USDT)", callback_data="topup")
    kb.button(text="💳 Пополнить через поддержку (карта)", callback_data="topup_card")
    kb.button(text="💸 Вывести средства",                  callback_data="withdraw")
    kb.button(text="🏠 Главное меню",                      callback_data="main_menu")
    kb.adjust(1)

    await cq.message.edit_text(
        f"💰 *Твой баланс:* *{bal} ₽*\n\nВыбери действие:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb.as_markup()
    )
    await cq.answer()

# ─────────────────── TOP-UP ───────────────────────────────────
@router.callback_query(F.data == "topup_card")
async def cb_topup_card(cq: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Написать в поддержку", url="https://t.me/LRoulette_support")
    kb.button(text="🏠 Главное меню",         callback_data="main_menu")
    kb.adjust(1)
    await cq.message.edit_text(
        "💳 *Пополнение через карту*\n\n"
        "Напиши в поддержку: @LRoulette\\_support\n\n"
        "Укажи:\n"
        "• Сумму пополнения \\(в рублях\\)\n"
        "• Свой Telegram ID \\(узнай у @userinfobot\\)\n\n"
        "Средства зачислят вручную в течение нескольких минут\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb.as_markup()
    )
    await cq.answer()

@router.callback_query(F.data == "topup")
async def cb_topup(cq: CallbackQuery, state: FSMContext):
    await state.set_state(TopupState.waiting_amount)
    await cq.message.edit_text(
        "💳 *Пополнение баланса*\n\nВведи сумму в *рублях* \\(минимум 90 ₽ \\= 1 USDT\\):",
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
    invoice     = await create_invoice(amount_usdt)
    if not invoice:
        await msg.answer("❌ Ошибка создания инвойса\\. Попробуй позже\\.", parse_mode=ParseMode.MARKDOWN_V2)
        await state.clear()
        return

    invoice_id = invoice.get("invoice_id") or invoice.get("id")
    pay_url    = invoice.get("pay_url") or invoice.get("bot_invoice_url", "")

    await state.update_data(invoice_id=str(invoice_id), amount_rub=amount_rub)
    await state.set_state(TopupState.waiting_check)

    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оплатить через CryptoBot",  url=pay_url)
    kb.button(text="✅ Я оплатил — проверить",      callback_data=f"check_pay:{invoice_id}:{amount_rub}")
    kb.button(text="🏠 Главное меню",               callback_data="main_menu")
    kb.adjust(1)

    await msg.answer(
        f"📄 *Инвойс создан\\!*\n\n"
        f"Сумма: *{escape_md(str(amount_usdt))} USDT* \\({amount_rub} ₽\\)\n"
        f"Курс: 1 USDT \\= {USDT_TO_RUB} ₽\n\n"
        f"После оплаты нажми *«Я оплатил — проверить»*\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb.as_markup()
    )

@router.callback_query(F.data.startswith("check_pay:"))
async def cb_check_pay(cq: CallbackQuery, state: FSMContext):
    parts      = cq.data.split(":")
    invoice_id = parts[1]
    amount_rub = int(parts[2])
    uid        = cq.from_user.id

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
        add_total_deposited(amount_rub)
        await state.clear()
        kb = InlineKeyboardBuilder()
        kb.button(text="🎲 Комнаты",      callback_data="rooms_list")
        kb.button(text="🏠 Главное меню", callback_data="main_menu")
        kb.adjust(1)
        await cq.message.edit_text(
            f"✅ *Оплата подтверждена\\!*\n\nНачислено: *{amount_rub} ₽*\nБаланс: *{get_balance(uid)} ₽*",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb.as_markup()
        )
        await cq.answer()
    elif status == "expired":
        await cq.answer("Инвойс истёк. Создай новый.", show_alert=True)
        await state.clear()
    else:
        await cq.answer("Оплата ещё не поступила. Подожди и попробуй снова.", show_alert=True)

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
    if not msg.text or msg.text.startswith("/"):
        await state.clear()
        return
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

    # Encode amount in callback_data — no state needed for method click
    await state.clear()
    commission_sbp = int(amount * COMMISSION_SBP)
    payout_sbp     = amount - commission_sbp
    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"📱 СБП — получишь {payout_sbp} ₽ (комиссия 5%)",
        callback_data=f"wmethod:sbp:{amount}"
    )
    kb.button(
        text=f"🔗 TRC20 — получишь {amount} ₽ (комиссия 0%)",
        callback_data=f"wmethod:trc20:{amount}"
    )
    kb.button(text="🏠 Главное меню", callback_data="main_menu")
    kb.adjust(1)
    await msg.answer(
        f"Сумма к списанию: *{amount} ₽*\n\nВыбери метод вывода:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb.as_markup()
    )

@router.callback_query(F.data.startswith("wmethod:"))
async def cb_withdraw_method(cq: CallbackQuery, state: FSMContext):
    parts  = cq.data.split(":")
    method = parts[1]
    amount = int(parts[2])
    uid    = cq.from_user.id

    bal = get_balance(uid)
    if bal < amount:
        await cq.answer("Недостаточно средств на балансе.", show_alert=True)
        return

    commission = int(amount * COMMISSION_SBP) if method == "sbp" else 0
    payout     = amount - commission

    await state.update_data(w_amount=amount, w_method=method, w_commission=commission, w_payout=payout)
    await state.set_state(WithdrawState.waiting_details)

    if method == "sbp":
        prompt = (
            f"📱 *СБП*\n\n"
            f"Сумма к списанию: *{amount} ₽*\n"
            f"Комиссия: *{commission} ₽* \\(5%\\)\n"
            f"*Получишь: {payout} ₽*\n\n"
            f"Введи номер телефона \\(формат: \\+7XXXXXXXXXX\\):"
        )
    else:
        prompt = (
            f"🔗 *TRC20*\n\n"
            f"Сумма к списанию: *{amount} ₽*\n"
            f"Комиссия: *0 ₽* \\(0%\\)\n"
            f"*Получишь: {amount} ₽*\n\n"
            f"Введи TRC20 адрес кошелька:"
        )
    await cq.message.edit_text(prompt, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_to_menu_kb())
    await cq.answer()

@router.message(WithdrawState.waiting_details)
async def withdraw_details(msg: Message, state: FSMContext):
    uid     = msg.from_user.id
    data    = await state.get_data()
    amount  = data.get("w_amount")
    method  = data.get("w_method")
    details = msg.text.strip()

    if not amount or not method:
        await state.clear()
        await msg.answer("Ошибка сессии\\. Начни вывод заново через «Мой баланс»\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    commission = data.get("w_commission", 0)
    payout     = data.get("w_payout", amount)

    bal = get_balance(uid)
    if bal < amount:
        await msg.answer("Недостаточно средств\\.", parse_mode=ParseMode.MARKDOWN_V2)
        await state.clear()
        return

    change_balance(uid, -amount)
    if commission > 0:
        add_withdraw_commission(commission)
    wid = create_withdrawal(uid, payout, method, details)
    await state.clear()

    method_name = "СБП" if method == "sbp" else "TRC20"
    comm_line   = f"Комиссия: *{commission} ₽*\n" if commission > 0 else "Комиссия: *0 ₽*\n"
    await msg.answer(
        f"✅ *Заявка №{wid} создана\\!*\n\n"
        f"Списано с баланса: *{amount} ₽*\n"
        f"{comm_line}"
        f"К выплате: *{payout} ₽*\n"
        f"Метод: *{method_name}*\n"
        f"Реквизиты: `{escape_md(details)}`\n\n"
        f"Администратор рассмотрит заявку в ближайшее время\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_keyboard()
    )
    try:
        await bot.send_message(
            ADMIN_ID,
            f"💸 *Новая заявка на вывод \\#{wid}*\n\n"
            f"Пользователь: {escape_md(fmt_user(uid, msg.from_user.username))}\n"
            f"Списано: *{amount} ₽* \\| Комиссия: *{commission} ₽* \\| К выплате: *{payout} ₽*\n"
            f"Метод: *{method_name}*\n"
            f"Реквизиты: `{escape_md(details)}`",
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        log.warning(e)

# ─────────────────── MINES GAME ───────────────────────────────
def mines_multiplier(mines: int, revealed: int) -> float:
    """Payout multiplier after opening 'revealed' safe cells. 3% house edge."""
    total = 25
    if revealed == 0:
        return 1.0
    prob = 1.0
    for i in range(revealed):
        prob *= (total - mines - i) / (total - i)
    return round(0.97 / prob, 2)

def mines_next_mult(mines: int, revealed: int) -> float:
    return mines_multiplier(mines, revealed + 1)

def _mine_noun(n: int) -> str:
    if n == 1:
        return "мина"
    if 2 <= n <= 4:
        return "мины"
    return "мин"

def build_mines_kb(uid: int, show_all: bool = False) -> InlineKeyboardMarkup:
    game = _mines_games.get(uid)
    if not game:
        return back_to_menu_kb()
    revealed  = set(game["revealed"])
    mine_pos  = set(game["mines_positions"])
    kb = InlineKeyboardBuilder()
    for i in range(25):
        if i in revealed:
            if i in mine_pos:
                text = "💥"   # mine that was hit
            else:
                text = "💎"   # safe cell
        elif show_all and i in mine_pos:
            text = "💣"       # reveal remaining mines on game over
        else:
            text = "⬜"
        cb = "mines_noop" if (show_all or i in revealed) else f"mines_cell:{i}"
        kb.button(text=text, callback_data=cb)
    kb.adjust(5, 5, 5, 5, 5)
    if not show_all:
        mult   = mines_multiplier(game["mines"], len(revealed))
        payout = round(game["bet"] * mult, 2)
        n_mult = mines_next_mult(game["mines"], len(revealed))
        kb.button(text=f"💰 Забрать {payout:.2f} ₽  (×{mult})", callback_data="mines_cash")
        kb.button(text=f"➡️ Следующая: ×{n_mult}",               callback_data="mines_noop")
        kb.adjust(5, 5, 5, 5, 5, 1, 1)
    kb.button(text="🏠 Главное меню", callback_data="mines_exit")
    return kb.as_markup()

def _mines_status(game: dict) -> str:
    rev   = len(game["revealed"])
    mult  = mines_multiplier(game["mines"], rev)
    pay   = round(game["bet"] * mult, 2)
    nxt   = mines_next_mult(game["mines"], rev)
    return (
        f"💣 *Мины* — {game['mines']} {_mine_noun(game['mines'])}\n\n"
        f"Ставка: *{game['bet']} ₽* \\| Открыто: *{rev}* ячеек\n"
        f"Текущий множитель: *×{escape_md(str(mult))}*\n"
        f"Можно забрать: *{escape_md(f'{pay:.2f}')} ₽*\n"
        f"Следующая ячейка: *×{escape_md(str(nxt))}*"
    )

@router.callback_query(F.data == "mines_start")
async def cb_mines_start(cq: CallbackQuery):
    uid = cq.from_user.id
    ensure_user(uid, cq.from_user.username)

    if uid in _mines_games and _mines_games[uid].get("active"):
        game = _mines_games[uid]
        await cq.message.edit_text(
            _mines_status(game),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_mines_kb(uid)
        )
        await cq.answer()
        return

    kb = InlineKeyboardBuilder()
    for n in [1, 2, 3, 5, 7, 10, 15, 20, 24]:
        kb.button(text=str(n), callback_data=f"mines_count:{n}")
    kb.button(text="🏠 Главное меню", callback_data="main_menu")
    kb.adjust(3, 3, 3, 1)
    await cq.message.edit_text(
        "💣 *Мины*\n\nВыбери количество мин на поле \\(1–24\\):\n\n"
        "_Чем больше мин — тем выше коэффициент_",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb.as_markup()
    )
    await cq.answer()

@router.callback_query(F.data.startswith("mines_count:"))
async def cb_mines_count(cq: CallbackQuery, state: FSMContext):
    uid   = cq.from_user.id
    mines = int(cq.data.split(":")[1])
    bal   = get_balance(uid)

    # Show preview multipliers
    p1  = mines_next_mult(mines, 0)
    p3  = mines_multiplier(mines, 3)
    p5  = mines_multiplier(mines, 5)
    p10 = mines_multiplier(mines, min(10, 25 - mines - 1))

    await state.update_data(mines_count=mines)
    await state.set_state(MinesState.waiting_bet)
    await cq.message.edit_text(
        f"💣 *Мины* — {mines} {_mine_noun(mines)}\n\n"
        f"Множители \\(примерно\\):\n"
        f"  1 ячейка → *×{escape_md(str(p1))}*\n"
        f"  3 ячейки → *×{escape_md(str(p3))}*\n"
        f"  5 ячеек  → *×{escape_md(str(p5))}*\n"
        f"  10 ячеек → *×{escape_md(str(p10))}*\n\n"
        f"Твой баланс: *{escape_md(str(bal))} ₽*\n"
        f"Введи ставку \\(1–1000 ₽\\):",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=back_to_menu_kb()
    )
    await cq.answer()

@router.message(MinesState.waiting_bet)
async def mines_bet_input(msg: Message, state: FSMContext):
    if not msg.text or msg.text.startswith("/"):
        await state.clear()
        return
    uid  = msg.from_user.id
    data = await state.get_data()
    mines = data.get("mines_count", 5)
    try:
        bet = int(msg.text.strip())
        if not 1 <= bet <= 1000:
            raise ValueError
    except ValueError:
        await msg.answer("Ставка от *1* до *1000* ₽\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    bal = get_balance(uid)
    if bal < bet:
        await msg.answer(f"Недостаточно средств\\. Баланс: *{bal} ₽*\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    change_balance(uid, -bet)
    await state.clear()

    mine_positions = random.sample(range(25), mines)
    _mines_games[uid] = {
        "bet":             bet,
        "mines":           mines,
        "mines_positions": mine_positions,
        "revealed":        [],
        "active":          True,
    }
    n_mult = mines_next_mult(mines, 0)
    await msg.answer(
        f"💣 *Мины* — {mines} {_mine_noun(mines)}\n\n"
        f"Ставка: *{bet} ₽*\n"
        f"Открой первую ячейку\\! Множитель: *×{escape_md(str(n_mult))}*\n\n"
        f"Не задень мину\\! 💥",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=build_mines_kb(uid)
    )

@router.callback_query(F.data.startswith("mines_cell:"))
async def cb_mines_cell(cq: CallbackQuery):
    uid  = cq.from_user.id
    cell = int(cq.data.split(":")[1])
    game = _mines_games.get(uid)

    if not game or not game["active"]:
        await cq.answer("Нет активной игры.", show_alert=True)
        return
    if cell in game["revealed"]:
        await cq.answer("Ячейка уже открыта.", show_alert=True)
        return

    game["revealed"].append(cell)

    if cell in game["mines_positions"]:
        # ── ПРОИГРЫШ ────────────────────────────────────────────
        game["active"] = False
        add_owner_profit(game["bet"])
        bet   = game["bet"]
        mines = game["mines"]
        rev   = len(game["revealed"]) - 1
        del _mines_games[uid]
        await cq.message.edit_text(
            f"💥 *МИНА\\! Проигрыш*\n\n"
            f"Ставка: *{bet} ₽* потеряна\n"
            f"Мин на поле: *{mines}*\n"
            f"Открыто безопасных ячеек: *{rev}*",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_mines_kb(uid, show_all=True)
        )
        await cq.answer("💥 Мина! Ты проиграл.", show_alert=True)
        return

    # ── БЕЗОПАСНАЯ ЯЧЕЙКА ────────────────────────────────────────
    revealed_count = len(game["revealed"])
    safe_total     = 25 - game["mines"]
    mult   = mines_multiplier(game["mines"], revealed_count)
    payout = round(game["bet"] * mult, 2)

    if revealed_count >= safe_total:
        # Все безопасные ячейки открыты — авто-кешаут
        game["active"] = False
        change_balance(uid, payout)
        with get_conn() as conn:
            conn.execute("UPDATE users SET total_won=total_won+? WHERE user_id=?", (payout, uid))
            conn.commit()
        profit = game["bet"] - payout
        if profit > 0:
            add_owner_profit(profit)
        bet   = game["bet"]
        mines = game["mines"]
        del _mines_games[uid]
        await cq.message.edit_text(
            f"🏆 *Все ячейки открыты\\!*\n\n"
            f"Ставка: *{bet} ₽*\n"
            f"Множитель: *×{escape_md(str(mult))}*\n"
            f"Выигрыш: *{escape_md(f'{payout:.2f}')} ₽*",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_mines_kb(uid, show_all=True)
        )
        await cq.answer(f"🏆 Выигрыш {payout:.2f} ₽!", show_alert=True)
        return

    await cq.message.edit_text(
        _mines_status(game),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=build_mines_kb(uid)
    )
    await cq.answer(f"💎 Безопасно! ×{mult}")

@router.callback_query(F.data == "mines_cash")
async def cb_mines_cash(cq: CallbackQuery):
    uid  = cq.from_user.id
    game = _mines_games.get(uid)
    if not game or not game["active"]:
        await cq.answer("Нет активной игры.", show_alert=True)
        return
    if not game["revealed"]:
        await cq.answer("Сначала открой хотя бы одну ячейку.", show_alert=True)
        return

    revealed_count = len(game["revealed"])
    mult   = mines_multiplier(game["mines"], revealed_count)
    payout = round(game["bet"] * mult, 2)
    bet    = game["bet"]
    mines  = game["mines"]

    game["active"] = False
    change_balance(uid, payout)
    with get_conn() as conn:
        conn.execute("UPDATE users SET total_won=total_won+? WHERE user_id=?", (payout, uid))
        conn.commit()
    profit = bet - payout
    if profit > 0:
        add_owner_profit(profit)
    del _mines_games[uid]

    await cq.message.edit_text(
        f"💰 *Выигрыш забран\\!*\n\n"
        f"Ставка: *{bet} ₽*\n"
        f"Открыто ячеек: *{revealed_count}*\n"
        f"Множитель: *×{escape_md(str(mult))}*\n"
        f"Выигрыш: *{escape_md(f'{payout:.2f}')} ₽*",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=build_mines_kb(uid, show_all=True)
    )
    await cq.answer(f"💰 Забрал {payout:.2f} ₽!")

@router.callback_query(F.data == "mines_exit")
async def cb_mines_exit(cq: CallbackQuery, state: FSMContext):
    uid  = cq.from_user.id
    game = _mines_games.get(uid)
    if game and game.get("active"):
        await cq.answer("Сначала забери выигрыш или продолжи игру.", show_alert=True)
        return
    await state.clear()
    await cq.message.edit_text(
        "🏠 *Главное меню*",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_keyboard()
    )
    await cq.answer()

@router.callback_query(F.data == "mines_noop")
async def cb_mines_noop(cq: CallbackQuery):
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
            ts = str(r["timestamp"])[:16].replace("T", " ")
            room_name = r["room_name"] if r["room_name"] else "Стандартная"
            parts.append(
                f"🏆 *\\#{i}* \\({escape_md(ts)}\\)\n"
                f"Комната: {escape_md(room_name)}\n"
                f"Победитель: {escape_md(r['winner_username'])}\n"
                f"Выигрыш: *{r['prize']} ₽* \\| Банк: *{r['bank']} ₽*\n"
            )
        text = "\n".join(parts)
    await cq.message.edit_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_to_menu_kb())
    await cq.answer()

# ─────────────────── HELP ─────────────────────────────────────
@router.callback_query(F.data == "help")
async def cb_help(cq: CallbackQuery):
    await cq.message.edit_text(
        "🆘 *Помощь*\n\nПо всем вопросам: @LRoulette\\_support\n\nВозврат к меню ниже\\.",
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

# ─────────────────── ADMIN COMMANDS ───────────────────────────
@router.message(Command("admin"))
async def cmd_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer("🔧 *Админ\\-панель*", parse_mode=ParseMode.MARKDOWN_V2, reply_markup=admin_keyboard())

@router.message(Command("givemoney"))
async def cmd_givemoney(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    parts = msg.text.strip().split()
    if len(parts) != 3:
        await msg.answer("Использование: /givemoney <user_id> <сумма>")
        return
    try:
        target_id = int(parts[1])
        amount    = int(parts[2])
    except ValueError:
        await msg.answer("user_id и сумма должны быть числами.")
        return

    user = get_user(target_id)
    if not user:
        await msg.answer(f"Пользователь {target_id} не найден.")
        return

    change_balance(target_id, amount)
    new_bal = get_balance(target_id)
    action  = "зачислено" if amount >= 0 else "списано"
    await msg.answer(
        f"✅ Пользователю {target_id} {action} *{abs(amount)} ₽*\\.\nНовый баланс: *{new_bal} ₽*\\.",
        parse_mode=ParseMode.MARKDOWN_V2
    )
    try:
        await bot.send_message(
            target_id,
            f"💰 Администратор {'начислил' if amount >= 0 else 'списал'} *{abs(amount)} ₽*\\.\n"
            f"Ваш баланс: *{new_bal} ₽*\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        log.warning(e)

@router.message(Command("start_roulette"))
async def cmd_start_roulette(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    room = get_room(DEFAULT_ROOM_ID)
    parts = get_room_participants(DEFAULT_ROOM_ID)
    if len(parts) < room["min_players"]:
        await msg.answer(f"Недостаточно участников: {len(parts)}/{room['min_players']}")
        return
    if not start_room_roulette(DEFAULT_ROOM_ID, DEFAULT_ROOM_DELAY):
        await msg.answer("Рулетка уже запущена!")
        return
    bank = sum(p["bet_amount"] for p in parts)
    await notify_room(DEFAULT_ROOM_ID, "🎲 *Рулетка запущена\\!* Победитель через *5 минут*\\.")
    await msg.answer(f"▶️ Рулетка запущена! Участников: {len(parts)}, банк: {bank} ₽")

@router.message(Command("reset_roulette"))
async def cmd_reset_roulette(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    parts = get_room_participants(DEFAULT_ROOM_ID)
    for p in parts:
        kick_from_room(DEFAULT_ROOM_ID, p["user_id"])
        try:
            await bot.send_message(
                p["user_id"],
                "🔄 *Комната сброшена администратором\\.* Ставка возвращена\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            log.warning(e)
    cancel_room_roulette(DEFAULT_ROOM_ID)
    cancel_room_timeout(DEFAULT_ROOM_ID)
    reset_default_room()
    await msg.answer(f"✅ Комната сброшена. Возвращено {len(parts)} ставок.")

# ─────────────────── ADMIN CALLBACKS ──────────────────────────
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
        mn   = "СБП" if w["method"] == "sbp" else "TRC20"
        text = (
            f"📋 *Заявка №{w['id']}*\n"
            f"Пользователь: {w['user_id']}\n"
            f"Сумма: *{w['amount']} ₽*\n"
            f"Метод: *{mn}*\n"
            f"Реквизиты: `{w['details']}`\n"
            f"Создана: {str(w['created_at'])[:16]}"
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Выполнено", callback_data=f"wadmin:complete:{w['id']}")
        kb.button(text="❌ Отказать",  callback_data=f"wadmin:reject:{w['id']}")
        kb.adjust(2)
        try:
            await cq.message.answer(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb.as_markup())
        except Exception as e:
            log.warning(e)
    await cq.answer()

@router.callback_query(F.data.startswith("wadmin:"))
async def cb_wadmin(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    _, action, wid_s = cq.data.split(":")
    wid = int(wid_s)
    w   = get_withdrawal(wid)
    if not w:
        await cq.answer("Заявка не найдена.", show_alert=True)
        return
    if w["status"] != "pending":
        await cq.answer(f"Уже обработана: {w['status']}", show_alert=True)
        return

    if action == "complete":
        update_withdrawal_status(wid, "completed")
        try:
            await bot.send_message(w["user_id"], f"✅ Заявка №{wid} выполнена\\.", parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            log.warning(e)
        await cq.message.edit_text(f"✅ Заявка №{wid} выполнена.")
    elif action == "reject":
        update_withdrawal_status(wid, "rejected")
        change_balance(w["user_id"], w["amount"])
        try:
            await bot.send_message(
                w["user_id"],
                f"❌ Заявка №{wid} отклонена\\. Средства возвращены на баланс\\.",
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
    s = get_extended_stats()
    total_owner = s["game_profit"] + s["commission"]
    await cq.message.edit_text(
        f"📊 *Подробная статистика*\n\n"
        f"👥 *Игроки*\n"
        f"Зарегистрировано: *{s['players']}*\n"
        f"Сейчас в комнатах: *{s['in_play']}*\n"
        f"Активных комнат: *{s['active_rooms']}*\n\n"
        f"🎲 *Игры*\n"
        f"Всего проведено игр: *{s['games_played']}*\n"
        f"Всего поставлено: *{s['total_bet']} ₽*\n"
        f"Всего выиграно: *{s['total_won']} ₽*\n\n"
        f"💰 *Финансы*\n"
        f"Всего пополнено \\(Crypto\\): *{s['deposited']} ₽*\n"
        f"Баланс на счетах: *{s['total_balance']} ₽*\n\n"
        f"📤 *Выводы*\n"
        f"Выполнено: *{s['w_done_count']}* шт\\. на *{s['w_done_sum']} ₽*\n"
        f"Ожидают: *{s['w_pend_count']}* шт\\. на *{s['w_pend_sum']} ₽*\n\n"
        f"💵 *Прибыль владельца*\n"
        f"С игр \\(20%\\): *{s['game_profit']} ₽*\n"
        f"С комиссий на вывод: *{s['commission']} ₽*\n"
        f"Итого: *{total_owner} ₽*",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=back_to_menu_kb()
    )
    await cq.answer()

@router.callback_query(F.data == "admin_reset")
async def cb_admin_reset(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("Нет доступа.", show_alert=True)
        return
    parts = get_room_participants(DEFAULT_ROOM_ID)
    for p in parts:
        kick_from_room(DEFAULT_ROOM_ID, p["user_id"])
        try:
            await bot.send_message(
                p["user_id"],
                "🔄 *Стандартная комната сброшена администратором\\.* Ставка возвращена\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            log.warning(e)
    cancel_room_roulette(DEFAULT_ROOM_ID)
    cancel_room_timeout(DEFAULT_ROOM_ID)
    reset_default_room()
    await cq.message.edit_text(f"✅ Стандартная комната сброшена. Возвращено {len(parts)} ставок.")
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
    text     = msg.text.strip()
    user_ids = get_all_user_ids()
    sent     = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception:
            pass
    await state.clear()
    await msg.answer(f"✅ Рассылка отправлена {sent}/{len(user_ids)} пользователям.")

# ─────────────────── WEB APP SERVER ───────────────────────────
import json
from aiohttp import web

WEBAPP_DIR = os.path.join(os.path.dirname(__file__), "webapp")
WEBAPP_PORT = int(os.getenv("PORT", "5000"))

async def _json(data):
    return web.Response(
        text=json.dumps(data, ensure_ascii=False),
        content_type="application/json"
    )

async def wa_index(req):
    return web.FileResponse(os.path.join(WEBAPP_DIR, "index.html"))

async def wa_profile(req):
    uid = int(req.rel_url.query.get("uid", 0))
    if not uid:
        return await _json({"ok": False, "error": "no uid"})
    ensure_user(uid, None)
    bal = get_balance(uid)
    u   = get_user(uid)
    return await _json({
        "ok":      True,
        "balance": bal,
        "total_bet": float(u["total_bet"]) if u else 0,
        "total_won": float(u["total_won"]) if u else 0,
    })

async def wa_stats(req):
    s = get_extended_stats()
    return await _json({"ok": True, "data": {
        "players":      s["players"],
        "total_bet":    float(s["total_bet"]),
        "total_won":    float(s["total_won"]),
        "games_played": s["games_played"],
    }})

async def wa_rooms(req):
    rooms = get_active_rooms()
    out = []
    for r in rooms:
        parts = get_room_participants(r["id"])
        bank  = sum(p["bet_amount"] for p in parts)
        out.append({
            "id":          r["id"],
            "name":        r["name"],
            "status":      r["status"],
            "players":     len(parts),
            "min_players": r["min_players"],
            "max_players": r["max_players"],
            "min_bet":     MIN_BET,
            "max_bet":     MAX_BET,
            "bank":        bank,
        })
    return await _json({"ok": True, "rooms": out})

async def wa_history(req):
    rows = get_history(20)
    out  = []
    for r in rows:
        out.append({
            "room_name":       r["room_name"],
            "winner_id":       r["winner_id"],
            "winner_username": r["winner_username"],
            "prize":           float(r["prize"]),
            "bank":            float(r["bank"]),
            "players_count":   r["players_count"],
            "timestamp":       str(r["timestamp"]),
        })
    return await _json({"ok": True, "history": out})

async def wa_mines_state(req):
    uid  = int(req.rel_url.query.get("uid", 0))
    game = _mines_games.get(uid)
    if game and game.get("active"):
        return await _json({"ok": True, "active": True, "game": {
            "bet":             game["bet"],
            "mines":           game["mines"],
            "revealed":        game["revealed"],
            "mines_positions": game["mines_positions"],
            "active":          True,
            "hit_cell":        None,
        }})
    return await _json({"ok": True, "active": False})

async def wa_mines_start(req):
    body = await req.json()
    uid   = int(body.get("uid", 0))
    bet   = float(body.get("bet", 0))
    mines = int(body.get("mines", 3))

    if not uid:
        return await _json({"ok": False, "error": "no uid"})
    if not (1 <= bet <= 1000):
        return await _json({"ok": False, "error": "Ставка 1–1000 ₽"})
    if not (1 <= mines <= 24):
        return await _json({"ok": False, "error": "Мины 1–24"})

    ensure_user(uid, None)
    bal = get_balance(uid)
    if bal < bet:
        return await _json({"ok": False, "error": f"Недостаточно средств. Баланс: {bal:.2f} ₽"})

    if uid in _mines_games and _mines_games[uid].get("active"):
        return await _json({"ok": False, "error": "Уже есть активная игра"})

    change_balance(uid, -bet)
    mine_positions = random.sample(range(25), mines)
    _mines_games[uid] = {
        "bet":             bet,
        "mines":           mines,
        "mines_positions": mine_positions,
        "revealed":        [],
        "active":          True,
    }
    return await _json({
        "ok":      True,
        "balance": get_balance(uid),
        "game": {
            "bet":             bet,
            "mines":           mines,
            "revealed":        [],
            "mines_positions": mine_positions,
            "active":          True,
            "hit_cell":        None,
        }
    })

async def wa_mines_cell(req):
    body = await req.json()
    uid  = int(body.get("uid", 0))
    cell = int(body.get("cell", -1))
    game = _mines_games.get(uid)

    if not game or not game["active"]:
        return await _json({"ok": False, "error": "Нет активной игры"})
    if cell < 0 or cell > 24:
        return await _json({"ok": False, "error": "Неверная ячейка"})
    if cell in game["revealed"]:
        return await _json({"ok": False, "error": "Уже открыта"})

    game["revealed"].append(cell)

    if cell in game["mines_positions"]:
        game["active"] = False
        add_owner_profit(game["bet"])
        game_snap = dict(game, hit_cell=cell)
        del _mines_games[uid]
        return await _json({
            "ok":      True,
            "hit":     True,
            "balance": get_balance(uid),
            "game":    {**game_snap, "active": False},
        })

    revealed_count = len(game["revealed"])
    safe_total     = 25 - game["mines"]
    mult   = mines_multiplier(game["mines"], revealed_count)
    payout = round(game["bet"] * mult, 2)

    if revealed_count >= safe_total:
        game["active"] = False
        change_balance(uid, payout)
        with get_conn() as conn:
            conn.execute("UPDATE users SET total_won=total_won+? WHERE user_id=?", (payout, uid))
            conn.commit()
        profit = game["bet"] - payout
        if profit > 0:
            add_owner_profit(profit)
        game_snap = dict(game)
        del _mines_games[uid]
        return await _json({
            "ok":           True,
            "hit":          False,
            "auto_cashout": True,
            "payout":       payout,
            "mult":         mult,
            "balance":      get_balance(uid),
            "game":         {**game_snap, "active": False},
        })

    return await _json({
        "ok":      True,
        "hit":     False,
        "auto_cashout": False,
        "payout":  payout,
        "mult":    mult,
        "balance": get_balance(uid),
        "game":    dict(game),
    })

async def wa_mines_cash(req):
    body = await req.json()
    uid  = int(body.get("uid", 0))
    game = _mines_games.get(uid)

    if not game or not game["active"]:
        return await _json({"ok": False, "error": "Нет активной игры"})

    revealed_count = len(game["revealed"])
    if revealed_count == 0:
        return await _json({"ok": False, "error": "Сначала открой хотя бы одну ячейку"})

    mult   = mines_multiplier(game["mines"], revealed_count)
    payout = round(game["bet"] * mult, 2)

    game["active"] = False
    change_balance(uid, payout)
    with get_conn() as conn:
        conn.execute("UPDATE users SET total_won=total_won+? WHERE user_id=?", (payout, uid))
        conn.commit()
    profit = game["bet"] - payout
    if profit > 0:
        add_owner_profit(profit)
    game_snap = dict(game)
    del _mines_games[uid]

    return await _json({
        "ok":      True,
        "payout":  payout,
        "mult":    mult,
        "balance": get_balance(uid),
        "game":    {**game_snap, "active": False},
    })

async def wa_topup_create(req):
    body       = await req.json()
    uid        = int(body.get("uid", 0))
    amount_rub = float(body.get("amount_rub", 0))

    if amount_rub < 90:
        return await _json({"ok": False, "error": "Минимум 90 ₽"})

    amount_usdt = amount_rub / USDT_TO_RUB

    result = await cryptobot_request("createInvoice", {
        "asset":       "USDT",
        "amount":      str(round(amount_usdt, 4)),
        "description": f"Пополнение LRoulette на {amount_rub:.0f} ₽",
        "payload":     f"{uid}",
        "allow_comments":  False,
        "allow_anonymous": False,
    })
    if not result or not result.get("ok"):
        return await _json({"ok": False, "error": "Ошибка CryptoBot"})

    inv = result["result"]
    return await _json({"ok": True, "url": inv.get("bot_invoice_url") or inv.get("pay_url", "")})

def build_webapp():
    app = web.Application()
    app.router.add_get('/',                  wa_index)
    app.router.add_get('/api/profile',       wa_profile)
    app.router.add_get('/api/stats',         wa_stats)
    app.router.add_get('/api/rooms',         wa_rooms)
    app.router.add_get('/api/history',       wa_history)
    app.router.add_get('/api/mines/state',   wa_mines_state)
    app.router.add_post('/api/mines/start',  wa_mines_start)
    app.router.add_post('/api/mines/cell',   wa_mines_cell)
    app.router.add_post('/api/mines/cash',   wa_mines_cash)
    app.router.add_post('/api/topup/create', wa_topup_create)
    app.router.add_static('/', WEBAPP_DIR)
    return app

# ─────────────────── MAIN ─────────────────────────────────────
async def main():
    global bot, dp
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Start web server
    webapp    = build_webapp()
    runner    = web.AppRunner(webapp)
    await runner.setup()
    site      = web.TCPSite(runner, "0.0.0.0", WEBAPP_PORT)
    await site.start()
    log.info(f"WebApp running on port {WEBAPP_PORT}")

    log.info("Bot starting...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
