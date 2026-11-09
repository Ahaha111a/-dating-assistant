# Запускается ТОЛЬКО локально на твоём устройстве для создания TELEGRAM_SESSION.
# Не загружай этот файл с введёнными данными и не отправляй коды входа кому-либо.
import asyncio
import os
from telethon import TelegramClient
from telethon.sessions import StringSession

async def main():
    api_id = int(input("Telegram API_ID: ").strip())
    api_hash = input("Telegram API_HASH: ").strip()
    phone = input("Номер отдельного Telegram-аккаунта с кодом страны: ").strip()
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start(phone=phone)
    print("\nТВОЯ TELEGRAM_SESSION (секрет, полный доступ к аккаунту):\n")
    print(client.session.save())
    print("\nСкопируй её напрямую в защищённую переменную TELEGRAM_SESSION на сервере.")
    print("Не отправляй её в чат, боту, GitHub или кому-либо ещё.")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
