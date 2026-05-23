"""Debug all three detection methods on Oracle Cloud."""
import aiohttp
import asyncio
import re
import xml.etree.ElementTree as ET

CHANNEL_URL = "https://www.youtube.com/@Parkmis0"
CHANNEL_ID = "UCNw2V9i-seLBu7THBAWlqIg"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", "Accept-Language": "ko-KR,ko;q=0.9"}
COOKIES = {"CONSENT": "PENDING+987", "SOCS": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"}
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


async def test():
    async with aiohttp.ClientSession() as s:
        # 1. /live scrape
        print("=== 1. /live page scrape ===")
        async with s.get(f"{CHANNEL_URL}/live", headers=HEADERS, cookies=COOKIES) as r:
            text = await r.text()
        is_live = '"isLive":true' in text or '"isLiveNow":true' in text
        print(f"  isLive detected: {is_live}")
        vd_start = text.find('"videoDetails":{')
        if vd_start != -1:
            chunk = text[vd_start:vd_start+2000]
            m = re.search(r'"channelId":"(UC[^"]*)"', chunk)
            print(f"  videoDetails channelId: {m.group(1) if m else 'NOT FOUND'}")
            print(f"  Expected channelId:     {CHANNEL_ID}")
            print(f"  Match: {m.group(1) == CHANNEL_ID if m else False}")
        else:
            print("  videoDetails: NOT FOUND")

        # 2. RSS feed
        print("\n=== 2. RSS feed ===")
        async with s.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}", headers=HEADERS) as r:
            xml_text = await r.text()
        root = ET.fromstring(xml_text)
        entries = root.findall("atom:entry", ATOM_NS)
        print(f"  Entries found: {len(entries)}")
        for entry in entries[:5]:
            vid = entry.find("yt:videoId", ATOM_NS)
            title = entry.find("atom:title", ATOM_NS)
            vid_text = vid.text if vid is not None else "?"
            title_text = title.text if title is not None else "?"
            # Check if live
            try:
                async with s.get(f"https://www.youtube.com/watch?v={vid_text}", headers=HEADERS, cookies=COOKIES) as r2:
                    vtext = await r2.text()
                live = '"isLive":true' in vtext or '"isLiveNow":true' in vtext
            except:
                live = False
            print(f"  {vid_text} | live={live} | {title_text}")

        # 3. Innertube API
        print("\n=== 3. Innertube browse API ===")
        payload = {
            "context": {"client": {"clientName": "WEB", "clientVersion": "2.20250523.00.00", "hl": "ko", "gl": "KR"}},
            "browseId": CHANNEL_ID,
        }
        async with s.post("https://www.youtube.com/youtubei/v1/browse", json=payload, headers={"Content-Type": "application/json"}) as r:
            print(f"  Status: {r.status}")
            data = await r.json()

        import json
        text = json.dumps(data, ensure_ascii=False)
        has_live_badge = '"BADGE_STYLE_TYPE_LIVE_NOW"' in text
        print(f"  BADGE_STYLE_TYPE_LIVE_NOW found: {has_live_badge}")

        if has_live_badge:
            badge_pos = text.find('"BADGE_STYLE_TYPE_LIVE_NOW"')
            chunk = text[max(0, badge_pos-2000):badge_pos+200]
            vids = re.findall(r'"videoId":"([^"]{11})"', chunk)
            print(f"  Video IDs near badge: {vids}")
        else:
            # Check what IS in the response
            print(f"  Response size: {len(text)} chars")
            badges = re.findall(r'"style":"([^"]*BADGE[^"]*)"', text)
            print(f"  Badge styles found: {set(badges)}")
            vid_ids = re.findall(r'"videoId":"([^"]{11})"', text)
            print(f"  Video IDs in response: {vid_ids[:10]}")
            # Check for any live indicators
            for marker in ['"isLive"', '"isLiveNow"', 'LIVE_NOW', 'channelFeaturedContent', 'featuredContent']:
                count = text.count(marker)
                if count > 0:
                    print(f"  '{marker}' found: {count} times")

asyncio.run(test())
