"""
Telegram Anti-Spam Bot
=======================
2 ayrı grup seti ve 2 ayrı log grubu ile çalışır.
Her log grubu kendi grubunu kontrol eder.
İstek sayısı 20'yi geçerse otomatik temizler.

Komutlar (İlgili log grubunda yazılır):
  /ac       - Botu aktif et (o grup için)
  /kapat    - Botu kapat (o grup için)
  /temizle  - Tüm bekleyen istekleri reddet (o grup için)
"""

import asyncio
import os
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List
from enum import Enum
import logging

from telethon import TelegramClient, events
from telethon.tl.functions.messages import HideChatJoinRequestRequest, GetChatInviteImportersRequest
from telethon.tl.types import InputUserEmpty
from dotenv import load_dotenv

# .env dosyasını yükle
load_dotenv()

# ═══════════════════════════════════════════════════════════════
# AYARLAR
# ═══════════════════════════════════════════════════════════════

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")

# Grup 1 ayarları
PROTECTED_GROUPS_1 = [int(x.strip()) for x in os.getenv("PROTECTED_GROUPS_1", "").split(",") if x.strip()]
LOG_CHANNEL_ID_1 = int(os.getenv("LOG_CHANNEL_ID_1", "0"))

# Grup 2 ayarları
PROTECTED_GROUPS_2 = [int(x.strip()) for x in os.getenv("PROTECTED_GROUPS_2", "").split(",") if x.strip()]
LOG_CHANNEL_ID_2 = int(os.getenv("LOG_CHANNEL_ID_2", "0"))

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
class GroupSetStats:
    """Bir grup seti için istatistikler"""
    total_rejected: int = 0
    groups: Dict[int, GroupStats] = field(default_factory=dict)
    state: BotState = BotState.ACTIVE
    clearing_in_progress: bool = False
    accumulated_rejected: int = 0
    last_cleanup_time: datetime = None

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

        # Her grup seti için ayrı state
        self.group_sets = {}

        # Grup 1 için
        if LOG_CHANNEL_ID_1 and PROTECTED_GROUPS_1:
            self.group_sets[1] = {
                'protected_groups': PROTECTED_GROUPS_1,
                'log_channel': LOG_CHANNEL_ID_1,
                'stats': GroupSetStats()
            }

        # Grup 2 için
        if LOG_CHANNEL_ID_2 and PROTECTED_GROUPS_2:
            self.group_sets[2] = {
                'protected_groups': PROTECTED_GROUPS_2,
                'log_channel': LOG_CHANNEL_ID_2,
                'stats': GroupSetStats()
            }

        # Log channel -> group set mapping
        self.log_to_set = {}
        for set_id, data in self.group_sets.items():
            self.log_to_set[data['log_channel']] = set_id

        self.me = None

    async def start(self):
        """Botu başlat"""
        logger.info("Bot başlatılıyor...")

        await self.client.start()

        self.me = await self.client.get_me()
        logger.info(f"Giriş yapıldı: @{self.me.username} ({self.me.first_name})")

        # Tüm log kanallarından komut al
        all_log_channels = [data['log_channel'] for data in self.group_sets.values()]

        if all_log_channels:
            self.client.add_event_handler(
                self.on_command,
                events.NewMessage(
                    chats=all_log_channels,
                    pattern=r'^/(ac|kapat|temizle)$'
                )
            )

        # Bilgilendirme logları
        for set_id, data in self.group_sets.items():
            logger.info(f"Grup Seti {set_id}: {len(data['protected_groups'])} grup, Log: {data['log_channel']}")

        logger.info("Bot AÇIK başladı.")

        # Başlangıçta hemen kontrol et
        for set_id in self.group_sets:
            await self.check_and_auto_clean(set_id)

        # Periyodik kontrol başlat
        asyncio.create_task(self.periodic_check())

        await self.client.run_until_disconnected()

    async def periodic_check(self):
        """Her 10 saniyede bir istek sayısını kontrol et"""
        logger.info("Periyodik kontrol döngüsü başlatıldı (10 saniye aralıklarla)")
        check_count = 0
        while True:
            await asyncio.sleep(10)
            check_count += 1

            for set_id, data in self.group_sets.items():
                stats = data['stats']

                if stats.state != BotState.ACTIVE:
                    if check_count % 6 == 0:  # Her dakikada bir logla
                        logger.info(f"Grup Seti {set_id} - Bot aktif değil (state: {stats.state})")
                    continue

                if stats.clearing_in_progress:
                    logger.info(f"Grup Seti {set_id} - Temizleme devam ediyor, kontrol atlanıyor")
                    continue

                # 2 dakika silme olmadıysa ve birikmiş varsa log at
                if stats.last_cleanup_time and stats.accumulated_rejected > 0:
                    elapsed = (datetime.now() - stats.last_cleanup_time).total_seconds()
                    if elapsed >= 120:  # 2 dakika
                        await self.send_log(set_id, f"✅ {stats.accumulated_rejected} istek temizlendi")
                        stats.accumulated_rejected = 0
                        stats.last_cleanup_time = None

                # Grup setindeki toplam bekleyen istek sayısını al
                total_pending = 0
                for group_id in data['protected_groups']:
                    count = await self.get_pending_count(group_id)
                    total_pending += count

                # Her 30 saniyede bir durum logla (3 check = 30 saniye)
                if check_count % 3 == 0:
                    logger.info(f"[Periyodik] Grup Seti {set_id} - Bekleyen: {total_pending}, Eşik: {AUTO_CLEAN_THRESHOLD}")

                if total_pending > AUTO_CLEAN_THRESHOLD:
                    logger.info(f"[Periyodik] Grup Seti {set_id} - Eşik aşıldı! Temizleme başlatılıyor...")
                    await self.do_cleanup(set_id, manual=False)

    async def on_command(self, event):
        """Komut handler - Log gruplarından"""
        command = event.text.lower().strip('/')
        chat_id = event.chat_id

        # Hangi grup setine ait olduğunu bul
        set_id = self.log_to_set.get(chat_id)
        if set_id is None:
            return

        data = self.group_sets[set_id]
        stats = data['stats']

        # ═══════════════════════════════════════════════════════
        # /ac - Botu aktif et (o grup seti için)
        # ═══════════════════════════════════════════════════════
        if command == 'ac':
            if stats.state == BotState.ACTIVE:
                await event.reply(f"Bot zaten aktif! (Grup Seti {set_id})")
                return

            if stats.state == BotState.CLEARING:
                await event.reply("Temizleme devam ediyor, bekleyin...")
                return

            stats.state = BotState.ACTIVE
            logger.info(f"Grup Seti {set_id} AKTİF edildi")
            await event.reply(f"🟢 Bot aktif! (Grup Seti {set_id})")

            # Açılınca hemen 20+ kontrol et
            await self.check_and_auto_clean(set_id)

        # ═══════════════════════════════════════════════════════
        # /kapat - Botu kapat (o grup seti için)
        # ═══════════════════════════════════════════════════════
        elif command == 'kapat':
            if stats.state == BotState.INACTIVE:
                await event.reply(f"Bot zaten kapalı! (Grup Seti {set_id})")
                return

            if stats.state == BotState.CLEARING:
                await event.reply("Temizleme devam ediyor, bekleyin...")
                return

            stats.state = BotState.INACTIVE
            logger.info(f"Grup Seti {set_id} KAPATILDI")
            await event.reply(f"🔴 Bot kapatıldı! (Grup Seti {set_id})")

        # ═══════════════════════════════════════════════════════
        # /temizle - Tüm istekleri reddet (o grup seti için)
        # ═══════════════════════════════════════════════════════
        elif command == 'temizle':
            if stats.clearing_in_progress:
                await event.reply("Temizleme zaten devam ediyor...")
                return

            await self.do_cleanup(set_id, manual=True)

    async def do_cleanup(self, set_id: int, manual=False):
        """Belirli grup setindeki tüm istekleri temizle"""
        data = self.group_sets[set_id]
        stats = data['stats']

        if stats.clearing_in_progress:
            logger.info(f"Grup Seti {set_id} - Temizleme zaten devam ediyor, atlanıyor")
            return

        logger.info(f"Grup Seti {set_id} - Temizleme başlıyor (Manual: {manual})")

        previous_state = stats.state
        stats.state = BotState.CLEARING
        stats.clearing_in_progress = True

        # Bu setteki tüm gruplardaki istekleri temizle
        total_rejected = 0
        for group_id in data['protected_groups']:
            logger.info(f"Grup {group_id} temizleniyor...")
            rejected = await self.clear_all_requests(group_id)
            total_rejected += rejected
            logger.info(f"Grup {group_id} - {rejected} istek reddedildi")

        stats.clearing_in_progress = False
        stats.state = previous_state if not manual else BotState.INACTIVE

        # Biriktir
        stats.accumulated_rejected += total_rejected
        stats.last_cleanup_time = datetime.now()

        logger.info(f"Grup Seti {set_id} - Temizleme tamamlandı: {total_rejected} istek silindi (Toplam birikmiş: {stats.accumulated_rejected})")

        # Eğer istek silindiyse log grubuna bildir
        if total_rejected > 0:
            await self.send_log(set_id, f"🗑️ {total_rejected} istek silindi (Otomatik: {not manual})")

    async def clear_all_requests(self, chat_id: int) -> int:
        """Bir gruptaki tüm bekleyen istekleri reddet"""
        total_rejected = 0

        try:
            chat = await self.client.get_entity(chat_id)
            chat_title = chat.title if hasattr(chat, 'title') else f"Grup {chat_id}"
            logger.info(f"Grup '{chat_title}' ({chat_id}) istekleri siliniyor...")

            while True:
                try:
                    result = await self.client(GetChatInviteImportersRequest(
                        peer=chat_id,
                        requested=True,
                        limit=100,
                        offset_date=None,
                        offset_user=InputUserEmpty(),
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

        return total_rejected

    async def get_pending_count(self, chat_id: int) -> int:
        """Bekleyen istek sayısını al"""
        try:
            result = await self.client(GetChatInviteImportersRequest(
                peer=chat_id,
                requested=True,
                limit=1,
                offset_date=None,
                offset_user=InputUserEmpty(),
                q=""
            ))
            count = result.count if hasattr(result, 'count') else len(result.importers)
            logger.info(f"Grup {chat_id} - Bekleyen istek: {count}")
            return count
        except Exception as e:
            logger.error(f"Bekleyen istek sayısı alınamadı (Grup {chat_id}): {e}")
            return 0

    async def check_and_auto_clean(self, set_id: int):
        """20'yi geçerse otomatik temizle"""
        data = self.group_sets[set_id]
        stats = data['stats']

        # State kontrolü ekle - sadece ACTIVE iken çalış
        if stats.state != BotState.ACTIVE:
            logger.info(f"Grup Seti {set_id} - Bot aktif değil, kontrol atlanıyor")
            return

        if stats.clearing_in_progress:
            logger.info(f"Grup Seti {set_id} - Temizleme devam ediyor, kontrol atlanıyor")
            return

        total_pending = 0
        for group_id in data['protected_groups']:
            count = await self.get_pending_count(group_id)
            total_pending += count

        logger.info(f"Grup Seti {set_id} - Toplam bekleyen istek: {total_pending} (Eşik: {AUTO_CLEAN_THRESHOLD})")

        if total_pending > AUTO_CLEAN_THRESHOLD:
            logger.info(f"Grup Seti {set_id} - Otomatik temizleme başlatılıyor: {total_pending} istek")
            await self.do_cleanup(set_id, manual=False)
        else:
            logger.info(f"Grup Seti {set_id} - Temizleme gerekmiyor ({total_pending} <= {AUTO_CLEAN_THRESHOLD})")

    async def send_log(self, set_id: int, message: str):
        """Belirli grup setinin log grubuna mesaj gönder"""
        data = self.group_sets.get(set_id)
        if not data:
            return

        log_channel = data['log_channel']
        if not log_channel:
            return

        try:
            await self.client.send_message(log_channel, f"[Grup Seti {set_id}] {message}")
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

    # En az bir grup seti tanımlı olmalı
    has_group_set = False

    if LOG_CHANNEL_ID_1 and PROTECTED_GROUPS_1:
        has_group_set = True
        logger.info(f"Grup Seti 1 yapılandırıldı: {len(PROTECTED_GROUPS_1)} grup")
    else:
        logger.warning("Grup Seti 1 yapılandırılmamış (PROTECTED_GROUPS_1 veya LOG_CHANNEL_ID_1 eksik)")

    if LOG_CHANNEL_ID_2 and PROTECTED_GROUPS_2:
        has_group_set = True
        logger.info(f"Grup Seti 2 yapılandırıldı: {len(PROTECTED_GROUPS_2)} grup")
    else:
        logger.warning("Grup Seti 2 yapılandırılmamış (PROTECTED_GROUPS_2 veya LOG_CHANNEL_ID_2 eksik)")

    if not has_group_set:
        logger.error("En az bir grup seti yapılandırılmalı!")
        return

    bot = AntiSpamBot()
    await bot.start()

if __name__ == "__main__":
    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║          TELEGRAM ANTI-SPAM BOT (2 GRUP SETİ)             ║
    ╠═══════════════════════════════════════════════════════════╣
    ║  Komutlar (İlgili log grubunda yazılır):                  ║
    ║    /ac      - Botu aktif et (o grup seti için)            ║
    ║    /kapat   - Botu kapat (o grup seti için)               ║
    ║    /temizle - Tüm istekleri reddet (o grup seti için)     ║
    ║                                                           ║
    ║  Grup Seti 1: PROTECTED_GROUPS_1 + LOG_CHANNEL_ID_1       ║
    ║  Grup Seti 2: PROTECTED_GROUPS_2 + LOG_CHANNEL_ID_2       ║
    ║                                                           ║
    ║  20+ istek = Otomatik temizleme                           ║
    ╚═══════════════════════════════════════════════════════════╝
    """)
    asyncio.run(main())
