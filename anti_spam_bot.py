"""
Telegram Anti-Spam Bot
=======================
Onaylı gruplarda spam join request'leri yönetir.
İstek sayısı 20'yi geçerse otomatik temizler.

Komutlar (Log grubunda yazılır):
  /ac       - Botu aktif et
  /kapat    - Botu kapat
  /temizle  - Tüm bekleyen istekleri reddet
"""

import asyncio
import os
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict
from enum import Enum
import logging

from telethon import TelegramClient, events
from telethon.tl.functions.messages import HideChatJoinRequestRequest, GetChatInviteImportersRequest
from dotenv import load_dotenv

# .env dosyasını yükle
load_dotenv()

# ═══════════════════════════════════════════════════════════════
# AYARLAR
# ═══════════════════════════════════════════════════════════════

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")

# Korunan grup ID'leri (birden fazla grup ekleyebilirsiniz)
PROTECTED_GROUPS = [int(x.strip()) for x in os.getenv("PROTECTED_GROUPS", "").split(",") if x.strip()]

# Log grubu ID'si (komutlar buradan alınır, loglar buraya gönderilir)
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

# Otomatik temizleme eşiği
AUTO_CLEAN_THRESHOLD = 20

# ═══════════════════════════════════════════════════════════════
# LOGGING AYARLARI
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# VERİ YAPILARI
# ═══════════════════════════════════════════════════════════════

class BotState(Enum):
    """Bot durumları"""
    ACTIVE = "active"
    INACTIVE = "inactive"
    CLEARING = "clearing"

@dataclass
class GroupStats:
    """Grup bazlı istatistikler"""
    pending_count: int = 0

@dataclass
class GlobalStats:
    """Genel istatistikler"""
    total_rejected: int = 0
    groups: Dict[int, GroupStats] = field(default_factory=dict)

# Global değişkenler
stats = GlobalStats()

# ═══════════════════════════════════════════════════════════════
# BOT SINIFI
# ═══════════════════════════════════════════════════════════════

