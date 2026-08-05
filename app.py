import asyncio
import contextlib
import json
import logging
import os
import tempfile
import uuid
from pathlib import Path

import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telethon.errors import SessionPasswordNeededError
from telethon import utils

from sync_engine import LiveSync, make_client, migrate_history

BASE = Path(__file__).parent
DATA = BASE / "data.json"
RUNTIME = BASE / "runtime"
RUNTIME.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("post_sync")

TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
CONFIGURED_ADMINS = {int(v) for v in os.environ.get("ADMIN_IDS", "").split(",") if v.strip().isdigit()}
bot = Bot(TOKEN)
dp = Dispatcher()
states: dict[int, dict] = {}
clients: dict[int, object] = {}
live: dict[str, LiveSync] = {}


def kb(*rows):
    normalized = []
    for row in rows:
        # A single button is conveniently passed as (text, callback_data).
        if len(row) == 2 and all(isinstance(item, str) for item in row):
            row = (row,)
        normalized.append([InlineKeyboardButton(text=text, callback_data=data) for text, data in row])
    return InlineKeyboardMarkup(inline_keyboard=normalized)


def load():
    if not DATA.exists():
        return {"owners": [], "accounts": {}, "tasks": {}}
    try:
        value = json.loads(DATA.read_text())
        return {"owners": value.get("owners", []), "accounts": value.get("accounts", {}), "tasks": value.get("tasks", {})}
    except Exception:
        log.exception("Cannot read data")
        return {"owners": [], "accounts": {}, "tasks": {}}


def save(value):
    handle, temp = tempfile.mkstemp(dir=BASE, prefix="data-", suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.flush(); os.fsync(file.fileno())
    os.replace(temp, DATA)


def allowed(user_id: int) -> bool:
    data = load()
    return user_id in CONFIGURED_ADMINS or user_id in data["owners"]


def require_ready():
    if not TOKEN or not API_ID or not API_HASH:
        raise RuntimeError("В .env должны быть BOT_TOKEN, API_ID и API_HASH")


async def menu(message, edit=False):
    data = load()
    text = f"Post Sync\n\nАккаунтов: {len(data['accounts'])}\nЗадач: {len(data['tasks'])}\nАктивных синхронизаций: {len(live)}"
    markup = kb(("👤 Аккаунты", "accounts"), ("🔄 Задачи", "tasks"), ("🔃 Обновить", "home"))
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


async def clear(user_id):
    states.pop(user_id, None)
    client = clients.pop(user_id, None)
    if client:
        with contextlib.suppress(Exception): await client.disconnect()


@dp.message(CommandStart())
async def start(message: Message):
    data = load()
    if not data["owners"] and not CONFIGURED_ADMINS:
        data["owners"] = [message.from_user.id]
        save(data)
    if not allowed(message.from_user.id):
        await message.answer("Доступ к панели закрыт.")
        return
    await clear(message.from_user.id)
    await menu(message)


@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery):
    await call.answer(); await menu(call.message, True)


@dp.callback_query(F.data == "accounts")
async def accounts(call: CallbackQuery):
    data = load(); rows = [("➕ Добавить аккаунт", "account:add")]
    rows += [(f"👤 {a['name']}", f"account:view:{key}") for key, a in data["accounts"].items()]
    rows.append(("← Назад", "home"))
    await call.answer(); await call.message.edit_text("Telegram-аккаунты", reply_markup=kb(*rows))


@dp.callback_query(F.data == "account:add")
async def add_account(call: CallbackQuery):
    states[call.from_user.id] = {"step": "account_name"}
    await call.answer(); await call.message.edit_text("Название аккаунта (например, Основной):")


@dp.callback_query(F.data.startswith("account:view:"))
async def account_view(call: CallbackQuery):
    key = call.data.rsplit(":", 1)[1]; account = load()["accounts"].get(key)
    if not account: await call.answer("Аккаунт не найден", show_alert=True); return
    await call.answer(); await call.message.edit_text(f"Аккаунт: {account['name']}\nТелефон: {account.get('phone', 'QR-вход')}", reply_markup=kb(("🗑 Удалить", f"account:delete:{key}"), ("← Назад", "accounts")))


