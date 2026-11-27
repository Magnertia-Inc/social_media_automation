"""
video_extractor.py
Extracts video metadata and spoken transcripts from YouTube videos, YouTube Shorts, and Instagram Reels.
"""

import os
import sys
import glob
import re
import requests
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

# Ensure local .venv packages are accessible even when launched from system python
_venv_site = os.path.join(os.path.dirname(__file__), ".venv", "lib", "python*", "site-packages")
for _p in glob.glob(_venv_site):
    if _p not in sys.path:
        sys.path.insert(0, _p)

load_dotenv()

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN")

def extract_video_id(url_or_id: str) -> dict:
    """Detects platform and extracts video ID or playlist ID from URL or raw ID."""
    text = (url_or_id or "").strip()
    
    # 1. YouTube Playlist URL or raw Playlist ID
    # e.g., https://www.youtube.com/playlist?list=PL... or PL...
    playlist_match = re.search(r'[?&]list=([a-zA-Z0-9_-]+)', text)
    if "playlist" in text and playlist_match:
        pid = playlist_match.group(1)
        return {
            "platform": "youtube_playlist",
            "video_id": pid,
            "playlist_id": pid,
            "clean_url": f"https://www.youtube.com/playlist?list={pid}"
        }
    if text.startswith("PL") and len(text) >= 16 and re.match(r'^[a-zA-Z0-9_-]+$', text):
        return {
            "platform": "youtube_playlist",
            "video_id": text,
            "playlist_id": text,
            "clean_url": f"https://www.youtube.com/playlist?list={text}"
        }

    # 2. Instagram Reel / Post
    if "instagram.com" in text:
        ig_match = re.search(r'instagram\.com/(?:reel|p)/([^/?#&]+)', text)
        if ig_match:
            return {
                "platform": "instagram_reel",
                "video_id": ig_match.group(1),
                "clean_url": f"https://www.instagram.com/reel/{ig_match.group(1)}/"
            }

    # 3. YouTube Shorts
    if "shorts" in text:
        shorts_match = re.search(r'shorts/([a-zA-Z0-9_-]{11})', text)
        if shorts_match:
            vid = shorts_match.group(1)
            return {
                "platform": "youtube_shorts",
                "video_id": vid,
                "clean_url": f"https://www.youtube.com/shorts/{vid}"
            }

    # 4. Hybrid URL (watch?v=...&list=PL...) - if user explicitly has list, record playlist_id
    if playlist_match and "watch" in text:
        # Check if user wants whole playlist or video. If list is present, give playlist precedence if no v
        vid_match = re.search(r'[?&]v=([a-zA-Z0-9_-]{11})', text)
        if not vid_match and playlist_match:
            pid = playlist_match.group(1)
            return {
                "platform": "youtube_playlist",
                "video_id": pid,
                "playlist_id": pid,
                "clean_url": f"https://www.youtube.com/playlist?list={pid}"
            }

    # 5. YouTube standard video (watch?v=, youtu.be/, embed/)
    yt_regex = r'(?:youtube\.com\/(?:[^\/]+\/.+\/|(?:v|e(?:mbed)?)\/|.*[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})'
    yt_match = re.search(yt_regex, text)
    if yt_match:
        vid = yt_match.group(1)
        res = {
            "platform": "youtube",
            "video_id": vid,
            "clean_url": f"https://www.youtube.com/watch?v={vid}"
        }
        if playlist_match:
            res["playlist_id"] = playlist_match.group(1)
        return res

    # 6. Raw 11-char ID
    if len(text) == 11 and re.match(r'^[a-zA-Z0-9_-]{11}$', text):
        return {
            "platform": "youtube",
            "video_id": text,
            "clean_url": f"https://www.youtube.com/watch?v={text}"
        }

    # Fallback default
    vid = text if text else "ic4HSdThFc8"
    return {
        "platform": "youtube",
        "video_id": vid,
        "clean_url": f"https://www.youtube.com/watch?v={vid}"
    }

