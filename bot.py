import asyncio
import html
import importlib.util
import logging
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta


# ==========================================
# АВТОУСТАНОВКА ЗАВИСИМОСТЕЙ (всё в одном файле)
# ==========================================
def _run(cmd: list) -> bool:
    return subprocess.run(cmd).returncode == 0


def bootstrap():
    """Ставит aiogram, playwright и браузер Chromium, если их ещё нет."""
    packages = {"aiogram": "aiogram>=3.7", "playwright": "playwright"}
    missing = [pkg for mod, pkg in packages.items() if importlib.util.find_spec(mod) is None]
    if missing:
        print(f"Устанавливаю: {', '.join(missing)} ...", flush=True)
        base = [sys.executable, "-m", "pip", "install", "-q"]
        if not _run(base + missing):
            _run(base + ["--break-system-packages"] + missing)

    marker = ".chromium_installed"
    if not os.path.exists(marker):
        print("Устанавливаю браузер Chromium (один раз, может занять пару минут) ...", flush=True)
        ok = _run([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"])
        if not ok:
            ok = _run([sys.executable, "-m", "playwright", "install", "chromium"])
        if ok:
            open(marker, "w").close()


bootstrap()

from aiogram import Bot, Dispatcher, F, Router  # noqa: E402
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
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
BOT_TOKEN = "8949252988:AAHwB5upEkrz_o3wSJUEmVEq7EnOOtZNrw8"  # Токен от @BotFather
ADMIN_ID = 8404647058  # Ваш Telegram ID (Богдан)
DB_PATH = "support_bot.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Невидимые символы (word joiner, zero-width space/joiners, BOM)
INVISIBLE_RE = re.compile("[⁠​‌‍﻿]")

# ==========================================
# ДЕФОЛТНАЯ КОНФИГУРАЦИЯ СИСТЕМЫ (CFG)
# ==========================================
DEFAULT_SETTINGS = {
    "target_url": "",  # Ссылка убрана (можно задать через /set target_url ССЫЛКА)
    "sel_problem": 'textarea, textarea[name*="message" i], textarea[name*="text" i]',
    "sel_name": 'input[name*="name" i], input[name*="fio" i], input[placeholder*="ФИО" i], input[placeholder*="имя" i]',
    "sel_email": 'input[type="email"], input[name*="mail" i]',
    "sel_phone": 'input[type="tel"], input[name*="phone" i], input[name*="tel" i]',
    "sel_submit": 'button[type="submit"], input[type="submit"], button, .button, div[role="button"]',
    "sel_captcha": 'iframe[title*="recaptcha challenge" i], iframe[src*="bframe"], .g-recaptcha:visible, .h-captcha:visible, .cf-turnstile:visible, input[name*="captcha" i]:visible, img[src*="captcha" i]:visible',
    "captcha_text": "я не робот,i'm not a robot,verify you are human,unusual traffic,подтвердите, что вы не робот",
    "sel_success": "",
    "success_text": "Спасибо за сообщение",
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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            sub_until TEXT,
            is_banned INTEGER DEFAULT 0
        )
    """)

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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    for key, val in DEFAULT_SETTINGS.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, str(val)))

    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, username, sub_until) VALUES (?, ?, ?)",
        (ADMIN_ID, "Yamayk_a", "2099-12-31 23:59:59"),
    )

    # Задачи, зависшие в обработке после перезапуска, возвращаем в очередь не будем:
    # помечаем как отмененные, чтобы они не запускались неожиданно.
    cur.execute("UPDATE queue SET status = 'cancelled' WHERE status = 'pending'")

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
    clean_val = INVISIBLE_RE.sub("", value).strip()
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
    if not user or user[3] == 1:
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
        [KeyboardButton(text="📋 Главное меню")],
    ]
    if user_id == ADMIN_ID:
        kb.append([KeyboardButton(text="👑 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def get_stop_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 Остановить рассылку", callback_data="stop_batch")]
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
active_tasks: dict[int, bool] = {}


def set_task_status(task_id: int, status: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE queue SET status = ?, attempts = attempts + 1 WHERE id = ?", (status, task_id))
    conn.commit()
    conn.close()


async def process_report_task(task_id: int, problem: str, name: str, email: str, phone: str):
    """Возвращает (успех: bool, текст_ошибки: str | None). Не выбрасывает исключений."""
    try:
        cfg = get_cfg()
        target_url = cfg.get("target_url", "").strip()
        if not target_url:
            raise RuntimeError("target_url не задан! Задайте URL через /set target_url <URL>")

        nav_timeout = int(cfg.get("nav_timeout", 45)) * 1000
        headless_mode = cfg.get("headless", "True").strip().lower() == "true"

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless_mode,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            try:
                context = await browser.new_context(user_agent=cfg.get("user_agent"))
                page = await context.new_page()
                page.set_default_timeout(nav_timeout)

                # 1. Переход на страницу
                await page.goto(target_url, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass  # сайт может бесконечно что-то подгружать — это не ошибка

                # 2. Заполнение полей (селекторы с запятой матчат несколько элементов -> берём первый)
                await page.locator(cfg["sel_problem"]).first.fill(problem)
                await page.locator(cfg["sel_email"]).first.fill(email)
                await page.locator(cfg["sel_phone"]).first.fill(phone)
                if cfg.get("sel_name"):
                    try:
                        await page.locator(cfg["sel_name"]).first.fill(name, timeout=3000)
                    except Exception:
                        pass

                # 3. Клик по кнопке отправки
                submit_btn = page.locator(cfg["sel_submit"]).first
                await submit_btn.click(force=True, timeout=nav_timeout)

                # 4. Проверка успешности отправки
                success_timeout = int(cfg.get("success_timeout", 15))
                await asyncio.sleep(3)
                content = (await page.content()).lower()

                is_success = False
                sel_success = cfg.get("sel_success", "").strip()
                if sel_success:
                    try:
                        await page.locator(sel_success).first.wait_for(
                            state="visible", timeout=success_timeout * 1000
                        )
                        is_success = True
                    except Exception:
                        is_success = False
                if not is_success:
                    success_keywords = [k.strip().lower() for k in cfg["success_text"].split(",") if k.strip()]
                    is_success = any(kw in content for kw in success_keywords)

                if is_success:
                    set_task_status(task_id, "success")
                    return True, None

                captcha_keywords = [k.strip().lower() for k in cfg["captcha_text"].split(",") if k.strip()]
                if any(ck in content for ck in captcha_keywords):
                    raise RuntimeError("Обнаружена защитная CAPTCHA / Cloudflare Turnstile")
                raise RuntimeError("Форма отправлена, но подтверждение успеха не найдено.")
            finally:
                # закрываем браузер до выхода из async_playwright()
                await browser.close()

    except Exception as e:
        logger.exception("Ошибка в задаче #%s", task_id)
        set_task_status(task_id, "failed")
        return False, str(e)


async def batch_worker(user_id: int, bot: Bot, first_id: int, last_id: int):
    active_tasks[user_id] = True
    try:
        while active_tasks.get(user_id, False):
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                "SELECT id, problem, name, email, phone FROM queue "
                "WHERE user_id = ? AND status = 'pending' AND id BETWEEN ? AND ? "
                "ORDER BY id ASC LIMIT 1",
                (user_id, first_id, last_id),
            )
            task = cur.fetchone()
            conn.close()

            if not task:
                break

            task_id, problem, name, email, phone = task
            success, error_msg = await process_report_task(task_id, problem, name, email, phone)

            if not success:
                msg_text = (
                    f"❌ Ошибка при отправке заявки #{task_id}\n"
                    f"Причина: {html.escape(str(error_msg))}\n\n"
                    f"⚠️ Пожалуйста, сообщите @Yamayk_a"
                )
                try:
                    await bot.send_message(user_id, msg_text)
                except Exception:
                    logger.exception("Не удалось отправить сообщение об ошибке")

            await asyncio.sleep(3)
    finally:
        active_tasks[user_id] = False

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) "
            "FROM queue WHERE user_id = ? AND id BETWEEN ? AND ?",
            (user_id, first_id, last_id),
        )
        total, succ, fail = cur.fetchone()
        conn.close()

        report_text = (
            "🏁 <b>Рассылка завершена!</b>\n\n"
            f"📦 Всего в пакете: {total or 0}\n"
            f"✅ Успешно отправлено: {succ or 0}\n"
            f"❌ Не отправились: {fail or 0}"
        )
        try:
            await bot.send_message(user_id, report_text, reply_markup=get_main_kb(user_id))
        except Exception:
            logger.exception("Не удалось отправить итоговый отчёт")


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
        f"👋 <b>Привет, {html.escape(msg.from_user.first_name or '')}!</b>\n\n"
        "Добро пожаловать в бота автоматической отправки жалоб и обращений.\n"
        "Используйте меню ниже для управления.",
        reply_markup=get_main_kb(msg.from_user.id),
    )


@router.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("❌ Ввод отменен. Возвращаю в главное меню.", reply_markup=get_main_kb(msg.from_user.id))


@router.message(F.text == "🚀 Отправить жалобы")
async def start_batch(msg: Message, state: FSMContext):
    add_user(msg.from_user.id, msg.from_user.username or "Unknown")
    if not is_sub_active(msg.from_user.id):
        await msg.answer("⛔ <b>Доступ ограничен.</b> У вас нет активной подписки. Обратитесь к @Yamayk_a.")
        return

    if active_tasks.get(msg.from_user.id):
        await msg.answer("⏳ У вас уже идёт рассылка. Дождитесь её завершения или остановите её.")
        return

    await state.set_state(FormState.problem)
    await msg.answer(
        "📝 <b>Шаг 1/5:</b> Введите текст проблемы/жалобы:\n(для отмены: /cancel)",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(FormState.problem, F.text)
async def process_problem(msg: Message, state: FSMContext):
    await state.update_data(problem=msg.text)
    await state.set_state(FormState.name)
    await msg.answer("👤 <b>Шаг 2/5:</b> Введите ФИО (или имя):")


@router.message(FormState.name, F.text)
async def process_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text)
    await state.set_state(FormState.email)
    await msg.answer("📧 <b>Шаг 3/5:</b> Введите Contact Email:")


@router.message(FormState.email, F.text)
async def process_email(msg: Message, state: FSMContext):
    await state.update_data(email=msg.text.strip())
    await state.set_state(FormState.phone)
    await msg.answer("📱 <b>Шаг 4/5:</b> Введите телефон (с кодом страны):")


@router.message(FormState.phone, F.text)
async def process_phone(msg: Message, state: FSMContext):
    await state.update_data(phone=msg.text.strip())
    await state.set_state(FormState.count)
    await msg.answer("🔢 <b>Шаг 5/5:</b> Сколько заявок отправить в этом пакете? (Введите число):")


@router.message(FormState.count, F.text)
async def process_count(msg: Message, state: FSMContext, bot: Bot):
    try:
        count = int(re.sub(r"\D", "", msg.text))
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
            (msg.from_user.id, data["problem"], data["name"], data["email"], data["phone"], now),
        )
    last_id = cur.lastrowid
    first_id = last_id - count + 1
    conn.commit()
    conn.close()

    await msg.answer(
        f"✅ <b>Пакет из {count} заявок создан и добавлен в очередь!</b>\n\n"
        "🚀 Запускаем процесс отправки...",
        reply_markup=get_stop_inline_kb(),
    )

    asyncio.create_task(batch_worker(msg.from_user.id, bot, first_id, last_id))


@router.callback_query(F.data == "stop_batch")
async def stop_batch_callback(call: CallbackQuery):
    user_id = call.from_user.id
    active_tasks[user_id] = False

    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE queue SET status = 'cancelled' WHERE user_id = ? AND status = 'pending'", (user_id,))
    conn.commit()
    conn.close()

    await call.answer("🛑 Рассылка остановлена!")
    try:
        await call.message.edit_text("🛑 <b>Рассылка была принудительно остановлена пользователем.</b>")
    except Exception:
        pass


@router.message(F.text == "📊 Моя статистика")
async def show_stats(msg: Message):
    user_id = msg.from_user.id
    user = get_user(user_id)
    sub_str = user[2] if user and user[2] else "Нет подписки"

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*), "
        "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) "
        "FROM queue WHERE user_id = ?",
        (user_id,),
    )
    total, succ, fail, pend = cur.fetchone()
    conn.close()

    await msg.answer(
        "📊 <b>Ваша статистика:</b>\n\n"
        f"🔑 Подписка активна до: <code>{html.escape(sub_str)}</code>\n"
        f"📦 Всего заявок: {total or 0}\n"
        f"✅ Успешно: {succ or 0}\n"
        f"❌ Ошибок: {fail or 0}\n"
        f"⏳ В очереди: {pend or 0}",
        reply_markup=get_main_kb(user_id),
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
    text = "⚙️ <b>Текущие настройки конфигурации (CFG):</b>\n\n"
    for k, v in cfg.items():
        text += f"• <code>{html.escape(k)}</code> = <code>{html.escape(str(v))}</code>\n"
    text += "\n💡 Для изменения используйте команду:\n<code>/set КЛЮЧ ЗНАЧЕНИЕ</code>"

    # Лимит Telegram — 4096 символов
    for i in range(0, len(text), 4000):
        await msg.answer(text[i:i + 4000])


@router.message(Command("set"))
async def admin_set(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return

    parts = msg.text.split(maxsplit=2)
    if len(parts) < 3:
        await msg.answer("⚠️ Формат: <code>/set КЛЮЧ ЗНАЧЕНИЕ</code>\nПример: <code>/set nav_timeout 60</code>")
        return

    key = INVISIBLE_RE.sub("", parts[1]).strip()
    val = INVISIBLE_RE.sub("", parts[2]).strip()

    set_cfg_param(key, val)
    await msg.answer(
        f"✅ Параметр <code>{html.escape(key)}</code> успешно обновлен на <code>{html.escape(val)}</code>!"
    )


@router.message(Command("sub"))
async def admin_sub(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    parts = msg.text.split()
    if len(parts) < 3:
        await msg.answer("⚠️ Формат: <code>/sub USER_ID ДНИ</code>\nПример: <code>/sub 12345678 30</code>")
        return
    try:
        uid = int(parts[1])
        days = int(parts[2])
        until = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        add_user(uid, "Unknown")  # пользователь мог ещё не писать боту
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE users SET sub_until = ? WHERE user_id = ?", (until, uid))
        conn.commit()
        conn.close()
        await msg.answer(f"✅ Подписка пользователю <code>{uid}</code> выдана до <code>{until}</code>")
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {html.escape(str(e))}")


# ==========================================
# ТОЧКА ВХОДА И ЗАПУСК БОТА
# ==========================================
async def main():
    if not BOT_TOKEN or BOT_TOKEN.startswith("YOUR_"):
        raise SystemExit("Укажите BOT_TOKEN в начале файла (токен от @BotFather).")

    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Бот успешно запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