@dp.callback_query(F.data.startswith("account:delete:"))
async def account_delete(call: CallbackQuery):
    key = call.data.rsplit(":", 1)[1]; data = load()
    used = [t["name"] for t in data["tasks"].values() if t["account_id"] == key]
    if used: await call.answer("Аккаунт используется в задачах", show_alert=True); return
    data["accounts"].pop(key, None); save(data); await accounts(call)


@dp.callback_query(F.data == "tasks")
async def tasks(call: CallbackQuery):
    data = load(); rows = [("➕ Создать задачу", "task:add")]
    rows += [(f"{'🟢' if key in live else '⚪'} {t['name']}", f"task:view:{key}") for key, t in data["tasks"].items()]
    rows.append(("← Назад", "home"))
    await call.answer(); await call.message.edit_text("Задачи синхронизации", reply_markup=kb(*rows))


@dp.callback_query(F.data == "task:add")
async def add_task(call: CallbackQuery):
    if not load()["accounts"]:
        await call.answer("Сначала добавьте Telegram-аккаунт", show_alert=True); return
    states[call.from_user.id] = {"step": "task_name"}
    await call.answer(); await call.message.edit_text("Название задачи:")


async def task_card(call, key):
    task = load()["tasks"].get(key)
    if not task: await call.answer("Задача не найдена", show_alert=True); return
    status = "🟢 непрерывная синхронизация" if key in live else "⚪ остановлена"
    text = (f"{task['name']}\n\n{status}\nИсточник: {task['source']}\nПолучатель: {task['target']}\n"
            f"История: {task['history_count']} последних постов\n"
            f"Пропустить в получателе: {task.get('target_skip', 0)}\n\n"
            "История редактирует существующие посты получателя: новый к новому.")
    rows = [("🧪 Проверить доступ", f"task:check:{key}"), ("📚 Загрузить историю", f"task:history:{key}"),
            ("⏭ Настроить пропуск", f"task:skip:{key}"),
            (("⏹ Остановить поток", f"task:stop:{key}") if key in live else ("▶️ Запустить поток", f"task:start:{key}")),
            ("🗑 Удалить", f"task:delete:{key}"), ("← Назад", "tasks")]
    await call.answer(); await call.message.edit_text(text, reply_markup=kb(*rows))


@dp.callback_query(F.data.startswith("task:view:"))
async def task_view(call: CallbackQuery): await task_card(call, call.data.rsplit(":", 1)[1])


async def task_client(task):
    account = load()["accounts"][task["account_id"]]
    client = make_client(API_ID, API_HASH, account["session"]); await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect(); raise RuntimeError("Сессия аккаунта не авторизована")
    return client


async def show_channel_choice(message, user_id: int, field: str, edit: bool = True):
    states[user_id]["step"] = f"task_{field}_choice"
    label = "источник" if field == "source" else "канал-получатель"
    text = f"Выберите {label}:\n\nМожно ввести публичную ссылку / @username либо открыть список доступных каналов и групп подключённого аккаунта."
    markup = kb(("✍️ Ввести ссылку", f"wizard:{field}:manual"), ("📂 Выбрать из моих каналов", f"wizard:{field}:list"))
    if edit: await message.edit_text(text, reply_markup=markup)
    else: await message.answer(text, reply_markup=markup)


