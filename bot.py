import asyncio
import logging
import re
import sqlite3
import typing
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from playwright.async_api import async_playwright

# ==========================================
# КОНФИГУРАЦИЯ БОТА И АДМИНИСТРАЦИИ
# ==========================================
BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN_HERE"  # Вставьте ваш токен от @BotFather
ADMIN_ID = 8404647058  # Ваш Telegram ID (Богдан)
DB_PATH = "support_bot.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ==========================================
# ДЕФОЛТНАЯ КОНФИГУРАЦИЯ СИСТЕМЫ (CFG)
# ==========================================
DEFAULT_SETTINGS = {
    "target_url": "https://telegram.org/support?setln=ru",
    "sel_problem": 'textarea, textarea[name*="message" i], textarea[name*="text" i]',
    "sel_name": 'input[name*="name" i], input[name*="fio" i], input[placeholder*="ФИО" i], input[placeholder*="имя" i]',
    "sel_email": 'input[type="email"], input[name*="mail" i]',
    "sel_phone": 'input[type="tel"], input[name*="phone" i], input[name*="tel" i]',
    "sel_submit": 'button[type="submit"], input[type="submit"], button, .button, div[role="button"]',
    "sel_captcha": 'iframe[title*="recaptcha challenge" i], iframe[src*="bframe"], .g-recaptcha:visible, .h-captcha:visible, .cf-turnstile:visible, input[name*="captcha" i]:visible, img[src*="captcha" i]:visible',
    "captcha_text": "я не робот,i'm not a robot,verify you are human,unusual traffic,подтвердите, что вы не робот",
    "sel_success": "",
    "success_text": "Спасибо за сообщение,спасибо за обращение,ваше обращение,успешно отправлен,успешно отправлено,thank you,successfully",
    "headless": "True",
    "step_screens": "False",
    "track_chat_id": "",
    "poll_interval": "2",
    "retry_delay": "60",
    "max_attempts": "2",
    "ping_timeout": "10",
    "nav_timeout": "45",
    "success_timeout": "15",
    "type_delay_min": "5",
    "type_delay_max": "15",
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

# ==========================================
# РАБОТА С БАЗОЙ ДАННЫХ (SQLITE)
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    # Таблица пользователей
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            sub_until TEXT,
            is_banned INTEGER DEFAULT 0
        )
    """)
    
    # Таблица очередей жалоб
    cur.execute("""
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            problem TEXT,
            name TEXT,
            email TEXT,
            phone TEXT,
            status TEXT DEFAULT 'pending',
            attempts INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    
    # Таблица настроек CFG
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    
    # Заполнение дефолтными настройками, если их еще нет
    for key, val in DEFAULT_SETTINGS.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, str(val)))
        
    # Регистрация админа по умолчанию
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, sub_until) VALUES (?, ?, ?)", 
                (ADMIN_ID, "Yamayk_a", "2099-12-31 23:59:59"))
    
    conn.commit()
    conn.close()

def get_cfg() -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM settings")
    rows = cur.fetchall()
    conn.close()
    cfg = DEFAULT_SETTINGS.copy()
    for k, v in rows:
        cfg[k] = v
    return cfg

def set_cfg_param(key: str, value: str):
    # Автоматическая очистка спецсимволов и мусора из Telegram
    clean_val = re.sub(r'[⁠​‌‍﻿]', '', value).strip()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, clean_val))
    conn.commit()
    conn.close()

