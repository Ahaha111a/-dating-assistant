import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

async def main():
    api_id = int(input("API_ID: ").strip())
    api_hash = input("API_HASH: ").strip()
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()
    print("\nTELEGRAM_SESSION (никому не отправляйте):\n")
    print(client.session.save())
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
