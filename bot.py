import os
import re
import hmac
import hashlib
import logging
import asyncio
import random
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
YOUTUBE_CHANNEL_URL = os.getenv("YOUTUBE_CHANNEL_URL", "https://www.youtube.com/@Parkmis0").rstrip("/")
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "0"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBSUB_SECRET = os.getenv("WEBSUB_SECRET", "bcalert-secret")
BACKUP_INTERVAL = int(os.getenv("BACKUP_INTERVAL", "300"))
ALERT_MESSAGE = os.getenv("ALERT_MESSAGE", "🐷 **{channel_name}님이 방송을 시작했습니다!** 🐷")
ALERT_ROLE_ID = os.getenv("ALERT_ROLE_ID", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bcalert")
KST = timezone(timedelta(hours=9))

SCRAPE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}
CONSENT_COOKIES = {"CONSENT": "PENDING+987", "SOCS": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"}
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
PUBSUBHUBBUB_HUB = "https://pubsubhubbub.appspot.com/subscribe"


def _first_match(pattern: str, text: str) -> str:
    m = re.search(pattern, text)
    return m.group(1) if m else ""


def _extract_from_video_page(text: str, video_id: str = "") -> dict | None:
    """Extract stream info from a YouTube video/live page HTML."""
    if '"isLive":true' not in text and '"isLiveNow":true' not in text:
        return None

    if not video_id:
        video_id = (
            _first_match(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([^"&]+)"', text)
            or _first_match(r'"videoDetails":\{"videoId":"([^"]{11})"', text)
            or _first_match(r'"videoId":"([^"]{11})"', text)
        )
    title = (
        _first_match(r'"videoDetails":\{"videoId":"[^"]*","title":"([^"]*)"', text)
        or _first_match(r'<meta name="title" content="([^"]*)"', text)
        or _first_match(r'<meta property="og:title" content="([^"]*)"', text)
    )
    channel_name = (
        _first_match(r'"ownerChannelName":"([^"]*)"', text)
        or _first_match(r'"author":"([^"]*)"', text)
    )
    thumbnail = _first_match(r'<meta property="og:image" content="([^"]*)"', text)

    return {
        "title": title or "방송 시작!",
        "channel_name": channel_name or "스트리머",
        "video_id": video_id or "",
        "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else "",
        "thumbnail": thumbnail or "",
    }


def _get_vd_channel_id(text: str) -> str:
    """Extract channelId from the videoDetails section."""
    vd_start = text.find('"videoDetails":{')
    if vd_start == -1:
        return ""
    chunk = text[vd_start:vd_start + 2000]
    return _first_match(r'"channelId":"(UC[^"]*)"', chunk)


async def check_oembed_owner(session: aiohttp.ClientSession, video_id: str, channel_url: str) -> dict | None:
    """Check if a video belongs to the expected channel via oEmbed API.
    Returns stream info dict if match, None otherwise."""
    try:
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        async with session.get(oembed_url) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

        handle = channel_url.rstrip("/").split("/")[-1]  # e.g. "@Parkmis0"
        author_url = data.get("author_url", "")

        if handle in author_url:
            return {
                "title": data.get("title", "방송 시작!"),
                "channel_name": data.get("author_name", "스트리머"),
                "video_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "thumbnail": f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
            }
    except Exception as e:
        log.error(f"oEmbed check error for {video_id}: {e}")
    return None


async def resolve_channel_id(session: aiohttp.ClientSession, channel_url: str) -> str:
    try:
        async with session.get(channel_url, headers=SCRAPE_HEADERS, cookies=CONSENT_COOKIES) as resp:
            text = await resp.text()
            match = re.search(r'"externalId":"(UC[^"]+)"', text)
            if match:
                return match.group(1)
            match = re.search(r'"channelId":"(UC[^"]+)"', text)
            if match:
                return match.group(1)
    except Exception as e:
        log.error(f"Failed to resolve channel ID: {e}")
    return ""


async def check_live_scrape(session: aiohttp.ClientSession, channel_url: str, expected_channel_id: str = "") -> dict | None:
    url = f"{channel_url}/live"
    try:
        async with session.get(url, headers=SCRAPE_HEADERS, cookies=CONSENT_COOKIES) as resp:
            if resp.status != 200:
                return None
            text = await resp.text()
    except Exception as e:
        log.error(f"Scrape error: {e}")
        return None

    if '"isLive":true' not in text and '"isLiveNow":true' not in text:
        return None

    if not expected_channel_id:
        return _extract_from_video_page(text)

    # Fast path: verify via videoDetails channelId (works on Korean IP)
    page_channel_id = _get_vd_channel_id(text)
    if page_channel_id == expected_channel_id:
        return _extract_from_video_page(text)

    # Overseas/Oracle path: oEmbed verification for each videoId on the page
    log.info(f"Channel verify: got '{page_channel_id}', expected '{expected_channel_id}' — trying oEmbed")
    all_vids = list(dict.fromkeys(re.findall(r'"videoId":"([^"]{11})"', text)))
    for vid in all_vids[:10]:
        info = await check_oembed_owner(session, vid, channel_url)
        if info:
            if await is_video_live(session, vid):
                log.info(f"[oEmbed] Found live: {vid} — {info['title']}")
                return info
            else:
                log.info(f"[oEmbed] {vid} matches channel but not live")

    # Fallback: RSS feed
    log.info("oEmbed found no live match — trying RSS")
    info = await check_live_rss(session, expected_channel_id)
    if info:
        return info

    # Last resort: innertube API
    log.info("RSS no match — trying innertube")
    return await check_live_innertube(session, expected_channel_id)


async def check_live_rss(session: aiohttp.ClientSession, channel_id: str) -> dict | None:
    """Fallback: check the channel's RSS feed for live videos."""
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        async with session.get(feed_url, headers=SCRAPE_HEADERS) as resp:
            if resp.status != 200:
                return None
            xml_text = await resp.text()
    except Exception as e:
        log.error(f"RSS fetch error: {e}")
        return None

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    for entry in root.findall("atom:entry", ATOM_NS)[:5]:
        vid_elem = entry.find("yt:videoId", ATOM_NS)
        if vid_elem is None or not vid_elem.text:
            continue

        video_id = vid_elem.text
        if not await is_video_live(session, video_id):
            continue

        title_elem = entry.find("atom:title", ATOM_NS)
        author_elem = entry.find("atom:author/atom:name", ATOM_NS)
        log.info(f"[RSS] Found live: {video_id}")

        return {
            "title": title_elem.text if title_elem is not None else "방송 시작!",
            "channel_name": author_elem.text if author_elem is not None else "스트리머",
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
        }

    return None


async def check_live_innertube(session: aiohttp.ClientSession, channel_id: str) -> dict | None:
    """Fallback: use YouTube's internal browse API (region-independent)."""
    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20250523.00.00",
                "hl": "ko",
                "gl": "KR",
            }
        },
        "browseId": channel_id,
    }
    try:
        async with session.post(
            "https://www.youtube.com/youtubei/v1/browse",
            json=payload,
            headers={"Content-Type": "application/json"},
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception as e:
        log.error(f"Innertube error: {e}")
        return None

    result = _find_live_in_json(data)
    if not result:
        return None

    video_id, title = result
    channel_name = _extract_channel_name(data)
    log.info(f"[Innertube] Found live: {video_id}")

    return {
        "title": title or "방송 시작!",
        "channel_name": channel_name or "스트리머",
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
    }


def _find_live_in_json(obj):
    """Recursively find a videoRenderer with BADGE_STYLE_TYPE_LIVE_NOW."""
    if isinstance(obj, dict):
        if "videoRenderer" in obj:
            vr = obj["videoRenderer"]
            for badge in vr.get("badges", []):
                style = badge.get("metadataBadgeRenderer", {}).get("style", "")
                if style == "BADGE_STYLE_TYPE_LIVE_NOW":
                    vid = vr.get("videoId", "")
                    runs = vr.get("title", {}).get("runs", [])
                    title = runs[0].get("text", "") if runs else ""
                    if vid:
                        return (vid, title)
        for val in obj.values():
            r = _find_live_in_json(val)
            if r:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _find_live_in_json(item)
            if r:
                return r
    return None


def _extract_channel_name(data: dict) -> str:
    """Extract channel name from innertube browse response."""
    header = data.get("header", {})
    for key in ["c4TabbedHeaderRenderer", "pageHeaderRenderer"]:
        if key not in header:
            continue
        h = header[key]
        if "title" in h and isinstance(h["title"], str):
            return h["title"]
        try:
            content = h["content"]["pageHeaderViewModel"]["title"]["dynamicTextViewModel"]["text"]
            return content.get("content", "")
        except (KeyError, TypeError):
            pass
    return ""


async def is_video_live(session: aiohttp.ClientSession, video_id: str) -> bool:
    """Quick check: is this video currently a live stream?"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        async with session.get(url, headers=SCRAPE_HEADERS, cookies=CONSENT_COOKIES) as resp:
            text = await resp.text()
    except Exception:
        return False
    return '"isLive":true' in text or '"isLiveNow":true' in text


async def verify_video_is_live(session: aiohttp.ClientSession, video_id: str) -> dict | None:
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        async with session.get(url, headers=SCRAPE_HEADERS, cookies=CONSENT_COOKIES) as resp:
            text = await resp.text()
    except Exception as e:
        log.error(f"Verify error: {e}")
        return None

    return _extract_from_video_page(text, video_id)


class AlertBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.is_live = False
        self.current_video_id: str | None = None
        self.http_session: aiohttp.ClientSession | None = None
        self.resolved_channel_id = YOUTUBE_CHANNEL_ID
        self._webhook_runner: web.AppRunner | None = None
        self._websub_active = False
        self._seen_msg_ids: set[int] = set()
        self._last_alert_time: float = 0
        self.scheduled_time: datetime | None = None  # 예정 방송 시간 (KST)

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.id in self._seen_msg_ids:
            return
        self._seen_msg_ids.add(message.id)
        if len(self._seen_msg_ids) > 500:
            self._seen_msg_ids = set(sorted(self._seen_msg_ids)[-250:])
        await self.process_commands(message)

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()

        if not self.resolved_channel_id:
            log.info("Resolving YouTube channel ID...")
            self.resolved_channel_id = await resolve_channel_id(self.http_session, YOUTUBE_CHANNEL_URL)
            if self.resolved_channel_id:
                log.info(f"Channel ID: {self.resolved_channel_id}")
            else:
                log.error("Could not resolve channel ID — WebSub will not work")

        if WEBHOOK_PORT and self.resolved_channel_id:
            await self._start_webhook_server()
            if WEBHOOK_URL:
                await self._subscribe_websub()
                self.renew_subscription.start()

        self.backup_check.change_interval(seconds=BACKUP_INTERVAL)
        self.backup_check.start()

    async def on_ready(self):
        mode = "WebSub + backup" if self._websub_active else "backup scraping only"
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")
        log.info(f"Mode: {mode} | Channel: {YOUTUBE_CHANNEL_URL} | Backup interval: {BACKUP_INTERVAL}s")

    async def close(self):
        self.backup_check.cancel()
        if self.renew_subscription.is_running():
            self.renew_subscription.cancel()
        if self._webhook_runner:
            await self._webhook_runner.cleanup()
        if self.http_session:
            await self.http_session.close()
        await super().close()

    # ── WebSub Webhook Server ──

    async def _start_webhook_server(self):
        app = web.Application()
        app.router.add_get("/webhook", self._websub_verify)
        app.router.add_post("/webhook", self._websub_notify)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
        await site.start()
        self._webhook_runner = runner
        log.info(f"Webhook server listening on 0.0.0.0:{WEBHOOK_PORT}")

    async def _websub_verify(self, request: web.Request) -> web.Response:
        mode = request.query.get("hub.mode", "")
        challenge = request.query.get("hub.challenge", "")
        topic = request.query.get("hub.topic", "")
        log.info(f"WebSub verification: mode={mode} topic={topic}")
        if mode == "subscribe":
            self._websub_active = True
            log.info("WebSub subscription confirmed!")
        return web.Response(text=challenge, content_type="text/plain")

    async def _websub_notify(self, request: web.Request) -> web.Response:
        body = await request.read()

        if WEBSUB_SECRET:
            sig_header = request.headers.get("X-Hub-Signature", "")
            if sig_header.startswith("sha1="):
                expected = hmac.new(WEBSUB_SECRET.encode(), body, hashlib.sha1).hexdigest()
                if not hmac.compare_digest(sig_header[5:], expected):
                    log.warning("WebSub: invalid HMAC signature — ignoring")
                    return web.Response(status=200)

        log.info(f"WebSub notification received ({len(body)} bytes)")
        asyncio.create_task(self._process_feed(body))
        return web.Response(status=200)

    async def _process_feed(self, body: bytes):
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            log.error("Failed to parse WebSub notification XML")
            return

        for entry in root.findall("atom:entry", ATOM_NS):
            vid_elem = entry.find("yt:videoId", ATOM_NS)
            if vid_elem is None or not vid_elem.text:
                continue

            video_id = vid_elem.text
            title_elem = entry.find("atom:title", ATOM_NS)
            author_elem = entry.find("atom:author/atom:name", ATOM_NS)
            log.info(f"WebSub: new video {video_id} — '{title_elem.text if title_elem is not None else '?'}'")

            if self.is_live and self.current_video_id == video_id:
                log.info("WebSub: already tracking this stream, skipping")
                continue

            if not await is_video_live(self.http_session, video_id):
                log.info(f"WebSub: video {video_id} is not a live stream")
                continue

            info = {
                "title": title_elem.text if title_elem is not None else "방송 시작!",
                "channel_name": author_elem.text if author_elem is not None else "스트리머",
                "video_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "thumbnail": f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
            }
            self.is_live = True
            self.current_video_id = video_id
            await self._send_alert(info)
            log.info(f"[WebSub] LIVE alert sent: {info.get('title')}")

    # ── WebSub Subscription Management ──

    async def _subscribe_websub(self):
        topic = f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={self.resolved_channel_id}"
        data = {
            "hub.callback": f"{WEBHOOK_URL}/webhook",
            "hub.topic": topic,
            "hub.verify": "async",
            "hub.mode": "subscribe",
            "hub.lease_seconds": "864000",
        }
        if WEBSUB_SECRET:
            data["hub.secret"] = WEBSUB_SECRET

        try:
            async with self.http_session.post(PUBSUBHUBBUB_HUB, data=data) as resp:
                status = resp.status
                log.info(f"WebSub subscribe request: {status} ({'accepted' if status == 202 else 'unexpected'})")
        except Exception as e:
            log.error(f"WebSub subscribe failed: {e}")

    @tasks.loop(hours=96)
    async def renew_subscription(self):
        await self._subscribe_websub()
        log.info("WebSub subscription renewed")

    @renew_subscription.before_loop
    async def before_renew(self):
        await self.wait_until_ready()

    # ── Backup Periodic Check (safety net only) ──

    @tasks.loop(seconds=300)
    async def backup_check(self):
        try:
            info = await check_live_scrape(self.http_session, YOUTUBE_CHANNEL_URL, self.resolved_channel_id)
            if info:
                vid = info.get("video_id")
                if not self.is_live or self.current_video_id != vid:
                    self.is_live = True
                    self.current_video_id = vid
                    await self._send_alert(info)
                    log.info(f"[Backup] LIVE alert sent: {info.get('title')}")
            else:
                if self.is_live:
                    log.info("Stream ended")
                self.is_live = False
                self.current_video_id = None
        except Exception as e:
            log.error(f"Backup check error: {e}")

    @backup_check.before_loop
    async def before_backup(self):
        await self.wait_until_ready()

    # ── Alert Sending ──

    async def _send_alert(self, info: dict, target_channel_id: int = 0):
        now = asyncio.get_event_loop().time()
        if not target_channel_id and now - self._last_alert_time < 60:
            log.info("Alert cooldown active, skipping duplicate")
            return
        if not target_channel_id:
            self._last_alert_time = now

        ch_id = target_channel_id or DISCORD_CHANNEL_ID
        channel = self.get_channel(ch_id)
        if not channel:
            log.error(f"Discord channel {ch_id} not found")
            return

        name = info.get("channel_name", "스트리머")

        embed = discord.Embed(
            title=info.get("title", "방송 시작!"),
            url=info.get("url", YOUTUBE_CHANNEL_URL),
            color=0xFF0000,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=f"{name} - YouTube Live", url=YOUTUBE_CHANNEL_URL)
        if info.get("thumbnail"):
            embed.set_image(url=info["thumbnail"])
        embed.set_footer(text="YouTube Live Alert")

        if random.random() < 0.1:
            msg = "🐷🐷🐷🐷🐷"
        else:
            msg = ALERT_MESSAGE.format(channel_name=name)

        if ALERT_ROLE_ID and not target_channel_id:
            msg = f"<@&{ALERT_ROLE_ID}> {msg}"

        await channel.send(msg, embed=embed)

        # 지각 메시지
        if self.scheduled_time and not target_channel_id:
            now_kst = datetime.now(KST)
            diff = now_kst - self.scheduled_time
            late_minutes = int(diff.total_seconds() // 60)
            if late_minutes > 0:
                hours = late_minutes // 60
                mins = late_minutes % 60
                if hours > 0:
                    late_str = f"{hours}시간 {mins}분" if mins else f"{hours}시간"
                else:
                    late_str = f"{mins}분"
                await channel.send(f"⏰ {late_str} 지각했어요.")
            self.scheduled_time = None  # 알림 후 자동 초기화


bot = AlertBot()


@bot.command(name="test")
async def cmd_test(ctx):
    """현재 방송 상태를 확인하고 이 채널에 테스트 알림을 보냄"""
    await ctx.send("🔍 방송 상태 확인 중...")
    info = await check_live_scrape(bot.http_session, YOUTUBE_CHANNEL_URL, bot.resolved_channel_id)
    if info:
        await bot._send_alert(info, target_channel_id=ctx.channel.id)
    else:
        await ctx.send("❌ 현재 방송 중이 아닙니다.")


@bot.command(name="status")
async def cmd_status(ctx):
    """모니터링 상태 확인"""
    lines = [f"📡 모니터링: {YOUTUBE_CHANNEL_URL}"]
    if bot._websub_active:
        lines.append(f"🔔 WebSub: **활성** (port {WEBHOOK_PORT})")
    elif WEBHOOK_PORT:
        lines.append(f"🔔 WebSub: 대기 중 (port {WEBHOOK_PORT}, 아직 구독 확인 안됨)")
    else:
        lines.append("🔔 WebSub: 비활성 (WEBHOOK_PORT 미설정)")
    lines.append(f"⏱️ 백업 체크 주기: {BACKUP_INTERVAL}초")
    if bot.is_live:
        lines.append(f"🐷 현재 방송 중! https://www.youtube.com/watch?v={bot.current_video_id}")
    else:
        lines.append("⬤ 현재 방송 중이 아님")
    await ctx.send("\n".join(lines))


@bot.command(name="check")
async def cmd_check(ctx):
    """수동으로 방송 상태 확인"""
    await ctx.send("🔍 확인 중...")
    info = await check_live_scrape(bot.http_session, YOUTUBE_CHANNEL_URL, bot.resolved_channel_id)
    if info:
        await ctx.send(f"🐷 방송 중: **{info.get('title')}** 🐷\n{info.get('url')}")
    else:
        await ctx.send("❌ 현재 방송 중이 아닙니다.")


@bot.command(name="예정")
async def cmd_schedule(ctx, time_str: str = ""):
    """방송 예정 시간 설정 (예: !예정 14:00) / 해제: !예정 해제"""
    if not time_str:
        if bot.scheduled_time:
            t = bot.scheduled_time.strftime("%H:%M")
            await ctx.send(f"⏰ 현재 예정 시간: **{t}**")
        else:
            await ctx.send("⏰ 설정된 예정 시간이 없습니다.\n사용법: `!예정 14:00`")
        return

    if time_str in ("해제", "취소", "삭제", "off"):
        bot.scheduled_time = None
        await ctx.send("⏰ 예정 시간이 해제되었습니다.")
        return

    try:
        parts = time_str.replace("시", ":").replace(".", ":").split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (ValueError, IndexError):
        await ctx.send("❌ 올바른 형식: `!예정 14:00` 또는 `!예정 14:30`")
        return

    now_kst = datetime.now(KST)
    scheduled = now_kst.replace(hour=hour, minute=minute, second=0, microsecond=0)
    bot.scheduled_time = scheduled
    await ctx.send(f"⏰ 예정 시간 설정: **{hour:02d}:{minute:02d}**")


@bot.command(name="61012")
async def cmd_easter(ctx):
    pigs = "🐷" * random.randint(20, 50)
    await ctx.send(pigs)
    await asyncio.sleep(0.5)
    await ctx.send(pigs)
    await asyncio.sleep(0.5)
    await ctx.send(pigs)


@bot.command(name="resub")
@commands.is_owner()
async def cmd_resub(ctx):
    """수동으로 WebSub 구독 갱신"""
    if not WEBHOOK_URL or not bot.resolved_channel_id:
        await ctx.send("❌ WEBHOOK_URL 또는 채널 ID가 설정되지 않았습니다.")
        return
    await bot._subscribe_websub()
    await ctx.send("✅ WebSub 구독 요청 완료")


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN이 설정되지 않았습니다. .env 파일을 확인하세요.")
        raise SystemExit(1)
    if not DISCORD_CHANNEL_ID:
        log.error("DISCORD_CHANNEL_ID가 설정되지 않았습니다. .env 파일을 확인하세요.")
        raise SystemExit(1)
    bot.run(DISCORD_TOKEN)
