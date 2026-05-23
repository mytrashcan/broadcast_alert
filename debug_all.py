"""Debug: test new detection approaches on Oracle Cloud."""
import aiohttp
import asyncio
import re
import json

CHANNEL_URL = "https://www.youtube.com/@Parkmis0"
CHANNEL_ID = "UCNw2V9i-seLBu7THBAWlqIg"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept-Language": "ko-KR,ko;q=0.9"}
COOKIES = {"CONSENT": "PENDING+987", "SOCS": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"}


async def test():
    async with aiohttp.ClientSession() as s:

        # 1. Get video ID from /live page
        print("=== 1. Get videoId from /live page ===")
        async with s.get(f"{CHANNEL_URL}/live", headers=HEADERS, cookies=COOKIES) as r:
            text = await r.text()
        is_live = '"isLive":true' in text or '"isLiveNow":true' in text
        print(f"  isLive: {is_live}")

        canonical = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([^"&]+)"', text)
        vd_vid = re.search(r'"videoDetails":\{"videoId":"([^"]{11})"', text)
        first_vid = re.search(r'"videoId":"([^"]{11})"', text)

        video_id = (canonical.group(1) if canonical else
                    vd_vid.group(1) if vd_vid else
                    first_vid.group(1) if first_vid else "")
        print(f"  canonical: {canonical.group(1) if canonical else 'NONE'}")
        print(f"  videoDetails vid: {vd_vid.group(1) if vd_vid else 'NONE'}")
        print(f"  first vid: {first_vid.group(1) if first_vid else 'NONE'}")

        # Check if our channel ID appears anywhere in the page
        our_channel_count = text.count(CHANNEL_ID)
        print(f"  Our channel ID in page: {our_channel_count} times")

        # 2. oEmbed — check which channel owns the video
        if video_id:
            print(f"\n=== 2. oEmbed for {video_id} ===")
            try:
                oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
                async with s.get(oembed_url) as r:
                    print(f"  Status: {r.status}")
                    if r.status == 200:
                        data = await r.json()
                        print(f"  author_name: {data.get('author_name')}")
                        print(f"  author_url: {data.get('author_url')}")
                        print(f"  title: {data.get('title')}")
                        is_our_channel = CHANNEL_URL.split('/')[-1] in data.get('author_url', '')
                        print(f"  Is our channel: {is_our_channel}")
            except Exception as e:
                print(f"  ERROR: {e}")

        # 3. Try /channel/ID/live format
        print(f"\n=== 3. /channel/{CHANNEL_ID}/live ===")
        try:
            async with s.get(f"https://www.youtube.com/channel/{CHANNEL_ID}/live", headers=HEADERS, cookies=COOKIES) as r:
                text2 = await r.text()
            is_live2 = '"isLive":true' in text2 or '"isLiveNow":true' in text2
            print(f"  isLive: {is_live2}")
            canonical2 = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([^"&]+)"', text2)
            vd2 = re.search(r'"videoDetails":\{"videoId":"([^"]{11})"', text2)
            print(f"  canonical: {canonical2.group(1) if canonical2 else 'NONE'}")
            print(f"  videoDetails vid: {vd2.group(1) if vd2 else 'NONE'}")

            if vd2:
                vd_start = text2.find('"videoDetails":{')
                chunk = text2[vd_start:vd_start+5000]
                ch_id = re.search(r'"channelId":"(UC[^"]*)"', chunk)
                print(f"  videoDetails channelId: {ch_id.group(1) if ch_id else 'NOT FOUND'}")
        except Exception as e:
            print(f"  ERROR: {e}")

        # 4. oEmbed for all unique video IDs (first 5)
        all_vids = list(dict.fromkeys(re.findall(r'"videoId":"([^"]{11})"', text)))[:5]
        print(f"\n=== 4. oEmbed check for all videoIds ({len(all_vids)} found) ===")
        for vid in all_vids:
            try:
                async with s.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json") as r:
                    if r.status == 200:
                        d = await r.json()
                        match = "@Parkmis0" in d.get("author_url", "") or "Parkmis0" in d.get("author_url", "")
                        print(f"  {vid} | {d.get('author_name','?'):10s} | match={match} | {d.get('title','?')[:40]}")
                    else:
                        print(f"  {vid} | status {r.status}")
            except Exception as e:
                print(f"  {vid} | ERROR: {e}")

        # 5. Embed live_stream — channel-ID-based, region-independent
        print(f"\n=== 5. embed/live_stream?channel={CHANNEL_ID} ===")
        try:
            embed_url = f"https://www.youtube.com/embed/live_stream?channel={CHANNEL_ID}"
            async with s.get(embed_url, headers=HEADERS, cookies=COOKIES) as r:
                etext = await r.text()
            embed_vid = re.search(r'"videoId":\s*"([^"]{11})"', etext)
            embed_live = '"isLive":true' in etext or '"playabilityStatus"' in etext
            print(f"  Status: {r.status}")
            print(f"  videoId: {embed_vid.group(1) if embed_vid else 'NONE'}")
            print(f"  hasPlayability: {'playabilityStatus' in etext}")
            print(f"  isLive in embed: {embed_live}")
            if embed_vid:
                print(f"  → https://www.youtube.com/watch?v={embed_vid.group(1)}")
            # Check for error/unplayable
            err = re.search(r'"reason":\s*"([^"]*)"', etext)
            if err:
                print(f"  reason: {err.group(1)}")
        except Exception as e:
            print(f"  ERROR: {e}")

        # 6. RSS feed (verify 404 issue)
        print(f"\n=== 6. RSS feed ===")
        try:
            rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
            async with s.get(rss_url, headers=HEADERS) as r:
                print(f"  Status: {r.status}")
                if r.status == 200:
                    rss_text = await r.text()
                    titles = re.findall(r'<title>([^<]+)</title>', rss_text)[:3]
                    print(f"  First titles: {titles}")
                else:
                    print(f"  Body (first 200): {(await r.text())[:200]}")
        except Exception as e:
            print(f"  ERROR: {e}")

        # 7. Wider channelId search in /live page
        print(f"\n=== 7. Wider channelId search in /live text ===")
        all_channel_ids = re.findall(r'"channelId":\s*"(UC[^"]*)"', text)
        unique_ch = list(dict.fromkeys(all_channel_ids))
        print(f"  Total channelId occurrences: {len(all_channel_ids)}")
        print(f"  Unique channelIds: {len(unique_ch)}")
        for ch in unique_ch[:5]:
            is_ours = ch == CHANNEL_ID
            count = all_channel_ids.count(ch)
            print(f"  {ch} {'✓ OURS' if is_ours else '      '} (x{count})")

asyncio.run(test())
