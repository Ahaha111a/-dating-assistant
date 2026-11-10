import asyncio
import logging
import os
from contextlib import suppress

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from openai import AsyncOpenAI
from supabase import create_client
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
DEBOUNCE_SECONDS = float(os.getenv("DEBOUNCE_SECONDS", "1.5"))
MAX_REPLY_CHARS = int(os.getenv("MAX_REPLY_CHARS", "3500"))
CONTEXT_MESSAGES = int(os.getenv("CONTEXT_MESSAGES", "16"))
MAX_QUEUE_PER_USER = int(os.getenv("MAX_QUEUE_PER_USER", "100"))

DEFAULT_PROMPT = os.getenv(
    "DEFAULT_SYSTEM_PROMPT",
    "Ты — ИИ-помощник владельца этого Telegram-аккаунта. Общайся естественно, "
    "доброжелательно, уважительно и по делу. Не выдумывай факты и обещания. "
    "Не выдавай себя за человека: если собеседник спрашивает, кто отвечает, "
    "честно сообщи, что это ИИ-помощник. Не принимай важных обязательств от имени владельца."
)

db = create_client(SUPABASE_URL, SUPABASE_KEY)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
llm = AsyncOpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
user_client = TelegramClient(StringSession(TG_SESSION), API_ID, API_HASH)

# Очереди находятся только в памяти процесса. Тексты переписок в БД не сохраняются.
queues = {}
workers = {}
stopping = False


# Supabase SDK синхронный — запускаем сетевые операции в отдельных потоках.
def _setting(key, default=None):
    r = db.table("operator_settings").select("value").eq("key", key).limit(1).execute()
    return r.data[0]["value"] if r.data else default


async def get_setting(key, default=None):
    return await asyncio.to_thread(_setting, key, default)


async def set_setting(key, value):
    await asyncio.to_thread(
        lambda: db.table("operator_settings").upsert({"key": key, "value": str(value)}).execute()
    )


async def get_allowed():
    def query():
        return db.table("operator_allowed_users").select(
            "telegram_user_id,username"
        ).order("added_at").execute().data or []
    return await asyncio.to_thread(query)


async def is_allowed(user_id):
    def query():
        r = db.table("operator_allowed_users").select(
            "telegram_user_id"
        ).eq("telegram_user_id", user_id).limit(1).execute()
        return bool(r.data)
    return await asyncio.to_thread(query)


async def add_allowed(user_id, username):
    await asyncio.to_thread(lambda: db.table("operator_allowed_users").upsert({
        "telegram_user_id": int(user_id), "username": username or ""
    }).execute())


async def remove_allowed(user_id):
    await asyncio.to_thread(lambda: db.table("operator_allowed_users").delete().eq(
        "telegram_user_id", int(user_id)
    ).execute())


async def enabled():
    return (await get_setting("enabled", "false")).lower() == "true"


async def system_prompt():
    return await get_setting("system_prompt", DEFAULT_PROMPT)


def is_admin(message):
    return bool(message.from_user and message.from_user.id == ADMIN_ID)


async def menu_text():
    state = "ВКЛЮЧЁН" if await enabled() else "ВЫКЛЮЧЕН"
    return (
        "🤖 <b>Telegram AI Operator</b>\n\n"
        f"Состояние: <b>{state}</b>\n"
        f"Разрешённых собеседников: <b>{len(await get_allowed())}</b>\n\n"
        "Команды:\n/start — меню\n/status — состояние\n"
        "/enable — включить\n/disable — выключить\n"
        "/add @username — добавить\n/remove @username — убрать\n"
        "/list — список\n/prompt текст — инструкция ИИ"
    )


@dp.message(Command("start"))
@dp.message(Command("help"))
async def start_cmd(message: Message):
    if is_admin(message):
        await message.answer(await menu_text(), parse_mode="HTML")


@dp.message(Command("status"))
async def status_cmd(message: Message):
    if not is_admin(message):
        return
    try:
        await message.answer(
            f"Режим: {'ВКЛ' if await enabled() else 'ВЫКЛ'}\n"
            f"Telegram-сессия: {'подключена' if user_client.is_connected() else 'не подключена'}\n"
            f"Собеседников: {len(await get_allowed())}\nМодель: {AI_MODEL}"
        )
    except Exception as exc:
        log.exception("Status check failed")
        await message.answer(f"Ошибка проверки: {type(exc).__name__}")


@dp.message(Command("enable"))
async def enable_cmd(message: Message):
    if is_admin(message):
        await set_setting("enabled", "true")
        await message.answer("✅ Автоматические ответы включены.")


