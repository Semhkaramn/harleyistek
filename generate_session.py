"""
Session String Generator
========================
VPS'de kullanmak için session string oluşturur.
Bu scripti LOCAL bilgisayarınızda çalıştırın!
"""

import asyncio
import os
from telethon import TelegramClient
from telethon.sessions import StringSession
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")

async def main():
    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║          🔐  SESSION STRING GENERATOR  🔐                 ║
    ╚═══════════════════════════════════════════════════════════╝
    """)

    if not API_ID or not API_HASH:
        print("❌ API_ID ve API_HASH .env dosyasında ayarlanmalı!")
        print("   https://my.telegram.org adresinden alabilirsiniz.")
        return

    print("📱 Telegram hesabınıza giriş yapılacak...")
    print("   Telefon numaranızı gireceksiniz.")
    print()

    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        session_string = client.session.save()

        print()
        print("═" * 60)
        print("✅ SESSION STRING BAŞARIYLA OLUŞTURULDU!")
        print("═" * 60)
        print()
        print("Aşağıdaki string'i .env dosyasındaki SESSION_STRING'e yapıştırın:")
        print()
        print("-" * 60)
        print(session_string)
        print("-" * 60)
        print()
        print("⚠️  DİKKAT: Bu string'i KİMSEYLE PAYLAŞMAYIN!")
        print("    Bu string ile hesabınıza erişilebilir!")
        print()

if __name__ == "__main__":
    asyncio.run(main())
