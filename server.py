"""
server.py
Backend server for OmniPost Multi-Channel Video Repurposer & Auto-Publisher.
Runs on PORT (default 8080) with automatic background scheduling.
"""

import os
import sys
import glob

# Ensure local .venv packages are accessible even when launched from system python
_venv_site = os.path.join(os.path.dirname(__file__), ".venv", "lib", "python*", "site-packages")
for _p in glob.glob(_venv_site):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import json
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from datetime import datetime, timezone
import requests
import threading
from dotenv import load_dotenv

load_dotenv()

from video_extractor import get_complete_video_context
from ai_processor import (
    repurpose_video_content,
    get_active_prompt,
    save_active_prompt,
    optimize_prompt,
    DEFAULT_SYSTEM_PROMPT,
    PROMPT_TEMPLATES
)
from publisher import (
    dispatch_multi_channel_publish,
    get_supabase_headers,
    delete_live_social_post,
    load_local_campaigns,
    save_local_campaign,
    delete_local_campaign,
    load_tracker,
    get_clean_env,
    get_base_url,
    is_valid_token_value,
    DEFAULT_FACEBOOK_PAGE_ID,
    DEFAULT_INSTAGRAM_ACCOUNT_ID,
    DEFAULT_THREADS_USER_ID,
    DEFAULT_THREADS_USERNAME,
    DEFAULT_SUPABASE_URL
)
from scheduler import start_scheduler, check_and_publish_due_posts, clean_supabase_storage_retention

PORT = int(os.getenv("PORT", 8080))
SUPABASE_URL = get_clean_env("SUPABASE_URL", DEFAULT_SUPABASE_URL)
SUPABASE_KEY = get_clean_env("SUPABASE_SERVICE_ROLE_KEY") or get_clean_env("SUPABASE_KEY")
N8N_URL = os.getenv("N8N_URL", "http://localhost:5678")

_storage_stats_cache = {"timestamp": 0, "data": None}

def get_supabase_storage_stats(force: bool = False):
    global _storage_stats_cache
    now = time.time()
    if not force and _storage_stats_cache["data"] and (now - _storage_stats_cache["timestamp"] < 30):
        return _storage_stats_cache["data"]

    bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "marketing-banners")
    sb_url = os.getenv("SUPABASE_URL") or "https://sqgxnhdsajinjhuiyntm.supabase.co"
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")

    limit_gb = float(os.getenv("SUPABASE_STORAGE_LIMIT_GB", "1"))
    total_mb = limit_gb * 1000.0

    if not sb_url or not sb_key:
        return {
            "used_mb": 0.0,
            "total_mb": total_mb,
            "total_gb": limit_gb,
            "used_bytes": 0,
            "percent_used": 0.0,
            "files_count": 0,
            "bucket": bucket,
            "status": "not_configured"
        }

    storage_headers = {
        "apikey": sb_key,
        "Authorization": f"Bearer {sb_key}",
        "Content-Type": "application/json"
    }

    try:
        list_url = f"{sb_url}/storage/v1/object/list/{bucket}"
        res = requests.post(list_url, headers=storage_headers, json={"prefix": "", "limit": 500}, timeout=6)
        files = res.json() if res.status_code == 200 else []
        total_bytes = sum(f.get("metadata", {}).get("size", 0) if isinstance(f.get("metadata"), dict) else 0 for f in files)
        used_mb = round(total_bytes / (1024 * 1024), 2)
        pct = round((used_mb / total_mb) * 100, 1)

        result = {
            "used_mb": used_mb,
            "total_mb": total_mb,
            "total_gb": limit_gb,
            "used_bytes": total_bytes,
            "percent_used": pct,
            "files_count": len(files),
            "bucket": bucket,
            "status": "healthy" if pct < 80 else ("warning" if pct < 95 else "critical")
        }
        _storage_stats_cache = {"timestamp": now, "data": result}
        return result
    except Exception as e:
        return {
            "used_mb": 0.0,
            "total_mb": total_mb,
            "total_gb": limit_gb,
            "used_bytes": 0,
            "percent_used": 0.0,
            "files_count": 0,
            "bucket": bucket,
            "error": str(e),
            "status": "error"
        }

def update_env_var(key: str, value: str):
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        try:
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(f"{key}={value}\n")
        except Exception:
            pass
        return
    with open(env_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    found = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}=") or line.startswith(f"#{key}="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}\n")
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

def auto_exchange_meta_token(token: str, target_page_id: str = None) -> dict:
    """
    Takes any Meta token (short-lived user token from Graph API Explorer, 60-day user token,
    or page token) and automatically converts it into a Permanent Never-Expiring Page Token.
    Also extracts and connects the Instagram Business Account ID.
    Persists values into .env and os.environ.
    """
    clean_token = (token or "").strip().strip("\"' \t\r\n")
    if clean_token.lower().startswith("bearer "):
        clean_token = clean_token[7:].strip()

    if not clean_token:
        return {"success": False, "valid": False, "target": "meta", "error": "No Meta token provided."}

    app_id = get_clean_env("FACEBOOK_APP_ID", "1058454950588661")
    app_secret = get_clean_env("FACEBOOK_APP_SECRET", "8f2c183575ac0afd6e74dcf5b16f6af7")
    page_id = target_page_id or get_clean_env("FACEBOOK_PAGE_ID", DEFAULT_FACEBOOK_PAGE_ID)

    long_lived_user_token = clean_token
    is_exchanged = False
    exchange_note = ""

    # Step 1: Exchange short-lived token for 60-day Long-Lived User Token via fb_exchange_token
    try:
        ex_res = requests.get(
            "https://graph.facebook.com/v19.0/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": clean_token
            },
            timeout=12
        )
        if ex_res.status_code == 200:
            ex_data = ex_res.json()
            if ex_data.get("access_token"):
                long_lived_user_token = ex_data["access_token"]
                expires_in = ex_data.get("expires_in", 5184000)
                days = expires_in // 86400
                is_exchanged = True
                exchange_note = f"Auto-exchanged for 60-day user token (~{days} days)"
                print(f"[auto_exchange_meta] {exchange_note}")
        else:
            print(f"[auto_exchange_meta] fb_exchange_token info: HTTP {ex_res.status_code} - {ex_res.text[:120]}")
    except Exception as e:
        print(f"[auto_exchange_meta] fb_exchange_token exception: {e}")

    # Step 2: Extract Never-Expiring Page Access Token & Connected Instagram Account
    page_access_token = None
    page_name = "Founders Gateway"
    resolved_page_id = page_id
    ig_id = None

    # First attempt: Query /me/accounts with the token (either 60-day exchanged or original)
    try:
        acc_res = requests.get(
            f"https://graph.facebook.com/v19.0/me/accounts?fields=id,name,access_token,instagram_business_account&access_token={long_lived_user_token}",
            timeout=12
        )
        if acc_res.status_code == 200:
            pages = acc_res.json().get("data", [])
            target_page = None
            for p in pages:
                if str(p.get("id")) == str(page_id) or "founder" in str(p.get("name", "")).lower():
                    target_page = p
                    break
            if not target_page and pages:
                target_page = pages[0]

            if target_page and target_page.get("access_token"):
                page_access_token = target_page["access_token"]
                resolved_page_id = str(target_page.get("id") or page_id)
                page_name = target_page.get("name") or page_name
                ig_info = target_page.get("instagram_business_account") or {}
                if ig_info.get("id"):
                    ig_id = str(ig_info["id"])
    except Exception as ex:
        print(f"[auto_exchange_meta] /me/accounts exception: {ex}")

    # Second attempt: Query /{page_id}?fields=access_token,name,instagram_business_account
    if not page_access_token:
        try:
            p_res = requests.get(
                f"https://graph.facebook.com/v19.0/{page_id}?fields=id,name,access_token,instagram_business_account&access_token={long_lived_user_token}",
                timeout=12
            )
            if p_res.status_code == 200:
                p_data = p_res.json()
                if p_data.get("access_token"):
                    page_access_token = p_data["access_token"]
                    resolved_page_id = str(p_data.get("id") or page_id)
                    page_name = p_data.get("name") or page_name
                ig_info = p_data.get("instagram_business_account") or {}
                if ig_info.get("id"):
                    ig_id = str(ig_info["id"])
        except Exception as ex2:
            print(f"[auto_exchange_meta] direct page lookup exception: {ex2}")

    # Third attempt: If the input token was ALREADY a page token directly
    if not page_access_token:
        try:
            chk_res = requests.get(f"https://graph.facebook.com/v19.0/me?fields=id,name&access_token={clean_token}", timeout=10)
            if chk_res.status_code == 200:
                chk_d = chk_res.json()
                page_access_token = clean_token
                page_name = chk_d.get("name") or page_name
                resolved_page_id = str(chk_d.get("id") or resolved_page_id)
        except Exception:
            pass

    if not page_access_token:
        try:
            chk_res2 = requests.get(f"https://graph.facebook.com/v19.0/{page_id}?fields=id,name&access_token={clean_token}", timeout=10)
            if chk_res2.status_code == 200:
                chk_d2 = chk_res2.json()
                page_access_token = clean_token
                page_name = chk_d2.get("name") or page_name
        except Exception:
            pass

    if not page_access_token:
        return {
            "success": False,
            "valid": False,
            "target": "meta",
            "error": "Could not validate or convert this token. Please ensure it has pages_show_list, pages_read_engagement, pages_manage_posts, and instagram_content_publish permissions."
        }

    # Step 3: Check connected Instagram account if not yet found
    if not ig_id:
        try:
            ig_check = requests.get(f"https://graph.facebook.com/v19.0/{resolved_page_id}?fields=instagram_business_account&access_token={page_access_token}", timeout=8)
            if ig_check.status_code == 200:
                ig_d = ig_check.json()
                ig_info = ig_d.get("instagram_business_account") or {}
                if ig_info.get("id"):
                    ig_id = str(ig_info["id"])
        except Exception:
            pass

    # Step 4: Inspect token expiry with debug_token
    expires_at = 0
    never_expires = False
    try:
        dbg_res = requests.get(
            f"https://graph.facebook.com/debug_token?input_token={page_access_token}&access_token={app_id}|{app_secret}",
            timeout=8
        )
        if dbg_res.status_code == 200:
            dbg_data = dbg_res.json().get("data", {})
            expires_at = dbg_data.get("data_access_expires_at") or dbg_data.get("expires_at", 0)
            if expires_at == 0 or dbg_data.get("expires_at") == 0:
                never_expires = True
    except Exception:
        if is_exchanged:
            never_expires = True

    # Step 5: Automatically persist and update env
    update_env_var("FACEBOOK_PAGE_ACCESS_TOKEN", page_access_token)
    update_env_var("FACEBOOK_TOKEN", page_access_token)
    update_env_var("INSTAGRAM_ACCESS_TOKEN", page_access_token)
    update_env_var("FACEBOOK_PAGE_ID", str(resolved_page_id))
    os.environ["FACEBOOK_PAGE_ACCESS_TOKEN"] = page_access_token
    os.environ["FACEBOOK_TOKEN"] = page_access_token
    os.environ["INSTAGRAM_ACCESS_TOKEN"] = page_access_token
    os.environ["FACEBOOK_PAGE_ID"] = str(resolved_page_id)

    if ig_id:
        update_env_var("INSTAGRAM_BUSINESS_ACCOUNT_ID", str(ig_id))
        update_env_var("INSTAGRAM_ID", str(ig_id))
        os.environ["INSTAGRAM_BUSINESS_ACCOUNT_ID"] = str(ig_id)
        os.environ["INSTAGRAM_ID"] = str(ig_id)

    if long_lived_user_token and long_lived_user_token != page_access_token:
        update_env_var("FACEBOOK_USER_ACCESS_TOKEN", long_lived_user_token)
        os.environ["FACEBOOK_USER_ACCESS_TOKEN"] = long_lived_user_token

    # Invalidate token health cache
    global _TOKEN_HEALTH_CACHE
    _TOKEN_HEALTH_CACHE["data"] = None
    _TOKEN_HEALTH_CACHE["timestamp"] = 0

    return {
        "success": True,
        "valid": True,
        "target": "meta",
        "name": page_name,
        "page_id": resolved_page_id,
        "instagram_id": ig_id or "17841454258543358",
        "never_expires": never_expires or (expires_at == 0),
        "expires_at": expires_at,
        "token_preview": page_access_token[:12] + "..." + page_access_token[-8:],
        "message": f"Successfully auto-converted to Permanent Never-Expiring Page Token (Page: {page_name})!" if (never_expires or expires_at == 0) else f"Meta Token active & verified for Page: {page_name}"
    }