class AntiSpamBot:
    def __init__(self):
        from telethon.sessions import StringSession

        if SESSION_STRING:
            self.client = TelegramClient(
                StringSession(SESSION_STRING),
                API_ID,
                API_HASH
            )
        else:
            self.client = TelegramClient(
                "anti_spam_session",
                API_ID,
                API_HASH
            )

        self.stats = stats
        self.state = BotState.INACTIVE
        self.clearing_in_progress = False
        self.me = None

    async def start(self):
        """Botu başlat"""
        logger.info("Bot başlatılıyor...")

        await self.client.start()

        self.me = await self.client.get_me()
        logger.info(f"Giriş yapıldı: @{self.me.username} ({self.me.first_name})")

        # Komut handler - SADECE LOG GRUBUNDAN
        self.client.add_event_handler(
            self.on_command,
            events.NewMessage(
                chats=[LOG_CHANNEL_ID],
                pattern=r'^/(ac|kapat|temizle)$'
            )
        )

        # Raw update handler for join requests
        self.client.add_event_handler(
            self.on_raw_update,
            events.Raw()
        )

        logger.info(f"Korunan grup: {len(PROTECTED_GROUPS)}")
        logger.info(f"Log grubu: {LOG_CHANNEL_ID}")
        logger.info("Bot KAPALI başladı. /ac komutu ile açın.")

        await self.client.run_until_disconnected()

    async def on_command(self, event):
        """Komut handler - Sadece log grubundan"""
        command = event.text.lower().strip('/')

        # ═══════════════════════════════════════════════════════
        # /ac - Botu aktif et
        # ═══════════════════════════════════════════════════════
        if command == 'ac':
            if self.state == BotState.ACTIVE:
                await event.reply("Bot zaten aktif!")
                return

            if self.state == BotState.CLEARING:
                await event.reply("Temizleme devam ediyor, bekleyin...")
                return

            self.state = BotState.ACTIVE
            logger.info("Bot AKTİF edildi")
            await event.reply("🟢 Bot aktif!")

            # Açılınca hemen 20+ kontrol et
            await self.check_and_auto_clean()

        # ═══════════════════════════════════════════════════════
        # /kapat - Botu kapat
        # ═══════════════════════════════════════════════════════
        elif command == 'kapat':
            if self.state == BotState.INACTIVE:
                await event.reply("Bot zaten kapalı!")
                return

            if self.state == BotState.CLEARING:
                await event.reply("Temizleme devam ediyor, bekleyin...")
                return

            self.state = BotState.INACTIVE
            logger.info("Bot KAPATILDI")
            await event.reply("🔴 Bot kapatıldı!")

        # ═══════════════════════════════════════════════════════
        # /temizle - Tüm istekleri reddet
        # ═══════════════════════════════════════════════════════
        elif command == 'temizle':
            if self.clearing_in_progress:
                await event.reply("Temizleme zaten devam ediyor...")
                return

            await self.do_cleanup(manual=True)

    async def do_cleanup(self, manual=False):
        """Tüm istekleri temizle"""
        if self.clearing_in_progress:
            return

        previous_state = self.state
        self.state = BotState.CLEARING
        self.clearing_in_progress = True

        logger.info("Temizleme başlatıldı")

        # Tüm gruplardaki istekleri temizle
        total_rejected = 0
        for group_id in PROTECTED_GROUPS:
            rejected = await self.clear_all_requests(group_id)
            total_rejected += rejected

        self.clearing_in_progress = False
        self.state = previous_state if not manual else BotState.INACTIVE

        # SADECE TEMİZLEME BİTİNCE LOG GÖNDER
        if total_rejected > 0:
            await self.send_log(f"✅ {total_rejected} istek temizlendi")
        else:
            await self.send_log("✅ Temizlendi (bekleyen istek yoktu)")

        logger.info(f"Temizleme tamamlandı - {total_rejected} istek reddedildi")

    async def clear_all_requests(self, chat_id: int) -> int:
        """Bir gruptaki tüm bekleyen istekleri reddet"""
        total_rejected = 0

        try:
            chat = await self.client.get_entity(chat_id)
            chat_title = chat.title if hasattr(chat, 'title') else f"Grup {chat_id}"

            logger.info(f"{chat_title} temizleniyor...")

            while True:
                try:
                    result = await self.client(GetChatInviteImportersRequest(
                        peer=chat_id,
                        requested=True,
                        limit=100,
                        offset_date=None,
                        offset_user=0,
                        q=""
                    ))

                    if not result.importers:
                        break

                    for importer in result.importers:
                        try:
                            await self.client(HideChatJoinRequestRequest(
                                peer=chat_id,
                                user_id=importer.user_id,
                                approved=False
                            ))

                            total_rejected += 1
                            self.stats.total_rejected += 1

                            if total_rejected % 10 == 0:
                                logger.info(f"{total_rejected} istek reddedildi...")

                            await asyncio.sleep(0.03)

                        except Exception as e:
                            if "HIDE_REQUESTER_MISSING" in str(e):
                                continue
                            logger.error(f"Red hatası: {e}")
                            await asyncio.sleep(0.1)

                except Exception as e:
                    error_str = str(e)
                    if "FLOOD_WAIT" in error_str:
                        import re
                        wait_match = re.search(r'(\d+)', error_str)
                        wait_time = int(wait_match.group(1)) if wait_match else 5
                        logger.warning(f"FloodWait: {wait_time} saniye bekleniyor...")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"İstek listesi hatası: {e}")
                        break

                await asyncio.sleep(0.5)

        except Exception as e:
            logger.error(f"Grup erişim hatası {chat_id}: {e}")

        # Grup pending count'u sıfırla
        if chat_id in self.stats.groups:
            self.stats.groups[chat_id].pending_count = 0

        return total_rejected

    async def get_pending_count(self, chat_id: int) -> int:
        """Bekleyen istek sayısını al"""
        try:
            result = await self.client(GetChatInviteImportersRequest(
                peer=chat_id,
                requested=True,
                limit=1,
                offset_date=None,
                offset_user=0,
                q=""
            ))
            return result.count if hasattr(result, 'count') else len(result.importers)
        except:
            return 0

    async def check_and_auto_clean(self):
        """20'yi geçerse otomatik temizle"""
        if self.state != BotState.ACTIVE or self.clearing_in_progress:
            return

        total_pending = 0
        for group_id in PROTECTED_GROUPS:
            count = await self.get_pending_count(group_id)
            total_pending += count

            if group_id not in self.stats.groups:
                self.stats.groups[group_id] = GroupStats()
            self.stats.groups[group_id].pending_count = count

        if total_pending > AUTO_CLEAN_THRESHOLD:
            logger.info(f"Otomatik temizleme tetiklendi: {total_pending} istek")
            await self.do_cleanup(manual=False)

    async def on_raw_update(self, event):
        """Raw update handler - Join Request'leri yakalar"""
        from telethon.tl.types import UpdateBotChatInviteRequester

        if self.state != BotState.ACTIVE:
            return

        if not isinstance(event, UpdateBotChatInviteRequester):
            return

        chat_id = event.peer.channel_id if hasattr(event.peer, 'channel_id') else event.peer.chat_id

        # Korunan gruplardan biri mi kontrol et
        if PROTECTED_GROUPS and chat_id not in PROTECTED_GROUPS and -100*chat_id not in PROTECTED_GROUPS:
            return

        # Grup istatistiklerini güncelle
        if chat_id not in self.stats.groups:
            self.stats.groups[chat_id] = GroupStats()

        self.stats.groups[chat_id].pending_count += 1

        logger.info(f"Yeni istek geldi - Bekleyen: {self.stats.groups[chat_id].pending_count}")

        # 20'yi geçtiyse otomatik temizle
        await self.check_and_auto_clean()

    async def send_log(self, message: str):
        """Log grubuna mesaj gönder"""
        if not LOG_CHANNEL_ID:
            return

        try:
            await self.client.send_message(LOG_CHANNEL_ID, message)
        except Exception as e:
            logger.error(f"Log gönderme hatası: {e}")

# ═══════════════════════════════════════════════════════════════
# ANA FONKSİYON
# ═══════════════════════════════════════════════════════════════

async def main():
    """Ana fonksiyon"""
    if not API_ID or not API_HASH:
        logger.error("API_ID ve API_HASH ayarlanmamış!")
        return

    if not LOG_CHANNEL_ID:
        logger.error("LOG_CHANNEL_ID ayarlanmamış!")
        return

    if not PROTECTED_GROUPS:
        logger.warning("PROTECTED_GROUPS boş")

    bot = AntiSpamBot()
    await bot.start()

if __name__ == "__main__":
    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║          TELEGRAM ANTI-SPAM BOT                           ║
    ╠═══════════════════════════════════════════════════════════╣
    ║  Komutlar (Log grubunda yazılır):                         ║
    ║    /ac      - Botu aktif et                               ║
    ║    /kapat   - Botu kapat                                  ║
    ║    /temizle - Tüm istekleri reddet                        ║
    ║                                                           ║
    ║  20+ istek = Otomatik temizleme                           ║
    ╚═══════════════════════════════════════════════════════════╝
    """)
    asyncio.run(main())