def fetch_youtube_metadata(video_id: str) -> dict:
    """Fetches video snippet and contentDetails from YouTube Data API v3."""
    url = f"https://www.googleapis.com/youtube/v3/videos?part=snippet,contentDetails&id={video_id}&key={YOUTUBE_API_KEY}"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        items = data.get("items", [])
        if items:
            snippet = items[0].get("snippet", {})
            content_details = items[0].get("contentDetails", {})
            thumbnails = snippet.get("thumbnails", {})
            thumb_url = (
                thumbnails.get("maxres", {}).get("url")
                or thumbnails.get("high", {}).get("url")
                or thumbnails.get("medium", {}).get("url")
                or thumbnails.get("default", {}).get("url")
                or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
            )
            return {
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "channel_title": snippet.get("channelTitle", ""),
                "tags": snippet.get("tags", []),
                "duration": content_details.get("duration", ""),
                "thumbnail_url": thumb_url,
                "published_at": snippet.get("publishedAt", "")
            }
    except Exception as e:
        print(f"[video_extractor] YouTube API warning: {e}")

    # Fallback to YouTube oEmbed (zero API key required, impervious to quota exhaustion)
    try:
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        o_res = requests.get(oembed_url, timeout=5)
        if o_res.status_code == 200:
            o_data = o_res.json()
            return {
                "title": o_data.get("title") or "YouTube Video",
                "description": "",
                "channel_title": o_data.get("author_name") or "YouTube Creator",
                "tags": [],
                "duration": "",
                "thumbnail_url": o_data.get("thumbnail_url") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                "published_at": ""
            }
    except Exception:
        pass
        
    return {
        "title": "YouTube Video",
        "description": "",
        "channel_title": "",
        "tags": [],
        "duration": "",
        "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "published_at": ""
    }