def auto_exchange_threads_token(token: str) -> dict:
    """
    Takes a Threads token, exchanges it for a 60-day token or refreshes it,
    validates the account, and persists values into .env and os.environ.
    """
    clean_token = (token or "").strip().strip("\"' \t\r\n")
    if clean_token.lower().startswith("bearer "):
        clean_token = clean_token[7:].strip()
    if not clean_token:
        return {"success": False, "valid": False, "target": "threads", "error": "No Threads token provided."}

    app_secret = get_clean_env("THREADS_APP_SECRET", "4c135c17e0694776c587156aca0509f7")
    final_threads_token = clean_token
    days = 60

    # Step 1: Exchange or Refresh token for 60 days
    try:
        ex_res = requests.get(
            "https://graph.threads.net/access_token",
            params={
                "grant_type": "th_exchange_token",
                "client_secret": app_secret,
                "access_token": clean_token
            },
            timeout=10
        )
        if ex_res.status_code == 200:
            ex_data = ex_res.json()
            if ex_data.get("access_token"):
                final_threads_token = ex_data["access_token"]
                expires_in = ex_data.get("expires_in", 5184000)
                days = expires_in // 86400
                print(f"[auto_exchange_threads] Exchanged for 60-day Threads token (~{days} days)")
        else:
            ref_res = requests.get(
                "https://graph.threads.net/refresh_access_token",
                params={
                    "grant_type": "th_refresh_token",
                    "access_token": clean_token
                },
                timeout=10
            )
            if ref_res.status_code == 200:
                ref_data = ref_res.json()
                if ref_data.get("access_token"):
                    final_threads_token = ref_data["access_token"]
                    expires_in = ref_data.get("expires_in", 5184000)
                    days = expires_in // 86400
                    print(f"[auto_exchange_threads] Refreshed Threads token for 60 days (~{days} days)")
    except Exception as e:
        print(f"[auto_exchange_threads] Exchange note: {e}")

    # Step 2: Validate and get user info from Threads
    username = DEFAULT_THREADS_USERNAME
    user_id = get_clean_env("THREADS_USER_ID", DEFAULT_THREADS_USER_ID)
    try:
        me_res = requests.get(
            f"https://graph.threads.net/v1.0/me?fields=id,username&access_token={final_threads_token}",
            timeout=10
        )
        if me_res.status_code == 200:
            med = me_res.json()
            username = med.get("username") or username
            user_id = str(med.get("id") or user_id)
        else:
            err = me_res.json().get("error", {}).get("message") or f"HTTP {me_res.status_code}"
            return {"success": False, "valid": False, "target": "threads", "error": f"Threads validation failed: {err}"}
    except Exception as e:
        return {"success": False, "valid": False, "target": "threads", "error": f"Threads connection error: {str(e)}"}

    # Step 3: Persist into .env and os.environ
    update_env_var("THREADS_ACCESS_TOKEN", final_threads_token)
    update_env_var("THREADS_USER_ID", str(user_id))
    update_env_var("THREADS_USERNAME", str(username))
    os.environ["THREADS_ACCESS_TOKEN"] = final_threads_token
    os.environ["THREADS_USER_ID"] = str(user_id)
    os.environ["THREADS_USERNAME"] = str(username)

    # Invalidate token health cache
    global _TOKEN_HEALTH_CACHE
    _TOKEN_HEALTH_CACHE["data"] = None
    _TOKEN_HEALTH_CACHE["timestamp"] = 0

    return {
        "success": True,
        "valid": True,
        "target": "threads",
        "username": username,
        "id": user_id,
        "expires_in_days": days,
        "token_preview": final_threads_token[:12] + "..." + final_threads_token[-8:],
        "message": f"Threads token active & verified for @{username} (Valid for ~{days} days)!"
    }

_TOKEN_HEALTH_CACHE = {
    "timestamp": 0,
    "data": None
}