async def show_account_channels(call: CallbackQuery, field: str):
    state = states.get(call.from_user.id)
    if not state or not state.get("account_id"):
        await call.answer("Мастер создания задачи завершён", show_alert=True); return
    account = load()["accounts"].get(state["account_id"])
    if not account:
        await call.answer("Аккаунт не найден", show_alert=True); return
    client = make_client(API_ID, API_HASH, account["session"])
    try:
        await client.connect()
        rows = []
        async for dialog in client.iter_dialogs(limit=200):
            if not (dialog.is_channel or dialog.is_group): continue
            title = (getattr(dialog.entity, "title", None) or dialog.name or "Без названия")[:50]
            peer = utils.get_peer_id(dialog.entity)
            rows.append((title, f"wizard:pick:{field}:{peer}"))
        rows.append(("← Ввести ссылку", f"wizard:{field}:manual"))
        await call.answer()
        await call.message.edit_text("Выберите канал или группу:\nЗакрытые каналы тоже отображаются, если аккаунт уже в них состоит.", reply_markup=kb(*rows))
    except Exception as exc:
        log.exception("Cannot list channels")
        await call.answer(f"Не удалось загрузить каналы: {exc}", show_alert=True)
    finally:
        with contextlib.suppress(Exception): await client.disconnect()


@dp.callback_query(F.data.startswith("task:check:"))
async def task_check(call: CallbackQuery):
    task = load()["tasks"].get(call.data.rsplit(":", 1)[1])
    try:
        client = await task_client(task); await client.get_entity(task["source"]); await client.get_entity(task["target"])
        probe = await client.send_message(task["target"], "Post Sync: проверка доступа"); await probe.delete(); await client.disconnect()
        await call.answer("Доступ подтвержден", show_alert=True)
    except Exception as exc: await call.answer(f"Ошибка: {str(exc)[:150]}", show_alert=True)


@dp.callback_query(F.data.startswith("task:history:"))
async def task_history(call: CallbackQuery):
    key = call.data.rsplit(":", 1)[1]; task = load()["tasks"].get(key)
    await call.answer(); await call.message.edit_text("Загружаю историю. Не закрывайте бот — статус появится здесь.")
    try:
        client = await task_client(task)
        async def progress(done, total, error):
            if done == total or done % 10 == 0: await call.message.edit_text(f"История: {done}/{total}. Ошибок: {error or 'нет'}")
        done, total, errors = await migrate_history(client, task["source"], task["target"], task["history_count"], task.get("target_skip", 0), progress)
        await client.disconnect(); await call.message.edit_text(f"Готово: {done}/{total}. Ошибок: {len(errors)}", reply_markup=kb(("← К задаче", f"task:view:{key}")))
    except Exception as exc:
        log.exception("Migration failed"); await call.message.edit_text(f"Миграция не выполнена: {exc}", reply_markup=kb(("← К задаче", f"task:view:{key}")))


@dp.callback_query(F.data.startswith("task:start:"))
async def task_start(call: CallbackQuery):
    key = call.data.rsplit(":", 1)[1]; task = load()["tasks"].get(key)
    try:
        account = load()["accounts"][task["account_id"]]; worker = LiveSync({**task, "session": account["session"]}, API_ID, API_HASH); await worker.start(); live[key] = worker
        await task_card(call, key)
    except Exception as exc: await call.answer(f"Не запущено: {exc}", show_alert=True)


@dp.callback_query(F.data.startswith("task:stop:"))
async def task_stop(call: CallbackQuery):
    key = call.data.rsplit(":", 1)[1]; worker = live.pop(key, None)
    if worker: await worker.stop()
    await task_card(call, key)


@dp.callback_query(F.data.startswith("task:delete:"))
async def task_delete(call: CallbackQuery):
    key = call.data.rsplit(":", 1)[1]
    if key in live: await live.pop(key).stop()
    data = load(); data["tasks"].pop(key, None); save(data); await tasks(call)


@dp.callback_query(F.data.startswith("task:skip:"))
async def task_skip(call: CallbackQuery):
    key = call.data.rsplit(":", 1)[1]
    if key not in load()["tasks"]:
        await call.answer("Задача не найдена", show_alert=True); return
    states[call.from_user.id] = {"step": "edit_skip", "task_id": key}
    await call.answer(); await call.message.edit_text("Сколько самых свежих постов получателя оставить без изменений?\n\nНапример, 2 — бот начнёт замену с третьего поста.")


