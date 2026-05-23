"""Debug all three detection methods on Oracle Cloud."""
import aiohttp
import asyncio
import re
import json
import xml.etree.ElementTree as ET

CHANNEL_URL = "https://www.youtube.com/@Parkmis0"
CHANNEL_ID = "UCNw2V9i-seLBu7THBAWlqIg"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept-Language": "ko-KR,ko;q=0.9"}
COOKIES = {"CONSENT": "PENDING+987", "SOCS": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"}
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


async def test():
    async with aiohttp.ClientSession() as s:

        # 1. /live scrape
        print("=== 1. /live page scrape ===")
        try:
            async with s.get(f"{CHANNEL_URL}/live", headers=HEADERS, cookies=COOKIES) as r:
                text = await r.text()
            is_live = '"isLive":true' in text or '"isLiveNow":true' in text
            print(f"  isLive: {is_live}")
            vd_start = text.find('"videoDetails":{')
            if vd_start != -1:
                chunk = text[vd_start:vd_start + 5000]
                m = re.search(r'"channelId":"(UC[^"]*)"', chunk)
                print(f"  videoDetails channelId: {m.group(1) if m else 'NOT FOUND (in 5000 chars)'}")
                vid = re.search(r'"videoId":"([^"]{11})"', chunk)
                print(f"  videoDetails videoId: {vid.group(1) if vid else 'NOT FOUND'}")
            else:
                print("  videoDetails section: NOT FOUND AT ALL")
        except Exception as e:
            print(f"  ERROR: {e}")

        # 2. RSS feed
        print("\n=== 2. RSS feed ===")
        try:
            async with s.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}", headers=HEADERS) as r:
                xml_text = await r.text()
                print(f"  Status: {r.status}")
                print(f"  Content length: {len(xml_text)}")
                print(f"  First 200 chars: {xml_text[:200]}")
            root = ET.fromstring(xml_text)
            entries = root.findall("atom:entry", ATOM_NS)
            print(f"  Entries: {len(entries)}")
            for entry in entries[:3]:
                vid = entry.find("yt:videoId", ATOM_NS)
                title = entry.find("atom:title", ATOM_NS)
                print(f"  - {vid.text if vid is not None else '?'} | {title.text if title is not None else '?'}")
        except Exception as e:
            print(f"  ERROR: {e}")

        # 3. Innertube API
        print("\n=== 3. Innertube browse API ===")
        try:
            payload = {
                "context": {"client": {"clientName": "WEB", "clientVersion": "2.20250523.00.00", "hl": "ko", "gl": "KR"}},
                "browseId": CHANNEL_ID,
            }
            async with s.post("https://www.youtube.com/youtubei/v1/browse", json=payload, headers={"Content-Type": "application/json"}) as r:
                print(f"  Status: {r.status}")
                data = await r.json()

            text = json.dumps(data, ensure_ascii=False)
            print(f"  Response size: {len(text)} chars")

            if "error" in data:
                print(f"  API Error: {data['error']}")

            has_badge = '"BADGE_STYLE_TYPE_LIVE_NOW"' in text
            print(f"  BADGE_STYLE_TYPE_LIVE_NOW: {'FOUND' if has_badge else 'NOT FOUND'}")

            badges = set(re.findall(r'"style":"(BADGE_STYLE[^"]*)"', text))
            print(f"  All badge styles: {badges or 'none'}")

            vids = re.findall(r'"videoId":"([^"]{11})"', text)
            print(f"  Video IDs ({len(vids)}): {list(dict.fromkeys(vids))[:10]}")

            for marker in ['channelFeaturedContent', '"isLive"', 'LIVE']:
                c = text.count(marker)
                if c:
                    print(f"  '{marker}' count: {c}")
        except Exception as e:
            print(f"  ERROR: {e}")

        # 4. Innertube with API key
        print("\n=== 4. Innertube with API key ===")
        try:
            payload = {
                "context": {"client": {"clientName": "WEB", "clientVersion": "2.20250523.00.00", "hl": "ko", "gl": "KR"}},
                "browseId": CHANNEL_ID,
            }
            async with s.post(
                "https://www.youtube.com/youtubei/v1/browse?key=AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as r:
                print(f"  Status: {r.status}")
                data = await r.json()

            text = json.dumps(data, ensure_ascii=False)
            has_badge = '"BADGE_STYLE_TYPE_LIVE_NOW"' in text
            print(f"  BADGE_STYLE_TYPE_LIVE_NOW: {'FOUND' if has_badge else 'NOT FOUND'}")
            vids = re.findall(r'"videoId":"([^"]{11})"', text)
            print(f"  Video IDs ({len(vids)}): {list(dict.fromkeys(vids))[:10]}")
        except Exception as e:
            print(f"  ERROR: {e}")

asyncio.run(test())
