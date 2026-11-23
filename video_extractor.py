"""
video_extractor.py
Extracts video metadata and spoken transcripts from YouTube videos, YouTube Shorts, and Instagram Reels.
"""

import os
import sys
import glob
import re
import requests
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
    """Detects platform and extracts video ID from URL or raw ID."""
    text = (url_or_id or "").strip()
    
    # 1. Instagram Reel / Post
    if "instagram.com" in text:
        ig_match = re.search(r'instagram\.com/(?:reel|p)/([^/?#&]+)', text)
        if ig_match:
            return {
                "platform": "instagram_reel",
                "video_id": ig_match.group(1),
                "clean_url": f"https://www.instagram.com/reel/{ig_match.group(1)}/"
            }

    # 2. YouTube Shorts
    if "shorts" in text:
        shorts_match = re.search(r'shorts/([a-zA-Z0-9_-]{11})', text)
        if shorts_match:
            vid = shorts_match.group(1)
            return {
                "platform": "youtube_shorts",
                "video_id": vid,
                "clean_url": f"https://www.youtube.com/shorts/{vid}"
            }

    # 3. YouTube standard video (watch?v=, youtu.be/, embed/)
    yt_regex = r'(?:youtube\.com\/(?:[^\/]+\/.+\/|(?:v|e(?:mbed)?)\/|.*[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})'
    yt_match = re.search(yt_regex, text)
    if yt_match:
        vid = yt_match.group(1)
        return {
            "platform": "youtube",
            "video_id": vid,
            "clean_url": f"https://www.youtube.com/watch?v={vid}"
        }

    # 4. Raw 11-char ID
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

def get_complete_video_context(url_or_id: str) -> dict:
    """End-to-end extraction helper returning comprehensive video context."""
    parsed = extract_video_id(url_or_id)
    platform = parsed["platform"]
    vid = parsed["video_id"]
    clean_url = parsed["clean_url"]
    
    if platform in ("youtube", "youtube_shorts"):
        meta = fetch_youtube_metadata(vid)
        transcript = fetch_youtube_transcript(vid)
    else:
        meta = fetch_instagram_metadata(vid)
        transcript = meta.get("description", "")
        
    return {
        "platform": platform,
        "video_id": vid,
        "clean_url": clean_url,
        "title": meta.get("title", ""),
        "description": meta.get("description", ""),
        "channel_title": meta.get("channel_title", ""),
        "tags": meta.get("tags", []),
        "thumbnail_url": meta.get("thumbnail_url", ""),
        "duration": meta.get("duration", ""),
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