@dp.message(Command("disable"))
async def disable_cmd(message: Message):
    if is_admin(message):
        await set_setting("enabled", "false")
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
        await add_allowed(entity.id, getattr(entity, "username", username))
        await message.answer(f"✅ Добавлен: @{getattr(entity, 'username', username)}")
    except Exception as exc:
        log.exception("Add user failed")
        await message.answer(f"Не удалось добавить: {type(exc).__name__}")


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
        await remove_allowed(entity.id)
        await message.answer(f"Удалён: @{getattr(entity, 'username', username)}")
    except Exception as exc:
        log.exception("Remove user failed")
        await message.answer(f"Не удалось удалить: {type(exc).__name__}")


@dp.message(Command("list"))
async def list_cmd(message: Message):
    if not is_admin(message):
        return
    rows = await get_allowed()
    if not rows:
        await message.answer("Список разрешённых собеседников пуст.")
        return
    await message.answer("Разрешённые собеседники:\n" + "\n".join(
        f"• @{r.get('username') or 'без_username'} — ID {r['telegram_user_id']}"
        for r in rows
    ))


@dp.message(Command("prompt"))
async def prompt_cmd(message: Message):
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 1:
        await message.answer("Текущая инструкция:\n\n" + await system_prompt())
        return
    prompt = parts[1].strip()
    if len(prompt) < 10 or len(prompt) > 12000:
        await message.answer("Инструкция должна содержать от 10 до 12 000 символов.")
        return
    await set_setting("system_prompt", prompt)
    await message.answer("✅ Инструкция ИИ обновлена.")


async def answer_with_ai(chat_id, fallback_text):
    messages = await user_client.get_messages(chat_id, limit=CONTEXT_MESSAGES)
    context = []
    for item in reversed(messages):
        if item.message:
            context.append({
                "role": "assistant" if item.out else "user",
                "content": item.message[:4000]
            })
    if not context or context[-1]["role"] != "user":
        context.append({"role": "user", "content": fallback_text[:4000]})
    response = await llm.chat.completions.create(
        model=AI_MODEL,
        messages=[{"role": "system", "content": await system_prompt()}] + context,
        temperature=0.7,
        max_tokens=900,
        timeout=45.0,
    )
    return (response.choices[0].message.content or "").strip()[:MAX_REPLY_CHARS]


async def process_event(event):
    if not await enabled() or not await is_allowed(event.sender_id):
        return
    text = (event.raw_text or "").strip()
    if not text:
        return
    answer = await answer_with_ai(event.chat_id, text)
    if answer:
        await event.respond(answer)


async def worker(user_id):
    q = queues[user_id]
    while not stopping:
        event = await q.get()
        # Склеиваем быструю серию входящих сообщений; ничего не отбрасываем по cooldown.
        await asyncio.sleep(DEBOUNCE_SECONDS)
        while True:
            try:
                event = q.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            await process_event(event)
        except FloodWaitError as exc:
            delay = max(1, int(exc.seconds))
            log.warning("Telegram rate limit: wait %s seconds", delay)
            await asyncio.sleep(delay)
            with suppress(Exception):
                await process_event(event)
        except Exception as exc:
            log.exception("Message processing failed for %s: %s", user_id, type(exc).__name__)


@user_client.on(events.NewMessage(incoming=True))
async def on_new_message(event):
    if not event.is_private or event.sender_id is None or not (event.raw_text or "").strip():
        return
    if not await enabled() or not await is_allowed(event.sender_id):
        return
    q = queues.get(event.sender_id)
    if q is None:
        q = asyncio.Queue(maxsize=MAX_QUEUE_PER_USER)
        queues[event.sender_id] = q
    if q.full():
        with suppress(asyncio.QueueEmpty):
            q.get_nowait()
    q.put_nowait(event)
    task = workers.get(event.sender_id)
    if task is None or task.done():
        workers[event.sender_id] = asyncio.create_task(worker(event.sender_id))


async def health(request):
    try:
        return web.json_response({
            "status": "ok",
            "telegram_connected": user_client.is_connected(),
            "enabled": await enabled(),
        })
    except Exception as exc:
        log.exception("Health check failed")
        return web.json_response({"status": "degraded", "error": type(exc).__name__}, status=503)


async def run_web():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()
    return runner


async def main():
    global stopping
    await user_client.connect()
    if not await user_client.is_user_authorized():
        raise RuntimeError("TELEGRAM_SESSION недействительна. Создайте новую локально.")
    log.info("Telegram user session connected")
    runner = await run_web()
    try:
        await dp.start_polling(bot)
    finally:
        stopping = True
        for task in workers.values():
            task.cancel()
        for task in workers.values():
            with suppress(asyncio.CancelledError):
                await task
        await runner.cleanup()
        await user_client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
