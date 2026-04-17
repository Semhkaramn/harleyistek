"""
Telegram Anti-Spam Bot
=======================
2 ayrı grup seti ve 2 ayrı log grubu ile çalışır.
Her log grubu kendi grubunu kontrol eder.

ÖZELLİKLER:
1. İSTEK KORUMASI (Gruplar):
   - İstek sayısı 20'yi geçerse otomatik temizler
   - /ac, /kapat, /temizle komutları

2. BOT SALDIRISI KORUMASI (Duyuru Kanalları):
   - 5 saniyede 10+ katılım = Bot saldırısı tespiti
   - Tüm saldırganlar otomatik engellenir (ban)
   - Saldırı bitene kadar koruma devam eder

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
from telethon.tl.functions.channels import EditBannedRequest, GetParticipantsRequest, GetFullChannelRequest
from telethon.tl.types import ChatBannedRights, ChannelParticipantsRecent
from collections import deque
import time
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
# DUYURU KANALI AYARLARI (Anti-Bot Saldırı)
# ═══════════════════════════════════════════════════════════════
# Duyuru kanalları (virgülle ayrılmış)
ANNOUNCEMENT_CHANNELS_1 = [int(x.strip()) for x in os.getenv("ANNOUNCEMENT_CHANNELS_1", "").split(",") if x.strip()]
ANNOUNCEMENT_CHANNELS_2 = [int(x.strip()) for x in os.getenv("ANNOUNCEMENT_CHANNELS_2", "").split(",") if x.strip()]

# Anti-bot ayarları
MASS_JOIN_THRESHOLD = int(os.getenv("MASS_JOIN_THRESHOLD", "10"))  # Kaç katılım
MASS_JOIN_WINDOW = int(os.getenv("MASS_JOIN_WINDOW", "5"))  # Kaç saniyede

# ═══════════════════════════════════════════════════════════════
# LOGGING AYARLARI
# ═══════════════════════════════════════════════════════════════

# Log dosyası ve konsol için handler'lar
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Konsol handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
console_handler.setFormatter(console_format)

# Dosya handler
file_handler = logging.FileHandler('bot.log', encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_format = logging.Formatter('%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
file_handler.setFormatter(file_format)

logger.addHandler(console_handler)
logger.addHandler(file_handler)

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
    state: BotState = BotState.ACTIVE
    threshold: int = 20
    total_rejected: int = 0

@dataclass
class GroupSetStats:
    """Bir grup seti için istatistikler"""
    total_rejected: int = 0
    groups: Dict[int, GroupStats] = field(default_factory=dict)
    state: BotState = BotState.ACTIVE
    clearing_in_progress: bool = False
    accumulated_rejected: int = 0
    last_cleanup_time: datetime = None

@dataclass
class AnnouncementChannelStats:
    """Duyuru kanalı için istatistikler"""
    recent_joins: deque = field(default_factory=lambda: deque(maxlen=500))
    attack_mode: bool = False
    attack_start_time: float = None
    total_banned: int = 0
    banned_in_current_attack: int = 0
    last_join_time: float = None
    channel_name: str = ""
    last_known_count: int = 0  # Son bilinen üye sayısı
    last_check_time: float = 0  # Son kontrol zamanı
    known_user_ids: set = field(default_factory=set)  # Bilinen üye ID'leri

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

        # Her grup seti için ayrı state (istek koruması)
        self.group_sets = {}

        # Duyuru kanalları için ayarlar (bot saldırısı koruması)
        self.announcement_sets = {}

        # Log channel -> set mapping
        self.log_to_set = {}

        # Kanal -> Set mapping (duyuru için)
        self.channel_to_set = {}

        # Grup/Kanal isimleri cache
        self.name_cache = {}

        # Set 1 yapılandırması
        if LOG_CHANNEL_ID_1:
            # Grup koruması varsa ekle
            if PROTECTED_GROUPS_1:
                group_stats = {gid: GroupStats() for gid in PROTECTED_GROUPS_1}
                self.group_sets[1] = {
                    'protected_groups': PROTECTED_GROUPS_1,
                    'log_channel': LOG_CHANNEL_ID_1,
                    'stats': GroupSetStats(groups=group_stats)
                }
                self.log_to_set[LOG_CHANNEL_ID_1] = 1

            # Duyuru koruması varsa ekle
            if ANNOUNCEMENT_CHANNELS_1:
                self.announcement_sets[1] = {
                    'channels': ANNOUNCEMENT_CHANNELS_1,
                    'log_channel': LOG_CHANNEL_ID_1,
                    'stats': {ch_id: AnnouncementChannelStats() for ch_id in ANNOUNCEMENT_CHANNELS_1}
                }
                for ch_id in ANNOUNCEMENT_CHANNELS_1:
                    self.channel_to_set[ch_id] = 1
                # Log channel mapping (duyuru için de gerekli)
                if LOG_CHANNEL_ID_1 not in self.log_to_set:
                    self.log_to_set[LOG_CHANNEL_ID_1] = 1

        # Set 2 yapılandırması
        if LOG_CHANNEL_ID_2:
            # Grup koruması varsa ekle
            if PROTECTED_GROUPS_2:
                group_stats = {gid: GroupStats() for gid in PROTECTED_GROUPS_2}
                self.group_sets[2] = {
                    'protected_groups': PROTECTED_GROUPS_2,
                    'log_channel': LOG_CHANNEL_ID_2,
                    'stats': GroupSetStats(groups=group_stats)
                }
                self.log_to_set[LOG_CHANNEL_ID_2] = 2

            # Duyuru koruması varsa ekle
            if ANNOUNCEMENT_CHANNELS_2:
                self.announcement_sets[2] = {
                    'channels': ANNOUNCEMENT_CHANNELS_2,
                    'log_channel': LOG_CHANNEL_ID_2,
                    'stats': {ch_id: AnnouncementChannelStats() for ch_id in ANNOUNCEMENT_CHANNELS_2}
                }
                for ch_id in ANNOUNCEMENT_CHANNELS_2:
                    self.channel_to_set[ch_id] = 2
                # Log channel mapping (duyuru için de gerekli)
                if LOG_CHANNEL_ID_2 not in self.log_to_set:
                    self.log_to_set[LOG_CHANNEL_ID_2] = 2

        self.me = None

    async def get_chat_name(self, chat_id: int) -> str:
        """Grup/Kanal ismini al (cache'li)"""
        if chat_id in self.name_cache:
            return self.name_cache[chat_id]

        try:
            chat = await self.client.get_entity(chat_id)
            name = chat.title if hasattr(chat, 'title') else f"Chat {chat_id}"
            self.name_cache[chat_id] = name
            return name
        except:
            return f"Chat {chat_id}"

    async def cache_all_names(self):
        """Tüm grup/kanal isimlerini cache'e al"""
        # Korunan gruplar
        for data in self.group_sets.values():
            for group_id in data['protected_groups']:
                await self.get_chat_name(group_id)
            await self.get_chat_name(data['log_channel'])

        # Duyuru kanalları
        for data in self.announcement_sets.values():
            for ch_id in data['channels']:
                name = await self.get_chat_name(ch_id)
                # Stats'a da kaydet
                if ch_id in data['stats']:
                    data['stats'][ch_id].channel_name = name
            await self.get_chat_name(data['log_channel'])

        logger.info(f"Toplam {len(self.name_cache)} grup/kanal ismi yüklendi")

    async def start(self):
        """Botu başlat"""
        logger.info("Bot başlatılıyor...")

        await self.client.start()

        self.me = await self.client.get_me()
        logger.info(f"Giriş yapıldı: @{self.me.username} ({self.me.first_name})")

        # Tüm grup/kanal isimlerini önceden al
        await self.cache_all_names()

        # Tüm log kanallarından komut al (hem grup hem duyuru setleri için)
        all_log_channels = set()
        for data in self.group_sets.values():
            all_log_channels.add(data['log_channel'])
        for data in self.announcement_sets.values():
            all_log_channels.add(data['log_channel'])
        all_log_channels = list(all_log_channels)

        if all_log_channels:
            self.client.add_event_handler(
                self.on_command,
                events.NewMessage(
                    chats=all_log_channels,
                    pattern=r'^/(ac|kapat|temizle|durum|esik)(\s+.*)?$'
                )
            )

        # Duyuru kanalları için periyodik kontrol
        all_announcement_channels = []
        for data in self.announcement_sets.values():
            all_announcement_channels.extend(data['channels'])

        if all_announcement_channels:
            logger.info(f"Duyuru kanalları izleniyor (periyodik kontrol): {len(all_announcement_channels)} kanal")
            logger.info(f"İzlenen kanal ID'leri: {all_announcement_channels}")
            # Periyodik kontrol task'ı aşağıda başlatılacak

        # Bilgilendirme logları
        for set_id, data in self.group_sets.items():
            logger.info(f"Grup Seti {set_id}: {len(data['protected_groups'])} grup, Log: {data['log_channel']}")

        for set_id, data in self.announcement_sets.items():
            logger.info(f"Duyuru Seti {set_id}: {len(data['channels'])} kanal, Log: {data['log_channel']}")

        logger.info("Bot AÇIK başladı.")
        logger.info(f"Anti-bot ayarları: {MASS_JOIN_THRESHOLD} katılım / {MASS_JOIN_WINDOW} saniye")
        logger.info(f"İzlenen duyuru kanalları: {list(self.channel_to_set.keys())}")

        # Başlangıçta hemen kontrol et
        for set_id in self.group_sets:
            await self.check_and_auto_clean(set_id)

        # Periyodik kontrol başlat
        asyncio.create_task(self.periodic_check())

        # Duyuru kanalları için periyodik kontrol başlat
        if self.announcement_sets:
            asyncio.create_task(self.periodic_announcement_check())

        await self.client.run_until_disconnected()

    async def periodic_check(self):
        """Her 10 saniyede bir istek sayısını kontrol et"""
        while True:
            await asyncio.sleep(10)

            for set_id, data in self.group_sets.items():
                stats = data['stats']

                if stats.state != BotState.ACTIVE or stats.clearing_in_progress:
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

                if total_pending > AUTO_CLEAN_THRESHOLD:
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
            return

        previous_state = stats.state
        stats.state = BotState.CLEARING
        stats.clearing_in_progress = True

        # Bu setteki tüm gruplardaki istekleri temizle
        total_rejected = 0
        for group_id in data['protected_groups']:
            rejected = await self.clear_all_requests(group_id)
            total_rejected += rejected

        stats.clearing_in_progress = False
        stats.state = previous_state if not manual else BotState.INACTIVE

        # Biriktir
        stats.accumulated_rejected += total_rejected
        stats.last_cleanup_time = datetime.now()

        logger.info(f"Grup Seti {set_id} - Temizlendi: {total_rejected} (Toplam: {stats.accumulated_rejected})")

    async def clear_all_requests(self, chat_id: int) -> int:
        """Bir gruptaki tüm bekleyen istekleri reddet"""
        total_rejected = 0

        try:
            chat = await self.client.get_entity(chat_id)
            chat_title = chat.title if hasattr(chat, 'title') else f"Grup {chat_id}"

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
                offset_user=0,
                q=""
            ))
            return result.count if hasattr(result, 'count') else len(result.importers)
        except:
            return 0

    async def check_and_auto_clean(self, set_id: int):
        """20'yi geçerse otomatik temizle"""
        data = self.group_sets[set_id]
        stats = data['stats']

        if stats.clearing_in_progress:
            return

        total_pending = 0
        for group_id in data['protected_groups']:
            count = await self.get_pending_count(group_id)
            total_pending += count

        if total_pending > AUTO_CLEAN_THRESHOLD:
            logger.info(f"Grup Seti {set_id} - Otomatik temizleme: {total_pending} istek")
            await self.do_cleanup(set_id, manual=False)

    async def send_log(self, set_id: int, message: str, group_id: int = None):
        """Belirli grup setinin log grubuna mesaj gönder"""
        data = self.group_sets.get(set_id)
        if not data:
            return

        log_channel = data['log_channel']
        if not log_channel:
            return

        try:
            # Grup ismi varsa ekle
            if group_id:
                group_name = await self.get_chat_name(group_id)
                prefix = f"[{group_name}]"
            else:
                prefix = ""

            await self.client.send_message(log_channel, f"{prefix} {message}".strip())
        except Exception as e:
            logger.error(f"Log gönderme hatası: {e}")

    # ═══════════════════════════════════════════════════════════════
    # DUYURU KANALI - BOT SALDIRISI KORUMASI (PERİYODİK KONTROL)
    # ═══════════════════════════════════════════════════════════════

    async def periodic_announcement_check(self):
        """Her 3 saniyede bir duyuru kanallarını kontrol et"""
        logger.info("🔄 Duyuru kanalları periyodik kontrol başlatıldı...")

        # İlk çalıştırmada mevcut üyeleri kaydet (bunları engellemeyeceğiz)
        await self.initialize_channel_members()

        while True:
            try:
                await asyncio.sleep(3)  # 3 saniyede bir kontrol

                for set_id, data in self.announcement_sets.items():
                    for channel_id in data['channels']:
                        await self.check_channel_for_new_members(set_id, channel_id)

            except Exception as e:
                logger.error(f"Periyodik duyuru kontrolü hatası: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def initialize_channel_members(self):
        """Başlangıçta mevcut üye sayısını kaydet"""
        for set_id, data in self.announcement_sets.items():
            for channel_id in data['channels']:
                stats = data['stats'].get(channel_id)
                if stats is None:
                    continue

                try:
                    # Kanal bilgisini al
                    channel = await self.client.get_entity(channel_id)
                    channel_name = channel.title if hasattr(channel, 'title') else f"Kanal {channel_id}"
                    stats.channel_name = channel_name

                    # Mevcut üye sayısını al (full chat info)
                    from telethon.tl.functions.channels import GetFullChannelRequest
                    full = await self.client(GetFullChannelRequest(channel_id))
                    stats.last_known_count = full.full_chat.participants_count
                    stats.last_check_time = time.time()

                    logger.info(f"📊 {channel_name}: {stats.last_known_count} mevcut üye")

                    # Log kanalına bildir
                    await self.send_announcement_log(
                        set_id,
                        channel_id,
                        f"🟢 **Bot aktif!**\nMevcut üye sayısı: {stats.last_known_count}\nKontrol aralığı: 3 saniye"
                    )

                except Exception as e:
                    logger.error(f"Kanal başlatma hatası {channel_id}: {e}")

    async def check_channel_for_new_members(self, set_id: int, channel_id: int):
        """Bir kanalda yeni üye kontrolü yap"""
        data = self.announcement_sets[set_id]
        stats = data['stats'].get(channel_id)
        if stats is None:
            return

        current_time = time.time()

        try:
            # Güncel üye sayısını al
            full = await self.client(GetFullChannelRequest(channel_id))
            current_count = full.full_chat.participants_count

            # Yeni üye var mı?
            new_members_count = current_count - stats.last_known_count

            if new_members_count > 0:
                logger.info(f"🔔 {stats.channel_name}: +{new_members_count} yeni üye! (Toplam: {current_count})")

                # Yeni üyeleri al (son eklenenler - recent participants)
                try:
                    from telethon.tl.types import ChannelParticipantsRecent
                    participants = await self.client.get_participants(
                        channel_id,
                        filter=ChannelParticipantsRecent(),
                        limit=min(new_members_count + 20, 100)
                    )

                    # Yeni üyeleri tespit et
                    new_user_ids = []
                    for p in participants:
                        if p.id not in stats.known_user_ids:
                            new_user_ids.append(p.id)
                            stats.known_user_ids.add(p.id)

                    if new_user_ids:
                        logger.info(f"🆕 {len(new_user_ids)} yeni kullanıcı tespit edildi")

                        # Katılımları kaydet
                        for user_id in new_user_ids:
                            stats.recent_joins.append({
                                'user_id': user_id,
                                'time': current_time
                            })

                        # Eski kayıtları temizle
                        cutoff_time = current_time - MASS_JOIN_WINDOW
                        while stats.recent_joins and stats.recent_joins[0]['time'] < cutoff_time:
                            stats.recent_joins.popleft()

                        recent_count = len(stats.recent_joins)
                        logger.info(f"📈 Son {MASS_JOIN_WINDOW}sn: {recent_count} katılım (Eşik: {MASS_JOIN_THRESHOLD})")

                        # Saldırı kontrolü
                        if recent_count >= MASS_JOIN_THRESHOLD:
                            await self.handle_mass_join_attack(set_id, channel_id, stats, new_user_ids)
                        elif stats.attack_mode:
                            # Saldırı modundayken gelen her yeni üyeyi engelle
                            for user_id in new_user_ids:
                                await self.ban_user(channel_id, user_id, set_id, stats)

                            # Saldırı bitmiş mi kontrol et
                            if recent_count < MASS_JOIN_THRESHOLD // 2:
                                await self.end_attack_mode(set_id, channel_id, stats)

                except Exception as e:
                    logger.error(f"Üye listesi alma hatası: {e}")

            # Güncelle
            stats.last_known_count = current_count
            stats.last_check_time = current_time

        except Exception as e:
            error_str = str(e)
            if "FLOOD_WAIT" in error_str:
                import re
                wait_match = re.search(r'(\d+)', error_str)
                wait_time = int(wait_match.group(1)) if wait_match else 10
                logger.warning(f"FloodWait: {wait_time} saniye bekleniyor...")
                await asyncio.sleep(wait_time)
            else:
                logger.error(f"Kanal kontrol hatası {channel_id}: {e}")

    async def handle_mass_join_attack(self, set_id: int, channel_id: int, stats: AnnouncementChannelStats, new_user_ids: list):
        """Toplu katılım saldırısını işle"""
        if not stats.attack_mode:
            # Saldırı başladı!
            stats.attack_mode = True
            stats.attack_start_time = time.time()
            stats.banned_in_current_attack = 0

            recent_count = len(stats.recent_joins)

            await self.send_announcement_log(
                set_id,
                channel_id,
                f"🚨 **BOT SALDIRISI TESPİT EDİLDİ!**\n"
                f"⚡ {MASS_JOIN_WINDOW} saniyede {recent_count} katılım!\n"
                f"🔒 Otomatik engelleme başlatıldı..."
            )
            logger.warning(f"🚨 BOT SALDIRISI: {stats.channel_name} - {recent_count} katılım/{MASS_JOIN_WINDOW}sn")

        # Tüm yeni üyeleri engelle
        for user_id in new_user_ids:
            await self.ban_user(channel_id, user_id, set_id, stats)

    async def end_attack_mode(self, set_id: int, channel_id: int, stats: AnnouncementChannelStats):
        """Saldırı modunu sonlandır"""
        stats.attack_mode = False

        await self.send_announcement_log(
            set_id,
            channel_id,
            f"✅ **Saldırı durduruldu!**\n"
            f"🚫 Bu saldırıda engellenen: {stats.banned_in_current_attack} hesap\n"
            f"📊 Genel toplam: {stats.total_banned} hesap"
        )
        logger.info(f"✅ Saldırı durduruldu: {stats.channel_name} - {stats.banned_in_current_attack} engellendi")

    async def ban_user(self, chat_id: int, user_id: int, set_id: int, stats: AnnouncementChannelStats):
        """Kullanıcıyı kanaldan engelle"""
        try:
            logger.debug(f"Ban işlemi başlatılıyor - Kanal: {chat_id}, Kullanıcı: {user_id}")

            # Kalıcı ban
            await self.client(EditBannedRequest(
                channel=chat_id,
                participant=user_id,
                banned_rights=ChatBannedRights(
                    until_date=None,  # Kalıcı
                    view_messages=True,
                    send_messages=True,
                    send_media=True,
                    send_stickers=True,
                    send_gifs=True,
                    send_games=True,
                    send_inline=True,
                    embed_links=True
                )
            ))

            stats.total_banned += 1
            stats.banned_in_current_attack += 1

            logger.info(f"✅ Kullanıcı engellendi: {user_id} (Toplam: {stats.banned_in_current_attack})")

            if stats.banned_in_current_attack % 50 == 0:
                logger.info(f"Saldırı devam ediyor: {stats.banned_in_current_attack} hesap engellendi...")

            await asyncio.sleep(0.05)  # Rate limit koruması

        except Exception as e:
            error_str = str(e)
            if "FLOOD_WAIT" in error_str:
                import re
                wait_match = re.search(r'(\d+)', error_str)
                wait_time = int(wait_match.group(1)) if wait_match else 2
                logger.warning(f"FloodWait ban: {wait_time}sn bekleniyor...")
                await asyncio.sleep(wait_time)
            elif "USER_NOT_PARTICIPANT" not in error_str and "CHAT_ADMIN_REQUIRED" not in error_str:
                logger.error(f"Ban hatası: {e}")
            elif "CHAT_ADMIN_REQUIRED" in error_str:
                logger.error(f"❌ Admin yetkisi yok! Kanal: {chat_id}")

    async def send_announcement_log(self, set_id: int, channel_id: int, message: str):
        """Duyuru seti log kanalına mesaj gönder"""
        data = self.announcement_sets.get(set_id)
        if not data:
            logger.error(f"Announcement set bulunamadı: {set_id}")
            return

        log_channel = data['log_channel']
        if not log_channel:
            logger.error(f"Log channel tanımlı değil: set {set_id}")
            return

        try:
            # Kanal ismini al
            channel_name = await self.get_chat_name(channel_id)
            full_message = f"[{channel_name}] {message}"
            await self.client.send_message(log_channel, full_message, parse_mode='md')
            logger.debug(f"Log gönderildi: {log_channel} -> {full_message[:50]}...")
        except Exception as e:
            logger.error(f"Duyuru log gönderme hatası: {e}", exc_info=True)

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

    # Duyuru kanalı bilgileri
    if ANNOUNCEMENT_CHANNELS_1:
        logger.info(f"Duyuru Seti 1 yapılandırıldı: {len(ANNOUNCEMENT_CHANNELS_1)} kanal")
    if ANNOUNCEMENT_CHANNELS_2:
        logger.info(f"Duyuru Seti 2 yapılandırıldı: {len(ANNOUNCEMENT_CHANNELS_2)} kanal")

    # En az bir set tanımlı olmalı
    has_announcement = bool(ANNOUNCEMENT_CHANNELS_1 or ANNOUNCEMENT_CHANNELS_2)

    if not has_group_set and not has_announcement:
        logger.error("En az bir grup seti veya duyuru kanalı yapılandırılmalı!")
        return

    bot = AntiSpamBot()
    await bot.start()

if __name__ == "__main__":
    print("""
    ╔═══════════════════════════════════════════════════════════════╗
    ║       TELEGRAM ANTI-SPAM BOT (GRUP + DUYURU KORUMASI)         ║
    ╠═══════════════════════════════════════════════════════════════╣
    ║  İSTEK KORUMASI (Gruplar):                                    ║
    ║    /ac      - Botu aktif et (o grup seti için)                ║
    ║    /kapat   - Botu kapat (o grup seti için)                   ║
    ║    /temizle - Tüm istekleri reddet (o grup seti için)         ║
    ║    20+ istek = Otomatik temizleme                             ║
    ║                                                               ║
    ║  BOT SALDIRISI KORUMASI (Duyuru Kanalları):                   ║
    ║    5 saniyede 10+ katılım = Saldırı tespiti                   ║
    ║    Otomatik engelleme (ban) başlatılır                        ║
    ║    Saldırı bitene kadar tüm katılımlar engellenir             ║
    ║                                                               ║
    ║  Yapılandırma (.env):                                         ║
    ║    PROTECTED_GROUPS_1/2    - Korunan gruplar                  ║
    ║    ANNOUNCEMENT_CHANNELS_1/2 - Duyuru kanalları               ║
    ║    LOG_CHANNEL_ID_1/2      - Log kanalları                    ║
    ║    MASS_JOIN_THRESHOLD     - Kaç katılımda saldırı (def: 10)  ║
    ║    MASS_JOIN_WINDOW        - Kaç saniyede (def: 5)            ║
    ╚═══════════════════════════════════════════════════════════════╝
    """)
    asyncio.run(main())
