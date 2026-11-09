import asyncio
import logging
import os
import time
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from openai import AsyncOpenAI
from supabase import create_client
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("telegram-ai-operator")

def env(name, default=None):
    value = os.getenv(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Не задана переменная окружения: {name}")
    return value

BOT_TOKEN = env("BOT_TOKEN")
ADMIN_ID = int(env("ADMIN_ID"))
API_ID = int(env("API_ID"))
API_HASH = env("API_HASH")
TG_SESSION = env("TELEGRAM_SESSION")
GROQ_API_KEY = env("GROQ_API_KEY")
SUPABASE_URL = env("SUPABASE_URL")
SUPABASE_KEY = env("SUPABASE_SERVICE_ROLE_KEY")
AI_MODEL = os.getenv("AI_MODEL", "llama-3.3-70b-versatile")
DEFAULT_PROMPT = os.getenv(
    "DEFAULT_SYSTEM_PROMPT",
    "Ты — помощник владельца этого Telegram-аккаунта. Общайся естественно, доброжелательно, "
    "уважительно и по делу. Не выдумывай факты и обещания. Если вопрос требует решения владельца, "
    "не принимай обязательств и попроси собеседника немного подождать."
)

db = create_client(SUPABASE_URL, SUPABASE_KEY)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
llm = AsyncOpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
user_client = TelegramClient(StringSession(TG_SESSION), API_ID, API_HASH)

# Runtime-only anti-burst guard. No message transcript is written to the database.
last_reply = {}
REPLY_COOLDOWN_SECONDS = int(os.getenv("REPLY_COOLDOWN_SECONDS", "20"))
MAX_REPLY_CHARS = int(os.getenv("MAX_REPLY_CHARS", "3500"))

def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == ADMIN_ID)

def get_setting(key, default=None):
    result = db.table("operator_settings").select("value").eq("key", key).limit(1).execute()
    return result.data[0]["value"] if result.data else default

def set_setting(key, value):
    db.table("operator_settings").upsert({"key": key, "value": str(value)}).execute()

def get_allowed():
    result = db.table("operator_allowed_users").select("telegram_user_id,username").order("added_at").execute()
    return result.data or []

def is_allowed(user_id):
    result = db.table("operator_allowed_users").select("telegram_user_id").eq("telegram_user_id", user_id).limit(1).execute()
    return bool(result.data)

def add_allowed(user_id, username):
    db.table("operator_allowed_users").upsert({
        "telegram_user_id": int(user_id),
        "username": username or ""
    }).execute()

def remove_allowed(user_id):
    db.table("operator_allowed_users").delete().eq("telegram_user_id", int(user_id)).execute()

def enabled():
    return get_setting("enabled", "false").lower() == "true"

def system_prompt():
    return get_setting("system_prompt", DEFAULT_PROMPT)

def menu_text():
    state = "ВКЛЮЧЁН" if enabled() else "ВЫКЛЮЧЕН"
    people = len(get_allowed())
    return (
        "🤖 <b>Telegram AI Operator</b>\n\n"
        f"Состояние: <b>{state}</b>\n"
        f"Разрешённых собеседников: <b>{people}</b>\n\n"
        "Команды:\n"
        "/enable — включить ответы\n"
        "/disable — выключить ответы\n"
        "/add @username — добавить собеседника\n"
        "/remove @username — убрать собеседника\n"
        "/list — список собеседников\n"
        "/prompt текст — изменить инструкции ИИ\n"
        "/status — состояние системы"
    )

@dp.message(Command("start"))
async def start_cmd(message: Message):
    if not is_admin(message):
        return
    await message.answer(menu_text(), parse_mode="HTML")

@dp.message(Command("help"))
async def help_cmd(message: Message):
    if not is_admin(message):
        return
    await message.answer(menu_text(), parse_mode="HTML")

@dp.message(Command("status"))
async def status_cmd(message: Message):
    if not is_admin(message):
        return
    try:
        tg_ok = user_client.is_connected()
        await message.answer(
            f"Режим: {'ВКЛ' if enabled() else 'ВЫКЛ'}\n"
            f"Telegram-сессия: {'подключена' if tg_ok else 'не подключена'}\n"
            f"Собеседников: {len(get_allowed())}\n"
            f"Модель: {AI_MODEL}"
        )
    except Exception as exc:
        log.exception("Status check failed")
        await message.answer(f"Не удалось проверить состояние: {type(exc).__name__}")

