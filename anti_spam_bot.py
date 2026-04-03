"""
Telegram Anti-Spam Bot
=======================
Onaylı gruplarda spam join request'leri otomatik reddeder.
3 saniye içinde 10+ istek gelirse flood olarak algılar ve hepsini reddeder.

Komutlar:
  /ac veya /on       - Botu aktif et
  /kapat veya /off   - Botu kapat
  /temizle veya /clear - Tüm bekleyen istekleri reddet
  /durum veya /status  - Bot durumunu göster
"""

import asyncio
import os
from datetime import datetime, timedelta
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Deque, List
from enum import Enum
import logging

from telethon import TelegramClient, events
from telethon.tl.functions.messages import HideChatJoinRequestRequest, GetChatInviteImportersRequest
from telethon.tl.types import PeerChannel
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

# Log kanalı ID'si (istatistikler buraya gönderilecek)
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

# Admin kullanıcı ID'leri (komut verebilecek kişiler)
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# Flood ayarları
FLOOD_WINDOW_SECONDS = int(os.getenv("FLOOD_WINDOW_SECONDS", "3"))
FLOOD_THRESHOLD = int(os.getenv("FLOOD_THRESHOLD", "10"))

# İstatistik özet aralığı (saniye)
STATS_INTERVAL = int(os.getenv("STATS_INTERVAL", "3600"))

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
    ACTIVE = "active"      # Aktif - flood algılama açık
    INACTIVE = "inactive"  # Pasif - hiçbir şey yapmaz
    CLEARING = "clearing"  # Temizleme - istekleri reddediyor

@dataclass
class RequestInfo:
    """Tek bir join request bilgisi"""
    user_id: int
    username: str
    first_name: str
    timestamp: datetime
    chat_id: int
    chat_title: str

@dataclass
class GroupStats:
    """Grup bazlı istatistikler"""
    total_requests: int = 0
    rejected_requests: int = 0
    flood_attacks: int = 0
    last_flood_time: datetime = None
    pending_requests: Deque[RequestInfo] = field(default_factory=deque)

@dataclass
class GlobalStats:
    """Genel istatistikler"""
    start_time: datetime = field(default_factory=datetime.now)
    total_requests: int = 0
    total_rejected: int = 0
    total_flood_attacks: int = 0
    groups: Dict[int, GroupStats] = field(default_factory=dict)

# Global değişkenler
stats = GlobalStats()

# ═══════════════════════════════════════════════════════════════
# BOT SINIFI
# ═══════════════════════════════════════════════════════════════