def get_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, sub_until, is_banned FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def add_user(user_id: int, username: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
    conn.commit()
    conn.close()

def is_sub_active(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    user = get_user(user_id)
    if not user or user[3] == 1:  # Banned
        return False
    if not user[2]:
        return False
    try:
        sub_date = datetime.strptime(user[2], "%Y-%m-%d %H:%M:%S")
        return sub_date > datetime.now()
    except Exception:
        return False

# ==========================================
# ИНТЕРФЕЙС ТЕЛЕГРАМ (КЛАВИАТУРЫ)
# ==========================================
def get_main_kb(user_id: int):
    kb = [
        [KeyboardButton(text="🚀 Отправить жалобы"), KeyboardButton(text="📊 Моя статистика")],
        [KeyboardButton(text="📋 Главное меню")]
    ]
    if user_id == ADMIN_ID:
        kb.append([KeyboardButton(text="👑 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_stop_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 Остановить рассылку", callback_query_data="stop_batch")]
    ])

# ==========================================
# СОСТОЯНИЯ FSM ДЛЯ СОЗДАНИЯ ПАКЕТА
# ==========================================
class FormState(StatesGroup):
    problem = State()
    name = State()
    email = State()
    phone = State()
    count = State()

# ==========================================
# ДВИЖОК PLAYWRIGHT (ОТПРАВКА ЖАЛОБ)
# ==========================================
active_tasks = {}  # {user_id: bool} (флаг работы)

async def process_report_task(task_id: int, user_id: int, problem: str, name: str, email: str, phone: str, bot: Bot):
    cfg = get_cfg()
    nav_timeout = int(cfg.get("nav_timeout", 45)) * 1000
    headless_mode = cfg.get("headless", "True").lower() == "true"
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless_mode,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = await browser.new_context(user_agent=cfg.get("user_agent"))
        page = await context.new_page()
        page.set_default_timeout(nav_timeout)
        
        try:
            # 1. Переход на страницу
            await page.goto(cfg["target_url"], wait_until="networkidle")
            
            # 2. Заполнение полей
            await page.fill(cfg["sel_problem"], problem)
            await page.fill(cfg["sel_email"], email)
            await page.fill(cfg["sel_phone"], phone)
            if cfg.get("sel_name"):
                try:
                    await page.fill(cfg["sel_name"], name, timeout=3000)
                except Exception:
                    pass  # Поле Имени может отсутствовать
            
            # 3. Клик по кнопке (С ИСПОЛЬЗОВАНИЕМ force=True против блокировки капчи)
            submit_btn = page.locator(cfg["sel_submit"]).first
            await submit_btn.click(force=True, timeout=nav_timeout)
            
            # 4. Проверка успешности отправки
            await asyncio.sleep(3)
            content = await page.content()
            success_keywords = [k.strip().lower() for k in cfg["success_text"].split(",") if k.strip()]
            
            is_success = any(kw in content.lower() for kw in success_keywords)
            
            if is_success:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("UPDATE queue SET status = 'success' WHERE id = ?", (task_id,))
                conn.commit()
                conn.close()
                return True, None
            else:
                # Проверка наличия капчи
                captcha_keywords = [k.strip().lower() for k in cfg["captcha_text"].split(",") if k.strip()]
                if any(ck in content.lower() for ck in captcha_keywords):
                    raise Exception("Обнаружена защитная CAPTCHA / Cloudflare Turnstile")
                else:
                    raise Exception("Форма отправлена, но подтверждение успеха не найдено.")
                    
        except Exception as e:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("UPDATE queue SET status = 'failed' WHERE id = ?", (task_id,))
            conn.commit()
            conn.close()
            return False, str(e)
        finally:
            await browser.close()

async def batch_worker(user_id: int, bot: Bot):
    active_tasks[user_id] = True
    
    while active_tasks.get(user_id, False):
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT id, problem, name, email, phone FROM queue WHERE user_id = ? AND status = 'pending' ORDER BY id ASC LIMIT 1", (user_id,))
        task = cur.fetchone()
        conn.close()
        
        if not task:
            break
            
        task_id, problem, name, email, phone = task
        success, error_msg = await process_report_task(task_id, user_id, problem, name, email, phone, bot)
        
        if not success:
            await bot.send_message(
                user_id,
                f"❌ Ошибка при отправке заявки #{task_id}
"
                f"Причина: {error_msg}

"
                f"⚠️ Пожалуйста, сообщите @Yamayk_a"
            )
        
        # Небольшая пауза между заявками
        await asyncio.sleep(3)
        
    active_tasks[user_id] = False
    
    # Финальный отчет
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), SUM(CASE WHEN status='success' THEN 1 ELSE 0 END), SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) FROM queue WHERE user_id = ?", (user_id,))
    total, succ, fail = cur.fetchone()
    conn.close()
    
    await bot.send_message(
        user_id,
        f"🏁 **Рассылка завершена!**

"
        f"📦 Всего в очереди была(о): {total}
"
        f"✅ Успешно отправлено: {succ or 0}
"
        f"❌ Не отправились: {fail or 0}",
        reply_markup=get_main_kb(user_id),
        parse_mode="Markdown"
    )

# ==========================================
# ХЕНДЛЕРЫ BOT (ROUTER)
# ==========================================
router = Router()

@router.message(CommandStart())
@router.message(F.text == "📋 Главное меню")
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    add_user(msg.from_user.id, msg.from_user.username or "Unknown")
    await msg.answer(
        f"👋 **Привет, {msg.from_user.first_name}!**

"
        f"Добро пожаловать в бота автоматической отправки жалоб и обращений.
"
        f"Используйте меню ниже для управления.",
        reply_markup=get_main_kb(msg.from_user.id),
        parse_mode="Markdown"
    )

@router.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("❌ Ввод отменен. Возвращаю в главное меню.", reply_markup=get_main_kb(msg.from_user.id))

@router.message(F.text == "🚀 Отправить жалобы")
async def start_batch(msg: Message, state: FSMContext):
    if not is_sub_active(msg.from_user.id):
        await msg.answer("⛔ **Доступ ограничен.** У вас нет активной подписки. Обратитесь к @Yamayk_a.")
        return
        
    await state.set_state(FormState.problem)
    await msg.answer("📝 **Шаг 1/5:** Введите текст проблемы/жалобы:", reply_markup=ReplyKeyboardRemove())

@router.message(FormState.problem)
async def process_problem(msg: Message, state: FSMContext):
    await state.update_data(problem=msg.text)
    await state.set_state(FormState.name)
    await msg.answer("👤 **Шаг 2/5:** Введите ФИО (или имя):")