@dp.message(Command("enable"))
async def enable_cmd(message: Message):
    if not is_admin(message):
        return
    set_setting("enabled", "true")
    await message.answer("✅ Автоматические ответы включены.")

@dp.message(Command("disable"))
async def disable_cmd(message: Message):
    if not is_admin(message):
        return
    set_setting("enabled", "false")
    await message.answer("⏸ Автоматические ответы выключены.")

@dp.message(Command("add"))
async def add_cmd(message: Message):
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Использование: /add @username")
        return
    username = parts[1].strip().lstrip("@")
    try:
        entity = await user_client.get_entity(username)
        if not getattr(entity, "id", None):
            await message.answer("Не удалось определить пользователя.")
            return
        add_allowed(entity.id, getattr(entity, "username", username))
        await message.answer(f"✅ Добавлен: @{getattr(entity, 'username', username)}")
    except Exception as exc:
        log.exception("Could not add Telegram user")
        await message.answer(f"Не удалось добавить пользователя: {type(exc).__name__}")

@dp.message(Command("remove"))
async def remove_cmd(message: Message):
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Использование: /remove @username")
        return
    username = parts[1].strip().lstrip("@")
    try:
        entity = await user_client.get_entity(username)
        remove_allowed(entity.id)
        await message.answer(f"Убран из списка: @{getattr(entity, 'username', username)}")
    except Exception as exc:
        log.exception("Could not remove Telegram user")
        await message.answer(f"Не удалось убрать пользователя: {type(exc).__name__}")

@dp.message(Command("list"))
async def list_cmd(message: Message):
    if not is_admin(message):
        return
    rows = get_allowed()
    if not rows:
        await message.answer("Список разрешённых собеседников пуст.")
        return
    lines = [f"• @{row.get('username') or 'без_username'} — ID {row['telegram_user_id']}" for row in rows]
    await message.answer("Разрешённые собеседники:\n" + "\n".join(lines))

@dp.message(Command("prompt"))
async def prompt_cmd(message: Message):
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 1:
        await message.answer("Текущая инструкция:\n\n" + system_prompt())
        return
    prompt = parts[1].strip()
    if len(prompt) < 10:
        await message.answer("Инструкция слишком короткая. Добавь больше деталей.")
        return
    set_setting("system_prompt", prompt)
    await message.answer("✅ Инструкция ИИ обновлена.")

async def answer_with_ai(chat_id, incoming_text):
    # Fetch a short live context from Telegram itself; nothing is persisted in our database.
    messages = await user_client.get_messages(chat_id, limit=12)
    context = []
    for item in reversed(messages):
        if not item.message:
            continue
        role = "assistant" if item.out else "user"
        context.append({"role": role, "content": item.message[:4000]})
    if not context or context[-1]["role"] != "user":
        context.append({"role": "user", "content": incoming_text[:4000]})

    response = await llm.chat.completions.create(
        model=AI_MODEL,
        messages=[{"role": "system", "content": system_prompt()}] + context,
        temperature=0.7,
        max_tokens=900,
    )
    text = (response.choices[0].message.content or "").strip()
    return text[:MAX_REPLY_CHARS]

@user_client.on(events.NewMessage(incoming=True))
async def on_new_message(event):
    try:
        if not enabled():
            return
        if not event.is_private or event.sender_id is None:
            return
        if not is_allowed(event.sender_id):
            return
        if not event.raw_text or not event.raw_text.strip():
            return
        now = time.monotonic()
        previous = last_reply.get(event.sender_id, 0)
        if now - previous < REPLY_COOLDOWN_SECONDS:
            return
        last_reply[event.sender_id] = now
        response = await answer_with_ai(event.chat_id, event.raw_text)
        if response:
            await event.respond(response)
    except FloodWaitError as exc:
        log.warning("Telegram rate limit: wait %s seconds", exc.seconds)
        await asyncio.sleep(min(exc.seconds, 300))
    except Exception as exc:
        log.exception("Incoming message processing failed: %s", type(exc).__name__)

async def health(request):
    return web.json_response({
        "status": "ok",
        "telegram_connected": user_client.is_connected(),
        "enabled": enabled()
    })

async def run_web():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health endpoint listening on port %s", port)
    return runner

async def main():
    await user_client.connect()
    if not await user_client.is_user_authorized():
        raise RuntimeError("TELEGRAM_SESSION недействительна. Сгенерируй новую сессию локально.")
    log.info("Telegram user session connected")
    runner = await run_web()
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await user_client.disconnect()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