@dp.message(F.text)
async def text_input(message: Message):
    if not allowed(message.from_user.id): return
    state = states.get(message.from_user.id)
    if not state: return
    text = message.text.strip(); step = state["step"]
    if step == "account_name":
        state.update(step="account_auth", name=text)
        await message.answer("Способ входа:", reply_markup=kb(("📱 По номеру", "account:auth:phone"), ("🔳 По QR-коду", "account:auth:qr")))
    elif step == "phone":
        try:
            client = make_client(API_ID, API_HASH, ""); await client.connect(); sent = await client.send_code_request(text)
            clients[message.from_user.id] = client; state.update(step="code", phone=text, phone_hash=sent.phone_code_hash)
            await message.answer("Код отправлен. Введите его одним сообщением:")
        except Exception as exc: await message.answer(f"Не удалось отправить код: {exc}")
    elif step == "code":
        client = clients.get(message.from_user.id)
        try:
            await client.sign_in(phone=state["phone"], code=text, phone_code_hash=state["phone_hash"]); await finish_account(message)
        except SessionPasswordNeededError: state["step"] = "password"; await message.answer("Введите пароль двухфакторной защиты:")
        except Exception as exc: await message.answer(f"Код не принят: {exc}")
    elif step == "password":
        try: await clients[message.from_user.id].sign_in(password=text); await finish_account(message)
        except Exception as exc: await message.answer(f"Пароль не принят: {exc}")
    elif step == "task_name":
        state.update(step="task_account", name=text); data = load(); rows = [(a["name"], f"task:account:{key}") for key, a in data["accounts"].items()]; await message.answer("Выберите аккаунт:", reply_markup=kb(*rows))
    elif step == "task_source":
        state["source"] = text; await show_channel_choice(message, message.from_user.id, "target", edit=False)
    elif step == "task_target":
        state.update(step="task_count", target=text); await message.answer("Сколько последних постов перенести в историю? Введите число, либо 0 для всех:")
    elif step == "task_count":
        try:
            count = int(text); assert count >= 0
            state.update(step="task_skip", history_count=count)
            await message.answer("Сколько последних постов получателя пропустить? Введите 0, если пропуск не нужен.")
        except Exception: await message.answer("Введите неотрицательное целое число.")
    elif step == "task_skip":
        try:
            skip = int(text); assert skip >= 0
            key = uuid.uuid4().hex[:10]; data = load(); data["tasks"][key] = {"name": state["name"], "account_id": state["account_id"], "source": state["source"], "target": state["target"], "source_id": state["source"], "target_id": state["target"], "history_count": state["history_count"], "target_skip": skip}; save(data); states.pop(message.from_user.id, None)
            await message.answer("Задача создана.", reply_markup=kb(("Открыть задачу", f"task:view:{key}"), ("Главное меню", "home")))
        except Exception: await message.answer("Введите неотрицательное целое число.")
    elif step == "edit_skip":
        try:
            skip = int(text); assert skip >= 0
            key = state["task_id"]; data = load(); data["tasks"][key]["target_skip"] = skip; save(data); states.pop(message.from_user.id, None)
            await message.answer(f"Готово: пропускаем {skip} последних постов получателя.", reply_markup=kb(("Открыть задачу", f"task:view:{key}")))
        except Exception: await message.answer("Введите неотрицательное целое число.")


@dp.callback_query(F.data.startswith("task:account:"))
async def choose_task_account(call: CallbackQuery):
    state = states.get(call.from_user.id)
    if not state or state.get("step") != "task_account": await call.answer("Мастер уже завершён", show_alert=True); return
    state["account_id"] = call.data.rsplit(":", 1)[1]
    await call.answer(); await show_channel_choice(call.message, call.from_user.id, "source")


