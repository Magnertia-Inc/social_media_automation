"""
publisher.py
Handles multi-platform live publishing (LinkedIn, Facebook, Instagram)
and logs to live_social_tracker.json and Supabase archive.
"""

import os
import json
import time
import threading
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from notifications import create_notification

load_dotenv()

_PUBLISH_LOCK = threading.Lock()
_IN_FLIGHT_PUBLISHES = set()
_RECENT_PUBLISH_CACHE = {} # fingerprint -> (timestamp, result_dict)

DEFAULT_FACEBOOK_PAGE_ID = "1485094014687108"
DEFAULT_INSTAGRAM_ACCOUNT_ID = "17841454258543358"
DEFAULT_THREADS_USER_ID = "29062241880123556"
DEFAULT_THREADS_USERNAME = "founders_gateway"
DEFAULT_SUPABASE_URL = "https://sqgxnhdsajinjhuiyntm.supabase.co"
DEFAULT_FACEBOOK_PAGE_TOKEN = "EAAPCqL9XMPUBTERSq0Ipm91lkyZA8wZBk0J8IhQVr9foab7ULOV2YZBNFT0eeJkDVvpuytxWRQAxcRoHDeMJez5VySKcZCsqZBkpvxPI4bB2WStlWhiNdnQz9cxsTvA9Dwnm2kl9T5jZA2LITlP15VFM9SBp4RB7XLqH4kTZCZAWt8B3c2FavbE609RF5LLwZBAfGdI1NsI7n"
DEFAULT_THREADS_ACCESS_TOKEN = "THAAXuRAK6195BYmFNMUdpd2tKR3JWdHZANVmIzX05wX0FMcFdKcDdIQzZAtcENDV0UzZA1NQVGhXUlJkV011MXgzUmNJeVJSWklOR1FHRDd3bElmZA194U2d0cHlsSUhielB5d2VlYk9mazY1SUVOYnZA3MmFyczY2N3k4c1I5ZA0d3OVp2QQZDZD"

def is_valid_token_value(val: str) -> bool:
    if not val:
        return False
    s = str(val).strip()
    if not s:
        return False
    lower = s.lower()
    if lower.startswith("your_") or lower.startswith("https://your-") or lower == "placeholder" or "your_" in lower:
        return False
    return True

def get_clean_env(key: str, default: str = None) -> str:
    val = os.getenv(key)
    if is_valid_token_value(val):
        return str(val).strip()
    return default

def get_base_url() -> str:
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN") or os.getenv("RAILWAY_STATIC_URL") or os.getenv("APP_URL")
    if domain:
        if not domain.startswith("http"):
            domain = f"https://{domain}"
        return domain.rstrip("/")
    return "http://localhost:8080"

LINKEDIN_TOKEN = get_clean_env("LINKEDIN_ACCESS_TOKEN") or get_clean_env("LINKEDIN_TOKEN")
LINKEDIN_AUTHOR = get_clean_env("LINKEDIN_AUTHOR", "urn:li:person:s6ZhpxFSf8")

FACEBOOK_PAGE_ID = get_clean_env("FACEBOOK_PAGE_ID", DEFAULT_FACEBOOK_PAGE_ID)
FACEBOOK_PAGE_TOKEN = get_clean_env("FACEBOOK_PAGE_ACCESS_TOKEN") or get_clean_env("FACEBOOK_TOKEN")

INSTAGRAM_ACCOUNT_ID = get_clean_env("INSTAGRAM_BUSINESS_ACCOUNT_ID") or get_clean_env("INSTAGRAM_ID", DEFAULT_INSTAGRAM_ACCOUNT_ID)
INSTAGRAM_ACCESS_TOKEN = get_clean_env("INSTAGRAM_ACCESS_TOKEN")
INSTAGRAM_SECONDARY_ACCESS_TOKEN = get_clean_env("INSTAGRAM_SECONDARY_ACCESS_TOKEN")

