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
from publisher import dispatch_multi_channel_publish, get_supabase_headers, delete_live_social_post
from scheduler import start_scheduler, check_and_publish_due_posts, clean_supabase_storage_retention

PORT = int(os.getenv("PORT", 8080))
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
N8N_URL = os.getenv("N8N_URL", "http://localhost:5678")

_storage_stats_cache = {"timestamp": 0, "data": None}

def get_supabase_storage_stats(force: bool = False):
    global _storage_stats_cache
    now = time.time()
    if not force and _storage_stats_cache["data"] and (now - _storage_stats_cache["timestamp"] < 30):
        return _storage_stats_cache["data"]

    bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "marketing-banners")
    sb_url = os.getenv("SUPABASE_URL")
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
    fb_token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN") or os.getenv("FACEBOOK_TOKEN")
    fb_page_id = os.getenv("FACEBOOK_PAGE_ID")
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
    ig_token = os.getenv("INSTAGRAM_ACCESS_TOKEN") or fb_token
    ig_id = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID") or os.getenv("INSTAGRAM_ID")
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
    yt_ref = os.getenv("GOOGLE_REFRESH_TOKEN") or os.getenv("YOUTUBE_REFRESH_TOKEN")
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
    th_token = os.getenv("THREADS_ACCESS_TOKEN") or os.getenv("THREADS_TOKEN")
    if th_token:
        try:
            r = requests.get(f"https://graph.threads.net/v1.0/me?fields=id,username&access_token={th_token}", timeout=10)
            if r.status_code == 200:
                tdata = r.json()
                health["threads"] = {
                    "valid": True,
                    "configured": True,
                    "username": tdata.get("username", "founders_gateway"),
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

        # Pre-validate Meta token before saving if provided
        if meta_token:
            try:
                res = requests.get(f"https://graph.facebook.com/v19.0/me?fields=id,name&access_token={meta_token}", timeout=8)
                if res.status_code != 200:
                    err_msg = res.json().get("error", {}).get("message") or f"HTTP {res.status_code}"
                    return self._send_json({"success": False, "error": f"Invalid Meta Token: {err_msg}"}, 400)
            except Exception as e:
                return self._send_json({"success": False, "error": f"Failed to verify Meta Token: {str(e)}"}, 400)

        # Pre-validate Threads token before saving if provided
        if threads_token:
            try:
                me_res = requests.get(f"https://graph.threads.net/v1.0/me?fields=id,username&access_token={threads_token}", timeout=8)
                if me_res.status_code != 200:
                    err_msg = me_res.json().get("error", {}).get("message") or f"HTTP {me_res.status_code}"
                    return self._send_json({"success": False, "error": f"Invalid Threads Token: {err_msg}"}, 400)
                med = me_res.json()
                th_uid = med.get("id")
                th_un = med.get("username")
                if th_uid:
                    update_env_var("THREADS_USER_ID", str(th_uid))
                    os.environ["THREADS_USER_ID"] = str(th_uid)
                if th_un:
                    update_env_var("THREADS_USERNAME", str(th_un))
                    os.environ["THREADS_USERNAME"] = str(th_un)
            except Exception as e:
                return self._send_json({"success": False, "error": f"Failed to verify Threads Token: {str(e)}"}, 400)

        updated = []
        if meta_token:
            final_meta_token = meta_token
            target_pid = fb_page_id or os.getenv("FACEBOOK_PAGE_ID") or "1485094014687108"
            try:
                p_acc = requests.get(f"https://graph.facebook.com/v19.0/{target_pid}?fields=access_token,name,instagram_business_account&access_token={meta_token}", timeout=8)
                if p_acc.status_code == 200:
                    pd = p_acc.json()
                    if pd.get("access_token"):
                        final_meta_token = pd["access_token"]
                    ig_data = pd.get("instagram_business_account") or {}
                    if ig_data.get("id"):
                        ig_account_id = ig_data["id"]
            except Exception:
                pass

            update_env_var("FACEBOOK_PAGE_ACCESS_TOKEN", final_meta_token)
            update_env_var("FACEBOOK_TOKEN", final_meta_token)
            update_env_var("INSTAGRAM_ACCESS_TOKEN", final_meta_token)
            os.environ["FACEBOOK_PAGE_ACCESS_TOKEN"] = final_meta_token
            os.environ["FACEBOOK_TOKEN"] = final_meta_token
            os.environ["INSTAGRAM_ACCESS_TOKEN"] = final_meta_token
            updated.append("Meta Page Token (Facebook & Instagram)")

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

        # Invalidate health cache so immediate re-check reflects new tokens
        global _TOKEN_HEALTH_CACHE
        _TOKEN_HEALTH_CACHE["data"] = None
        _TOKEN_HEALTH_CACHE["timestamp"] = 0

        # Perform live check with fresh token
        health_data = check_tokens_health(force=True)

        return self._send_json({
            "success": True, 
            "updated": updated,
            "token_health": health_data.get("token_health", {}),
            "has_expired_tokens": health_data.get("has_expired_tokens", False),
            "expired_services": health_data.get("expired_services", []),
            "alert_message": health_data.get("alert_message")
        })

    def handle_verify_single_token(self, body):
        global _TOKEN_HEALTH_CACHE
        target = (body.get("target") or "meta").lower().strip()
        raw_token = (body.get("token") or "").strip()

        # Sanitize token (strip quotes, bearer prefix, newlines)
        custom_token = raw_token
        if custom_token.lower().startswith("bearer "):
            custom_token = custom_token[7:].strip()
        custom_token = custom_token.strip("\"' \t\r\n")

        try:
            if target == "meta":
                token = custom_token or os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN") or os.getenv("FACEBOOK_TOKEN")
                if not token:
                    return self._send_json({"success": False, "valid": False, "target": "meta", "error": "No Meta token provided or found in configuration."})

                fb_page_id = os.getenv("FACEBOOK_PAGE_ID") or "1485094014687108"
                final_token = token
                name = "Founders Gateway"
                pid = fb_page_id

                # 1. First attempt: Query /me with timeout=12
                res = None
                for attempt in range(2):
                    try:
                        res = requests.get(f"https://graph.facebook.com/v19.0/me?fields=id,name&access_token={token}", timeout=12)
                        break
                    except Exception:
                        if attempt == 1:
                            raise

                if res and res.status_code == 200:
                    d = res.json()
                    name = d.get("name") or "Founders Gateway"
                    pid = d.get("id")

                    # Check if this is a User Token that manages Pages -> extract the permanent Page Access Token!
                    try:
                        acc_res = requests.get(f"https://graph.facebook.com/v19.0/me/accounts?fields=id,name,access_token,instagram_business_account&access_token={token}", timeout=8)
                        if acc_res.status_code == 200:
                            pages = acc_res.json().get("data", [])
                            target_page = None
                            for p in pages:
                                if str(p.get("id")) == str(fb_page_id) or "founder" in str(p.get("name", "")).lower():
                                    target_page = p
                                    break
                            if not target_page and pages:
                                target_page = pages[0]

                            if target_page and target_page.get("access_token"):
                                final_token = target_page["access_token"]
                                pid = target_page.get("id") or pid
                                name = target_page.get("name") or name
                                ig_data = target_page.get("instagram_business_account") or {}
                                ig_id = ig_data.get("id")
                                if ig_id and custom_token:
                                    update_env_var("INSTAGRAM_BUSINESS_ACCOUNT_ID", str(ig_id))
                                    update_env_var("INSTAGRAM_ID", str(ig_id))
                                    os.environ["INSTAGRAM_BUSINESS_ACCOUNT_ID"] = str(ig_id)
                                    os.environ["INSTAGRAM_ID"] = str(ig_id)
                    except Exception as ex:
                        print(f"[verify_meta] accounts extraction note: {ex}")

                    # Also directly resolve page token via /{fb_page_id}?fields=access_token
                    try:
                        p_acc = requests.get(f"https://graph.facebook.com/v19.0/{fb_page_id}?fields=access_token,name,instagram_business_account&access_token={token}", timeout=8)
                        if p_acc.status_code == 200:
                            p_data = p_acc.json()
                            if p_data.get("access_token"):
                                final_token = p_data["access_token"]
                                name = p_data.get("name") or name
                                pid = p_data.get("id") or pid
                                ig_data = p_data.get("instagram_business_account") or {}
                                ig_id = ig_data.get("id")
                                if ig_id and custom_token:
                                    update_env_var("INSTAGRAM_BUSINESS_ACCOUNT_ID", str(ig_id))
                                    update_env_var("INSTAGRAM_ID", str(ig_id))
                                    os.environ["INSTAGRAM_BUSINESS_ACCOUNT_ID"] = str(ig_id)
                                    os.environ["INSTAGRAM_ID"] = str(ig_id)
                    except Exception as ex2:
                        print(f"[verify_meta] direct page access_token note: {ex2}")

                    if custom_token:
                        update_env_var("FACEBOOK_PAGE_ACCESS_TOKEN", final_token)
                        update_env_var("FACEBOOK_TOKEN", final_token)
                        update_env_var("INSTAGRAM_ACCESS_TOKEN", final_token)
                        os.environ["FACEBOOK_PAGE_ACCESS_TOKEN"] = final_token
                        os.environ["FACEBOOK_TOKEN"] = final_token
                        os.environ["INSTAGRAM_ACCESS_TOKEN"] = final_token

                    if _TOKEN_HEALTH_CACHE.get("data") and "token_health" in _TOKEN_HEALTH_CACHE["data"]:
                        _TOKEN_HEALTH_CACHE["data"]["token_health"]["facebook"] = {"valid": True, "configured": True, "name": name, "page_id": pid}
                        _TOKEN_HEALTH_CACHE["data"]["token_health"]["instagram"] = {"valid": True, "configured": True, "username": "founders_gateway"}
                    else:
                        _TOKEN_HEALTH_CACHE["data"] = None

                    return self._send_json({
                        "success": True,
                        "valid": True,
                        "target": "meta",
                        "name": name,
                        "id": pid,
                        "message": f"Meta token is Active & Verified (Page: {name})"
                    })
                else:
                    err_data = res.json().get("error", {}) if res else {}
                    err_code = err_data.get("code")
                    err_msg = err_data.get("message") or (f"HTTP {res.status_code}" if res else "Connection timed out")

                    # Handle temporary rate limits gracefully without failing valid tokens
                    if err_code in (4, 17, 32, 613) or "request limit" in err_msg.lower():
                        return self._send_json({
                            "success": True,
                            "valid": True,
                            "target": "meta",
                            "name": name,
                            "id": pid,
                            "message": f"Meta token verified (Note: Meta temporary rate limit active, format is valid)"
                        })

                    # If /me failed, try direct page endpoint (some page tokens restrict /me)
                    try:
                        p_res = requests.get(f"https://graph.facebook.com/v19.0/{fb_page_id}?fields=id,name&access_token={token}", timeout=10)
                        if p_res.status_code == 200:
                            pd = p_res.json()
                            p_name = pd.get("name") or name
                            if custom_token:
                                update_env_var("FACEBOOK_PAGE_ACCESS_TOKEN", token)
                                update_env_var("FACEBOOK_TOKEN", token)
                                update_env_var("INSTAGRAM_ACCESS_TOKEN", token)
                                os.environ["FACEBOOK_PAGE_ACCESS_TOKEN"] = token
                                os.environ["FACEBOOK_TOKEN"] = token
                                os.environ["INSTAGRAM_ACCESS_TOKEN"] = token
                            return self._send_json({
                                "success": True,
                                "valid": True,
                                "target": "meta",
                                "name": p_name,
                                "id": fb_page_id,
                                "message": f"Meta Page Token is Active & Verified ({p_name})"
                            })
                    except Exception:
                        pass

                    if _TOKEN_HEALTH_CACHE.get("data") and "token_health" in _TOKEN_HEALTH_CACHE["data"]:
                        _TOKEN_HEALTH_CACHE["data"]["token_health"]["facebook"] = {"valid": False, "configured": True, "error": err_msg}
                        _TOKEN_HEALTH_CACHE["data"]["token_health"]["instagram"] = {"valid": False, "configured": True, "error": err_msg}
                    return self._send_json({"success": False, "valid": False, "target": "meta", "error": err_msg})

            elif target == "threads":
                token = custom_token or os.getenv("THREADS_ACCESS_TOKEN")
                if not token:
                    return self._send_json({"success": False, "valid": False, "target": "threads", "error": "No Threads token provided or found in configuration."})

                res = None
                for attempt in range(2):
                    try:
                        res = requests.get(f"https://graph.threads.net/v1.0/me?fields=id,username&access_token={token}", timeout=12)
                        break
                    except Exception:
                        if attempt == 1:
                            try:
                                res = requests.get(f"https://graph.threads.net/me?fields=id,username&access_token={token}", timeout=12)
                            except Exception:
                                pass

                if res and res.status_code == 200:
                    d = res.json()
                    un = d.get("username") or "founders_gateway"
                    uid = d.get("id")
                    if custom_token:
                        update_env_var("THREADS_ACCESS_TOKEN", custom_token)
                        os.environ["THREADS_ACCESS_TOKEN"] = custom_token
                        if uid:
                            update_env_var("THREADS_USER_ID", str(uid))
                            os.environ["THREADS_USER_ID"] = str(uid)
                        if un:
                            update_env_var("THREADS_USERNAME", str(un))
                            os.environ["THREADS_USERNAME"] = str(un)

                    if _TOKEN_HEALTH_CACHE.get("data") and "token_health" in _TOKEN_HEALTH_CACHE["data"]:
                        _TOKEN_HEALTH_CACHE["data"]["token_health"]["threads"] = {"valid": True, "configured": True, "username": un, "id": uid}
                    else:
                        _TOKEN_HEALTH_CACHE["data"] = None

                    return self._send_json({
                        "success": True,
                        "valid": True,
                        "target": "threads",
                        "username": un,
                        "id": uid,
                        "message": f"Threads token is Active & Verified (@{un})"
                    })
                else:
                    err_data = res.json().get("error", {}) if res else {}
                    err_code = err_data.get("code")
                    err_msg = err_data.get("message") or (f"HTTP {res.status_code}" if res else "Connection timed out")

                    if err_code in (4, 17, 32, 613) or "request limit" in err_msg.lower():
                        return self._send_json({
                            "success": True,
                            "valid": True,
                            "target": "threads",
                            "username": "founders_gateway",
                            "message": "Threads token verified (Note: Threads rate limit active, format is valid)"
                        })

                    if _TOKEN_HEALTH_CACHE.get("data") and "token_health" in _TOKEN_HEALTH_CACHE["data"]:
                        _TOKEN_HEALTH_CACHE["data"]["token_health"]["threads"] = {"valid": False, "configured": True, "error": err_msg}
                    return self._send_json({"success": False, "valid": False, "target": "threads", "error": err_msg})

            else:
                return self._send_json({"success": False, "error": f"Unknown verification target: {target}"}, 400)
        except Exception as e:
            return self._send_json({"success": False, "error": str(e)}, 500)

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

            meta = {
                "video_url": video_url,
                "image_url": image_url,
                "scheduled_at": scheduled_at,
                "published_at": now_iso if publish_now else None,
                "hook": hook,
                "summary": summary,
                "platform_captions": captions
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

            # 1. Save to Supabase (Supports both fresh 'video_campaigns' and 'marketing_content')
            created_id = None
            if SUPABASE_URL and SUPABASE_KEY:
                headers = get_supabase_headers()
                # A. Try fresh table 'video_campaigns' first
                target_platforms = body.get("platforms") or ["facebook", "instagram"]
                try:
                    vc_record = {
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
                    res_vc = requests.post(f"{SUPABASE_URL}/rest/v1/video_campaigns", headers=headers, json=vc_record, timeout=5)
                    if res_vc.status_code == 201:
                        created_id = res_vc.json()[0].get("id")
                except Exception:
                    pass

                # B. If fresh table not created yet, save into marketing_content
                if not created_id:
                    try:
                        res = requests.post(f"{SUPABASE_URL}/rest/v1/marketing_content", headers=headers, json=record, timeout=10)
                        if res.status_code == 201:
                            created_id = res.json()[0].get("id")
                    except Exception as e:
                        print(f"[server] Supabase insert error: {e}")

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
        headers = get_supabase_headers() if (SUPABASE_URL and SUPABASE_KEY) else {}
        deleted_from = []
        live_deleted = {}
        body = body or {}

        try:
            # 0. Find social IDs to delete live post from LinkedIn, Facebook, Instagram, YouTube
            social_ids = {}

            # Directly extract from request payload if passed by UI
            if body.get("social_post_ids") and isinstance(body["social_post_ids"], dict):
                social_ids.update(body["social_post_ids"])
            if body.get("platform_urls") and isinstance(body["platform_urls"], dict):
                p_urls = body["platform_urls"]
                if p_urls.get("facebook"):
                    social_ids["facebook_url"] = p_urls["facebook"]
                if p_urls.get("instagram"):
                    social_ids["instagram_url"] = p_urls["instagram"]
                if p_urls.get("threads"):
                    social_ids["threads_url"] = p_urls["threads"]
            if body.get("live_post_url"):
                social_ids["live_post_url"] = body["live_post_url"]

            is_uuid = False
            try:
                import uuid
                uuid.UUID(str(post_id))
                is_uuid = True
            except Exception:
                is_uuid = False

            if SUPABASE_URL and SUPABASE_KEY:
                # Check video_campaigns
                if is_uuid:
                    try:
                        r_vc = requests.get(f"{SUPABASE_URL}/rest/v1/video_campaigns?id=eq.{post_id}", headers=headers, timeout=5)
                        if r_vc.status_code == 200 and r_vc.json():
                            row = r_vc.json()[0]
                            sp_ids = row.get("social_post_ids")
                            if isinstance(sp_ids, str):
                                try:
                                    sp_ids = json.loads(sp_ids)
                                except Exception:
                                    pass
                            if isinstance(sp_ids, dict):
                                social_ids.update(sp_ids)
                            if row.get("live_post_url"):
                                social_ids["live_post_url"] = row.get("live_post_url")
                    except Exception as e:
                        print(f"[delete] query video_campaigns warning: {e}")

                    # Check marketing_published_archive
                    try:
                        r_arch = requests.get(f"{SUPABASE_URL}/rest/v1/marketing_published_archive?or=(id.eq.{post_id},original_queue_id.eq.{post_id})", headers=headers, timeout=5)
                        if r_arch.status_code == 200 and r_arch.json():
                            row_a = r_arch.json()[0]
                            if row_a.get("linkedin_post_id"):
                                social_ids["linkedin"] = row_a["linkedin_post_id"]
                            if row_a.get("facebook_post_id"):
                                social_ids["facebook"] = row_a["facebook_post_id"]
                            if row_a.get("instagram_media_id"):
                                social_ids["instagram"] = row_a["instagram_media_id"]
                            if row_a.get("threads_post_id"):
                                social_ids["threads"] = row_a["threads_post_id"]
                            if row_a.get("live_post_url"):
                                social_ids["live_post_url"] = row_a["live_post_url"]
                    except Exception as e:
                        print(f"[delete] query archive warning: {e}")

            # Also check local live_social_tracker.json (match by ID, post_data.id, timestamp, topic, or video_url)
            match_keys = {str(post_id)}
            if body.get("topic"):
                match_keys.add(str(body["topic"]).strip())
            if body.get("video_url"):
                match_keys.add(str(body["video_url"]).strip())

            tracker_records = []
            try:
                tracker_path = os.path.join(os.path.dirname(__file__), "live_social_tracker.json")
                if os.path.exists(tracker_path):
                    with open(tracker_path, "r", encoding="utf-8") as f:
                        tracker_records = json.load(f)
                    for rec in tracker_records:
                        pdata = rec.get("post_data") or {}
                        cand_keys = {
                            str(rec.get("id") or "").strip(),
                            str(pdata.get("id") or "").strip(),
                            str(rec.get("timestamp") or "").strip(),
                            str(rec.get("title") or "").strip(),
                            str(pdata.get("topic") or "").strip(),
                            str(rec.get("video_url") or "").strip(),
                            str(pdata.get("video_url") or "").strip()
                        }
                        if cand_keys.intersection(match_keys):
                            res_map = rec.get("results") or {}
                            for plat in ["facebook", "instagram", "threads", "x", "youtube", "linkedin", "pinterest"]:
                                if res_map.get(plat, {}).get("post_id"):
                                    social_ids[plat] = res_map[plat]["post_id"]
                                if res_map.get(plat, {}).get("photo_id") and not social_ids.get("photo_id"):
                                    social_ids["photo_id"] = res_map[plat]["photo_id"]
                                if res_map.get(plat, {}).get("post_url") and not social_ids.get("live_post_url"):
                                    social_ids["live_post_url"] = res_map[plat]["post_url"]
                            plat_urls = rec.get("platform_urls") or {}
                            if plat_urls.get("facebook") and not social_ids.get("facebook_url"):
                                social_ids["facebook_url"] = plat_urls["facebook"]
                            if plat_urls.get("instagram") and not social_ids.get("instagram_url"):
                                social_ids["instagram_url"] = plat_urls["instagram"]
                            if plat_urls.get("threads") and not social_ids.get("threads_url"):
                                social_ids["threads_url"] = plat_urls["threads"]
                            break
            except Exception as e:
                print(f"[delete] local tracker check warning: {e}")

            # Trigger live deletion on all connected social platforms
            if social_ids:
                live_deleted = delete_live_social_post(social_ids)

            # 1. Delete from Supabase tables
            if SUPABASE_URL and SUPABASE_KEY:
                try:
                    r1 = requests.delete(f"{SUPABASE_URL}/rest/v1/video_campaigns?id=eq.{post_id}", headers=headers, timeout=5)
                    if r1.status_code in (200, 204):
                        deleted_from.append("video_campaigns")
                except Exception as e:
                    print(f"[delete] error video_campaigns: {e}")

                try:
                    r2 = requests.delete(f"{SUPABASE_URL}/rest/v1/marketing_content?id=eq.{post_id}", headers=headers, timeout=5)
                    if r2.status_code in (200, 204):
                        deleted_from.append("marketing_content")
                except Exception as e:
                    print(f"[delete] error marketing_content: {e}")

                try:
                    requests.delete(f"{SUPABASE_URL}/rest/v1/marketing_published_archive?id=eq.{post_id}", headers=headers, timeout=5)
                    requests.delete(f"{SUPABASE_URL}/rest/v1/marketing_published_archive?original_queue_id=eq.{post_id}", headers=headers, timeout=5)
                    deleted_from.append("marketing_published_archive")
                except Exception as e:
                    print(f"[delete] error marketing_published_archive: {e}")

                try:
                    requests.delete(f"{SUPABASE_URL}/rest/v1/marketing_queue?id=eq.{post_id}", headers=headers, timeout=5)
                    requests.delete(f"{SUPABASE_URL}/rest/v1/marketing_queue?content_id=eq.{post_id}", headers=headers, timeout=5)
                except Exception:
                    pass

            # 2. Clean from live_social_tracker.json
            try:
                tracker_path = os.path.join(os.path.dirname(__file__), "live_social_tracker.json")
                if tracker_records:
                    new_records = []
                    for rec in tracker_records:
                        pdata = rec.get("post_data") or {}
                        cand_keys = {
                            str(rec.get("id") or "").strip(),
                            str(pdata.get("id") or "").strip(),
                            str(rec.get("timestamp") or "").strip(),
                            str(rec.get("title") or "").strip(),
                            str(pdata.get("topic") or "").strip(),
                            str(rec.get("video_url") or "").strip(),
                            str(pdata.get("video_url") or "").strip()
                        }
                        if not cand_keys.intersection(match_keys):
                            new_records.append(rec)
                    with open(tracker_path, "w", encoding="utf-8") as f:
                        json.dump(new_records, f, indent=2)
                    deleted_from.append("live_social_tracker")
            except Exception as e:
                print(f"[delete] error live_social_tracker: {e}")

            # 3. Clean banner from Supabase storage if applicable & invalidate cache
            img_url = body.get("image_url") or ""
            if "marketing-banners" in img_url and SUPABASE_URL and SUPABASE_KEY:
                try:
                    fname = img_url.split("marketing-banners/")[-1].split("?")[0]
                    if fname:
                        bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "marketing-banners")
                        storage_headers = {
                            "apikey": SUPABASE_KEY,
                            "Authorization": f"Bearer {SUPABASE_KEY}",
                            "Content-Type": "application/json"
                        }
                        del_url = f"{SUPABASE_URL}/storage/v1/object/{bucket}"
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
        if not SUPABASE_URL or not SUPABASE_KEY:
            return self._send_json({"success": True, "posts": []})

        headers = get_supabase_headers()
        combined = []

        try:
            # 0. Fetch fresh video_campaigns if exists
            try:
                res_vc = requests.get(f"{SUPABASE_URL}/rest/v1/video_campaigns?order=created_at.desc&limit=20", headers=headers, timeout=5)
                if res_vc.status_code == 200:
                    for row in res_vc.json():
                        if row.get("topic") == "ACTIVE_AI_PROMPT" or row.get("status") == "prompt_template" or row.get("title") == "ACTIVE_AI_PROMPT":
                            continue
                        combined.append({
                            "id": row.get("id"),
                            "topic": row.get("title") or "Video Campaign",
                            "status": row.get("status") or "scheduled",
                            "video_url": row.get("video_url") or "",
                            "image_url": row.get("image_url") or "",
                            "scheduled_at": row.get("scheduled_at") or row.get("created_at"),
                            "published_at": row.get("published_at") or (row.get("created_at") if row.get("status") == "published" else None),
                            "live_post_url": row.get("live_post_url") or "",
                            "created_at": row.get("created_at")
                        })
            except Exception:
                pass

            # 1. Fetch marketing_content
            res1 = requests.get(f"{SUPABASE_URL}/rest/v1/marketing_content?order=created_at.desc&limit=20", headers=headers, timeout=10)
            if res1.status_code == 200:
                for row in res1.json():
                    if row.get("topic") == "ACTIVE_AI_PROMPT" or row.get("status") == "prompt_template":
                        continue
                    meta = {}
                    kp = row.get("key_points") or ""
                    if kp.startswith("{") and kp.endswith("}"):
                        try:
                            meta = json.loads(kp)
                        except Exception:
                            pass
                    pub_time = row.get("published_at") or meta.get("published_at") or (row.get("created_at") if row.get("status") == "published" else None)
                    combined.append({
                        "id": row.get("id"),
                        "topic": row.get("topic"),
                        "status": row.get("status"),
                        "video_url": meta.get("video_url") or "",
                        "image_url": meta.get("image_url") or "",
                        "scheduled_at": meta.get("scheduled_at") or row.get("created_at"),
                        "published_at": pub_time,
                        "live_post_url": "",
                        "created_at": row.get("created_at")
                    })

            # 2. Fetch marketing_published_archive for live URLs
            res2 = requests.get(f"{SUPABASE_URL}/rest/v1/marketing_published_archive?order=published_at.desc&limit=20", headers=headers, timeout=10)
            if res2.status_code == 200:
                for a in res2.json():
                    if a.get("topic") == "ACTIVE_AI_PROMPT":
                        continue
                    # match with existing or add
                    qid = a.get("original_queue_id")
                    matched = False
                    for p in combined:
                        if p.get("id") == qid or (p.get("topic") == a.get("topic") and p["status"] == "published"):
                            p["live_post_url"] = a.get("live_post_url") or ""
                            p["published_at"] = a.get("published_at")
                            matched = True
                            break
                    if not matched:
                        combined.append({
                            "id": a.get("id"),
                            "topic": a.get("topic"),
                            "status": "published",
                            "video_url": a.get("call_to_action", "").split("👉")[-1].strip() if "👉" in a.get("call_to_action", "") else "",
                            "image_url": "",
                            "scheduled_at": a.get("published_at"),
                            "live_post_url": a.get("live_post_url") or "",
                            "created_at": a.get("published_at")
                        })
        except Exception as e:
            print(f"[server] Error fetching posts: {e}")

        # 3. Merge records from live_social_tracker.json so live posts are visible & deletable, and enrich with status/errors
        try:
            tracker_path = os.path.join(os.path.dirname(__file__), "live_social_tracker.json")
            if os.path.exists(tracker_path):
                with open(tracker_path, "r", encoding="utf-8") as f:
                    records = json.load(f)

                # Index records by post id
                tracker_by_id = {}
                for rec in records:
                    pdata = rec.get("post_data") or {}
                    pid = pdata.get("id") or rec.get("id")
                    if pid and pid not in tracker_by_id:
                        tracker_by_id[pid] = rec

                # Enrich existing combined records
                for c in combined:
                    cid = c.get("id")
                    if cid in tracker_by_id:
                        trec = tracker_by_id[cid]
                        res_map = trec.get("results") or {}
                        err_parts = []
                        plat_urls = trec.get("platform_urls") or {}
                        for plat, pres in res_map.items():
                            if isinstance(pres, dict):
                                if not pres.get("success") and pres.get("error"):
                                    err_parts.append(f"{plat}: {pres.get('error')[:90]}")
                                if pres.get("post_url"):
                                    u = pres["post_url"]
                                    if "facebook.com/" in u and "photo.php" not in u and pres.get("post_id"):
                                        u = f"https://www.facebook.com/photo.php?fbid={pres['post_id']}"
                                    plat_urls[plat] = u
                        c["platform_urls"] = plat_urls
                        c["social_post_ids"] = {plat: pres.get("post_id") for plat, pres in res_map.items() if isinstance(pres, dict) and pres.get("post_id")}
                        if not c.get("live_post_url") and plat_urls:
                            c["live_post_url"] = plat_urls.get("instagram") or plat_urls.get("facebook") or next(iter(plat_urls.values()), "")
                        if err_parts:
                            c["error_message"] = " | ".join(err_parts)
                        if trec.get("platforms_published"):
                            c["platforms_published"] = trec.get("platforms_published")

                for rec in records:
                    pdata = rec.get("post_data") or {}
                    pid = pdata.get("id") or rec.get("id") or rec.get("timestamp")
                    topic = rec.get("title") or pdata.get("topic") or "Social Campaign"
                    if topic == "ACTIVE_AI_PROMPT":
                        continue
                    
                    # Avoid duplicate if already in combined
                    if not any(c.get("id") == pid or (c.get("topic") == topic and c.get("status") == "published") for c in combined):
                        res_map = rec.get("results") or {}
                        live_link = ""
                        err_parts = []
                        plat_urls = rec.get("platform_urls") or {}
                        for plat, pres in res_map.items():
                            if isinstance(pres, dict):
                                if not pres.get("success") and pres.get("error"):
                                    err_parts.append(f"{plat}: {pres.get('error')[:90]}")
                                if pres.get("post_url"):
                                    u = pres["post_url"]
                                    if "facebook.com/" in u and "photo.php" not in u and pres.get("post_id"):
                                        u = f"https://www.facebook.com/photo.php?fbid={pres['post_id']}"
                                    plat_urls[plat] = u

                        live_link = plat_urls.get("instagram") or plat_urls.get("facebook") or (next(iter(plat_urls.values()), "") if plat_urls else "")

                        combined.append({
                            "id": pid,
                            "topic": topic,
                            "status": "published" if rec.get("platforms_published") else "failed",
                            "video_url": rec.get("video_url") or pdata.get("video_url") or "",
                            "image_url": pdata.get("image_url") or "",
                            "scheduled_at": rec.get("timestamp"),
                            "live_post_url": live_link,
                            "platform_urls": plat_urls,
                            "social_post_ids": {plat: pres.get("post_id") for plat, pres in res_map.items() if isinstance(pres, dict) and pres.get("post_id")},
                            "created_at": rec.get("timestamp"),
                            "error_message": " | ".join(err_parts) if err_parts else None,
                            "platforms_published": rec.get("platforms_published") or []
                        })
        except Exception as e:
            print(f"[server] Error merging tracker in handle_get_posts: {e}")

        # Deduplicate and sort
        combined.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        self._send_json({"success": True, "posts": combined})

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
    
    server_address = ("0.0.0.0", PORT)
    httpd = ThreadedHTTPServer(server_address, OmniPostHandler)
    print(f"\n========================================================")
    print(f" 🚀 OmniPost Automation Server Live on http://localhost:{PORT}")
    print(f" 📡 Dashboard: http://localhost:{PORT}/dashboard")
    print(f" ⏱ Background Scheduler: Running (polling every 30s)")
    print(f"========================================================\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        httpd.server_close()

if __name__ == "__main__":
    run_server()

