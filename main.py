# main.py
import os
import re
import asyncio
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
import aiosqlite

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.filters.state import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup

# --- Конфіг та лог
load_dotenv()
logging.basicConfig(level=logging.INFO)
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN не знайдено в змінних оточення. Додайте BOT_TOKEN у .env або в оточення сервера.")

DB_PATH = os.path.join(os.path.dirname(__file__), "db.sqlite3")

# --- Bot & Dispatcher
storage = MemoryStorage()
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=storage)

# --- Клавіатура (reply)
main_keyboard = types.ReplyKeyboardMarkup(
    keyboard=[
        [types.KeyboardButton(text="📥 Додати заробіток"), types.KeyboardButton(text="💸 Додати витрати")],
        [types.KeyboardButton(text="📊 Моя статистика"), types.KeyboardButton(text="📅 Звіт за період")],
        [types.KeyboardButton(text="🏆 Топ водіїв"), types.KeyboardButton(text="🚘 Мій автомобіль")],
        [types.KeyboardButton(text="⚙️ Налаштування")],
    ],
    resize_keyboard=True
)

# --- Станова машина для реєстрації + додавання фінансів
class Registration(StatesGroup):
    waiting_for_name = State()
    waiting_for_nickname = State()
    waiting_for_car_model = State()
    waiting_for_car_number = State()

class AddIncome(StatesGroup):
    waiting_for_amount = State()

class AddExpense(StatesGroup):
    waiting_for_amount = State()
    waiting_for_type = State()

# --- Паттерни та допоміжні функції
PLATE_RE = re.compile(r'^[A-ZА-Я]{2}\d{4}[A-ZА-Я]{2}$', re.I)  # дозволяє лат/київські літери
def normalize_plate(text: str) -> str:
    return text.strip().upper().replace(" ", "")

def is_valid_money(text: str) -> bool:
    try:
        v = float(text.replace(",", "."))
        return v > 0
    except:
        return False

def parse_money(text: str) -> float:
    return float(text.replace(",", "."))

# --- Ініціалізація та запити до БД ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            tg_first_name TEXT,
            name TEXT,
            nickname TEXT UNIQUE,
            car_model TEXT,
            car_number TEXT,
            lang TEXT DEFAULT 'uk',
            report_period TEXT DEFAULT 'weekly',
            registered_at TEXT
        );

        CREATE TABLE IF NOT EXISTS incomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            ts TEXT,
            note TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            type TEXT,
            ts TEXT,
            note TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        """)
        await db.commit()

async def fetchone(query, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, params)
        row = await cur.fetchone()
        return row

async def execute(query, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(query, params)
        await db.commit()

# --- Юзер-функції ---
async def user_exists(user_id: int) -> bool:
    r = await fetchone("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
    return r is not None

async def save_user(user_id: int, tg_first_name: str, data: dict):
    now = datetime.utcnow().isoformat()
    await execute("""
        INSERT OR REPLACE INTO users (user_id, tg_first_name, name, nickname, car_model, car_number, registered_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, tg_first_name, data.get("name"), data.get("nickname"), data.get("car_model"), data.get("car_number"), now))

async def nickname_exists(nickname: str) -> bool:
    r = await fetchone("SELECT 1 FROM users WHERE LOWER(nickname)=LOWER(?)", (nickname,))
    return r is not None

# --- Баланс і статистика ---
async def get_balance(user_id:int, since: datetime=None):
    async with aiosqlite.connect(DB_PATH) as db:
        if since:
            ts = since.isoformat()
            cur_in = await db.execute("SELECT COALESCE(SUM(amount),0) FROM incomes WHERE user_id=? AND ts>=?", (user_id, ts))
            cur_ex = await db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE user_id=? AND ts>=?", (user_id, ts))
        else:
            cur_in = await db.execute("SELECT COALESCE(SUM(amount),0) FROM incomes WHERE user_id=?", (user_id,))
            cur_ex = await db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE user_id=?", (user_id,))
        total_in = (await cur_in.fetchone())[0] or 0.0
        total_ex = (await cur_ex.fetchone())[0] or 0.0
        return total_in, total_ex, total_in - total_ex