@router.message(FormState.name)
async def process_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text)
    await state.set_state(FormState.email)
    await msg.answer("📧 **Шаг 3/5:** Введите Contact Email:")

@router.message(FormState.email)
async def process_email(msg: Message, state: FSMContext):
    await state.update_data(email=msg.text)
    await state.set_state(FormState.phone)
    await msg.answer("📱 **Шаг 4/5:** Введите телефон (с кодом страны):")

@router.message(FormState.phone)
async def process_phone(msg: Message, state: FSMContext):
    await state.update_data(phone=msg.text)
    await state.set_state(FormState.count)
    await msg.answer("🔢 **Шаг 5/5:** Сколько заявок отправить в этом пакете? (Введите число):")

@router.message(FormState.count)
async def process_count(msg: Message, state: FSMContext, bot: Bot):
    try:
        count = int(re.sub(r'\D', '', msg.text))
        if count <= 0 or count > 500:
            raise ValueError()
    except Exception:
        await msg.answer("❌ Пожалуйста, введите корректное число от 1 до 500:")
        return

    data = await state.get_data()
    await state.clear()
    
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for _ in range(count):
        cur.execute(
            "INSERT INTO queue (user_id, problem, name, email, phone, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (msg.from_user.id, data["problem"], data["name"], data["email"], data["phone"], now)
        )
    conn.commit()
    conn.close()
    
    await msg.answer(
        f"✅ **Пакет из {count} заявок создан и добавлен в очередь!**

"
        f"🚀 Запускаем процесс отправки...",
        reply_markup=get_stop_inline_kb(),
        parse_mode="Markdown"
    )
    
    asyncio.create_task(batch_worker(msg.from_user.id, bot))

@router.callback_query(F.data == "stop_batch")
async def stop_batch_callback(call: CallbackQuery):
    user_id = call.from_user.id
    active_tasks[user_id] = False
    
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE queue SET status = 'cancelled' WHERE user_id = ? AND status = 'pending'", (user_id,))
    conn.commit()
    conn.close()
    
    await call.answer("🛑 Рассылка остановлена!")
    await call.message.edit_text("🛑 **Рассылка была принудительно остановлена пользователем.**")

@router.message(F.text == "📊 Моя статистика")
async def show_stats(msg: Message):
    user_id = msg.from_user.id
    user = get_user(user_id)
    sub_str = user[2] if user and user[2] else "Нет подписки"
    
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), SUM(CASE WHEN status='success' THEN 1 ELSE 0 END), SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) FROM queue WHERE user_id = ?", (user_id,))
    total, succ, fail, pend = cur.fetchone()
    conn.close()
    
    await msg.answer(
        f"📊 **Ваша статистика:**

"
        f"🔑 Подписка активна до: `{sub_str}`
"
        f"📦 Всего заявок: {total or 0}
"
        f"✅ Успешно: {succ or 0}
"
        f"❌ Ошибок: {fail or 0}
"
        f"⏳ В очереди: {pend or 0}",
        reply_markup=get_main_kb(user_id),
        parse_mode="Markdown"
    )

# ==========================================
# АДМИНИСТРАТИВНЫЕ КОМАНДЫ (CFG И ЮЗЕРЫ)
# ==========================================
@router.message(Command("cfg"))
@router.message(F.text == "👑 Админ-панель")
async def admin_cfg(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
        
    cfg = get_cfg()
    text = "⚙️ **Текущие настройки конфигурации (CFG):**

"
    for k, v in cfg.items():
        text += f"• `{k}` = `{v}`
"
    text += "
💡 Для изменения используйте команду:
`/set КЛЮЧ ЗНАЧЕНИЕ`"
    
    await msg.answer(text, parse_mode="Markdown")

@router.message(Command("set"))
async def admin_set(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
        
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 3:
        await msg.answer("⚠️ Формат: `/set КЛЮЧ ЗНАЧЕНИЕ`
Пример: `/set nav_timeout 60`", parse_mode="Markdown")
        return
        
    key, val = parts[1], parts[2]
    # Фильтрация скрытых невидимых символов
    key = re.sub(r'[⁠​‌‍﻿]', '', key).strip()
    val = re.sub(r'[⁠​‌‍﻿]', '', val).strip()
    
    set_cfg_param(key, val)
    await msg.answer(f"✅ Параметр `{key}` успешно обновлен на `{val}`!", parse_mode="Markdown")

@router.message(Command("sub"))
async def admin_sub(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    parts = msg.text.split()
    if len(parts) < 3:
        await msg.answer("⚠️ Формат: `/sub USER_ID ДНИ`
Пример: `/sub 12345678 30`", parse_mode="Markdown")
        return
    try:
        uid = int(parts[1])
        days = int(parts[2])
        until = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE users SET sub_until = ? WHERE user_id = ?", (until, uid))
        conn.commit()
        conn.close()
        await msg.answer(f"✅ Подписка пользователю `{uid}` выдана до `{until}`", parse_mode="Markdown")
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {e}")

# ==========================================
# ТОЧКА ВХОДА И ЗАПУСК БОТА
# ==========================================
async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    
    logger.info("Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