class AntiSpamBot:
    def __init__(self):
        from telethon.sessions import StringSession

        # Heroku için StringSession kullan (dosya tabanlı session çalışmaz)
        if SESSION_STRING:
            self.client = TelegramClient(
                StringSession(SESSION_STRING),
                API_ID,
                API_HASH
            )
        else:
            # Local development için dosya tabanlı session
            self.client = TelegramClient(
                "anti_spam_session",
                API_ID,
                API_HASH
            )

        self.stats = stats
        self.state = BotState.INACTIVE  # Başlangıçta kapalı
        self.flood_in_progress: Dict[int, bool] = {}
        self.clearing_in_progress = False
        self.me = None

    async def start(self):
        """Botu başlat"""
        logger.info("🚀 Bot başlatılıyor...")

        # Bağlan
        await self.client.start()

        self.me = await self.client.get_me()
        logger.info(f"✅ Giriş yapıldı: @{self.me.username} ({self.me.first_name})")

        # Komut handler'ları kaydet
        self.client.add_event_handler(
            self.on_command,
            events.NewMessage(pattern=r'^/(ac|on|kapat|off|temizle|clear|durum|status)$')
        )

        # Raw update handler for join requests
        self.client.add_event_handler(
            self.on_raw_update,
            events.Raw()
        )

        # Başlangıç mesajı gönder
        await self.send_log(
            "🟡 **Bot Başlatıldı (KAPALI)**\n\n"
            f"👤 Hesap: @{self.me.username}\n"
            f"🛡️ Korunan Grup: {len(PROTECTED_GROUPS)}\n"
            f"⏱️ Flood Penceresi: {FLOOD_WINDOW_SECONDS}s\n"
            f"🚫 Flood Eşiği: {FLOOD_THRESHOLD} istek\n\n"
            f"📝 **Komutlar:**\n"
            f"`/ac` - Botu aç\n"
            f"`/kapat` - Botu kapat\n"
            f"`/temizle` - İstekleri temizle\n"
            f"`/durum` - Durumu göster"
        )

        # Periyodik istatistik gönderimi başlat
        asyncio.create_task(self.periodic_stats())

        logger.info(f"🛡️ {len(PROTECTED_GROUPS)} grup ayarlandı")
        logger.info(f"⏱️ Flood: {FLOOD_WINDOW_SECONDS}s içinde {FLOOD_THRESHOLD} istek")
        logger.info("⚠️ Bot KAPALI başladı. /ac komutu ile açın.")

        await self.client.run_until_disconnected()

    async def on_command(self, event):
        """Komut handler"""
        sender = await event.get_sender()
        user_id = sender.id

        # Admin kontrolü
        if ADMIN_IDS and user_id not in ADMIN_IDS:
            return

        # Kendi mesajlarımızı da kabul et
        if user_id != self.me.id and ADMIN_IDS and user_id not in ADMIN_IDS:
            return

        command = event.text.lower().strip('/')
        chat = await event.get_chat()

        # ═══════════════════════════════════════════════════════
        # /ac veya /on - Botu aktif et
        # ═══════════════════════════════════════════════════════
        if command in ['ac', 'on']:
            if self.state == BotState.ACTIVE:
                await event.reply("⚠️ Bot zaten **aktif**!")
                return

            if self.state == BotState.CLEARING:
                await event.reply("⚠️ Temizleme devam ediyor, bekleyin...")
                return

            self.state = BotState.ACTIVE
            logger.info(f"🟢 Bot AKTİF edildi - {sender.first_name}")

            await event.reply(
                "🟢 **Bot AKTİF**\n\n"
                f"⏱️ {FLOOD_WINDOW_SECONDS}s içinde {FLOOD_THRESHOLD}+ istek = Otomatik RED\n"
                "🛡️ Flood koruması çalışıyor..."
            )

            await self.send_log(
                f"🟢 **Bot AKTİF EDİLDİ**\n\n"
                f"👤 Aktif eden: [{sender.first_name}](tg://user?id={user_id})\n"
                f"⏱️ Zaman: {datetime.now().strftime('%H:%M:%S')}"
            )

        # ═══════════════════════════════════════════════════════
        # /kapat veya /off - Botu kapat
        # ═══════════════════════════════════════════════════════
        elif command in ['kapat', 'off']:
            if self.state == BotState.INACTIVE:
                await event.reply("⚠️ Bot zaten **kapalı**!")
                return

            if self.state == BotState.CLEARING:
                await event.reply("⚠️ Temizleme devam ediyor, bekleyin...")
                return

            self.state = BotState.INACTIVE
            logger.info(f"🔴 Bot KAPATILDI - {sender.first_name}")

            await event.reply(
                "🔴 **Bot KAPALI**\n\n"
                "⏸️ Hiçbir işlem yapılmıyor.\n"
                "📝 Açmak için: `/ac`"
            )

            await self.send_log(
                f"🔴 **Bot KAPATILDI**\n\n"
                f"👤 Kapatan: [{sender.first_name}](tg://user?id={user_id})\n"
                f"⏱️ Zaman: {datetime.now().strftime('%H:%M:%S')}"
            )

        # ═══════════════════════════════════════════════════════
        # /temizle veya /clear - Tüm istekleri reddet
        # ═══════════════════════════════════════════════════════
        elif command in ['temizle', 'clear']:
            if self.clearing_in_progress:
                await event.reply("⚠️ Temizleme zaten devam ediyor...")
                return

            previous_state = self.state
            self.state = BotState.CLEARING
            self.clearing_in_progress = True

            status_msg = await event.reply(
                "🧹 **Temizleme Başlatıldı**\n\n"
                "🔄 Tüm bekleyen istekler reddediliyor...\n"
                "⏳ Lütfen bekleyin..."
            )

            logger.info(f"🧹 Temizleme başlatıldı - {sender.first_name}")

            await self.send_log(
                f"🧹 **TEMİZLEME BAŞLATILDI**\n\n"
                f"👤 Başlatan: [{sender.first_name}](tg://user?id={user_id})\n"
                f"⏱️ Zaman: {datetime.now().strftime('%H:%M:%S')}"
            )

            # Tüm gruplardaki istekleri temizle
            total_rejected = 0
            for group_id in PROTECTED_GROUPS:
                rejected = await self.clear_all_requests(group_id)
                total_rejected += rejected

            self.clearing_in_progress = False
            self.state = BotState.INACTIVE  # Temizlik bitince KAPALI duruma geç

            await status_msg.edit(
                f"✅ **Temizleme Tamamlandı**\n\n"
                f"🚫 Reddedilen: **{total_rejected}** istek\n"
                f"🔴 Bot şu an **KAPALI**\n"
                f"📝 Açmak için: `/ac`"
            )

            await self.send_log(
                f"✅ **TEMİZLEME TAMAMLANDI**\n\n"
                f"🚫 Reddedilen: **{total_rejected}** istek\n"
                f"🔴 Bot durumu: KAPALI\n"
                f"⏱️ Zaman: {datetime.now().strftime('%H:%M:%S')}"
            )

            logger.info(f"✅ Temizleme tamamlandı - {total_rejected} istek reddedildi")

        # ═══════════════════════════════════════════════════════
        # /durum veya /status - Bot durumunu göster
        # ═══════════════════════════════════════════════════════
        elif command in ['durum', 'status']:
            state_emoji = {
                BotState.ACTIVE: "🟢 AKTİF",
                BotState.INACTIVE: "🔴 KAPALI",
                BotState.CLEARING: "🧹 TEMİZLİYOR"
            }

            uptime = datetime.now() - self.stats.start_time
            hours, remainder = divmod(int(uptime.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)

            await event.reply(
                f"📊 **Bot Durumu**\n\n"
                f"**Durum:** {state_emoji[self.state]}\n"
                f"**Çalışma Süresi:** {hours}s {minutes}dk {seconds}sn\n\n"
                f"**📈 İstatistikler:**\n"
                f"├ Toplam İstek: {self.stats.total_requests}\n"
                f"├ Reddedilen: {self.stats.total_rejected}\n"
                f"├ Flood Saldırısı: {self.stats.total_flood_attacks}\n"
                f"└ Onay Oranı: {self.calculate_approval_rate():.1f}%\n\n"
                f"**⚙️ Ayarlar:**\n"
                f"├ Flood Penceresi: {FLOOD_WINDOW_SECONDS}s\n"
                f"└ Flood Eşiği: {FLOOD_THRESHOLD} istek"
            )

    async def clear_all_requests(self, chat_id: int) -> int:
        """Bir gruptaki tüm bekleyen istekleri reddet"""
        rejected_count = 0

        try:
            # Grup entity'sini al
            chat = await self.client.get_entity(chat_id)
            chat_title = chat.title if hasattr(chat, 'title') else f"Grup {chat_id}"

            logger.info(f"🧹 {chat_title} temizleniyor...")

            # Bekleyen istekleri al
            offset_date = None
            offset_user = None

            while True:
                try:
                    result = await self.client(GetChatInviteImportersRequest(
                        peer=chat_id,
                        requested=True,
                        limit=100,
                        offset_date=offset_date,
                        offset_user=offset_user or 0,
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

                            rejected_count += 1
                            self.stats.total_rejected += 1

                            # Grup istatistiklerini güncelle
                            if chat_id in self.stats.groups:
                                self.stats.groups[chat_id].rejected_requests += 1

                            logger.info(f"🚫 Reddedildi: User {importer.user_id}")

                            # Rate limit koruması
                            await asyncio.sleep(0.2)

                        except Exception as e:
                            logger.error(f"❌ Red hatası: {e}")

                    # Sonraki sayfa için offset ayarla
                    if len(result.importers) < 100:
                        break

                    last = result.importers[-1]
                    offset_date = last.date
                    offset_user = last.user_id

                except Exception as e:
                    logger.error(f"❌ İstek listesi alınamadı: {e}")
                    break

        except Exception as e:
            logger.error(f"❌ Grup erişim hatası {chat_id}: {e}")

        return rejected_count

    async def on_raw_update(self, event):
        """Raw update handler - Join Request'leri yakalar"""
        from telethon.tl.types import UpdateBotChatInviteRequester

        # Bot kapalıysa veya temizlik yapılıyorsa hiçbir şey yapma
        if self.state != BotState.ACTIVE:
            return

        if not isinstance(event, UpdateBotChatInviteRequester):
            return

        chat_id = event.peer.channel_id if hasattr(event.peer, 'channel_id') else event.peer.chat_id

        # Korunan gruplardan biri mi kontrol et
        if PROTECTED_GROUPS and chat_id not in PROTECTED_GROUPS and -100*chat_id not in PROTECTED_GROUPS:
            if PROTECTED_GROUPS:
                return

        user = event.user_id

        try:
            user_entity = await self.client.get_entity(user)
            username = user_entity.username or "Yok"
            first_name = user_entity.first_name or "Bilinmiyor"
        except:
            username = "Bilinmiyor"
            first_name = "Bilinmiyor"

        try:
            chat_entity = await self.client.get_entity(chat_id)
            chat_title = chat_entity.title
        except:
            chat_title = f"Grup {chat_id}"

        # İstek bilgisini oluştur
        request_info = RequestInfo(
            user_id=user,
            username=username,
            first_name=first_name,
            timestamp=datetime.now(),
            chat_id=chat_id,
            chat_title=chat_title
        )

        await self.process_join_request(request_info, event)

    async def process_join_request(self, request: RequestInfo, raw_event):
        """Join request'i işle"""
        # Bot aktif değilse çık
        if self.state != BotState.ACTIVE:
            return

        chat_id = request.chat_id

        # Grup istatistiklerini al veya oluştur
        if chat_id not in self.stats.groups:
            self.stats.groups[chat_id] = GroupStats()

        group_stats = self.stats.groups[chat_id]

        # İstatistikleri güncelle
        self.stats.total_requests += 1
        group_stats.total_requests += 1

        # Eski istekleri temizle (pencere dışındakiler)
        current_time = datetime.now()
        cutoff_time = current_time - timedelta(seconds=FLOOD_WINDOW_SECONDS)

        while group_stats.pending_requests and group_stats.pending_requests[0].timestamp < cutoff_time:
            group_stats.pending_requests.popleft()

        # Yeni isteği ekle
        group_stats.pending_requests.append(request)

        pending_count = len(group_stats.pending_requests)

        logger.info(
            f"📥 Yeni istek: @{request.username} ({request.first_name}) "
            f"-> {request.chat_title} | Pencerede: {pending_count}/{FLOOD_THRESHOLD}"
        )

        # Flood kontrolü
        if pending_count >= FLOOD_THRESHOLD:
            if not self.flood_in_progress.get(chat_id, False):
                self.flood_in_progress[chat_id] = True
                group_stats.flood_attacks += 1
                self.stats.total_flood_attacks += 1
                group_stats.last_flood_time = current_time

                logger.warning(f"🚨 FLOOD TESPİT! {request.chat_title} - {pending_count} istek")

                # Log kanalına bildir
                await self.send_log(
                    f"🚨 **FLOOD TESPİT EDİLDİ!**\n\n"
                    f"📍 Grup: **{request.chat_title}**\n"
                    f"⏱️ Süre: {FLOOD_WINDOW_SECONDS} saniye içinde\n"
                    f"📊 İstek Sayısı: **{pending_count}**\n"
                    f"🔄 Tüm istekler reddediliyor..."
                )

                # Penceredeki istekleri reddet
                rejected_count = await self.reject_pending_requests(chat_id)

                await self.send_log(
                    f"✅ **Flood Engellendi**\n\n"
                    f"📍 Grup: **{request.chat_title}**\n"
                    f"🚫 Reddedilen: **{rejected_count}** istek\n"
                    f"⏱️ Zaman: {current_time.strftime('%H:%M:%S')}\n\n"
                    f"🔴 Bot durumu: **KAPALI**\n"
                    f"📝 Tekrar açmak için: `/ac`"
                )

                # Flood bitti, botu kapat
                self.flood_in_progress[chat_id] = False
                self.state = BotState.INACTIVE

                logger.info("🔴 Flood engellendi, bot KAPALI duruma geçti")

    async def reject_pending_requests(self, chat_id: int) -> int:
        """Penceredeki bekleyen istekleri reddet"""
        group_stats = self.stats.groups[chat_id]
        rejected_count = 0

        while group_stats.pending_requests:
            request = group_stats.pending_requests.popleft()

            try:
                await self.client(HideChatJoinRequestRequest(
                    peer=chat_id,
                    user_id=request.user_id,
                    approved=False
                ))

                rejected_count += 1
                group_stats.rejected_requests += 1
                self.stats.total_rejected += 1

                logger.info(f"🚫 Reddedildi: @{request.username} ({request.first_name})")

                # Rate limit koruması
                await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(f"❌ Red hatası: {e}")

        return rejected_count

    async def send_log(self, message: str):
        """Log kanalına mesaj gönder"""
        if not LOG_CHANNEL_ID:
            return

        try:
            await self.client.send_message(
                LOG_CHANNEL_ID,
                message,
                parse_mode='markdown'
            )
        except Exception as e:
            logger.error(f"❌ Log gönderme hatası: {e}")

    async def periodic_stats(self):
        """Periyodik istatistik özeti gönder"""
        while True:
            await asyncio.sleep(STATS_INTERVAL)

            # Sadece bot aktifse rapor gönder
            if self.state == BotState.INACTIVE:
                continue

            uptime = datetime.now() - self.stats.start_time
            hours, remainder = divmod(int(uptime.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)

            state_emoji = {
                BotState.ACTIVE: "🟢 AKTİF",
                BotState.INACTIVE: "🔴 KAPALI",
                BotState.CLEARING: "🧹 TEMİZLİYOR"
            }

            # Grup bazlı istatistikler
            group_stats_text = ""
            for gid, gstats in self.stats.groups.items():
                if gstats.total_requests > 0:
                    try:
                        chat = await self.client.get_entity(gid)
                        chat_name = chat.title
                    except:
                        chat_name = f"Grup {gid}"

                    group_stats_text += (
                        f"\n📍 **{chat_name}**\n"
                        f"   • Toplam: {gstats.total_requests}\n"
                        f"   • Reddedilen: {gstats.rejected_requests}\n"
                        f"   • Flood: {gstats.flood_attacks}\n"
                    )

            if not group_stats_text:
                group_stats_text = "\n_Henüz istek gelmedi_"

            stats_message = (
                f"📊 **PERİYODİK RAPOR**\n"
                f"{'═' * 25}\n\n"
                f"**Durum:** {state_emoji[self.state]}\n"
                f"⏱️ Çalışma: {hours}s {minutes}dk\n"
                f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
                f"**📈 GENEL**\n"
                f"├ Toplam İstek: **{self.stats.total_requests}**\n"
                f"├ Reddedilen: **{self.stats.total_rejected}**\n"
                f"├ Flood: **{self.stats.total_flood_attacks}**\n"
                f"└ Onay: **{self.calculate_approval_rate():.1f}%**\n\n"
                f"**📍 GRUPLAR**{group_stats_text}"
            )

            await self.send_log(stats_message)
            logger.info("📊 Periyodik rapor gönderildi")

    def calculate_approval_rate(self) -> float:
        """Onay oranını hesapla"""
        if self.stats.total_requests == 0:
            return 100.0
        approved = self.stats.total_requests - self.stats.total_rejected
        return (approved / self.stats.total_requests) * 100

# ═══════════════════════════════════════════════════════════════
# ANA FONKSİYON
# ═══════════════════════════════════════════════════════════════

async def main():
    """Ana fonksiyon"""
    if not API_ID or not API_HASH:
        logger.error("❌ API_ID ve API_HASH ayarlanmamış!")
        return

    if not LOG_CHANNEL_ID:
        logger.warning("⚠️ LOG_CHANNEL_ID ayarlanmamış")

    if not PROTECTED_GROUPS:
        logger.warning("⚠️ PROTECTED_GROUPS boş, tüm gruplar korunacak")

    if not ADMIN_IDS:
        logger.warning("⚠️ ADMIN_IDS boş, sadece hesap sahibi komut verebilir")

    bot = AntiSpamBot()
    await bot.start()

if __name__ == "__main__":
    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║          🛡️  TELEGRAM ANTI-SPAM BOT  🛡️                  ║
    ╠═══════════════════════════════════════════════════════════╣
    ║  Komutlar:                                                ║
    ║    /ac      - Botu aktif et                               ║
    ║    /kapat   - Botu kapat                                  ║
    ║    /temizle - Tüm istekleri reddet                        ║
    ║    /durum   - Durumu göster                               ║
    ╚═══════════════════════════════════════════════════════════╝
    """)
    asyncio.run(main())