# --- Рейтинги ---
async def get_top_drivers(limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT u.nickname, u.name, COALESCE(SUM(i.amount),0) - COALESCE(SUM(e.amount),0) AS balance
            FROM users u
            LEFT JOIN incomes i ON i.user_id = u.user_id
            LEFT JOIN expenses e ON e.user_id = u.user_id
            GROUP BY u.user_id
            ORDER BY balance DESC
            LIMIT ?
        """, (limit,))
        rows = await cur.fetchall()
        return rows

# --- Обробники: реєстрація / старт ---
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    if await user_exists(uid):
        await message.answer(f"З поверненням, {message.from_user.first_name}!", reply_markup=main_keyboard)
    else:
        await message.answer("Вітаю! Щоб почати, введіть ваше справжнє ім'я:")
        await state.set_state(Registration.waiting_for_name)

async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("Придумайте унікальний псевдонім (він буде незмінний):")
    await state.set_state(Registration.waiting_for_nickname)

async def process_nickname(message: types.Message, state: FSMContext):
    nick = message.text.strip()
    if await nickname_exists(nick):
        await message.answer("Цей псевдонім вже зайнятий. Оберіть інший:")
        return
    await state.update_data(nickname=nick)
    await message.answer("Вкажіть марку та модель авто (наприклад Renault Logan):")
    await state.set_state(Registration.waiting_for_car_model)

async def process_car_model(message: types.Message, state: FSMContext):
    await state.update_data(car_model=message.text.strip())
    await message.answer("Вкажіть номер автомобіля (наприклад BC1234AB):")
    await state.set_state(Registration.waiting_for_car_number)

async def process_car_number(message: types.Message, state: FSMContext):
    plate = normalize_plate(message.text)
    if not PLATE_RE.match(plate):
        await message.answer("Невірний формат номера. Спробуйте у форматі BC1234AB (без пробілів).")
        return
    await state.update_data(car_number=plate)
    data = await state.get_data()
    await save_user(message.from_user.id, message.from_user.first_name, data)
    await message.answer("✅ Реєстрацію завершено!", reply_markup=main_keyboard)
    await state.clear()

# --- Обробники кнопок: додати доход/витрати і статистика ---
async def add_income_start(message: types.Message, state: FSMContext):
    if not await user_exists(message.from_user.id):
        await message.answer("Ви не зареєстровані. Надішліть /start щоб зареєструватися.")
        return
    await message.answer("Введіть суму доходу (наприклад 250.50):")
    await state.set_state(AddIncome.waiting_for_amount)

async def add_income_amount(message: types.Message, state: FSMContext):
    if not is_valid_money(message.text):
        await message.answer("Некоректна сума. Введіть число > 0.")
        return
    amount = parse_money(message.text)
    ts = datetime.utcnow().isoformat()
    await execute("INSERT INTO incomes (user_id, amount, ts, note) VALUES (?, ?, ?, ?)", (message.from_user.id, amount, ts, None))
    await message.answer(f"Додано до доходів: {amount:.2f}", reply_markup=main_keyboard)
    await state.clear()

async def add_expense_start(message: types.Message, state: FSMContext):
    if not await user_exists(message.from_user.id):
        await message.answer("Ви не зареєстровані. Надішліть /start щоб зареєструватися.")
        return
    await message.answer("Введіть суму витрати (наприклад 50.00):")
    await state.set_state(AddExpense.waiting_for_amount)

async def add_expense_amount(message: types.Message, state: FSMContext):
    if not is_valid_money(message.text):
        await message.answer("Некоректна сума. Введіть число > 0.")
        return
    await state.update_data(exp_amount=parse_money(message.text))
    # показуємо варіанти типів витрат inline
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="Паливо", callback_data="exp_type:fuel"),
         types.InlineKeyboardButton(text="Мийка", callback_data="exp_type:wash")],
        [types.InlineKeyboardButton(text="Ремонт", callback_data="exp_type:repair"),
         types.InlineKeyboardButton(text="Інше", callback_data="exp_type:other")]
    ])
    await message.answer("Оберіть тип витрати:", reply_markup=kb)
    await state.set_state(AddExpense.waiting_for_type)

async def exp_type_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    amount = data.get("exp_amount")
    if amount is None:
        await callback.message.answer("Не знайдено суму. Почніть заново.")
        await state.clear()
        return
    _, type_cb = callback.data.split(":", 1)
    ts = datetime.utcnow().isoformat()
    await execute("INSERT INTO expenses (user_id, amount, type, ts, note) VALUES (?, ?, ?, ?, ?)",
                  (callback.from_user.id, amount, type_cb, ts, None))
    await callback.message.answer(f"Додано витрату {amount:.2f} ({type_cb})", reply_markup=main_keyboard)
    await state.clear()

# --- Статистика: день/тиждень/місяць та довільний період ---
async def my_stats_handler(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if not await user_exists(uid):
        await message.answer("Ви не зареєстровані. Надішліть /start щоб зареєструватися.")
        return
    now = datetime.utcnow()
    day_from = now - timedelta(days=1)
    week_from = now - timedelta(days=7)
    month_from = now - timedelta(days=30)
    din, dex, dbal = await get_balance(uid, since=day_from)
    win, wex, wbal = await get_balance(uid, since=week_from)
    minc, mexp, mbal = await get_balance(uid, since=month_from)
    total_in, total_ex, total_balance = await get_balance(uid, since=None)
    text = (
        f"Баланс загальний: {total_balance:.2f}\n"
        f"Сьогодні: +{din:.2f} -{dex:.2f} = {dbal:.2f}\n"
        f"Тиждень: +{win:.2f} -{wex:.2f} = {wbal:.2f}\n"
        f"Місяць: +{minc:.2f} -{mexp:.2f} = {mbal:.2f}"
    )
    await message.answer(text, reply_markup=main_keyboard)

# --- Звіт за період: запит дат (простота: YYYY-MM-DD) ---
async def report_period_start(message: types.Message, state: FSMContext):
    await message.answer("Введіть початкову дату у форматі YYYY-MM-DD:")
    await state.set_state(State().set_state)  # тимчасово використовуємо загальний стан
    # але краще реалізувати окрему StatesGroup для звітів. Для стислості нижче — простий підхід.

# Додатково — спростимо: обробник з regex на дату
async def report_period_handler(message: types.Message, state: FSMContext):
    # очікуємо два рядки: start,end або формат обробки послідовно — тут для простої UX попросимо ввести як "YYYY-MM-DD,YYYY-MM-DD"
    txt = message.text.strip()
    parts = [p.strip() for p in txt.split(",") if p.strip()]
    if len(parts) != 2:
        await message.answer("Невірний формат. Надішліть у вигляді: 2025-01-01,2025-01-31")
        return
    try:
        start = datetime.fromisoformat(parts[0])
        end = datetime.fromisoformat(parts[1]) + timedelta(days=1)
    except Exception:
        await message.answer("Невірний формат дат. Спробуйте ще раз.")
        return
    uid = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        cur_i = await db.execute("SELECT ts,amount FROM incomes WHERE user_id=? AND ts>=? AND ts<? ORDER BY ts", (uid, start.isoformat(), end.isoformat()))
        incomes = await cur_i.fetchall()
        cur_e = await db.execute("SELECT ts,amount,type FROM expenses WHERE user_id=? AND ts>=? AND ts<? ORDER BY ts", (uid, start.isoformat(), end.isoformat()))
        expenses = await cur_e.fetchall()
    text = f"Звіт за період {parts[0]} — {parts[1]}:\n\n"
    text += "Доходи:\n"
    for r in incomes:
        text += f"- {r[0][:10]}: {r[1]:.2f}\n"
    text += "\nВитрати:\n"
    for r in expenses:
        text += f"- {r[0][:10]}: {r[2]} {r[1]:.2f}\n"
    bal = sum(r[1] for r in incomes) - sum(r[1] for r in expenses)
    text += f"\nСальдо за період: {bal:.2f}"
    await message.answer(text, reply_markup=main_keyboard)

# --- Мій автомобіль: перегляд та редагування (без зміни псевдоніма) ---
async def my_car_handler(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    r = await fetchone("SELECT name,nickname,car_model,car_number FROM users WHERE user_id=?", (uid,))
    if not r:
        await message.answer("Ви ще не зареєстровані.")
        return
    name, nick, car_model, car_number = r
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="Редагувати ім'я", callback_data="edit:name")],
        [types.InlineKeyboardButton(text="Редагувати авто", callback_data="edit:car")],
        [types.InlineKeyboardButton(text="Закрити", callback_data="edit:close")]
    ])
    await message.answer(f"👤 {name}\n🏷️ {nick}\n🚘 {car_model} ({car_number})", reply_markup=kb)

async def edit_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    _, action = callback.data.split(":",1)
    if action == "name":
        await callback.message.answer("Введіть нове ім'я:")
        await state.set_state(Registration.waiting_for_name)
    elif action == "car":
        await callback.message.answer("Введіть нову марку та модель авто:")
        await state.set_state(Registration.waiting_for_car_model)
    elif action == "close":
        await callback.message.delete()
    else:
        await callback.message.answer("Невідома дія.")

# --- Налаштування (мінімально: зміна мови/періодичності звітів) ---
async def settings_handler(message: types.Message, state: FSMContext):
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="Мова: Українська", callback_data="setlang:uk")],
        [types.InlineKeyboardButton(text="Період звітів: Щотижня", callback_data="setperiod:weekly"),
         types.InlineKeyboardButton(text="Період звітів: Щомісяця", callback_data="setperiod:monthly")],
    ])
    await message.answer("Налаштування:", reply_markup=kb)

async def settings_callback(callback: types.CallbackQuery):
    await callback.answer()
    action, val = callback.data.split(":",1)
    if action == "setlang":
        await execute("UPDATE users SET lang=? WHERE user_id=?", (val, callback.from_user.id))
        await callback.message.answer("Мову збережено.")
    elif action == "setperiod":
        await execute("UPDATE users SET report_period=? WHERE user_id=?", (val, callback.from_user.id))
        await callback.message.answer("Період звітів змінено.")

# --- Топ водіїв ---
async def top_drivers_handler(message: types.Message, state: FSMContext):
    rows = await get_top_drivers(limit=10)
    if not rows:
        await message.answer("Поки що немає даних для рейтингу.")
        return
    text = "🏆 Топ водіїв:\n"
    for i, r in enumerate(rows, start=1):
        nick, name, bal = r
        text += f"{i}. {name} ({nick}) — {bal:.2f}\n"
    await message.answer(text, reply_markup=main_keyboard)

# --- Реєстрація хендлерів у Dispatcher ---
dp.message.register(cmd_start, Command(commands=["start"]))
dp.message.register(process_name, StateFilter(Registration.waiting_for_name))
dp.message.register(process_nickname, StateFilter(Registration.waiting_for_nickname))
dp.message.register(process_car_model, StateFilter(Registration.waiting_for_car_model))
dp.message.register(process_car_number, StateFilter(Registration.waiting_for_car_number))

dp.message.register(add_income_start, lambda m: m.text == "📥 Додати заробіток")
dp.message.register(add_income_amount, StateFilter(AddIncome.waiting_for_amount))
dp.message.register(add_expense_start, lambda m: m.text == "💸 Додати витрати")
dp.message.register(add_expense_amount, StateFilter(AddExpense.waiting_for_amount))
dp.callback_query.register(exp_type_callback, lambda c: c.data and c.data.startswith("exp_type:"))

dp.message.register(my_stats_handler, lambda m: m.text == "📊 Моя статистика")
dp.message.register(report_period_handler, lambda m: "," in m.text and m.text[0].isdigit())  # простий catch для "YYYY-MM-DD,YYYY-MM-DD"
dp.message.register(report_period_start, lambda m: m.text == "📅 Звіт за період")

dp.message.register(top_drivers_handler, lambda m: m.text == "🏆 Топ водіїв")
dp.message.register(my_car_handler, lambda m: m.text == "🚘 Мій автомобіль")
dp.callback_query.register(edit_callback, lambda c: c.data and c.data.startswith("edit:"))

dp.message.register(settings_handler, lambda m: m.text == "⚙️ Налаштування")
dp.callback_query.register(settings_callback, lambda c: c.data and c.data.startswith("set"))

# --- Запуск ---
async def main():
    await init_db()
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