@dp.callback_query(F.data.startswith("wizard:"))
async def task_channel_wizard(call: CallbackQuery):
    parts = call.data.split(":")
    if len(parts) < 3: return
    if parts[1] == "pick":
        action, field, tail = "pick", parts[2], parts[3:]
    else:
        field, action, tail = parts[1], parts[2], parts[3:]
    state = states.get(call.from_user.id)
    if not state or field not in ("source", "target"):
        await call.answer("Мастер создания задачи завершён", show_alert=True); return
    if action == "list":
        await show_account_channels(call, field); return
    if action == "manual":
        state["step"] = f"task_{field}"
        label = "канала-источника" if field == "source" else "вашего канала-получателя"
        await call.answer(); await call.message.edit_text(f"Введите ссылку или @username {label}:")
    elif action == "pick" and tail:
        state[field] = int(tail[0])
        await call.answer("Канал выбран")
        if field == "source": await show_channel_choice(call.message, call.from_user.id, "target")
        else:
            state["step"] = "task_count"
            await call.message.edit_text("Сколько последних постов перенести в историю? Введите число, либо 0 для всех:")


async def finish_account(message):
    state = states[message.from_user.id]; client = clients[message.from_user.id]; key = uuid.uuid4().hex[:10]; data = load()
    data["accounts"][key] = {"name": state["name"], "phone": state.get("phone", "QR-вход"), "session": client.session.save()}; save(data)
    await clear(message.from_user.id); await message.answer("Аккаунт добавлен.", reply_markup=kb(("К аккаунтам", "accounts"), ("Главное меню", "home")))


@dp.callback_query(F.data == "account:auth:phone")
async def account_auth_phone(call: CallbackQuery):
    state = states.get(call.from_user.id)
    if not state or state.get("step") != "account_auth":
        await call.answer("Начните добавление аккаунта заново", show_alert=True); return
    state["step"] = "phone"
    await call.answer(); await call.message.edit_text("Номер телефона в международном формате, например +79990000000:")


@dp.callback_query(F.data == "account:auth:qr")
async def account_auth_qr(call: CallbackQuery):
    state = states.get(call.from_user.id)
    if not state or state.get("step") != "account_auth":
        await call.answer("Начните добавление аккаунта заново", show_alert=True); return
    try:
        client = make_client(API_ID, API_HASH, "")
        await client.connect()
        qr_login = await client.qr_login()
        path = RUNTIME / f"login-{call.from_user.id}.png"
        qrcode.make(qr_login.url).save(path)
        clients[call.from_user.id] = client
        state["step"] = "qr"
        await call.answer(); await call.message.edit_text("QR-код отправлен. Откройте Telegram: Настройки → Устройства → Подключить устройство.")
        await bot.send_photo(call.message.chat.id, FSInputFile(path), caption="Срок действия QR — 2 минуты.")
        asyncio.create_task(wait_qr(call.message, call.from_user.id, qr_login))
    except Exception as exc:
        log.exception("QR login failed")
        await call.answer(f"QR не создан: {exc}", show_alert=True)


async def wait_qr(message: Message, user_id: int, qr_login):
    try:
        await asyncio.wait_for(qr_login.wait(), timeout=120)
        if states.get(user_id, {}).get("step") != "qr": return
        await finish_account(message)
    except SessionPasswordNeededError:
        if user_id in states:
            states[user_id]["step"] = "password"
            await message.answer("QR подтверждён. Введите пароль двухфакторной защиты:")
    except asyncio.TimeoutError:
        await clear(user_id)
        await message.answer("Срок действия QR истёк. Добавьте аккаунт заново.")
    except Exception:
        log.exception("QR wait failed")
        await clear(user_id)
        await message.answer("Не удалось завершить QR-вход. Попробуйте ещё раз.")


async def main():
    require_ready()
    data = load()
    for key, task in data["tasks"].items():
        if task.get("autostart"):
            with contextlib.suppress(Exception):
                account = data["accounts"][task["account_id"]]; worker = LiveSync({**task, "session": account["session"]}, API_ID, API_HASH); await worker.start(); live[key] = worker
    await dp.start_polling(bot)


if __name__ == "__main__": asyncio.run(main())