GOOGLE_CLIENT_ID = get_clean_env("GOOGLE_CLIENT_ID") or get_clean_env("YOUTUBE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = get_clean_env("GOOGLE_CLIENT_SECRET") or get_clean_env("YOUTUBE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = get_clean_env("GOOGLE_REFRESH_TOKEN") or get_clean_env("YOUTUBE_REFRESH_TOKEN")

SUPABASE_URL = get_clean_env("SUPABASE_URL", DEFAULT_SUPABASE_URL)
SUPABASE_KEY = get_clean_env("SUPABASE_SERVICE_ROLE_KEY") or get_clean_env("SUPABASE_KEY")

TRACKER_FILE = os.path.join(os.path.dirname(__file__), "live_social_tracker.json")
LOCAL_CAMPAIGNS_FILE = os.path.join(os.path.dirname(__file__), "local_campaigns_db.json")

def load_local_campaigns() -> list:
    if os.path.exists(LOCAL_CAMPAIGNS_FILE):
        try:
            with open(LOCAL_CAMPAIGNS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []
    return []

def save_local_campaign(campaign: dict) -> None:
    try:
        campaigns = load_local_campaigns()
        cid = str(campaign.get("id") or "").strip()
        topic = (campaign.get("topic") or campaign.get("title") or "").strip()
        vurl = (campaign.get("video_url") or "").strip()
        updated = False
        for i, c in enumerate(campaigns):
            c_id = str(c.get("id") or "").strip()
            c_topic = (c.get("topic") or c.get("title") or "").strip()
            c_vurl = (c.get("video_url") or "").strip()
            # Match by ID or by same topic and video_url
            if (cid and c_id == cid) or (topic and c_topic == topic and vurl and c_vurl == vurl):
                campaigns[i] = {**c, **campaign, "id": c_id or cid}
                updated = True
                break
        if not updated:
            campaigns.insert(0, campaign)
        tmp_file = f"{LOCAL_CAMPAIGNS_FILE}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(campaigns[:200], f, indent=2, ensure_ascii=False)
        os.replace(tmp_file, LOCAL_CAMPAIGNS_FILE)
    except Exception as e:
        print(f"[local_db] save_local_campaign error: {e}")

def delete_local_campaign(campaign_id: str, topic: str = None, video_url: str = None) -> bool:
    try:
        campaigns = load_local_campaigns()
        initial_len = len(campaigns)
        cid_str = str(campaign_id or "").strip()
        clean_topic = topic.strip().lower() if topic else None
        clean_vurl = video_url.strip().lower() if video_url else None

        def should_remove(c):
            c_id = str(c.get("id") or "").strip()
            if cid_str and c_id == cid_str:
                return True
            c_topic = (c.get("topic") or c.get("title") or "").strip().lower()
            c_vurl = (c.get("video_url") or "").strip().lower()
            if clean_topic and c_topic == clean_topic:
                if not clean_vurl or not c_vurl or clean_vurl == c_vurl:
                    return True
            return False

        campaigns = [c for c in campaigns if not should_remove(c)]
        if len(campaigns) < initial_len:
            tmp_file = f"{LOCAL_CAMPAIGNS_FILE}.tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(campaigns[:200], f, indent=2, ensure_ascii=False)
            os.replace(tmp_file, LOCAL_CAMPAIGNS_FILE)
            return True
    except Exception as e:
        print(f"[local_db] delete_local_campaign error: {e}")
    return False

def get_youtube_access_token():
    """Exchanges Google refresh token for fresh OAuth access token."""
    client_id = get_clean_env("GOOGLE_CLIENT_ID") or get_clean_env("YOUTUBE_CLIENT_ID")
    client_secret = get_clean_env("GOOGLE_CLIENT_SECRET") or get_clean_env("YOUTUBE_CLIENT_SECRET")
    refresh_token = get_clean_env("GOOGLE_REFRESH_TOKEN") or get_clean_env("YOUTUBE_REFRESH_TOKEN")

    if not client_id or not client_secret or not refresh_token:
        base_url = get_base_url()
        return None, f"Missing Google/YouTube OAuth credentials. Connect your channel at {base_url}/auth/youtube"

    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }
    try:
        r = requests.post(token_url, data=data, timeout=12)
        if r.status_code == 200:
            return r.json().get("access_token"), None
        else:
            return None, f"Google OAuth error: {r.status_code} (Re-authenticate at http://localhost:8080/auth/youtube)"
    except Exception as e:
        return None, str(e)

def publish_to_youtube(text: str, video_url: str = None, video_id: str = None, image_url: str = None) -> dict:
    """
    Publishes promotional takeaways comment to the target video using official YouTube API.
    """
    import re
    if not video_id and video_url:
        match = re.search(r"(?:v=|\/shorts\/|youtu\.be\/)([a-zA-Z0-9_-]{11})", video_url)
        if match:
            video_id = match.group(1)

    if not video_id:
        return {"success": False, "error": "No YouTube video ID found to publish comment"}

    token, err = get_youtube_access_token()
    if not token:
        return {"success": False, "error": err}

    url = "https://www.googleapis.com/youtube/v3/commentThreads?part=snippet"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "snippet": {
            "videoId": video_id,
            "topLevelComment": {
                "snippet": {
                    "textOriginal": text
                }
            }
        }
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=15)
        if res.status_code in (200, 201):
            comment_id = res.json().get("id", "")
            return {
                "success": True,
                "post_id": comment_id,
                "post_url": f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}"
            }
        else:
            return {"success": False, "error": f"YouTube API {res.status_code}: {res.text}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def get_supabase_headers():
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or SUPABASE_KEY
    return {
        "apikey": key or "",
        "Authorization": f"Bearer {key or ''}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def load_tracker():
    if os.path.exists(TRACKER_FILE):
        try:
            with open(TRACKER_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_to_tracker(entry: dict):
    records = load_tracker()
    records.insert(0, entry)
    tmp_file = f"{TRACKER_FILE}.tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(records[:100], f, indent=2)
        os.replace(tmp_file, TRACKER_FILE)
    except Exception as e:
        print(f"[publisher] Warning saving tracker: {e}")

def upload_image_to_linkedin(image_url: str) -> str:
    """Registers and uploads an image to LinkedIn, returning the asset URN."""
    if not LINKEDIN_TOKEN or not image_url:
        return None

    try:
        # Download image bytes
        r_img = requests.get(image_url, timeout=15)
        if r_img.status_code != 200:
            return None
        img_bytes = r_img.content
        content_type = r_img.headers.get("Content-Type", "image/jpeg").split(";")[0]

        headers = {
            "Authorization": f"Bearer {LINKEDIN_TOKEN}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0"
        }

        # 1. Register Upload
        reg_url = "https://api.linkedin.com/v2/assets?action=registerUpload"
        reg_payload = {
            "registerUploadRequest": {
                "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                "owner": LINKEDIN_AUTHOR,
                "serviceRelationships": [{
                    "relationshipType": "OWNER",
                    "identifier": "urn:li:userGeneratedContent"
                }]
            }
        }
        res_reg = requests.post(reg_url, headers=headers, json=reg_payload, timeout=15)
        if res_reg.status_code != 200:
            print(f"[publisher] LinkedIn registerUpload failed: {res_reg.text}")
            return None

        reg_data = res_reg.json()
        upload_url = reg_data["value"]["uploadMechanism"]["com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"]["uploadUrl"]
        asset_urn = reg_data["value"]["asset"]

        # 2. Upload Binary Image Bytes
        up_headers = {
            "Authorization": f"Bearer {LINKEDIN_TOKEN}",
            "Content-Type": content_type
        }
        res_up = requests.put(upload_url, headers=up_headers, data=img_bytes, timeout=30)
        if res_up.status_code in (200, 201):
            return asset_urn
        else:
            print(f"[publisher] LinkedIn image PUT failed: {res_up.status_code} {res_up.text}")
            return None
    except Exception as e:
        print(f"[publisher] Error uploading image to LinkedIn: {e}")
        return None

def publish_to_linkedin(text: str, video_url: str = None, image_url: str = None) -> dict:
    """Publishes a live post to LinkedIn with optional attached image."""
    if not LINKEDIN_TOKEN:
        return {"success": False, "error": "Missing LINKEDIN_ACCESS_TOKEN"}
    
    url = "https://api.linkedin.com/v2/ugcPosts"
    headers = {
        "Authorization": f"Bearer {LINKEDIN_TOKEN}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0"
    }

    # Attempt to upload image to LinkedIn
    asset_urn = None
    if image_url:
        asset_urn = upload_image_to_linkedin(image_url)

    share_content = {
        "shareCommentary": {
            "text": text
        }
    }

    if asset_urn:
        share_content["shareMediaCategory"] = "IMAGE"
        share_content["media"] = [
            {
                "status": "READY",
                "description": {"text": "Campaign Media"},
                "media": asset_urn,
                "title": {"text": "Media"}
            }
        ]
    else:
        share_content["shareMediaCategory"] = "NONE"

    payload = {
        "author": LINKEDIN_AUTHOR,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": share_content
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
        }
    }

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=20)
        if res.status_code in (200, 201):
            post_id = res.json().get("id", "")
            return {
                "success": True,
                "post_id": post_id,
                "post_url": f"https://www.linkedin.com/feed/update/{post_id}/" if post_id else "https://www.linkedin.com/feed/",
                "has_image": bool(asset_urn)
            }
        else:
            return {"success": False, "error": f"LinkedIn HTTP {res.status_code}: {res.text}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def get_verified_page_access_token(page_id: str, token: str) -> str:
    """
    Ensures that the token used for Facebook Page publishing is a genuine PAGE access token.
    If the provided token is a User token, automatically exchanges it for the Page Access Token.
    """
    if not token or not page_id:
        return token
    try:
        # Check /page_id?fields=access_token
        r = requests.get(f"https://graph.facebook.com/v19.0/{page_id}?fields=access_token&access_token={token}", timeout=8)
        if r.status_code == 200:
            resolved_token = r.json().get("access_token")
            if resolved_token:
                os.environ["FACEBOOK_PAGE_ACCESS_TOKEN"] = resolved_token
                try:
                    from server import update_env_var
                    update_env_var("FACEBOOK_PAGE_ACCESS_TOKEN", resolved_token)
                except Exception:
                    pass
                return resolved_token
    except Exception as e:
        print(f"[fb] Note resolving page token: {e}")
    return token

def check_existing_facebook_post(page_id: str, text: str, token: str) -> dict:
    """
    Checks if a post with matching text was published to this Facebook Page within the last 15 minutes.
    """
    if not page_id or not token or not text:
        return None
    try:
        url = f"https://graph.facebook.com/v19.0/{page_id}/published_posts"
        params = {
            "fields": "id,message,created_time",
            "limit": 5,
            "access_token": token
        }
        res = requests.get(url, params=params, timeout=8)
        if res.status_code == 200:
            data = res.json().get("data", [])
            clean_incoming = text.strip()
            compare_incoming = clean_incoming[:70].lower()
            now_dt = datetime.now(timezone.utc)
            for item in data:
                existing_msg = (item.get("message") or "").strip()
                if not existing_msg:
                    continue
                compare_existing = existing_msg[:70].lower()
                if (compare_incoming and compare_incoming in compare_existing) or (compare_existing and compare_existing in compare_incoming):
                    ts_str = item.get("created_time")
                    if ts_str:
                        try:
                            ts_clean = ts_str.replace("+0000", "+00:00")
                            post_dt = datetime.fromisoformat(ts_clean)
                            age_seconds = (now_dt - post_dt).total_seconds()
                            if age_seconds < 3600:  # within 1 hour
                                post_id = item.get("id")
                                print(f"[facebook] 🛡️ Duplicate blocked: Post already published on Facebook {int(age_seconds)}s ago (ID: {post_id}). Reusing.")
                                return {
                                    "success": True,
                                    "post_id": post_id,
                                    "post_url": f"https://www.facebook.com/{post_id}",
                                    "already_existed": True
                                }
                        except Exception:
                            pass
    except Exception as e:
        print(f"[facebook] Duplicate check warning: {e}")
    return None

def publish_to_facebook(text: str, image_url: str = None, link: str = None) -> dict:
    """Publishes to Facebook Page feed or photo with multi-token fallback."""
    load_dotenv(override=True)
    page_id = get_clean_env("FACEBOOK_PAGE_ID", DEFAULT_FACEBOOK_PAGE_ID)
    if not page_id:
        page_id = DEFAULT_FACEBOOK_PAGE_ID

    candidate_keys = [
        "FACEBOOK_PAGE_ACCESS_TOKEN",
        "FACEBOOK_TOKEN",
        "FACEBOOK_USER_TOKEN",
        "FACEBOOK_USER_ACCESS_TOKEN"
    ]
    raw_tokens = []
    for k in candidate_keys:
        v = get_clean_env(k)
        if v and is_valid_token_value(v) and v not in raw_tokens:
            raw_tokens.append(v)
    if DEFAULT_FACEBOOK_PAGE_TOKEN not in raw_tokens:
        raw_tokens.append(DEFAULT_FACEBOOK_PAGE_TOKEN)

    tokens = []
    for t in raw_tokens:
        verified = get_verified_page_access_token(page_id, t)
        if verified and verified not in tokens:
            tokens.append(verified)

    if not tokens:
        return {"success": False, "error": "Missing FACEBOOK_PAGE_ACCESS_TOKEN or FACEBOOK_TOKEN"}

    # Check for existing post
    existing_fb = check_existing_facebook_post(page_id, text, tokens[0])
    if existing_fb:
        return existing_fb

    if image_url:
        image_url = ensure_reliable_media_url(image_url)

    last_err = None
    for page_token in tokens:
        try:
            if image_url:
                # Publish Photo Post
                url = f"https://graph.facebook.com/v19.0/{page_id}/photos"
                payload = {
                    "url": image_url,
                    "caption": text,
                    "access_token": page_token
                }
                res = requests.post(url, json=payload, timeout=20)
                if res.status_code == 200:
                    data = res.json()
                    photo_id = data.get("id", "")
                    feed_post_id = data.get("post_id", "")
                    post_id = feed_post_id or photo_id
                    post_url = f"https://www.facebook.com/photo.php?fbid={photo_id or post_id}"
                    try:
                        lookup_id = photo_id or post_id
                        perm_res = requests.get(f"https://graph.facebook.com/v19.0/{lookup_id}?fields=link&access_token={page_token}", timeout=5)
                        if perm_res.status_code == 200:
                            post_url = perm_res.json().get("link") or post_url
                    except Exception:
                        pass
                    os.environ["FACEBOOK_PAGE_ACCESS_TOKEN"] = page_token
                    return {
                        "success": True,
                        "post_id": post_id,
                        "photo_id": photo_id,
                        "post_url": post_url
                    }
                elif res.status_code in (400, 401, 403):
                    err_txt = res.text
                    last_err = f"Facebook HTTP {res.status_code}: {err_txt}"
                    if "OAuthException" in err_txt or "expired" in err_txt.lower():
                        continue

            # Publish Feed Post (or fallback)
            url = f"https://graph.facebook.com/v19.0/{page_id}/feed"
            payload = {
                "message": text,
                "access_token": page_token
            }
            if link:
                payload["link"] = link

            res = requests.post(url, json=payload, timeout=20)
            if res.status_code == 200:
                post_id = res.json().get("id", "")
                post_url = f"https://www.facebook.com/{post_id}"
                try:
                    perm_res = requests.get(f"https://graph.facebook.com/v19.0/{post_id}?fields=link&access_token={page_token}", timeout=5)
                    if perm_res.status_code == 200:
                        post_url = perm_res.json().get("link") or post_url
                except Exception:
                    pass
                os.environ["FACEBOOK_PAGE_ACCESS_TOKEN"] = page_token
                return {
                    "success": True,
                    "post_id": post_id,
                    "post_url": post_url
                }
            elif res.status_code in (400, 401, 403):
                err_txt = res.text
                last_err = f"Facebook HTTP {res.status_code}: {err_txt}"
                if "OAuthException" in err_txt or "expired" in err_txt.lower():
                    continue
            else:
                last_err = f"Facebook HTTP {res.status_code}: {res.text}"
        except Exception as e:
            last_err = str(e)
            continue

    return {"success": False, "error": last_err or "Facebook publication failed"}

def ensure_reliable_media_url(image_url: str) -> str:
    """
    Ensures the image URL is reliably fetchable by Meta's Instagram/Facebook crawlers.
    YouTube CDN (i.ytimg.com) often blocks or throttles Meta crawlers (HTTP 400 'Only photo or video can be accepted').
    If the image is from YouTube or external site, auto-caches it to Supabase Storage.
    """
    if not image_url or not isinstance(image_url, str):
        return image_url

    # If already hosted on Supabase Storage, it is 100% reliable
    if "supabase.co" in image_url:
        return image_url

    sb_url = os.getenv("SUPABASE_URL") or SUPABASE_URL or "https://sqgxnhdsajinjhuiyntm.supabase.co"
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
    if not sb_url or not sb_key or str(sb_url).startswith("https://your-project"):
        return image_url

    try:
        # Download image bytes
        r = requests.get(image_url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and len(r.content) > 1000:
            content_type = r.headers.get("Content-Type") or "image/jpeg"
            ext = "jpg" if ("jpeg" in content_type or "jpg" in content_type) else "png"
            dest_name = f"thumb_{int(time.time())}_{abs(hash(image_url)) % 100000}.{ext}"
            bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "marketing-banners")
            upload_url = f"{sb_url}/storage/v1/object/{bucket}/{dest_name}"
            headers = {
                "apikey": sb_key,
                "Authorization": f"Bearer {sb_key}",
                "Content-Type": content_type,
                "x-upsert": "true"
            }
            u_res = requests.post(upload_url, headers=headers, data=r.content, timeout=10)
            if u_res.status_code in (200, 201):
                public_url = f"{sb_url}/storage/v1/object/public/{bucket}/{dest_name}"
                print(f"[publisher] 🖼️ Re-hosted image to Supabase Storage for Meta crawler: {public_url}")
                return public_url
    except Exception as e:
        print(f"[publisher] Note re-hosting image: {e}")

    return image_url

def check_existing_instagram_media(account_id: str, caption: str, token: str) -> dict:
    """
    Checks if a post with a matching caption was already published to this Instagram account
    recently (e.g. within the last 15 minutes, or from Facebook auto-cross-post).
    Returns existing post info if found, or None.
    """
    if not account_id or not token or not caption:
        return None
    try:
        url = f"https://graph.facebook.com/v19.0/{account_id}/media"
        params = {
            "fields": "id,caption,permalink,timestamp",
            "limit": 10,
            "access_token": token
        }
        res = requests.get(url, params=params, timeout=8)
        if res.status_code == 200:
            data = res.json().get("data", [])
            clean_incoming = caption.strip()
            compare_incoming = clean_incoming[:70].lower()
            now_dt = datetime.now(timezone.utc)
            for item in data:
                existing_cap = (item.get("caption") or "").strip()
                if not existing_cap:
                    continue
                compare_existing = existing_cap[:70].lower()
                if (compare_incoming and compare_incoming in compare_existing) or (compare_existing and compare_existing in compare_incoming):
                    ts_str = item.get("timestamp")
                    if ts_str:
                        try:
                            ts_clean = ts_str.replace("+0000", "+00:00")
                            post_dt = datetime.fromisoformat(ts_clean)
                            age_seconds = (now_dt - post_dt).total_seconds()
                            if age_seconds < 3600:  # within 1 hour
                                print(f"[instagram] 🛡️ Duplicate blocked: Post '{compare_incoming[:30]}...' already exists on Instagram (ID: {item.get('id')}, published {int(age_seconds)}s ago). Reusing.")
                                return {
                                    "success": True,
                                    "post_id": item.get("id"),
                                    "post_url": item.get("permalink") or f"https://www.instagram.com/p/{item.get('id')}/",
                                    "already_existed": True
                                }
                        except Exception:
                            pass
    except Exception as e:
        print(f"[instagram] Duplicate check warning: {e}")
    return None

def publish_to_instagram(caption: str, image_url: str) -> dict:
    """
    Publishes image with caption to Instagram Business Account.
    Enforces strictly single-token execution, extended readiness polling,
    and automatic duplicate detection (preventing duplicate posts from retries or FB cross-posting).
    """
    load_dotenv(override=True)
    account_id = (
        get_clean_env("INSTAGRAM_BUSINESS_ACCOUNT_ID")
        or get_clean_env("INSTAGRAM_ID", DEFAULT_INSTAGRAM_ACCOUNT_ID)
    )
    if not account_id:
        account_id = DEFAULT_INSTAGRAM_ACCOUNT_ID

    candidate_keys = [
        "FACEBOOK_PAGE_ACCESS_TOKEN",
        "INSTAGRAM_ACCESS_TOKEN",
        "INSTAGRAM_SECONDARY_ACCESS_TOKEN",
        "FACEBOOK_TOKEN",
        "FACEBOOK_USER_TOKEN",
        "FACEBOOK_USER_ACCESS_TOKEN"
    ]
    raw_tokens = []
    for k in candidate_keys:
        v = get_clean_env(k)
        if v and is_valid_token_value(v) and v not in raw_tokens:
            raw_tokens.append(v)
    if DEFAULT_FACEBOOK_PAGE_TOKEN not in raw_tokens:
        raw_tokens.append(DEFAULT_FACEBOOK_PAGE_TOKEN)

    # 1. Select the single best verified working token upfront
    chosen_token = None
    for t in raw_tokens:
        verified = get_verified_page_access_token(DEFAULT_FACEBOOK_PAGE_ID, t)
        if verified:
            chosen_token = verified
            break

    if not chosen_token:
        return {"success": False, "error": "Missing INSTAGRAM_ACCESS_TOKEN or FACEBOOK_PAGE_ACCESS_TOKEN"}

    # 2. Check if identical post was already published on Instagram (e.g. by FB cross-post or recent attempt)
    existing_post = check_existing_instagram_media(account_id, caption, chosen_token)
    if existing_post:
        return existing_post

    # 2b. Re-host image to Supabase CDN if needed (YouTube i.ytimg CDN often blocks Meta crawler)
    reliable_image_url = ensure_reliable_media_url(image_url) if image_url else None

    # 3. Create SINGLE Media Container
    media_url = f"https://graph.facebook.com/v19.0/{account_id}/media"
    container_payload = {
        "image_url": reliable_image_url or image_url or "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=1080&q=80",
        "caption": caption,
        "access_token": chosen_token
    }

    try:
        c_res = requests.post(media_url, json=container_payload, timeout=20)
        # If it failed with 400 and reliable_image_url was tried, fallback retry with raw image_url
        if c_res.status_code != 200 and image_url and reliable_image_url != image_url:
            container_payload["image_url"] = image_url
            c_res = requests.post(media_url, json=container_payload, timeout=20)

        if c_res.status_code != 200:
            err_text = c_res.text
            return {"success": False, "error": f"IG Container HTTP {c_res.status_code}: {err_text}"}

        creation_id = c_res.json().get("id")
        if not creation_id:
            return {"success": False, "error": f"IG Container returned no ID: {c_res.text}"}

        # 4. Extended Container Readiness Polling (up to 15 attempts * 2s = 30 seconds)
        container_ready = False
        for _attempt in range(15):
            time.sleep(2)
            try:
                status_res = requests.get(
                    f"https://graph.facebook.com/v19.0/{creation_id}?fields=status_code&access_token={chosen_token}",
                    timeout=5
                )
                if status_res.status_code == 200:
                    s_code = status_res.json().get("status_code")
                    if s_code == "FINISHED":
                        container_ready = True
                        break
                    elif s_code == "ERROR":
                        return {"success": False, "error": f"IG Container processing failed: {status_res.text}"}
            except Exception:
                pass

        if not container_ready:
            time.sleep(3)

        # 5. Publish Media Container (Single Execution)
        publish_url = f"https://graph.facebook.com/v19.0/{account_id}/media_publish"
        p_res = requests.post(publish_url, json={
            "creation_id": creation_id,
            "access_token": chosen_token
        }, timeout=25)

        if p_res.status_code == 200:
            post_id = p_res.json().get("id", "")
            post_url = f"https://www.instagram.com/p/{post_id}/"
            try:
                perm_res = requests.get(
                    f"https://graph.facebook.com/v19.0/{post_id}?fields=permalink&access_token={chosen_token}",
                    timeout=5
                )
                if perm_res.status_code == 200:
                    post_url = perm_res.json().get("permalink") or post_url
            except Exception:
                pass
            os.environ["INSTAGRAM_ACCESS_TOKEN"] = chosen_token
            return {
                "success": True,
                "post_id": post_id,
                "post_url": post_url
            }
        else:
            # If publish failed or timed out, double check if post actually succeeded on Instagram
            post_check = check_existing_instagram_media(account_id, caption, chosen_token)
            if post_check:
                return post_check
            return {"success": False, "error": f"IG Publish HTTP {p_res.status_code}: {p_res.text}"}
    except Exception as e:
        # Check if the post succeeded despite client-side exception
        post_check = check_existing_instagram_media(account_id, caption, chosen_token)
        if post_check:
            return post_check
        return {"success": False, "error": f"Instagram publish exception: {str(e)}"}

def check_existing_threads_post(user_id: str, text: str, token: str) -> dict:
    """Checks if identical text was already published on Threads in the last 1 hour."""
    if not user_id or not token or not text:
        return None
    try:
        url = f"https://graph.threads.net/v1.0/{user_id}/threads"
        params = {"fields": "id,text,timestamp,permalink", "limit": 5, "access_token": token}
        res = requests.get(url, params=params, timeout=8)
        if res.status_code == 200:
            data = res.json().get("data", [])
            clean_incoming = text.strip()[:70].lower()
            now_dt = datetime.now(timezone.utc)
            for item in data:
                existing_text = (item.get("text") or "").strip()[:70].lower()
                if (clean_incoming and clean_incoming in existing_text) or (existing_text and existing_text in clean_incoming):
                    ts_str = item.get("timestamp")
                    if ts_str:
                        try:
                            ts_clean = ts_str.replace("+0000", "+00:00")
                            post_dt = datetime.fromisoformat(ts_clean)
                            age = (now_dt - post_dt).total_seconds()
                            if age < 3600:
                                post_id = item.get("id")
                                print(f"[threads] 🛡️ Duplicate blocked: Post already published on Threads {int(age)}s ago. Reusing.")
                                return {
                                    "success": True,
                                    "post_id": post_id,
                                    "post_url": item.get("permalink") or f"https://www.threads.net/post/{post_id}",
                                    "already_existed": True
                                }
                        except Exception:
                            pass
    except Exception as e:
        print(f"[threads] Duplicate check warning: {e}")
    return None

def publish_to_threads(text: str, image_url: str = None) -> dict:
    """
    Publishes a post (text or image) to Threads via Meta's Official Threads API.
    Docs: https://developers.facebook.com/docs/threads/overview
    """
    load_dotenv(override=True)
    threads_user_id = get_clean_env("THREADS_USER_ID", DEFAULT_THREADS_USER_ID)
    threads_token = get_clean_env("THREADS_ACCESS_TOKEN") or get_clean_env("THREADS_TOKEN") or DEFAULT_THREADS_ACCESS_TOKEN

    if not threads_token:
        base_url = get_base_url()
        return {"success": False, "error": f"Missing THREADS_ACCESS_TOKEN. Connect your Threads account via {base_url}/auth/threads or in Accounts & Token Setup."}

    # If user_id is missing, auto-fetch from /me endpoint
    if not threads_user_id:
        try:
            me_res = requests.get(f"https://graph.threads.net/v1.0/me?fields=id,username&access_token={threads_token}", timeout=8)
            if me_res.status_code == 200:
                me_data = me_res.json()
                threads_user_id = me_data.get("id")
                threads_username = me_data.get("username")
                if threads_user_id:
                    os.environ["THREADS_USER_ID"] = str(threads_user_id)
                    if threads_username:
                        os.environ["THREADS_USERNAME"] = str(threads_username)
                    try:
                        from server import update_env_var
                        update_env_var("THREADS_USER_ID", str(threads_user_id))
                        if threads_username:
                            update_env_var("THREADS_USERNAME", str(threads_username))
                    except Exception:
                        pass
        except Exception as e:
            print(f"[threads] Warning fetching /me: {e}")

    if not threads_user_id:
        return {"success": False, "error": "Could not determine Threads user ID. Please reconnect Threads OAuth or set THREADS_USER_ID in .env."}

    # Threads has a strict 500-character limit per post
    clean_text = (text or "").strip()
    if len(clean_text) > 500:
        import re
        url_match = re.search(r'(https?://[^\s]+)', clean_text)
        if url_match:
            found_url = url_match.group(1).rstrip(".,;)>]")
            url_footer = f"\n\n👉 {found_url}"
            text_without_url = clean_text.replace(found_url, "").strip()
            avail = 500 - len(url_footer) - 3
            if avail > 40:
                clean_text = text_without_url[:avail].rstrip() + "..." + url_footer
            else:
                clean_text = clean_text[:497] + "..."
        else:
            clean_text = clean_text[:497] + "..."

    # Check for existing post
    existing_th = check_existing_threads_post(threads_user_id, clean_text, threads_token)
    if existing_th:
        return existing_th

    if image_url:
        image_url = ensure_reliable_media_url(image_url)

    try:
        # Step 1: Create Container
        container_url = f"https://graph.threads.net/v1.0/{threads_user_id}/threads"
        container_payload = {
            "text": clean_text,
            "access_token": threads_token
        }
        if image_url:
            container_payload["media_type"] = "IMAGE"
            container_payload["image_url"] = image_url
        else:
            container_payload["media_type"] = "TEXT"

        c_res = requests.post(container_url, data=container_payload, timeout=20)
        # Fallback to TEXT container if IMAGE was rejected
        if c_res.status_code not in (200, 201) and image_url:
            print(f"[threads] Image container failed ({c_res.status_code}): {c_res.text}. Retrying with text fallback...")
            container_payload["media_type"] = "TEXT"
            container_payload.pop("image_url", None)
            c_res = requests.post(container_url, data=container_payload, timeout=20)

        if c_res.status_code not in (200, 201):
            return {"success": False, "error": f"Threads container error {c_res.status_code}: {c_res.text}"}

        creation_id = c_res.json().get("id")
        if not creation_id:
            return {"success": False, "error": f"Threads container returned no ID: {c_res.text}"}

        # Step 2: Poll Container Status (wait until FINISHED)
        for _ in range(6):
            time.sleep(2)
            try:
                s_res = requests.get(
                    f"https://graph.threads.net/v1.0/{creation_id}?fields=status_code,error_message&access_token={threads_token}",
                    timeout=5
                )
                if s_res.status_code == 200:
                    s_data = s_res.json()
                    status = s_data.get("status_code")
                    if status == "FINISHED":
                        break
                    elif status == "ERROR":
                        # If container processing error and image was used, fallback to text
                        if image_url:
                            print("[threads] Image container processing error. Falling back to text-only...")
                            return publish_to_threads(text=text, image_url=None)
                        return {"success": False, "error": f"Threads container failed: {s_data.get('error_message', s_res.text)}"}
            except Exception:
                pass

        # Step 3: Publish Container
        publish_url = f"https://graph.threads.net/v1.0/{threads_user_id}/threads_publish"
        p_res = requests.post(publish_url, data={
            "creation_id": creation_id,
            "access_token": threads_token
        }, timeout=20)

        if p_res.status_code in (200, 201):
            p_data = p_res.json()
            post_id = p_data.get("id")
            username = os.getenv("THREADS_USERNAME") or "founders_gateway"
            post_url = f"https://www.threads.net/@{username}/post/{post_id}" if post_id else f"https://www.threads.net/@{username}"
            return {
                "success": True,
                "post_id": post_id,
                "post_url": post_url
            }
        else:
            return {"success": False, "error": f"Threads publish failed {p_res.status_code}: {p_res.text}"}

    except Exception as e:
        return {"success": False, "error": str(e)}

def refresh_x_oauth2_token() -> str:
    """Refreshes X OAuth 2.0 access token using rotating X_REFRESH_TOKEN and updates .env."""
    client_id = os.getenv("X_CLIENT_ID")
    client_secret = os.getenv("X_CLIENT_SECRET")
    refresh_token = os.getenv("X_REFRESH_TOKEN")

    if not client_id or not refresh_token:
        return None

    try:
        url = "https://api.twitter.com/2/oauth2/token"
        data = {
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "client_id": client_id
        }
        auth = (client_id, client_secret) if client_secret else None
        res = requests.post(url, data=data, auth=auth, timeout=15)
        if res.status_code == 200:
            token_data = res.json()
            new_access_token = token_data.get("access_token")
            new_refresh_token = token_data.get("refresh_token")

            if new_access_token:
                os.environ["X_ACCESS_TOKEN"] = new_access_token
                try:
                    from server import update_env_var
                    update_env_var("X_ACCESS_TOKEN", new_access_token)
                    if new_refresh_token:
                        os.environ["X_REFRESH_TOKEN"] = new_refresh_token
                        update_env_var("X_REFRESH_TOKEN", new_refresh_token)
                except Exception:
                    pass
                return new_access_token
        else:
            print(f"[publisher] X token refresh returned {res.status_code}: {res.text}")
    except Exception as e:
        print(f"[publisher] Error refreshing X token: {e}")
    return None

def publish_to_x(text: str, image_url: str = None) -> dict:
    """
    Publishes a post (tweet) to X (Twitter) using Twitter API v2.
    Supports either:
    1. OAuth 2.0 User Token (X_ACCESS_TOKEN) - generated via 1-click /auth/x
    2. OAuth 1.0a User Context (X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET)
    """
    api_key = os.getenv("X_API_KEY") or os.getenv("TWITTER_API_KEY")
    api_secret = os.getenv("X_API_SECRET") or os.getenv("TWITTER_API_SECRET")
    access_token = os.getenv("X_ACCESS_TOKEN") or os.getenv("TWITTER_ACCESS_TOKEN")
    access_token_secret = os.getenv("X_ACCESS_TOKEN_SECRET") or os.getenv("TWITTER_ACCESS_TOKEN_SECRET")

    # X text limit is 280 characters
    tweet_text = text.strip()
    if len(tweet_text) > 280:
        tweet_text = tweet_text[:277] + "..."

    url = "https://api.twitter.com/2/tweets"
    payload = {"text": tweet_text}

    # 1. Try OAuth 1.0a if all 4 keys are present
    if all([api_key, api_secret, access_token, access_token_secret]):
        try:
            from requests_oauthlib import OAuth1
            auth = OAuth1(api_key, api_secret, access_token, access_token_secret)
            res = requests.post(url, auth=auth, json=payload, timeout=15)
            if res.status_code in (200, 201):
                data = res.json().get("data", {})
                tweet_id = data.get("id", "")
                return {
                    "success": True,
                    "post_id": tweet_id,
                    "post_url": f"https://x.com/i/web/status/{tweet_id}" if tweet_id else "https://x.com"
                }
            elif res.status_code == 402:
                return {
                    "success": False,
                    "error": "X API 402: Credits depleted. X (Twitter) requires active API credits on console.x.com to publish tweets."
                }
            else:
                return {"success": False, "error": f"X API HTTP {res.status_code}: {res.text}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # 2. Try OAuth 2.0 Bearer Token (if generated via /auth/x)
    if access_token:
        try:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            }
            res = requests.post(url, headers=headers, json=payload, timeout=15)
            if res.status_code == 401:
                print("[publisher] X token expired (401), attempting automatic refresh...")
                new_token = refresh_x_oauth2_token()
                if new_token:
                    headers["Authorization"] = f"Bearer {new_token}"
                    res = requests.post(url, headers=headers, json=payload, timeout=15)

            if res.status_code in (200, 201):
                data = res.json().get("data", {})
                tweet_id = data.get("id", "")
                return {
                    "success": True,
                    "post_id": tweet_id,
                    "post_url": f"https://x.com/i/web/status/{tweet_id}" if tweet_id else "https://x.com"
                }
            elif res.status_code == 402:
                return {
                    "success": False,
                    "error": "X API 402: Credits depleted. X (Twitter) requires active API credits on console.x.com to publish tweets."
                }
            else:
                return {"success": False, "error": f"X API HTTP {res.status_code}: {res.text}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    base_url = get_base_url()
    return {
        "success": False,
        "error": f"Missing X (Twitter) credentials. Connect your X account at {base_url}/auth/x"
    }

def publish_to_pinterest(title: str, description: str, image_url: str = None, link: str = None, board_id: str = None) -> dict:
    """
    Creates a new Pin on Pinterest using Pinterest API v5.
    Requires an image_url and board_id.
    """
    token = get_clean_env("PINTEREST_ACCESS_TOKEN") or get_clean_env("PINTEREST_TOKEN")
    board = board_id or get_clean_env("PINTEREST_BOARD_ID")

    if not token:
        base_url = get_base_url()
        return {"success": False, "error": f"Missing PINTEREST_ACCESS_TOKEN. Connect your Pinterest account at {base_url}/auth/pinterest"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    # If board_id is missing, auto-fetch the first board
    if not board:
        try:
            b_res = requests.get("https://api.pinterest.com/v5/boards", headers=headers, timeout=10)
            if b_res.status_code == 200:
                items = b_res.json().get("items", [])
                if items:
                    board = items[0].get("id")
                    os.environ["PINTEREST_BOARD_ID"] = str(board)
        except Exception:
            pass

    if not board:
        return {"success": False, "error": "No Pinterest Board found. Please create at least one board on your Pinterest account."}

    # Pinterest requires an image
    img = image_url or "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=1080&q=80"

    payload = {
        "board_id": str(board),
        "title": title[:100],
        "description": description[:800],
        "media_source": {
            "source_type": "image_url",
            "url": img
        }
    }
    if link:
        payload["link"] = link

    try:
        url = "https://api.pinterest.com/v5/pins"
        res = requests.post(url, headers=headers, json=payload, timeout=20)
        if res.status_code in (200, 201):
            data = res.json()
            pin_id = data.get("id", "")
            return {
                "success": True,
                "post_id": pin_id,
                "post_url": f"https://www.pinterest.com/pin/{pin_id}/" if pin_id else "https://www.pinterest.com"
            }
        else:
            return {"success": False, "error": f"Pinterest API HTTP {res.status_code}: {res.text}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def dispatch_multi_channel_publish(post_data: dict) -> dict:
    """
    Executes live publishing across configured social networks,
    records results in Supabase archive & local tracker.
    Guarantees thread-safe single publishing (no duplicate posts).
    """
    load_dotenv(override=True)
    post_id = post_data.get("id")
    topic = post_data.get("topic") or post_data.get("title") or ""
    video_url = post_data.get("video_url") or ""
    hook = post_data.get("hook") or ""

    # Unique content fingerprint for bulletproof deduplication
    clean_vurl = video_url.strip().lower()
    clean_topic = topic.strip().lower()[:50]
    clean_hook = hook.strip().lower()[:40]
    content_fingerprint = f"content:{clean_vurl}:{clean_topic}:{clean_hook}"
    id_fingerprint = f"id:{post_id}" if post_id else None

    with _PUBLISH_LOCK:
        now_ts = time.time()
        # 1. Block if identical content or ID is already in-flight right now
        if content_fingerprint in _IN_FLIGHT_PUBLISHES or (id_fingerprint and id_fingerprint in _IN_FLIGHT_PUBLISHES):
            print(f"[publisher] ⚠️ DUPLICATE BLOCKED: Post '{topic}' is already in-flight! Skipping duplicate execution.")
            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "published_platforms": [],
                "results": {"status": "duplicate_in_flight_blocked"},
                "duplicate_blocked": True
            }

        # 2. Block if identical content or ID was already published in the last 180 seconds (3 minutes)
        for fp in [content_fingerprint, id_fingerprint]:
            if fp and fp in _RECENT_PUBLISH_CACHE:
                last_ts, last_res = _RECENT_PUBLISH_CACHE[fp]
                if now_ts - last_ts < 180:
                    print(f"[publisher] ⚠️ DUPLICATE BLOCKED: Post '{topic}' was already published {int(now_ts - last_ts)}s ago! Returning existing result.")
                    return last_res

        _IN_FLIGHT_PUBLISHES.add(content_fingerprint)
        if id_fingerprint:
            _IN_FLIGHT_PUBLISHES.add(id_fingerprint)

    try:
        results = {}
        published_platforms = []
        
        # 1. Content preparation
        hook = post_data.get("hook", "")
        content = post_data.get("generated_content", "")
        summary = post_data.get("summary", "")
        hashtags = post_data.get("hashtags", "")
        cta = post_data.get("call_to_action", "")
        video_url = post_data.get("video_url", "")
        image_url = post_data.get("image_url", "")
        captions = post_data.get("platform_captions") or {}
        target_platforms = post_data.get("platforms") or ["facebook", "instagram", "threads"]

        # Format text variants
        full_repurposed_text = (f"🔥 {hook}\n\n" if hook else "") + content + (f"\n\n📌 Highlights:\n{summary}" if summary else "") + (f"\n\n{cta}" if cta else "") + (f"\n\n{hashtags}" if hashtags else "")
        li_text = captions.get("linkedin") or full_repurposed_text
        fb_text = captions.get("facebook") or full_repurposed_text
        ig_caption = captions.get("instagram") or full_repurposed_text
        yt_comment = f"🔥 {hook}\n\n📌 Key Takeaways:\n{summary}\n\n{hashtags}" if summary else f"🔥 {hook}\n\n{content}\n\n{hashtags}"
        x_tweet = captions.get("x") or captions.get("twitter") or f"{hook}\n\n{cta}\n\n{hashtags}"

        # 2. LinkedIn (Publishes text commentary + attached image banner)
        if "linkedin" in target_platforms:
            li_res = publish_to_linkedin(li_text, video_url, image_url)
            results["linkedin"] = li_res
            if li_res.get("success"):
                published_platforms.append("linkedin")

        # 3. Facebook
        if "facebook" in target_platforms:
            fb_res = publish_to_facebook(fb_text, image_url, video_url)
            results["facebook"] = fb_res
            if fb_res.get("success"):
                published_platforms.append("facebook")

        # 4. Instagram
        if "instagram" in target_platforms:
            ig_res = publish_to_instagram(ig_caption, image_url)
            results["instagram"] = ig_res
            if ig_res.get("success"):
                published_platforms.append("instagram")

        # 4b. Threads (Meta Threads - 500 char max with guaranteed clickable link)
        if "threads" in target_platforms:
            th_caption = captions.get("threads")
            if not th_caption or len(th_caption.strip()) < 10 or (video_url and video_url not in th_caption):
                hook_p = f"🔥 {hook}\n\n" if hook else ""
                cta_p = f"\n\n👉 Watch: {video_url}" if video_url else ""
                tags_p = f"\n{hashtags}" if hashtags else ""
                avail_len = 500 - len(hook_p) - len(cta_p) - len(tags_p) - 4
                body_source = summary if (summary and len(summary) > 20) else content
                if len(body_source) > avail_len and avail_len > 30:
                    body_p = body_source[:avail_len].rstrip() + "..."
                else:
                    body_p = body_source[:avail_len].rstrip()
                th_caption = f"{hook_p}{body_p}{cta_p}{tags_p}".strip()
            th_res = publish_to_threads(th_caption, image_url)
            results["threads"] = th_res
            if th_res.get("success"):
                published_platforms.append("threads")

        # 5. YouTube (Community Post or Video Comment)
        if "youtube" in target_platforms:
            yt_res = publish_to_youtube(yt_comment, video_url=video_url, video_id=post_data.get("video_id"), image_url=image_url)
            results["youtube"] = yt_res
            if yt_res.get("success"):
                published_platforms.append("youtube")

        # 6. X (Twitter)
        if "x" in target_platforms or "twitter" in target_platforms:
            x_res = publish_to_x(x_tweet, image_url=image_url)
            results["x"] = x_res
            if x_res.get("success"):
                published_platforms.append("x")

        # 7. Pinterest (Pin)
        if "pinterest" in target_platforms:
            pin_title = hook[:100] or post_data.get("topic") or "Video Campaign"
            pin_desc = f"{hook}\n\n{content}\n\n{cta}\n\n{hashtags}"[:800]
            pin_res = publish_to_pinterest(pin_title, pin_desc, image_url, video_url)
            results["pinterest"] = pin_res
            if pin_res.get("success"):
                published_platforms.append("pinterest")

        # 6. Tracker & Persistence
        now_iso = datetime.now(timezone.utc).isoformat()
        platform_urls = {
            plat: res.get("post_url") for plat, res in results.items() if isinstance(res, dict) and res.get("post_url")
        }
        live_single_url = (
            results.get("threads", {}).get("post_url")
            or results.get("instagram", {}).get("post_url")
            or results.get("facebook", {}).get("post_url")
            or results.get("linkedin", {}).get("post_url")
            or results.get("youtube", {}).get("post_url")
            or video_url
        )
        tracker_entry = {
            "timestamp": now_iso,
            "title": post_data.get("topic") or post_data.get("title") or "Video Repurpose Campaign",
            "video_url": video_url,
            "platforms_published": published_platforms,
            "results": results,
            "platform_urls": platform_urls,
            "live_post_url": live_single_url,
            "post_data": post_data
        }
        save_to_tracker(tracker_entry)

        # Update or record into persistent local campaigns database
        try:
            save_local_campaign({
                "id": str(post_data.get("id") or int(time.time())),
                "topic": post_data.get("topic") or post_data.get("title") or "Video Campaign",
                "title": post_data.get("topic") or post_data.get("title") or "Video Campaign",
                "video_url": video_url,
                "image_url": image_url,
                "hook": hook,
                "summary": summary,
                "generated_content": content,
                "hashtags": hashtags,
                "call_to_action": cta,
                "status": "published" if published_platforms else "failed",
                "scheduled_at": now_iso,
                "published_at": now_iso if published_platforms else None,
                "live_post_url": live_single_url,
                "platform_urls": platform_urls,
                "platforms_published": published_platforms,
                "social_post_ids": {plat: pres.get("post_id") for plat, pres in results.items() if isinstance(pres, dict) and pres.get("post_id")},
                "created_at": now_iso
            })
        except Exception as ex_loc:
            print(f"[publisher] Warning saving to local campaigns: {ex_loc}")

        # 6b. Automatically record failure notifications in database for any failed platform
        for plat in target_platforms:
            res_item = results.get(plat)
            if isinstance(res_item, dict) and not res_item.get("success"):
                err_msg = res_item.get("error") or "Publishing failed"
                try:
                    create_notification(
                        platform=plat,
                        title=f"{plat.capitalize()} Publishing Failed",
                        message=f"Failed to publish '{post_data.get('topic') or 'Video Campaign'}' to {plat.capitalize()}.",
                        type="error",
                        post_id=str(post_data.get('id') or ""),
                        error_details=str(err_msg)
                    )
                except Exception as ne:
                    print(f"[publisher] Notification error: {ne}")

        # 7. Record in Supabase archive and update marketing_content status
        record_id = post_data.get("id")
        sb_url = os.getenv("SUPABASE_URL") or SUPABASE_URL or "https://sqgxnhdsajinjhuiyntm.supabase.co"
        sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
        if sb_url and sb_key and not str(sb_url).startswith("https://your-project") and not str(sb_key).startswith("your_supabase"):
            headers = get_supabase_headers()
            try:
                # Insert into marketing_published_archive
                archive_payload = {
                    "topic": post_data.get("topic") or post_data.get("title") or "Video Repurpose Campaign",
                    "business_name": post_data.get("business_name") or "",
                    "published_content": li_text[:4000],
                    "hashtags": hashtags,
                    "call_to_action": cta,
                    "platforms_published": published_platforms,
                    "linkedin_post_id": results.get("linkedin", {}).get("post_id"),
                    "facebook_post_id": results.get("facebook", {}).get("post_id"),
                    "instagram_media_id": results.get("instagram", {}).get("post_id"),
                    "threads_post_id": results.get("threads", {}).get("post_id"),
                    "live_post_url": live_single_url,
                    "published_at": now_iso
                }
                if record_id:
                    try:
                        import uuid
                        uuid.UUID(str(record_id))
                        archive_payload["original_queue_id"] = str(record_id)
                    except Exception:
                        pass

                requests.post(f"{sb_url}/rest/v1/marketing_published_archive", headers=headers, json=archive_payload, timeout=10)

                # Update status in video_campaigns and marketing_content
                if record_id:
                    social_ids = {
                        "facebook": results.get("facebook", {}).get("post_id"),
                        "photo_id": results.get("facebook", {}).get("photo_id"),
                        "instagram": results.get("instagram", {}).get("post_id"),
                        "threads": results.get("threads", {}).get("post_id"),
                        "x": results.get("x", {}).get("post_id"),
                        "youtube": results.get("youtube", {}).get("post_id"),
                        "linkedin": results.get("linkedin", {}).get("post_id")
                    }
                    post_status = "published" if published_platforms else "failed"
                    try:
                        requests.patch(
                            f"{sb_url}/rest/v1/video_campaigns?id=eq.{record_id}",
                            headers=headers,
                            json={
                                "status": post_status,
                                "published_at": now_iso,
                                "live_post_url": archive_payload["live_post_url"],
                                "social_post_ids": social_ids
                            },
                            timeout=5
                        )
                    except Exception:
                        pass

                    try:
                        requests.patch(
                            f"{sb_url}/rest/v1/marketing_content?id=eq.{record_id}",
                            headers=headers,
                            json={
                                "status": post_status
                            },
                            timeout=5
                        )
                    except Exception:
                        pass
            except Exception as e:
                print(f"[publisher] Supabase archive warning: {e}")

        final_res = {
            "timestamp": now_iso,
            "published_platforms": published_platforms,
            "results": results,
            "platform_urls": platform_urls,
            "live_post_url": live_single_url
        }
        with _PUBLISH_LOCK:
            _RECENT_PUBLISH_CACHE[content_fingerprint] = (time.time(), final_res)
            if id_fingerprint:
                _RECENT_PUBLISH_CACHE[id_fingerprint] = (time.time(), final_res)
        return final_res
    finally:
        with _PUBLISH_LOCK:
            _IN_FLIGHT_PUBLISHES.discard(content_fingerprint)
            if id_fingerprint:
                _IN_FLIGHT_PUBLISHES.discard(id_fingerprint)

def delete_live_social_post(social_ids: dict) -> dict:
    """
    Deletes the published post from live social media accounts:
    - LinkedIn: Deletes the live post from user's feed
    - Facebook: Deletes the photo/feed post from the Page
    - Instagram: Deletes the media container/post from Instagram
    - YouTube: Deletes the promotional comment from YouTube video
    """
    deleted = {}
    if not isinstance(social_ids, dict):
        return deleted
    
    # 1. LinkedIn
    li_id = social_ids.get("linkedin") or social_ids.get("linkedin_post_id") or social_ids.get("li")
    live_url = social_ids.get("live_post_url") or ""
    if not li_id and "urn:li:" in live_url:
        import re
        m = re.search(r"(urn:li:[a-zA-Z]+:[0-9]+)", live_url)
        if m:
            li_id = m.group(1)

    token = os.getenv("LINKEDIN_ACCESS_TOKEN") or os.getenv("LINKEDIN_TOKEN")
    if li_id and token:
        try:
            import urllib.parse
            headers = {
                "Authorization": f"Bearer {token}",
                "X-Restli-Protocol-Version": "2.0.0"
            }
            li_id_str = str(li_id).strip()
            u_enc = urllib.parse.quote(li_id_str)
            r = requests.delete(f"https://api.linkedin.com/v2/ugcPosts/{u_enc}", headers=headers, timeout=10)
            if r.status_code in (200, 204):
                deleted["linkedin"] = True
            else:
                r2 = requests.delete(f"https://api.linkedin.com/v2/shares/{u_enc}", headers=headers, timeout=10)
                if r2.status_code in (200, 204):
                    deleted["linkedin"] = True
                else:
                    num_id = li_id_str.split(":")[-1]
                    r3 = requests.delete(f"https://api.linkedin.com/v2/shares/{num_id}", headers=headers, timeout=10)
                    deleted["linkedin"] = r3.status_code in (200, 204)
        except Exception as e:
            print(f"[delete_live] LinkedIn error: {e}")
            deleted["linkedin"] = False

    # 2. Facebook (Page Feed or Photo post)
    fb_id = social_ids.get("facebook") or social_ids.get("facebook_post_id") or social_ids.get("fb")
    photo_id = social_ids.get("photo_id") or social_ids.get("facebook_photo_id")
    fb_url = social_ids.get("facebook_url") or (social_ids.get("live_post_url") if "facebook.com" in str(social_ids.get("live_post_url") or "") else "")
    fb_token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN") or os.getenv("FACEBOOK_TOKEN") or DEFAULT_FACEBOOK_PAGE_TOKEN
    page_id = os.getenv("FACEBOOK_PAGE_ID") or FACEBOOK_PAGE_ID
    if fb_token:
        deleted["facebook"] = False
        candidates = []
        if fb_id:
            fb_id_str = str(fb_id).strip()
            candidates.append(fb_id_str)
            if page_id and not fb_id_str.startswith(f"{page_id}_"):
                candidates.append(f"{page_id}_{fb_id_str}")
        if photo_id:
            photo_id_str = str(photo_id).strip()
            if photo_id_str not in candidates:
                candidates.append(photo_id_str)
        if fb_url and "fbid=" in fb_url:
            import re
            m_fb = re.search(r"fbid=([0-9]+)", fb_url)
            if m_fb and m_fb.group(1) not in candidates:
                candidates.append(m_fb.group(1))

        for cid in candidates:
            try:
                r = requests.delete(f"https://graph.facebook.com/v19.0/{cid}?access_token={fb_token}", timeout=10)
                if r.status_code == 200 and r.json().get("success", True) is True:
                    deleted["facebook"] = True
                    print(f"[delete_live] Facebook deleted post/photo {cid} successfully!")
                    break
                else:
                    print(f"[delete_live] Facebook delete on {cid} returned HTTP {r.status_code}: {r.text}")
            except Exception as e:
                print(f"[delete_live] Facebook error on {cid}: {e}")

        # Fallback: If not deleted and we have photo_id or fbid in URL, lookup parent post in published_posts
        if not deleted["facebook"] and page_id:
            lookup_fbid = photo_id or (candidates[0] if candidates else None)
            if not lookup_fbid and fb_url and "fbid=" in fb_url:
                import re
                m_fb = re.search(r"fbid=([0-9]+)", fb_url)
                if m_fb:
                    lookup_fbid = m_fb.group(1)

            if lookup_fbid:
                try:
                    r_posts = requests.get(f"https://graph.facebook.com/v19.0/{page_id}/published_posts?fields=id,attachments&limit=30&access_token={fb_token}", timeout=10)
                    if r_posts.status_code == 200:
                        for p in r_posts.json().get("data", []):
                            matched_post = False
                            for att in p.get("attachments", {}).get("data", []):
                                if str(att.get("target", {}).get("id")) == str(lookup_fbid) or str(lookup_fbid) in str(att.get("url", "")):
                                    matched_post = True
                                    break
                            if matched_post:
                                p_id = p["id"]
                                r_del = requests.delete(f"https://graph.facebook.com/v19.0/{p_id}?access_token={fb_token}", timeout=10)
                                if r_del.status_code == 200 and r_del.json().get("success", True) is True:
                                    deleted["facebook"] = True
                                    print(f"[delete_live] Facebook resolved photo {lookup_fbid} -> post {p_id} and deleted successfully!")
                                    break
                except Exception as e:
                    print(f"[delete_live] Facebook fallback lookup error: {e}")

    # 3. Instagram (Published Photo or Carousel Post)
    ig_id = social_ids.get("instagram") or social_ids.get("instagram_media_id") or social_ids.get("ig")
    ig_url = social_ids.get("instagram_url") or (social_ids.get("live_post_url") if "instagram.com" in str(social_ids.get("live_post_url") or "") else "")
    account_id = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID") or os.getenv("INSTAGRAM_ID") or INSTAGRAM_ACCOUNT_ID or DEFAULT_INSTAGRAM_ACCOUNT_ID
    ig_tokens = [t for t in [
        DEFAULT_FACEBOOK_PAGE_TOKEN,
        os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN"),
        os.getenv("INSTAGRAM_ACCESS_TOKEN"),
        os.getenv("FACEBOOK_TOKEN")
    ] if t and is_valid_token_value(t)]
    ig_tokens = list(dict.fromkeys(ig_tokens))

    deleted["instagram"] = False
    if ig_id and ig_tokens:
        ig_id_str = str(ig_id).strip()
        for token_candidate in ig_tokens:
            try:
                r = requests.delete(f"https://graph.facebook.com/v19.0/{ig_id_str}?access_token={token_candidate}", timeout=10)
                if r.status_code == 200 and r.json().get("success", True) is True:
                    deleted["instagram"] = True
                    print(f"[delete_live] Instagram media {ig_id_str} deleted successfully!")
                    break
                else:
                    print(f"[delete_live] Instagram delete HTTP {r.status_code}: {r.text}")
            except Exception as e:
                print(f"[delete_live] Instagram error: {e}")

    # Fallback: If not deleted yet, resolve shortcode from URL and delete
    if not deleted["instagram"] and ig_url and account_id and ig_tokens:
        import re
        m_sc = re.search(r"instagram\.com/(?:p|reel)/([A-Za-z0-9_-]+)", ig_url)
        if m_sc:
            shortcode = m_sc.group(1)
            token = ig_tokens[0]
            try:
                r_med = requests.get(f"https://graph.facebook.com/v19.0/{account_id}/media?fields=id,shortcode&limit=30&access_token={token}", timeout=10)
                if r_med.status_code == 200:
                    for item in r_med.json().get("data", []):
                        if item.get("shortcode") == shortcode:
                            mid = item["id"]
                            r_del = requests.delete(f"https://graph.facebook.com/v19.0/{mid}?access_token={token}", timeout=10)
                            if r_del.status_code == 200 and r_del.json().get("success", True) is True:
                                deleted["instagram"] = True
                                print(f"[delete_live] Instagram resolved shortcode {shortcode} -> media {mid} and deleted successfully!")
                                break
            except Exception as e:
                print(f"[delete_live] Instagram shortcode lookup error: {e}")

    # 4. YouTube (Promotional Comment)
    yt_id = social_ids.get("youtube") or social_ids.get("youtube_post_id") or social_ids.get("yt")
    if yt_id:
        yt_token, _ = get_youtube_access_token()
        if yt_token:
            try:
                headers = {"Authorization": f"Bearer {yt_token}"}
                yt_id_str = str(yt_id).strip()
                r = requests.delete(f"https://www.googleapis.com/youtube/v3/comments?id={yt_id_str}", headers=headers, timeout=10)
                deleted["youtube"] = r.status_code in (200, 204)
            except Exception as e:
                print(f"[delete_live] YouTube error: {e}")
                deleted["youtube"] = False

    # 5. X / Twitter (Delete Tweet)
    x_id = social_ids.get("x") or social_ids.get("x_post_id") or social_ids.get("twitter")
    if x_id:
        x_id_str = str(x_id).strip()
        api_key = os.getenv("X_API_KEY") or os.getenv("TWITTER_API_KEY")
        api_secret = os.getenv("X_API_SECRET") or os.getenv("TWITTER_API_SECRET")
        access_token = os.getenv("X_ACCESS_TOKEN") or os.getenv("TWITTER_ACCESS_TOKEN")
        access_token_secret = os.getenv("X_ACCESS_TOKEN_SECRET") or os.getenv("TWITTER_ACCESS_TOKEN_SECRET")
        deleted["x"] = False

        # Try OAuth 1.0a
        if all([api_key, api_secret, access_token, access_token_secret]):
            try:
                from requests_oauthlib import OAuth1
                auth = OAuth1(api_key, api_secret, access_token, access_token_secret)
                r = requests.delete(f"https://api.twitter.com/2/tweets/{x_id_str}", auth=auth, timeout=10)
                deleted["x"] = r.status_code in (200, 204)
            except Exception as e:
                print(f"[delete_live] X OAuth 1.0a error: {e}")

        # Try OAuth 2.0 Bearer token
        if not deleted["x"] and access_token:
            try:
                headers = {"Authorization": f"Bearer {access_token}"}
                r = requests.delete(f"https://api.twitter.com/2/tweets/{x_id_str}", headers=headers, timeout=10)
                if r.status_code in (200, 204):
                    deleted["x"] = True
                elif r.status_code == 401:
                    new_token = refresh_x_oauth2_token()
                    if new_token:
                        headers["Authorization"] = f"Bearer {new_token}"
                        r2 = requests.delete(f"https://api.twitter.com/2/tweets/{x_id_str}", headers=headers, timeout=10)
                        deleted["x"] = r2.status_code in (200, 204)
            except Exception as e:
                print(f"[delete_live] X OAuth 2.0 error: {e}")

    # 6. Pinterest (Delete Pin)
    pin_id = social_ids.get("pinterest") or social_ids.get("pinterest_post_id") or social_ids.get("pin")
    pin_token = os.getenv("PINTEREST_ACCESS_TOKEN") or os.getenv("PINTEREST_TOKEN")
    if pin_id and pin_token:
        try:
            headers = {"Authorization": f"Bearer {pin_token}"}
            r = requests.delete(f"https://api.pinterest.com/v5/pins/{pin_id}", headers=headers, timeout=10)
            deleted["pinterest"] = r.status_code in (200, 204)
        except Exception as e:
            print(f"[delete_live] Pinterest error: {e}")
            deleted["pinterest"] = False

    # 7. Threads (Delete Thread)
    th_id = social_ids.get("threads") or social_ids.get("threads_post_id") or social_ids.get("th")
    th_url = social_ids.get("threads_url") or (social_ids.get("live_post_url") if "threads.net" in str(social_ids.get("live_post_url") or "") else "")
    if not th_id and th_url and "threads.net" in th_url:
        import re
        m_th = re.search(r"threads\.net/(?:@[^/]+/post/|post/|t/)([0-9a-zA-Z_]+)", th_url)
        if m_th:
            th_id = m_th.group(1)

    th_token = os.getenv("THREADS_ACCESS_TOKEN") or os.getenv("THREADS_TOKEN") or DEFAULT_THREADS_ACCESS_TOKEN
    if th_id and th_token:
        try:
            r_th = requests.delete(f"https://graph.threads.net/v1.0/{th_id}?access_token={th_token}", timeout=10)
            if r_th.status_code in (200, 204) or (r_th.status_code == 200 and r_th.json().get("success") is True):
                deleted["threads"] = True
                print(f"[delete_live] Threads post {th_id} successfully deleted!")
            else:
                print(f"[delete_live] Threads delete returned HTTP {r_th.status_code}: {r_th.text}")
                deleted["threads"] = False
        except Exception as e:
            print(f"[delete_live] Threads delete exception: {e}")
            deleted["threads"] = False

    return deleted

if __name__ == "__main__":
    print("Testing publisher module...")
    test_payload = {
        "title": "SATIRE NEWS DIWALI TEST",
        "hook": "🚀 Exclusive Satire Breakdown!",
        "generated_content": "Testing automated social distribution system.",
        "call_to_action": "Watch now: https://www.youtube.com/watch?v=ic4HSdThFc8",
        "hashtags": "#Marketing #Testing #Automation",
        "video_url": "https://www.youtube.com/watch?v=ic4HSdThFc8"
    }
    res = dispatch_multi_channel_publish(test_payload)
    print("Publishing summary:", res)