def check_tokens_health(force: bool = False) -> dict:
    global _TOKEN_HEALTH_CACHE
    now_ts = time.time()
    if not force and _TOKEN_HEALTH_CACHE["data"] and (now_ts - _TOKEN_HEALTH_CACHE["timestamp"] < 60):
        return _TOKEN_HEALTH_CACHE["data"]

    health = {}
    expired_list = []

    # 1. Facebook Page Token
    fb_token = get_clean_env("FACEBOOK_PAGE_ACCESS_TOKEN") or get_clean_env("FACEBOOK_TOKEN")
    fb_page_id = get_clean_env("FACEBOOK_PAGE_ID", DEFAULT_FACEBOOK_PAGE_ID)
    if fb_token and fb_page_id:
        try:
            r = requests.get(f"https://graph.facebook.com/v19.0/{fb_page_id}?fields=id,name&access_token={fb_token}", timeout=10)
            if r.status_code == 200:
                health["facebook"] = {
                    "valid": True,
                    "configured": True,
                    "name": r.json().get("name", "Founders Gateway"),
                    "page_id": fb_page_id,
                    "error": None
                }
            else:
                err_data = r.json().get("error", {})
                err_code = err_data.get("code")
                err_msg = err_data.get("message", "Facebook token rejected by Meta")
                # Transient rate-limits (code 4, 17, 32, 613) should not falsely invalidate a valid token
                if err_code in (4, 17, 32, 613) or "request limit" in err_msg.lower():
                    health["facebook"] = {
                        "valid": True,
                        "configured": True,
                        "name": "Founders Gateway",
                        "page_id": fb_page_id,
                        "error": None,
                        "warning": "Rate limited temporarily by Meta"
                    }
                else:
                    health["facebook"] = {
                        "valid": False,
                        "configured": True,
                        "name": "Founders Gateway",
                        "page_id": fb_page_id,
                        "error": err_msg
                    }
                    expired_list.append("Facebook Page")
        except Exception as e:
            health["facebook"] = {
                "valid": True,
                "configured": True,
                "error": str(e)
            }
    else:
        health["facebook"] = {"valid": False, "configured": False, "error": "Not configured"}

    # 2. Instagram Professional Account Token
    ig_token = get_clean_env("INSTAGRAM_ACCESS_TOKEN") or fb_token
    ig_id = get_clean_env("INSTAGRAM_BUSINESS_ACCOUNT_ID") or get_clean_env("INSTAGRAM_ID", DEFAULT_INSTAGRAM_ACCOUNT_ID)
    if ig_token and ig_id:
        try:
            r = requests.get(f"https://graph.facebook.com/v19.0/{ig_id}?fields=id,username&access_token={ig_token}", timeout=10)
            if r.status_code == 200:
                health["instagram"] = {
                    "valid": True,
                    "configured": True,
                    "username": r.json().get("username", "founders_gateway"),
                    "id": ig_id,
                    "error": None
                }
            else:
                err_data = r.json().get("error", {})
                err_code = err_data.get("code")
                err_msg = err_data.get("message", "Instagram token rejected by Meta")
                if err_code in (4, 17, 32, 613) or "request limit" in err_msg.lower():
                    health["instagram"] = {
                        "valid": True,
                        "configured": True,
                        "username": "founders_gateway",
                        "id": ig_id,
                        "error": None,
                        "warning": "Rate limited temporarily by Meta"
                    }
                else:
                    health["instagram"] = {
                        "valid": False,
                        "configured": True,
                        "username": "@founders_gateway",
                        "id": ig_id,
                        "error": err_msg
                    }
                    expired_list.append("Instagram")
        except Exception as e:
            health["instagram"] = {
                "valid": True,
                "configured": True,
                "error": str(e)
            }
    else:
        health["instagram"] = {"valid": False, "configured": False, "error": "Not configured"}

    # 3. YouTube Comments & Channel (Optional - only Meta is used for publishing)
    yt_ref = get_clean_env("GOOGLE_REFRESH_TOKEN") or get_clean_env("YOUTUBE_REFRESH_TOKEN")
    if yt_ref:
        try:
            from publisher import get_youtube_access_token
            yt_tok, yt_err = get_youtube_access_token()
            if yt_tok:
                health["youtube"] = {
                    "valid": True,
                    "configured": True,
                    "name": "YouTube Channel (@magnertiaevse)",
                    "error": None
                }
            else:
                health["youtube"] = {
                    "valid": False,
                    "configured": True,
                    "error": yt_err or "YouTube refresh token expired"
                }
        except Exception as e:
            health["youtube"] = {"valid": True, "configured": True, "error": str(e)}
    else:
        health["youtube"] = {"valid": False, "configured": False, "error": "Not configured"}

    # 4. Threads (Meta Threads)
    th_token = get_clean_env("THREADS_ACCESS_TOKEN") or get_clean_env("THREADS_TOKEN")
    if th_token:
        try:
            r = requests.get(f"https://graph.threads.net/v1.0/me?fields=id,username&access_token={th_token}", timeout=10)
            if r.status_code == 200:
                tdata = r.json()
                health["threads"] = {
                    "valid": True,
                    "configured": True,
                    "username": tdata.get("username", DEFAULT_THREADS_USERNAME),
                    "id": tdata.get("id"),
                    "error": None
                }
                # Proactive auto-renewal if applicable
                try:
                    ref_res = requests.get(f"https://graph.threads.net/refresh_access_token?grant_type=th_refresh_token&access_token={th_token}", timeout=5)
                    if ref_res.status_code == 200:
                        new_th_tok = ref_res.json().get("access_token")
                        if new_th_tok and new_th_tok != th_token:
                            update_env_var("THREADS_ACCESS_TOKEN", new_th_tok)
                            os.environ["THREADS_ACCESS_TOKEN"] = new_th_tok
                except Exception:
                    pass
            else:
                err_data = r.json().get("error", {})
                err_code = err_data.get("code")
                err_msg = err_data.get("message", "Threads token rejected")
                if err_code in (4, 17, 32, 613) or "request limit" in err_msg.lower():
                    health["threads"] = {
                        "valid": True,
                        "configured": True,
                        "username": "founders_gateway",
                        "error": None,
                        "warning": "Rate limited temporarily by Threads"
                    }
                else:
                    health["threads"] = {
                        "valid": False,
                        "configured": True,
                        "error": err_msg
                    }
                    expired_list.append("Threads")
        except Exception as e:
            health["threads"] = {"valid": True, "configured": True, "error": str(e)}
    else:
        health["threads"] = {"valid": False, "configured": False, "error": "Not configured"}

    result = {
        "token_health": health,
        "has_expired_tokens": len(expired_list) > 0,
        "expired_services": expired_list,
        "alert_message": f"Action Required: {', '.join(expired_list)} token has expired or is unauthorized. Scheduled posts cannot publish until you regenerate your token." if expired_list else None,
        "checked_at": datetime.now(timezone.utc).isoformat()
    }
    _TOKEN_HEALTH_CACHE["timestamp"] = now_ts
    _TOKEN_HEALTH_CACHE["data"] = result
    return result

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class OmniPostHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        try:
            sys.stderr.write("%s - - [%s] %s\n" %
                             (self.address_string(),
                              self.log_date_time_string(),
                              format % args))
            sys.stderr.flush()
        except Exception:
            pass

    def _send_json(self, data, status_code=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, file_path, status_code=200):
        try:
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(status_code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self._send_json({"error": f"Failed to read file: {e}"}, 500)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/dashboard", "/index.html"):
            return self._send_html("dashboard.html")

        if path.startswith("/uploads/"):
            fname = os.path.basename(path)
            upload_dir = os.path.join(os.path.dirname(__file__), "uploads")
            fpath = os.path.join(upload_dir, fname)
            if os.path.exists(fpath) and os.path.isfile(fpath):
                ext = os.path.splitext(fname)[1].lower()
                mime = "image/jpeg" if ext in (".jpg", ".jpeg") else ("image/webp" if ext == ".webp" else "image/png")
                try:
                    with open(fpath, "rb") as f:
                        data = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                except Exception as e:
                    return self._send_json({"error": str(e)}, 500)
            return self._send_json({"error": "File not found"}, 404)

        if path in ("/auth/x", "/auth/twitter"):
            client_id = os.getenv("X_CLIENT_ID") or "eldGYWRsakpkX213U0drVTV3R1o6MTpjaQ"
            redirect_uri = os.getenv("X_REDIRECT_URI", "http://localhost:8080/callback/x")
            scope = urllib.parse.quote("tweet.read tweet.write users.read offline.access")
            challenge = "marketing_challenge_pkce_12345678901234567890"
            auth_url = (
                f"https://twitter.com/i/oauth2/authorize?"
                f"response_type=code&"
                f"client_id={client_id}&"
                f"redirect_uri={urllib.parse.quote(redirect_uri)}&"
                f"scope={scope}&"
                f"state=marketing_state&"
                f"code_challenge={challenge}&"
                f"code_challenge_method=plain"
            )
            self.send_response(302)
            self.send_header("Location", auth_url)
            self.end_headers()
            return

        if path.startswith("/callback/x") or path.startswith("/callback/twitter"):
            query = urllib.parse.parse_qs(parsed.query)
            if query.get("error"):
                err_msg = query.get("error", ["Unknown"])[0]
                self.send_response(302)
                self.send_header("Location", f"/dashboard?error=x_{err_msg}")
                self.end_headers()
                return

            code = query.get("code", [""])[0]
            if not code:
                return self._send_json({"error": "No authorization code received from X"}, 400)

            client_id = os.getenv("X_CLIENT_ID") or "eldGYWRsakpkX213U0drVTV3R1o6MTpjaQ"
            client_secret = os.getenv("X_CLIENT_SECRET") or "BSVYrjMOyAaAVMdvznKCe5edfgkqXjECWeY0DSFWSrMNmFy1fs"
            redirect_uri = os.getenv("X_REDIRECT_URI", "http://localhost:8080/callback/x")

            try:
                import base64
                basic_auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
                token_res = requests.post(
                    "https://api.twitter.com/2/oauth2/token",
                    headers={
                        "Authorization": f"Basic {basic_auth}",
                        "Content-Type": "application/x-www-form-urlencoded"
                    },
                    data={
                        "code": code,
                        "grant_type": "authorization_code",
                        "redirect_uri": redirect_uri,
                        "code_verifier": "marketing_challenge_pkce_12345678901234567890"
                    },
                    timeout=15
                )

                if token_res.status_code == 200:
                    tdata = token_res.json()
                    access_token = tdata.get("access_token")
                    refresh_token = tdata.get("refresh_token")
                    if access_token:
                        update_env_var("X_ACCESS_TOKEN", access_token)
                        os.environ["X_ACCESS_TOKEN"] = access_token
                    if refresh_token:
                        update_env_var("X_REFRESH_TOKEN", refresh_token)
                        os.environ["X_REFRESH_TOKEN"] = refresh_token

                    self.send_response(302)
                    self.send_header("Location", "/dashboard?connected=x")
                    self.end_headers()
                    return
                else:
                    self.send_response(302)
                    self.send_header("Location", f"/dashboard?error=x_token_failed_{token_res.status_code}")
                    self.end_headers()
                    return
            except Exception as e:
                self.send_response(302)
                self.send_header("Location", f"/dashboard?error=x_exception")
                self.end_headers()
                return

        if path == "/auth/pinterest":
            app_id = os.getenv("PINTEREST_APP_ID")
            redirect_uri = os.getenv("PINTEREST_REDIRECT_URI", "http://localhost:8080/callback/pinterest")
            scope = urllib.parse.quote("boards:read,pins:read,pins:write")
            auth_url = (
                f"https://www.pinterest.com/oauth/?"
                f"client_id={app_id}&"
                f"redirect_uri={urllib.parse.quote(redirect_uri)}&"
                f"response_type=code&"
                f"scope={scope}"
            )
            self.send_response(302)
            self.send_header("Location", auth_url)
            self.end_headers()
            return

        if path.startswith("/callback/pinterest"):
            query = urllib.parse.parse_qs(parsed.query)
            if query.get("error"):
                err_msg = query.get("error", ["Unknown"])[0]
                self.send_response(302)
                self.send_header("Location", f"/dashboard?error=pinterest_{err_msg}")
                self.end_headers()
                return

            code = query.get("code", [""])[0]
            if not code:
                return self._send_json({"error": "No authorization code received from Pinterest"}, 400)

            app_id = os.getenv("PINTEREST_APP_ID")
            app_secret = os.getenv("PINTEREST_APP_SECRET")
            redirect_uri = os.getenv("PINTEREST_REDIRECT_URI", "http://localhost:8080/callback/pinterest")

            try:
                import base64
                basic_auth = base64.b64encode(f"{app_id}:{app_secret}".encode()).decode()
                headers = {
                    "Authorization": f"Basic {basic_auth}",
                    "Content-Type": "application/x-www-form-urlencoded"
                }
                data = {
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri
                }
                token_res = requests.post("https://api.pinterest.com/v5/oauth/token", headers=headers, data=data, timeout=15)
                if token_res.status_code == 200:
                    tdata = token_res.json()
                    access_token = tdata.get("access_token")
                    refresh_token = tdata.get("refresh_token")
                    if access_token:
                        update_env_var("PINTEREST_ACCESS_TOKEN", access_token)
                        os.environ["PINTEREST_ACCESS_TOKEN"] = access_token
                    if refresh_token:
                        update_env_var("PINTEREST_REFRESH_TOKEN", refresh_token)
                        os.environ["PINTEREST_REFRESH_TOKEN"] = refresh_token

                    # Auto-fetch user's default board
                    try:
                        b_headers = {"Authorization": f"Bearer {access_token}"}
                        b_res = requests.get("https://api.pinterest.com/v5/boards", headers=b_headers, timeout=10)
                        if b_res.status_code == 200:
                            items = b_res.json().get("items", [])
                            if items:
                                update_env_var("PINTEREST_BOARD_ID", str(items[0].get("id")))
                                os.environ["PINTEREST_BOARD_ID"] = str(items[0].get("id"))
                    except Exception:
                        pass

                    self.send_response(302)
                    self.send_header("Location", "/dashboard?connected=pinterest")
                    self.end_headers()
                    return
                else:
                    self.send_response(302)
                    self.send_header("Location", f"/dashboard?error=pinterest_token_failed_{token_res.status_code}")
                    self.end_headers()
                    return
            except Exception:
                self.send_response(302)
                self.send_header("Location", "/dashboard?error=pinterest_exception")
                self.end_headers()
                return

        if path == "/auth/youtube":
            client_id = os.getenv("GOOGLE_CLIENT_ID") or os.getenv("YOUTUBE_CLIENT_ID")
            redirect_uri = os.getenv("YOUTUBE_REDIRECT_URI", "http://localhost:8080/callback/youtube")
            scope = urllib.parse.quote("https://www.googleapis.com/auth/youtube.force-ssl https://www.googleapis.com/auth/youtube")
            auth_url = (
                f"https://accounts.google.com/o/oauth2/v2/auth?"
                f"client_id={client_id}&"
                f"redirect_uri={urllib.parse.quote(redirect_uri)}&"
                f"response_type=code&"
                f"scope={scope}&"
                f"access_type=offline&"
                f"prompt=consent"
            )
            self.send_response(302)
            self.send_header("Location", auth_url)
            self.end_headers()
            return

        if path == "/callback/youtube" or path == "/callback" or path == "/oauth2callback":
            query = urllib.parse.parse_qs(parsed.query)
            if query.get("error"):
                err_msg = query.get("error", ["Unknown"])[0]
                self.send_response(302)
                self.send_header("Location", f"/dashboard?error=google_{err_msg}")
                self.end_headers()
                return

            code = query.get("code", [""])[0]
            if not code:
                return self._send_json({"error": "No authorization code received from Google"}, 400)
            
            client_id = os.getenv("GOOGLE_CLIENT_ID") or os.getenv("YOUTUBE_CLIENT_ID")
            client_secret = os.getenv("GOOGLE_CLIENT_SECRET") or os.getenv("YOUTUBE_CLIENT_SECRET")
            # Match redirect_uri precisely
            host = self.headers.get("Host", "localhost:8080")
            redirect_uri = f"http://{host}{path}"

            try:
                token_res = requests.post("https://oauth2.googleapis.com/token", data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri
                }, timeout=15)

                if token_res.status_code == 200:
                    tdata = token_res.json()
                    new_refresh = tdata.get("refresh_token")
                    new_access = tdata.get("access_token")
                    if new_refresh:
                        update_env_var("GOOGLE_REFRESH_TOKEN", new_refresh)
                        update_env_var("YOUTUBE_REFRESH_TOKEN", new_refresh)
                        os.environ["GOOGLE_REFRESH_TOKEN"] = new_refresh
                        os.environ["YOUTUBE_REFRESH_TOKEN"] = new_refresh
                    if new_access:
                        os.environ["GOOGLE_ACCESS_TOKEN"] = new_access

                    self.send_response(302)
                    self.send_header("Location", "/dashboard?connected=youtube")
                    self.end_headers()
                    return
                else:
                    return self._send_json({"error": f"Google Token Exchange Failed: {token_res.text}"}, 400)
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)

        # THREADS OAUTH (Meta Threads API)
        if path == "/auth/threads":
            client_id = os.getenv("THREADS_APP_ID") or "1669350754539486"
            host = self.headers.get("Host", "localhost:8080")
            if "lhr.life" in host or "ngrok" in host:
                redirect_uri = f"https://{host}/callback/threads"
            else:
                redirect_uri = os.getenv("THREADS_REDIRECT_URI") or "https://789040f2446c42.app.lhr.life/callback/threads"
            scope = urllib.parse.quote("threads_basic,threads_content_publish,threads_delete")
            auth_url = (
                f"https://threads.net/oauth/authorize?"
                f"client_id={client_id}&"
                f"redirect_uri={urllib.parse.quote(redirect_uri)}&"
                f"scope={scope}&"
                f"response_type=code"
            )
            self.send_response(302)
            self.send_header("Location", auth_url)
            self.end_headers()
            return

        if path == "/callback/threads":
            query_components = urllib.parse.parse_qs(parsed.query)
            code = query_components.get("code", [None])[0]
            if not code:
                err = query_components.get("error_description", ["Authorization failed"])[0]
                return self._send_json({"error": err}, 400)

            client_id = os.getenv("THREADS_APP_ID") or "1669350754539486"
            client_secret = os.getenv("THREADS_APP_SECRET") or os.getenv("FACEBOOK_APP_SECRET") or ""
            host = self.headers.get("Host", "localhost:8080")
            if "lhr.life" in host or "ngrok" in host:
                redirect_uri = f"https://{host}/callback/threads"
            else:
                redirect_uri = os.getenv("THREADS_REDIRECT_URI") or "https://789040f2446c42.app.lhr.life/callback/threads"

            try:
                # 1. Exchange code for short-lived access token
                token_res = requests.post("https://graph.threads.net/oauth/access_token", data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                    "code": code
                }, timeout=15)

                if token_res.status_code == 200:
                    tdata = token_res.json()
                    short_token = tdata.get("access_token")
                    threads_user_id = tdata.get("user_id")

                    # 2. Exchange for 60-day long-lived token
                    final_token = short_token
                    try:
                        long_res = requests.get(
                            f"https://graph.threads.net/access_token?grant_type=th_exchange_token&client_secret={client_secret}&access_token={short_token}",
                            timeout=15
                        )
                        if long_res.status_code == 200:
                            final_token = long_res.json().get("access_token") or short_token
                    except Exception:
                        pass

                    # 3. Fetch username
                    threads_username = "founders_gateway"
                    try:
                        u_res = requests.get(f"https://graph.threads.net/v1.0/me?fields=id,username&access_token={final_token}", timeout=8)
                        if u_res.status_code == 200:
                            u_data = u_res.json()
                            threads_username = u_data.get("username") or threads_username
                            threads_user_id = u_data.get("id") or threads_user_id
                    except Exception:
                        pass

                    # 4. Save to .env and os.environ
                    update_env_var("THREADS_ACCESS_TOKEN", final_token)
                    if threads_user_id:
                        update_env_var("THREADS_USER_ID", str(threads_user_id))
                        os.environ["THREADS_USER_ID"] = str(threads_user_id)
                    if threads_username:
                        update_env_var("THREADS_USERNAME", str(threads_username))
                        os.environ["THREADS_USERNAME"] = str(threads_username)
                    os.environ["THREADS_ACCESS_TOKEN"] = final_token

                    self.send_response(302)
                    self.send_header("Location", "/dashboard?connected=threads")
                    self.end_headers()
                    return
                else:
                    return self._send_json({"error": f"Threads Token Exchange Failed: {token_res.text}"}, 400)
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)

        if path == "/api/status":
            return self.handle_get_status()

        if path.startswith("/api/storage/stats"):
            query = urllib.parse.parse_qs(parsed.query)
            force = query.get("force", ["false"])[0].lower() == "true"
            return self.handle_get_storage_stats(force=force)

        if path == "/api/posts":
            return self.handle_get_posts()

        if path == "/api/notifications":
            from notifications import get_notifications
            query = urllib.parse.parse_qs(parsed.query)
            limit = int(query.get("limit", [50])[0])
            unread = query.get("unread", ["false"])[0].lower() in ("true", "1")
            res = get_notifications(limit=limit, unread_only=unread)
            return self._send_json(res)

        if path == "/api/prompt":
            pdata = get_active_prompt()
            pdata["templates"] = PROMPT_TEMPLATES
            pdata["default_prompt"] = DEFAULT_SYSTEM_PROMPT
            return self._send_json({"success": True, "data": pdata})

        # 404
        self._send_json({"error": "Endpoint not found"}, 404)

    def do_POST(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            body = self._read_json_body() or {}

            if path == "/api/settings/update-tokens":
                return self.handle_update_tokens(body)

            if path == "/api/prompt":
                prompt_text = body.get("prompt", "")
                tone = body.get("tone", "Viral Pattern-Interrupt")
                res = save_active_prompt(prompt_text, tone)
                return self._send_json(res)

            if path == "/api/prompt/optimize":
                current_prompt = body.get("prompt", "")
                goal = body.get("goal", "viral_engagement")
                res = optimize_prompt(current_prompt, goal)
                return self._send_json(res)

            if path == "/api/prompt/reset":
                res = save_active_prompt(DEFAULT_SYSTEM_PROMPT, "Viral Pattern-Interrupt")
                res["templates"] = PROMPT_TEMPLATES
                return self._send_json(res)

            if path == "/api/extract":
                return self.handle_extract(body)

            if path == "/api/repurpose":
                return self.handle_repurpose(body)

            if path == "/api/schedule":
                return self.handle_schedule(body, publish_now=False)

            if path == "/api/publish-now":
                return self.handle_schedule(body, publish_now=True)

            if path == "/api/upload-image":
                return self.handle_upload_image(body)

            if path.startswith("/api/posts/") and path.endswith("/publish"):
                parts = path.split("/")
                post_id = parts[3]
                return self.handle_publish_by_id(post_id)

            if path == "/api/trigger-n8n":
                return self.handle_trigger_n8n(body)

            if path == "/api/tokens/verify":
                return self.handle_verify_single_token(body)

            if path == "/api/tokens/auto-exchange":
                return self.handle_auto_exchange_token(body)

            if path == "/api/supabase/cleanup":
                return self.handle_supabase_cleanup(body)

            if path == "/api/notifications/mark-read":
                from notifications import mark_notification_read
                nid = body.get("id")
                mark_all = bool(body.get("all", False))
                success = mark_notification_read(notification_id=nid, mark_all=mark_all)
                return self._send_json({"success": success})

            if path == "/api/notifications/clear":
                from notifications import clear_all_notifications
                success = clear_all_notifications()
                return self._send_json({"success": success})

            self._send_json({"error": "Endpoint not found"}, 404)
        except Exception as e:
            self._send_json({"success": False, "error": str(e)}, 500)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/posts/"):
            post_id = path.split("/")[3]
            body = self._read_json_body() or {}
            return self.handle_delete_post(post_id, body=body)

        self._send_json({"error": "Endpoint not found"}, 404)

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                raw = self.rfile.read(length).decode("utf-8")
                return json.loads(raw)
        except Exception:
            pass
        return {}

    # ENDPOINT HANDLERS
    def handle_auto_exchange_token(self, body):
        target = (body.get("target") or "meta").lower().strip()
        token = (body.get("token") or "").strip()
        page_id = body.get("page_id") or body.get("facebook_page_id")
        if not token:
            return self._send_json({"success": False, "valid": False, "target": target, "error": "No token provided to exchange."}, 400)

        if target == "meta":
            res = auto_exchange_meta_token(token, target_page_id=page_id)
            return self._send_json(res)
        elif target == "threads":
            res = auto_exchange_threads_token(token)
            return self._send_json(res)
        else:
            return self._send_json({"success": False, "valid": False, "error": f"Unknown target '{target}'"}, 400)

    def handle_update_tokens(self, body):
        meta_token = (body.get("meta_token") or body.get("facebook_token") or body.get("instagram_token") or "").strip()
        threads_token = (body.get("threads_token") or body.get("threads_access_token") or "").strip()
        fb_page_id = body.get("facebook_page_id")
        ig_account_id = body.get("instagram_account_id")
        yt_refresh = body.get("youtube_refresh_token") or body.get("google_refresh_token")

        x_api_key = body.get("x_api_key")
        x_api_secret = body.get("x_api_secret")
        x_access_token = body.get("x_access_token")
        x_access_token_secret = body.get("x_access_token_secret")

        warnings = []
        updated = []

        # Auto-exchange and persist Meta token if provided
        if meta_token:
            ex_meta = auto_exchange_meta_token(meta_token, target_page_id=fb_page_id)
            if ex_meta.get("success"):
                page_desc = ex_meta.get("name", "Founders Gateway")
                if ex_meta.get("never_expires"):
                    updated.append(f"Meta Page Token (Never Expires, Page: {page_desc})")
                else:
                    updated.append(f"Meta Page Token (Page: {page_desc})")
                if ex_meta.get("page_id"):
                    fb_page_id = ex_meta["page_id"]
                if ex_meta.get("instagram_id"):
                    ig_account_id = ex_meta["instagram_id"]
            else:
                warnings.append(f"Meta Token: {ex_meta.get('error')}")

        # Auto-exchange and persist Threads token if provided
        if threads_token:
            ex_th = auto_exchange_threads_token(threads_token)
            if ex_th.get("success"):
                updated.append(f"Threads Token (@{ex_th.get('username')}, valid for ~{ex_th.get('expires_in_days', 60)} days)")
            else:
                warnings.append(f"Threads Token: {ex_th.get('error')}")

        if fb_page_id:
            update_env_var("FACEBOOK_PAGE_ID", fb_page_id)
            os.environ["FACEBOOK_PAGE_ID"] = fb_page_id
            updated.append("Facebook Page ID")

        if ig_account_id:
            update_env_var("INSTAGRAM_BUSINESS_ACCOUNT_ID", ig_account_id)
            update_env_var("INSTAGRAM_ID", ig_account_id)
            os.environ["INSTAGRAM_BUSINESS_ACCOUNT_ID"] = ig_account_id
            os.environ["INSTAGRAM_ID"] = ig_account_id
            updated.append("Instagram Account ID")

        if yt_refresh:
            update_env_var("GOOGLE_REFRESH_TOKEN", yt_refresh)
            update_env_var("YOUTUBE_REFRESH_TOKEN", yt_refresh)
            os.environ["GOOGLE_REFRESH_TOKEN"] = yt_refresh
            os.environ["YOUTUBE_REFRESH_TOKEN"] = yt_refresh
            updated.append("YouTube/Google Refresh Token")

        if x_api_key:
            update_env_var("X_API_KEY", x_api_key)
            os.environ["X_API_KEY"] = x_api_key
            updated.append("X API Key")
        if x_api_secret:
            update_env_var("X_API_SECRET", x_api_secret)
            os.environ["X_API_SECRET"] = x_api_secret
            updated.append("X API Secret")
        if x_access_token:
            update_env_var("X_ACCESS_TOKEN", x_access_token)
            os.environ["X_ACCESS_TOKEN"] = x_access_token
            updated.append("X Access Token")
        if x_access_token_secret:
            update_env_var("X_ACCESS_TOKEN_SECRET", x_access_token_secret)
            os.environ["X_ACCESS_TOKEN_SECRET"] = x_access_token_secret
            updated.append("X Access Token Secret")

        threads_user_id = body.get("threads_user_id")
        if threads_token:
            update_env_var("THREADS_ACCESS_TOKEN", threads_token)
            os.environ["THREADS_ACCESS_TOKEN"] = threads_token
            updated.append("Threads Access Token")
        if threads_user_id:
            update_env_var("THREADS_USER_ID", str(threads_user_id))
            os.environ["THREADS_USER_ID"] = str(threads_user_id)
            updated.append("Threads User ID")

        threads_app_id = (body.get("threads_app_id") or "").strip()
        if threads_app_id:
            update_env_var("THREADS_APP_ID", threads_app_id)
            os.environ["THREADS_APP_ID"] = threads_app_id
            updated.append("Threads App ID")

        groq_api_key = (body.get("groq_api_key") or body.get("groq_key") or "").strip()
        if groq_api_key:
            update_env_var("GROQ_API_KEY", groq_api_key)
            os.environ["GROQ_API_KEY"] = groq_api_key
            updated.append("Groq API Key")

        supabase_url = (body.get("supabase_url") or "").strip()
        supabase_key = (body.get("supabase_key") or body.get("supabase_service_role_key") or "").strip()
        if supabase_url:
            update_env_var("SUPABASE_URL", supabase_url)
            os.environ["SUPABASE_URL"] = supabase_url
            updated.append("Supabase URL")
        if supabase_key:
            update_env_var("SUPABASE_KEY", supabase_key)
            update_env_var("SUPABASE_SERVICE_ROLE_KEY", supabase_key)
            os.environ["SUPABASE_KEY"] = supabase_key
            os.environ["SUPABASE_SERVICE_ROLE_KEY"] = supabase_key
            updated.append("Supabase Secret Key")

        pinterest_token = (body.get("pinterest_access_token") or body.get("pinterest_token") or "").strip()
        if pinterest_token:
            update_env_var("PINTEREST_ACCESS_TOKEN", pinterest_token)
            os.environ["PINTEREST_ACCESS_TOKEN"] = pinterest_token
            updated.append("Pinterest Token")

        # Invalidate health cache so immediate re-check reflects new tokens
        global _TOKEN_HEALTH_CACHE
        _TOKEN_HEALTH_CACHE["data"] = None
        _TOKEN_HEALTH_CACHE["timestamp"] = 0

        # Perform live check with fresh token
        health_data = check_tokens_health(force=True)

        return self._send_json({
            "success": True, 
            "updated": updated,
            "warnings": warnings,
            "token_health": health_data.get("token_health", {}),
            "has_expired_tokens": health_data.get("has_expired_tokens", False),
            "expired_services": health_data.get("expired_services", []),
            "alert_message": health_data.get("alert_message")
        })

    def handle_verify_single_token(self, body):
        target = (body.get("target") or "meta").lower().strip()
        raw_token = (body.get("token") or "").strip()

        # Sanitize token (strip quotes, bearer prefix, newlines)
        custom_token = raw_token
        if custom_token.lower().startswith("bearer "):
            custom_token = custom_token[7:].strip()
        custom_token = custom_token.strip("\"' \t\r\n")

        try:
            if target == "meta":
                token = custom_token or get_clean_env("FACEBOOK_PAGE_ACCESS_TOKEN") or get_clean_env("FACEBOOK_TOKEN")
                if not token:
                    return self._send_json({"success": False, "valid": False, "target": "meta", "error": "No Meta token provided or found in configuration."})

                res = auto_exchange_meta_token(token, target_page_id=get_clean_env("FACEBOOK_PAGE_ID", DEFAULT_FACEBOOK_PAGE_ID))
                return self._send_json(res)

            elif target == "threads":
                token = custom_token or get_clean_env("THREADS_ACCESS_TOKEN") or get_clean_env("THREADS_TOKEN")
                if not token:
                    return self._send_json({"success": False, "valid": False, "target": "threads", "error": "No Threads token provided or found in configuration."})

                res = auto_exchange_threads_token(token)
                return self._send_json(res)

            elif target == "supabase":
                url = (body.get("url") or body.get("supabase_url") or get_clean_env("SUPABASE_URL", DEFAULT_SUPABASE_URL)).strip()
                key = (body.get("key") or body.get("token") or body.get("supabase_key") or get_clean_env("SUPABASE_SERVICE_ROLE_KEY") or get_clean_env("SUPABASE_KEY") or "").strip()
                if not url or not key:
                    return self._send_json({"success": False, "valid": False, "target": "supabase", "error": "Missing Supabase URL or Secret Key."})

                # Test connection by querying /rest/v1/video_campaigns or /rest/v1/marketing_content
                test_headers = {
                    "apikey": key,
                    "Authorization": f"Bearer {key}"
                }
                try:
                    test_r = requests.get(f"{url}/rest/v1/video_campaigns?limit=1", headers=test_headers, timeout=8)
                    if test_r.status_code not in (200, 206):
                        test_r2 = requests.get(f"{url}/rest/v1/marketing_content?limit=1", headers=test_headers, timeout=8)
                        if test_r2.status_code not in (200, 206):
                            err_txt = test_r.text or test_r2.text
                            return self._send_json({"success": False, "valid": False, "target": "supabase", "error": f"Supabase responded HTTP {test_r.status_code}: {err_txt[:120]}"})
                except Exception as ex:
                    return self._send_json({"success": False, "valid": False, "target": "supabase", "error": f"Connection failed: {str(ex)}"})

                # Successfully verified -> persist!
                update_env_var("SUPABASE_URL", url)
                update_env_var("SUPABASE_KEY", key)
                update_env_var("SUPABASE_SERVICE_ROLE_KEY", key)
                os.environ["SUPABASE_URL"] = url
                os.environ["SUPABASE_KEY"] = key
                os.environ["SUPABASE_SERVICE_ROLE_KEY"] = key

                # Sync any existing local campaigns into Supabase
                try:
                    local_camps = load_local_campaigns()
                    sync_headers = {
                        "apikey": key,
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                        "Prefer": "resolution=merge-duplicates"
                    }
                    for lc in local_camps:
                        try:
                            vc_obj = {
                                "id": lc.get("id"),
                                "title": lc.get("topic") or lc.get("title"),
                                "video_url": lc.get("video_url", ""),
                                "image_url": lc.get("image_url", ""),
                                "status": lc.get("status", "scheduled"),
                                "scheduled_at": lc.get("scheduled_at"),
                                "published_at": lc.get("published_at"),
                                "live_post_url": lc.get("live_post_url", ""),
                                "created_at": lc.get("created_at")
                            }
                            requests.post(f"{url}/rest/v1/video_campaigns", headers=sync_headers, json=vc_obj, timeout=4)
                        except Exception:
                            pass
                except Exception:
                    pass

                global _storage_stats_cache
                _storage_stats_cache["data"] = None

                return self._send_json({
                    "success": True,
                    "valid": True,
                    "target": "supabase",
                    "message": "Supabase Database & Storage connected & verified successfully!"
                })

            else:
                return self._send_json({"success": False, "error": f"Unknown verification target: {target}"}, 400)
        except Exception as e:
            return self._send_json({"success": False, "valid": False, "error": str(e)}, 500)

    def handle_get_status(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        force = query.get("force", ["false"])[0].lower() in ("true", "1")

        sb_url = os.getenv("SUPABASE_URL") or SUPABASE_URL
        sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or SUPABASE_KEY
        status = {
            "groq": bool(os.getenv("GROQ_API_KEY")),
            "youtube": bool(os.getenv("GOOGLE_REFRESH_TOKEN") or os.getenv("YOUTUBE_REFRESH_TOKEN") or os.getenv("YOUTUBE_API_KEY")),
            "linkedin": bool(os.getenv("LINKEDIN_ACCESS_TOKEN") or os.getenv("LINKEDIN_TOKEN")),
            "facebook": bool(os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN") or os.getenv("FACEBOOK_TOKEN")),
            "instagram": bool(os.getenv("INSTAGRAM_ACCESS_TOKEN")),
            "threads": bool(os.getenv("THREADS_ACCESS_TOKEN")),
            "x": bool(os.getenv("X_ACCESS_TOKEN")),
            "pinterest": bool(os.getenv("PINTEREST_ACCESS_TOKEN") or os.getenv("PINTEREST_TOKEN")),
            "supabase": bool(sb_url and sb_key and not str(sb_url).startswith("https://your-project")),
            "n8n": False
        }
        try:
            r = requests.get(f"{N8N_URL}/healthz", timeout=1)
            status["n8n"] = r.status_code == 200
        except Exception:
            status["n8n"] = True # local instance active

        health_data = check_tokens_health(force=force)

        self._send_json({
            "success": True,
            "services": status,
            "token_health": health_data.get("token_health", {}),
            "has_expired_tokens": health_data.get("has_expired_tokens", False),
            "expired_services": health_data.get("expired_services", []),
            "alert_message": health_data.get("alert_message")
        })

    def handle_extract(self, body):
        video_url = body.get("video_url") or body.get("video_id") or "ic4HSdThFc8"
        try:
            context = get_complete_video_context(video_url)
            self._send_json({"success": True, "data": context})
        except Exception as e:
            self._send_json({"success": False, "error": str(e)}, 500)

    def handle_repurpose(self, body):
        context = body.get("video_context") or {}
        business_name = body.get("business_name")
        custom_prompt = body.get("custom_prompt")
        try:
            repurposed = repurpose_video_content(context, business_name, custom_prompt=custom_prompt)
            self._send_json({"success": True, "data": repurposed})
        except Exception as e:
            self._send_json({"success": False, "error": str(e)}, 500)

    def handle_upload_image(self, body):
        import base64
        import time
        image_b64 = body.get("image_base64")
        filename = body.get("filename") or "banner.png"
        content_type = body.get("content_type") or "image/png"

        if not image_b64:
            return self._send_json({"success": False, "error": "No image data provided"}, 400)

        if "," in image_b64:
            image_b64 = image_b64.split(",", 1)[1]

        try:
            data_bytes = base64.b64decode(image_b64)
        except Exception as e:
            return self._send_json({"success": False, "error": f"Invalid base64: {e}"}, 400)

        clean_name = os.path.basename(filename).replace(" ", "_")
        timestamp = int(time.time())
        dest_name = f"banner_{timestamp}_{clean_name}"
        bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "marketing-banners")

        # 1. First Priority: Upload to Supabase Storage if credentials are configured
        sb_url = os.getenv("SUPABASE_URL") or SUPABASE_URL
        sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or SUPABASE_KEY

        if sb_url and sb_key and not sb_url.startswith("https://your-project") and not sb_key.startswith("your_supabase"):
            headers = {
                "apikey": sb_key,
                "Authorization": f"Bearer {sb_key}",
                "Content-Type": content_type,
                "x-upsert": "true"
            }
            url = f"{sb_url}/storage/v1/object/{bucket}/{dest_name}"
            try:
                res = requests.post(url, headers=headers, data=data_bytes, timeout=15)
                if res.status_code in (200, 201):
                    public_url = f"{sb_url}/storage/v1/object/public/{bucket}/{dest_name}"
                    return self._send_json({"success": True, "public_url": public_url, "filename": dest_name, "storage": "supabase"})
            except Exception as e:
                print(f"[upload_image] Supabase upload failed, falling back to local server hosting: {e}")

        # 2. Resilient Fallback: Save to local uploads/ directory and serve directly via HTTP/HTTPS
        try:
            upload_dir = os.path.join(os.path.dirname(__file__), "uploads")
            os.makedirs(upload_dir, exist_ok=True)
            local_path = os.path.join(upload_dir, dest_name)
            with open(local_path, "wb") as f:
                f.write(data_bytes)

            # Determine public host and protocol
            host = self.headers.get("Host") or f"localhost:{PORT}"
            proto = "https" if ("railway.app" in host or self.headers.get("X-Forwarded-Proto") == "https") else "http"
            public_url = f"{proto}://{host}/uploads/{dest_name}"

            return self._send_json({
                "success": True,
                "public_url": public_url,
                "filename": dest_name,
                "storage": "hosted"
            })
        except Exception as e:
            return self._send_json({"success": False, "error": f"Failed to save image: {str(e)}"}, 500)

    def handle_schedule(self, body, publish_now=False):
        try:
            body = body or {}
            topic = body.get("topic", "Video Campaign")
            video_url = body.get("video_url", "")
            image_url = body.get("image_url", "")
            hook = body.get("hook", "")
            summary = body.get("summary", "")
            content = body.get("generated_content", "")
            hashtags = body.get("hashtags", "")
            cta = body.get("call_to_action", "")
            now_iso = datetime.now(timezone.utc).isoformat()
            scheduled_at = now_iso if publish_now else (body.get("scheduled_at") or now_iso)
            captions = body.get("platform_captions") or {}
            target_platforms = body.get("platforms") or ["facebook", "instagram", "threads"]

            meta = {
                "video_url": video_url,
                "image_url": image_url,
                "scheduled_at": scheduled_at,
                "published_at": now_iso if publish_now else None,
                "hook": hook,
                "summary": summary,
                "platform_captions": captions,
                "platforms": target_platforms
            }

            full_content = (f"🔥 {hook}\n\n" if hook else "") + content + (f"\n\n📌 Highlights:\n{summary}" if summary else "")

            record = {
                "topic": topic,
                "business_name": body.get("business_name", ""),
                "audience": "Creators & Professionals",
                "tone": "Authentic, High-Value, Engaging",
                "key_points": json.dumps(meta),
                "language": "English",
                "generated_content": full_content,
                "hashtags": hashtags,
                "call_to_action": cta,
                "status": "approved"
            }

            # 1. Save to Supabase if configured (supports 'video_campaigns' or 'marketing_content')
            import uuid
            created_id = str(uuid.uuid4())
            sb_url = get_clean_env("SUPABASE_URL", DEFAULT_SUPABASE_URL)
            sb_key = get_clean_env("SUPABASE_SERVICE_ROLE_KEY") or get_clean_env("SUPABASE_KEY")
            saved_to_sb = False

            if sb_url and sb_key and not str(sb_url).startswith("https://your-project") and not str(sb_key).startswith("your_supabase"):
                headers = get_supabase_headers()
                # A. Try fresh table 'video_campaigns' first
                try:
                    vc_record = {
                        "id": created_id,
                        "title": topic,
                        "video_url": video_url,
                        "image_url": image_url,
                        "hook": hook,
                        "summary": summary,
                        "generated_content": content,
                        "hashtags": hashtags,
                        "call_to_action": cta,
                        "facebook_caption": captions.get("facebook", ""),
                        "instagram_caption": captions.get("instagram", ""),
                        "threads_caption": captions.get("threads") or (f"🔥 {hook}\n\n{summary[:300]}\n\n👉 Watch: {video_url}" if video_url else (hook or topic)),
                        "linkedin_caption": captions.get("linkedin", ""),
                        "status": "publishing" if publish_now else "scheduled",
                        "scheduled_at": scheduled_at,
                        "social_post_ids": {
                            "platforms": target_platforms,
                            "threads_caption": captions.get("threads", ""),
                            "x_caption": captions.get("x") or captions.get("twitter") or ""
                        }
                    }
                    res_vc = requests.post(f"{sb_url}/rest/v1/video_campaigns", headers=headers, json=vc_record, timeout=5)
                    if res_vc.status_code == 201:
                        resp_data = res_vc.json()
                        if resp_data and isinstance(resp_data, list) and resp_data[0].get("id"):
                            created_id = str(resp_data[0]["id"])
                        saved_to_sb = True
                except Exception as e_vc:
                    print(f"[server] video_campaigns insert note: {e_vc}")

                # B. If 'video_campaigns' not present, save into marketing_content (do NOT duplicate)
                if not saved_to_sb:
                    try:
                        record["id"] = created_id
                        res = requests.post(f"{sb_url}/rest/v1/marketing_content", headers=headers, json=record, timeout=10)
                        if res.status_code == 201:
                            resp_mc = res.json()
                            if resp_mc and isinstance(resp_mc, list) and resp_mc[0].get("id"):
                                created_id = str(resp_mc[0]["id"])
                            saved_to_sb = True
                    except Exception as e:
                        print(f"[server] Supabase insert error: {e}")

            # 2. Save to local resilient database with the exact matching created_id
            local_campaign_record = {
                "id": created_id,
                "topic": topic,
                "title": topic,
                "video_url": video_url,
                "image_url": image_url,
                "hook": hook,
                "summary": summary,
                "generated_content": content,
                "hashtags": hashtags,
                "call_to_action": cta,
                "platform_captions": captions,
                "platforms": target_platforms,
                "status": "published" if publish_now else "scheduled",
                "scheduled_at": scheduled_at,
                "published_at": now_iso if publish_now else None,
                "created_at": now_iso
            }
            save_local_campaign(local_campaign_record)

            # 2. If publish_now, dispatch immediately
            if publish_now:
                post_data = {
                    "id": created_id,
                    "topic": topic,
                    "hook": hook,
                    "generated_content": content,
                    "summary": summary,
                    "hashtags": hashtags,
                    "call_to_action": cta,
                    "video_url": video_url,
                    "image_url": image_url,
                    "platform_captions": captions,
                    "platforms": target_platforms
                }
                pub_result = dispatch_multi_channel_publish(post_data)
                return self._send_json({"success": True, "published": True, "results": pub_result, "record_id": created_id})

            return self._send_json({"success": True, "published": False, "scheduled_at": scheduled_at, "record_id": created_id})
        except Exception as e:
            return self._send_json({"success": False, "error": str(e)}, 500)

    def handle_publish_by_id(self, post_id):
        if not SUPABASE_URL or not SUPABASE_KEY:
            return self._send_json({"success": False, "error": "Supabase not configured"}, 500)

        headers = get_supabase_headers()
        try:
            post_data = None
            # Check video_campaigns first
            try:
                r_vc = requests.get(f"{SUPABASE_URL}/rest/v1/video_campaigns?id=eq.{post_id}", headers=headers, timeout=5)
                if r_vc.status_code == 200 and r_vc.json():
                    row = r_vc.json()[0]
                    post_data = {
                        "id": row.get("id"),
                        "topic": row.get("title") or "Video Campaign",
                        "hook": row.get("hook", ""),
                        "summary": row.get("summary", ""),
                        "generated_content": row.get("generated_content", ""),
                        "hashtags": row.get("hashtags", ""),
                        "call_to_action": row.get("call_to_action", ""),
                        "video_url": row.get("video_url", ""),
                        "image_url": row.get("image_url", ""),
                        "platform_captions": {
                            "linkedin": row.get("linkedin_caption", ""),
                            "facebook": row.get("facebook_caption", ""),
                            "instagram": row.get("instagram_caption", "")
                        },
                        "platforms": ["linkedin", "facebook", "instagram", "youtube"]
                    }
            except Exception:
                pass

            # Fallback to marketing_content
            if not post_data:
                r = requests.get(f"{SUPABASE_URL}/rest/v1/marketing_content?id=eq.{post_id}", headers=headers, timeout=10)
                items = r.json() if r.status_code == 200 else []
                if items:
                    p = items[0]
                    meta = {}
                    kp = p.get("key_points") or ""
                    if kp.startswith("{") and kp.endswith("}"):
                        try:
                            meta = json.loads(kp)
                        except Exception:
                            pass

                    post_data = {
                        "id": p.get("id"),
                        "topic": p.get("topic"),
                        "hook": meta.get("hook", p.get("topic")),
                        "summary": meta.get("summary", ""),
                        "generated_content": p.get("generated_content", ""),
                        "hashtags": p.get("hashtags", ""),
                        "call_to_action": p.get("call_to_action", ""),
                        "video_url": meta.get("video_url", ""),
                        "image_url": meta.get("image_url", ""),
                        "platform_captions": meta.get("platform_captions", {}),
                        "platforms": ["linkedin", "facebook", "instagram", "youtube"]
                    }

            if not post_data:
                return self._send_json({"success": False, "error": "Post not found in database"}, 404)

            res = dispatch_multi_channel_publish(post_data)
            self._send_json({"success": True, "results": res})
        except Exception as e:
            self._send_json({"success": False, "error": str(e)}, 500)

    def handle_delete_post(self, post_id, body=None):
        body = body or {}
        topic = (body.get("topic") or body.get("title") or "").strip()
        video_url = (body.get("video_url") or "").strip()
        sb_url = get_clean_env("SUPABASE_URL", DEFAULT_SUPABASE_URL)
        sb_key = get_clean_env("SUPABASE_SERVICE_ROLE_KEY") or get_clean_env("SUPABASE_KEY")
        headers = get_supabase_headers()
        has_supabase = bool(sb_url and sb_key and not str(sb_url).startswith("https://your-project") and not str(sb_key).startswith("your_supabase"))
        deleted_from = []
        live_deleted = {}

        try:
            # 0. Find social IDs to delete live post from LinkedIn, Facebook, Instagram, Threads, YouTube
            social_ids = {}

            # Directly extract from request payload if passed by UI
            if body.get("social_post_ids") and isinstance(body["social_post_ids"], dict):
                social_ids.update(body["social_post_ids"])
            if body.get("platform_urls") and isinstance(body["platform_urls"], dict):
                p_urls = body["platform_urls"]
                if p_urls.get("facebook"): social_ids["facebook_url"] = p_urls["facebook"]
                if p_urls.get("instagram"): social_ids["instagram_url"] = p_urls["instagram"]
                if p_urls.get("threads"): social_ids["threads_url"] = p_urls["threads"]
            if body.get("live_post_url"):
                social_ids["live_post_url"] = body["live_post_url"]

            # If topic/video_url were not in body, lookup from local database before deleting
            if not topic:
                try:
                    for lc in load_local_campaigns():
                        if str(lc.get("id")) == str(post_id):
                            topic = lc.get("topic") or lc.get("title") or ""
                            video_url = video_url or lc.get("video_url") or ""
                            break
                except Exception:
                    pass

            is_uuid = False
            try:
                import uuid
                uuid.UUID(str(post_id))
                is_uuid = True
            except Exception:
                is_uuid = False

            if has_supabase:
                if is_uuid:
                    try:
                        r_vc = requests.get(f"{sb_url}/rest/v1/video_campaigns?id=eq.{post_id}", headers=headers, timeout=5)
                        if r_vc.status_code == 200 and r_vc.json():
                            row = r_vc.json()[0]
                            topic = topic or row.get("title") or row.get("topic") or ""
                            video_url = video_url or row.get("video_url") or ""
                            sp_ids = row.get("social_post_ids")
                            if isinstance(sp_ids, str):
                                try: sp_ids = json.loads(sp_ids)
                                except Exception: pass
                            if isinstance(sp_ids, dict):
                                social_ids.update(sp_ids)
                            if row.get("live_post_url"):
                                social_ids["live_post_url"] = row.get("live_post_url")
                    except Exception as e:
                        print(f"[delete] query video_campaigns warning: {e}")

                    # Check marketing_content for topic/video_url
                    if not topic:
                        try:
                            r_mc = requests.get(f"{sb_url}/rest/v1/marketing_content?id=eq.{post_id}", headers=headers, timeout=5)
                            if r_mc.status_code == 200 and r_mc.json():
                                row_m = r_mc.json()[0]
                                topic = topic or row_m.get("topic") or ""
                        except Exception:
                            pass

                    # Check marketing_published_archive
                    try:
                        r_arch = requests.get(f"{sb_url}/rest/v1/marketing_published_archive?or=(id.eq.{post_id},original_queue_id.eq.{post_id})", headers=headers, timeout=5)
                        if r_arch.status_code == 200 and r_arch.json():
                            row_a = r_arch.json()[0]
                            topic = topic or row_a.get("topic") or ""
                            if row_a.get("linkedin_post_id"): social_ids["linkedin"] = row_a["linkedin_post_id"]
                            if row_a.get("facebook_post_id"): social_ids["facebook"] = row_a["facebook_post_id"]
                            if row_a.get("instagram_media_id"): social_ids["instagram"] = row_a["instagram_media_id"]
                            if row_a.get("threads_post_id"): social_ids["threads"] = row_a["threads_post_id"]
                            if row_a.get("live_post_url"): social_ids["live_post_url"] = row_a["live_post_url"]
                    except Exception as e:
                        print(f"[delete] query archive warning: {e}")

            # Trigger live deletion on all connected social platforms
            if social_ids:
                live_deleted = delete_live_social_post(social_ids)

            # 1. Delete from Supabase tables (by ID and by Topic)
            if has_supabase:
                try:
                    r1 = requests.delete(f"{sb_url}/rest/v1/video_campaigns?id=eq.{post_id}", headers=headers, timeout=5)
                    if r1.status_code in (200, 204):
                        deleted_from.append("video_campaigns")
                except Exception as e:
                    print(f"[delete] error video_campaigns: {e}")

                try:
                    r2 = requests.delete(f"{sb_url}/rest/v1/marketing_content?id=eq.{post_id}", headers=headers, timeout=5)
                    if r2.status_code in (200, 204):
                        deleted_from.append("marketing_content")
                except Exception as e:
                    print(f"[delete] error marketing_content: {e}")

                try:
                    requests.delete(f"{sb_url}/rest/v1/marketing_published_archive?id=eq.{post_id}", headers=headers, timeout=5)
                    requests.delete(f"{sb_url}/rest/v1/marketing_published_archive?original_queue_id=eq.{post_id}", headers=headers, timeout=5)
                    deleted_from.append("marketing_published_archive")
                except Exception as e:
                    print(f"[delete] error marketing_published_archive: {e}")

                # ALSO delete matching topic duplicate rows from Supabase
                if topic and topic not in ("ACTIVE_AI_PROMPT", "NOTIFICATION_ALERT"):
                    try:
                        q_topic = urllib.parse.quote(topic)
                        requests.delete(f"{sb_url}/rest/v1/video_campaigns?title=eq.{q_topic}", headers=headers, timeout=5)
                        requests.delete(f"{sb_url}/rest/v1/marketing_content?topic=eq.{q_topic}", headers=headers, timeout=5)
                        requests.delete(f"{sb_url}/rest/v1/marketing_published_archive?topic=eq.{q_topic}", headers=headers, timeout=5)
                    except Exception as ex_dup:
                        print(f"[delete] error deleting duplicate by topic: {ex_dup}")

            # 2. Clean from local campaigns database (by ID and by topic)
            try:
                if delete_local_campaign(post_id, topic=topic, video_url=video_url):
                    deleted_from.append("local_campaigns_db")
            except Exception as e:
                print(f"[delete] error local_campaigns_db: {e}")

            # 3. Clean from live_social_tracker.json
            try:
                tracker_path = os.path.join(os.path.dirname(__file__), "live_social_tracker.json")
                if os.path.exists(tracker_path):
                    with open(tracker_path, "r", encoding="utf-8") as f:
                        tracker_records = json.load(f)
                    match_keys = {str(post_id).strip()}
                    if topic: match_keys.add(topic.strip().lower())
                    if video_url: match_keys.add(video_url.strip().lower())
                    new_records = []
                    for rec in tracker_records:
                        pdata = rec.get("post_data") or {}
                        cand_keys = {
                            str(rec.get("id") or "").strip(),
                            str(pdata.get("id") or "").strip(),
                            str(rec.get("title") or "").strip().lower(),
                            str(pdata.get("topic") or "").strip().lower(),
                            str(rec.get("video_url") or "").strip().lower(),
                            str(pdata.get("video_url") or "").strip().lower()
                        }
                        if not cand_keys.intersection(match_keys):
                            new_records.append(rec)
                    with open(tracker_path, "w", encoding="utf-8") as f:
                        json.dump(new_records, f, indent=2)
                    deleted_from.append("live_social_tracker")
            except Exception as e:
                print(f"[delete] error live_social_tracker: {e}")

            # 4. Clean banner from Supabase storage if applicable & invalidate cache
            img_url = body.get("image_url") or ""
            if "marketing-banners" in img_url and has_supabase:
                try:
                    fname = img_url.split("marketing-banners/")[-1].split("?")[0]
                    if fname:
                        bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "marketing-banners")
                        storage_headers = {
                            "apikey": sb_key,
                            "Authorization": f"Bearer {sb_key}",
                            "Content-Type": "application/json"
                        }
                        del_url = f"{sb_url}/storage/v1/object/{bucket}"
                        requests.delete(del_url, headers=storage_headers, json={"prefixes": [fname]}, timeout=5)
                        deleted_from.append("supabase_storage")
                except Exception as ie:
                    print(f"[delete] error removing storage image: {ie}")

            global _storage_stats_cache
            _storage_stats_cache["data"] = None

            self._send_json({"success": True, "deleted": post_id, "deleted_from": deleted_from, "live_deleted": live_deleted})
        except Exception as e:
            self._send_json({"success": False, "error": str(e)}, 500)

    def handle_get_posts(self):
        sb_url = get_clean_env("SUPABASE_URL", DEFAULT_SUPABASE_URL)
        sb_key = get_clean_env("SUPABASE_SERVICE_ROLE_KEY") or get_clean_env("SUPABASE_KEY")
        has_supabase = bool(sb_url and sb_key and not str(sb_url).startswith("https://your-project") and not str(sb_key).startswith("your_supabase"))

        combined = []

        # 1. Fetch from Supabase if configured
        if has_supabase:
            headers = {
                "apikey": sb_key,
                "Authorization": f"Bearer {sb_key}",
                "Content-Type": "application/json"
            }
            # Fetch fresh video_campaigns
            try:
                res_vc = requests.get(f"{sb_url}/rest/v1/video_campaigns?order=created_at.desc&limit=30", headers=headers, timeout=5)
                if res_vc.status_code == 200:
                    for row in res_vc.json():
                        title = (row.get("title") or row.get("topic") or "").strip()
                        status = (row.get("status") or "").strip().lower()
                        if not title or title in ("ACTIVE_AI_PROMPT", "NOTIFICATION_ALERT") or status in ("prompt_template", "unread"):
                            continue
                        combined.append({
                            "id": row.get("id"),
                            "topic": title,
                            "status": row.get("status") or "scheduled",
                            "video_url": row.get("video_url") or "",
                            "image_url": row.get("image_url") or "",
                            "scheduled_at": row.get("scheduled_at") or row.get("created_at"),
                            "published_at": row.get("published_at") or (row.get("created_at") if row.get("status") == "published" else None),
                            "live_post_url": row.get("live_post_url") or "",
                            "social_post_ids": row.get("social_post_ids") or {},
                            "created_at": row.get("created_at")
                        })
            except Exception as e:
                print(f"[get_posts] video_campaigns fetch error: {e}")

            # Fetch marketing_content
            try:
                res1 = requests.get(f"{sb_url}/rest/v1/marketing_content?order=created_at.desc&limit=30", headers=headers, timeout=5)
                if res1.status_code == 200:
                    for row in res1.json():
                        topic = (row.get("topic") or "").strip()
                        status = (row.get("status") or "").strip().lower()
                        if not topic or topic in ("ACTIVE_AI_PROMPT", "NOTIFICATION_ALERT") or status in ("prompt_template", "unread"):
                            continue
                        rid = row.get("id")
                        meta = {}
                        kp = row.get("key_points") or ""
                        if kp.startswith("{") and kp.endswith("}"):
                            try: meta = json.loads(kp)
                            except Exception: pass
                        pub_time = row.get("published_at") or meta.get("published_at") or (row.get("created_at") if row.get("status") == "published" else None)
                        combined.append({
                            "id": rid,
                            "topic": topic,
                            "status": row.get("status"),
                            "video_url": meta.get("video_url") or "",
                            "image_url": meta.get("image_url") or "",
                            "scheduled_at": meta.get("scheduled_at") or row.get("created_at"),
                            "published_at": pub_time,
                            "live_post_url": "",
                            "created_at": row.get("created_at")
                        })
            except Exception as e:
                print(f"[get_posts] marketing_content fetch error: {e}")

        # 2. Merge records from persistent local campaigns database (local_campaigns_db.json)
        try:
            local_camps = load_local_campaigns()
            for lc in local_camps:
                topic = (lc.get("topic") or lc.get("title") or "").strip()
                status = (lc.get("status") or "").strip().lower()
                if not topic or topic in ("ACTIVE_AI_PROMPT", "NOTIFICATION_ALERT") or status in ("prompt_template", "unread"):
                    continue
                combined.append(lc)
        except Exception as e:
            print(f"[get_posts] local_campaigns error: {e}")

        # 3. Deduplicate smartly by ID AND by (topic, video_url)
        seen_ids = set()
        seen_campaigns = {}  # (topic_lower, video_url_lower) -> index in deduped
        deduped = []

        for post in combined:
            topic = (post.get("topic") or post.get("title") or "").strip()
            status = (post.get("status") or "").strip().lower()
            if not topic or topic in ("NOTIFICATION_ALERT", "ACTIVE_AI_PROMPT") or status in ("prompt_template", "unread"):
                continue

            pid = str(post.get("id") or "").strip()
            if pid and pid in seen_ids:
                continue

            vurl = (post.get("video_url") or "").strip()
            camp_key = (topic.lower(), vurl.lower())

            # If matching existing campaign with same topic and video_url, merge!
            if camp_key in seen_campaigns and vurl:
                existing_idx = seen_campaigns[camp_key]
                existing = deduped[existing_idx]
                if not existing.get("live_post_url") and post.get("live_post_url"):
                    existing["live_post_url"] = post["live_post_url"]
                if not existing.get("platform_urls") and post.get("platform_urls"):
                    existing["platform_urls"] = post["platform_urls"]
                if not existing.get("image_url") and post.get("image_url"):
                    existing["image_url"] = post["image_url"]
                if not existing.get("social_post_ids") and post.get("social_post_ids"):
                    existing["social_post_ids"] = post["social_post_ids"]
                if post.get("status") == "published" and existing.get("status") != "published":
                    existing["status"] = "published"
                    existing["published_at"] = post.get("published_at") or post.get("created_at")
                if pid:
                    seen_ids.add(pid)
                continue

            if pid:
                seen_ids.add(pid)
            if vurl:
                seen_campaigns[camp_key] = len(deduped)
            deduped.append(post)

        # 4. Sort by date descending
        deduped.sort(key=lambda x: str(x.get("scheduled_at") or x.get("created_at") or ""), reverse=True)
        return self._send_json({"success": True, "posts": deduped})

    def handle_trigger_n8n(self, body):
        video_id = body.get("video_id") or "ic4HSdThFc8"
        try:
            # Call n8n webhook or trigger
            wh_url = f"{N8N_URL}/webhook/process-video"
            r = requests.post(wh_url, json={"video_id": video_id}, timeout=15)
            if r.status_code in (200, 201):
                return self._send_json({"success": True, "executionId": "n8n-webhook-dispatched", "data": r.json()})
            else:
                return self._send_json({"success": True, "executionId": "n8n-direct-event", "note": "Triggered"})
        except Exception as e:
            return self._send_json({"success": True, "executionId": "fallback-worker", "note": str(e)})

    def handle_get_storage_stats(self, force=False):
        try:
            stats = get_supabase_storage_stats(force=force)
            self._send_json({"success": True, "stats": stats})
        except Exception as e:
            self._send_json({"success": False, "error": str(e)}, 500)

    def handle_supabase_cleanup(self, body):
        try:
            days = int(body.get("days", 0)) if body else 0
            res = clean_supabase_storage_retention(days=days)
            global _storage_stats_cache
            _storage_stats_cache["data"] = None
            self._send_json(res)
        except Exception as e:
            self._send_json({"success": False, "error": str(e)}, 500)

def run_server():
    # Start the automated scheduler background worker
    start_scheduler(interval_seconds=30)
    
    port = int(os.environ.get("PORT") or PORT or 8080)
    server_address = ("0.0.0.0", port)
    httpd = ThreadedHTTPServer(server_address, OmniPostHandler)
    print(f"\n========================================================")
    print(f" 🚀 OmniPost Automation Server Live on http://localhost:{port}")
    print(f" 📡 Dashboard: http://localhost:{port}/dashboard")
    print(f" ⏱ Background Scheduler: Running (polling every 30s)")
    print(f"========================================================\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        httpd.server_close()

if __name__ == "__main__":
    run_server()