def fetch_youtube_transcript(video_id: str) -> str:
    """Attempts to retrieve spoken transcript/captions for the video."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        api = YouTubeTranscriptApi()
        
        # 1. Try modern .fetch()
        try:
            entries = api.fetch(video_id)
            if entries:
                snippets = [e.text if hasattr(e, 'text') else (e.get('text', '') if isinstance(e, dict) else str(e)) for e in entries[:80]]
                return " ".join(snippets)
        except Exception:
            pass

        # 2. Try .list() and iterate
        try:
            transcript_list = api.list(video_id)
            for t in transcript_list:
                try:
                    data = t.fetch()
                    snippets = [item.text if hasattr(item, 'text') else item.get('text', '') for item in data[:80]]
                    if snippets:
                        return " ".join(snippets)
                except Exception:
                    continue
        except Exception:
            pass

        # 3. Try legacy get_transcript
        if hasattr(YouTubeTranscriptApi, 'get_transcript'):
            try:
                data = YouTubeTranscriptApi.get_transcript(video_id)
                snippets = [item.get('text', '') for item in data[:80]]
                if snippets:
                    return " ".join(snippets)
            except Exception:
                pass
    except Exception as e:
        print(f"[video_extractor] Transcript not available: {e}")
    return ""

def fetch_instagram_metadata(media_id: str) -> dict:
    """Fetches Instagram Reel metadata if access token is available."""
    if not INSTAGRAM_ACCESS_TOKEN:
        return {
            "title": f"Instagram Reel ({media_id})",
            "description": "Instagram Reel content",
            "channel_title": "Instagram Creator",
            "tags": ["#Reels", "#Instagram"],
            "thumbnail_url": "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=1080&q=80",
            "duration": "0:30"
        }
        
    url = f"https://graph.facebook.com/v19.0/{media_id}?fields=caption,media_type,permalink,thumbnail_url,timestamp&access_token={INSTAGRAM_ACCESS_TOKEN}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            caption = data.get("caption", "")
            first_line = caption.split("\n")[0] if caption else "Instagram Reel"
            return {
                "title": first_line[:80],
                "description": caption,
                "channel_title": "Instagram Creator",
                "tags": [w for w in caption.split() if w.startswith("#")],
                "thumbnail_url": data.get("thumbnail_url", "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=1080&q=80"),
                "duration": "Reel"
            }
    except Exception as e:
        print(f"[video_extractor] Instagram API warning: {e}")

    return {
        "title": f"Instagram Reel ({media_id})",
        "description": "Instagram Reel content",
        "channel_title": "Instagram Creator",
        "tags": ["#Reels", "#Viral"],
        "thumbnail_url": "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=1080&q=80",
        "duration": "Reel"
    }

def fetch_youtube_playlist_metadata(playlist_id: str) -> dict:
    """
    Fetches playlist details and items.
    Tries YouTube Data API v3 first, and falls back to YouTube public Atom RSS feed
    (ensuring 100% reliability with zero API keys or zero quota issues).
    """
    title = "YouTube Playlist"
    description = ""
    channel_title = ""
    thumbnail_url = f"https://i.ytimg.com/vi/{playlist_id}/hqdefault.jpg"
    items = []
    item_count = 0

    # 1. Try YouTube Data API v3 if key available
    if YOUTUBE_API_KEY:
        try:
            # A. Playlist details
            pl_url = f"https://www.googleapis.com/youtube/v3/playlists?part=snippet,contentDetails&id={playlist_id}&key={YOUTUBE_API_KEY}"
            p_res = requests.get(pl_url, timeout=10)
            if p_res.status_code == 200:
                p_data = p_res.json().get("items", [])
                if p_data:
                    p_snip = p_data[0].get("snippet", {})
                    p_content = p_data[0].get("contentDetails", {})
                    title = p_snip.get("title", title)
                    description = p_snip.get("description", "")
                    channel_title = p_snip.get("channelTitle", "")
                    item_count = p_content.get("itemCount", 0)
                    thumbs = p_snip.get("thumbnails", {})
                    thumbnail_url = (
                        thumbs.get("maxres", {}).get("url")
                        or thumbs.get("high", {}).get("url")
                        or thumbs.get("medium", {}).get("url")
                        or thumbs.get("default", {}).get("url")
                        or thumbnail_url
                    )

            # B. Playlist items (up to 50)
            it_url = f"https://www.googleapis.com/youtube/v3/playlistItems?part=snippet,contentDetails&playlistId={playlist_id}&maxResults=50&key={YOUTUBE_API_KEY}"
            it_res = requests.get(it_url, timeout=10)
            if it_res.status_code == 200:
                it_data = it_res.json().get("items", [])
                for idx, it in enumerate(it_data, 1):
                    snip = it.get("snippet", {})
                    res_id = snip.get("resourceId", {}).get("videoId")
                    if not res_id:
                        continue
                    item_title = snip.get("title", f"Video {idx}")
                    if item_title in ("Private video", "Deleted video"):
                        continue
                    ithumbs = snip.get("thumbnails", {})
                    ithumb = (
                        ithumbs.get("high", {}).get("url")
                        or ithumbs.get("medium", {}).get("url")
                        or ithumbs.get("default", {}).get("url")
                        or f"https://i.ytimg.com/vi/{res_id}/hqdefault.jpg"
                    )
                    items.append({
                        "video_id": res_id,
                        "title": item_title,
                        "description": snip.get("description", ""),
                        "thumbnail_url": ithumb,
                        "position": snip.get("position", idx),
                        "clean_url": f"https://www.youtube.com/watch?v={res_id}"
                    })
                if items and not item_count:
                    item_count = len(items)
                if items and (not thumbnail_url or "hqdefault.jpg" in thumbnail_url):
                    thumbnail_url = items[0]["thumbnail_url"]
        except Exception as e:
            print(f"[video_extractor] YouTube API playlist warning: {e}")

    # 2. Fallback to public RSS feed if API didn't yield items
    if not items:
        try:
            rss_url = f"https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}"
            rss_res = requests.get(rss_url, timeout=6)
            if rss_res.status_code == 200:
                root = ET.fromstring(rss_res.content)
                feed_title = root.find("{http://www.w3.org/2005/Atom}title")
                feed_author = root.find("{http://www.w3.org/2005/Atom}author/{http://www.w3.org/2005/Atom}name")
                if feed_title is not None and feed_title.text:
                    title = feed_title.text
                if feed_author is not None and feed_author.text:
                    channel_title = feed_author.text

                ns = {
                    "atom": "http://www.w3.org/2005/Atom",
                    "yt": "http://www.youtube.com/xml/schemas/2015",
                    "media": "http://search.yahoo.com/mrss/"
                }
                entries = root.findall("atom:entry", ns)
                for idx, entry in enumerate(entries, 1):
                    vid_elem = entry.find("yt:videoId", ns)
                    title_elem = entry.find("atom:title", ns)
                    desc_elem = entry.find("media:group/media:description", ns)
                    thumb_elem = entry.find("media:group/media:thumbnail", ns)

                    vid_str = vid_elem.text if vid_elem is not None else ""
                    if not vid_str:
                        continue
                    v_title = title_elem.text if title_elem is not None else f"Episode {idx}"
                    v_desc = desc_elem.text if desc_elem is not None else ""
                    v_thumb = (
                        thumb_elem.attrib.get("url")
                        if thumb_elem is not None and "url" in thumb_elem.attrib
                        else f"https://i.ytimg.com/vi/{vid_str}/hqdefault.jpg"
                    )

                    items.append({
                        "video_id": vid_str,
                        "title": v_title,
                        "description": v_desc,
                        "thumbnail_url": v_thumb,
                        "position": idx,
                        "clean_url": f"https://www.youtube.com/watch?v={vid_str}"
                    })

                item_count = len(items)
                if items:
                    thumbnail_url = items[0]["thumbnail_url"]
        except Exception as e:
            print(f"[video_extractor] YouTube RSS fallback warning: {e}")

    return {
        "title": title or "YouTube Playlist Series",
        "description": description or f"A curated multi-part video series containing {len(items)} episodes.",
        "channel_title": channel_title or "YouTube Creator",
        "thumbnail_url": thumbnail_url,
        "item_count": item_count or len(items),
        "items": items
    }

def get_complete_video_context(url_or_id: str) -> dict:
    """End-to-end extraction helper returning comprehensive video or playlist context."""
    parsed = extract_video_id(url_or_id)
    platform = parsed["platform"]
    vid = parsed["video_id"]
    clean_url = parsed["clean_url"]
    
    if platform == "youtube_playlist":
        meta = fetch_youtube_playlist_metadata(vid)
        items = meta.get("items", [])
        
        # Formulate curriculum summary transcript for AI prompt
        curriculum_lines = [f"Playlist Title: {meta.get('title', 'Featured Series')}"]
        if meta.get("description"):
            curriculum_lines.append(f"Playlist Overview: {meta.get('description')}")
        curriculum_lines.append(f"Total Episodes: {meta.get('item_count', len(items))}")
        curriculum_lines.append("Episodes Breakdown:")
        for idx, it in enumerate(items, 1):
            curriculum_lines.append(f"{idx}. {it.get('title', 'Episode ' + str(idx))}")
            if it.get("description"):
                short_desc = it.get("description", "").replace("\n", " ").strip()[:140]
                if short_desc:
                    curriculum_lines.append(f"   Summary: {short_desc}")
        
        combined_transcript = "\n".join(curriculum_lines)

        return {
            "is_playlist": True,
            "platform": "youtube_playlist",
            "video_id": vid,
            "playlist_id": vid,
            "clean_url": clean_url,
            "title": meta.get("title", "YouTube Playlist Series"),
            "description": meta.get("description", ""),
            "channel_title": meta.get("channel_title", ""),
            "tags": ["#Series", "#Playlist", "#Masterclass", "#FullCourse"],
            "thumbnail_url": meta.get("thumbnail_url", ""),
            "duration": f"{meta.get('item_count', len(items))} Videos",
            "item_count": meta.get("item_count", len(items)),
            "items": items,
            "transcript": combined_transcript
        }

    if platform in ("youtube", "youtube_shorts"):
        meta = fetch_youtube_metadata(vid)
        transcript = fetch_youtube_transcript(vid)
    else:
        meta = fetch_instagram_metadata(vid)
        transcript = meta.get("description", "")
        
    return {
        "is_playlist": False,
        "platform": platform,
        "video_id": vid,
        "playlist_id": parsed.get("playlist_id"),
        "clean_url": clean_url,
        "title": meta.get("title", ""),
        "description": meta.get("description", ""),
        "channel_title": meta.get("channel_title", ""),
        "tags": meta.get("tags", []),
        "thumbnail_url": meta.get("thumbnail_url", ""),
        "duration": meta.get("duration", ""),
        "item_count": 1,
        "items": [],
        "transcript": transcript
    }

if __name__ == "__main__":
    test_id = "ic4HSdThFc8"
    print(f"Testing video extractor with ID: {test_id}")
    res = get_complete_video_context(test_id)
    print("Title:", res["title"])
    print("Platform:", res["platform"])
    print("Clean URL:", res["clean_url"])
    print("Transcript preview:", res["transcript"][:100] if res["transcript"] else "None")

