"""
scheduler.py
Background scheduler engine that periodically inspects scheduled marketing posts
in Supabase and triggers live publishing when due.
"""

import os
import json
import time
import threading
from datetime import datetime, timezone, timedelta
import requests
from dotenv import load_dotenv
from publisher import dispatch_multi_channel_publish, delete_live_social_post, load_local_campaigns, save_local_campaign

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL") or "https://sqgxnhdsajinjhuiyntm.supabase.co"
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")

def get_headers():
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or SUPABASE_KEY
    return {
        "apikey": key or "",
        "Authorization": f"Bearer {key or ''}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def is_post_due(sched_str, now_dt):
    """
    Safely determines if a scheduled time string has arrived.
    Supports ISO formats with Z, timezone offsets (+00:00), or naive timestamps.
    Returns True if due, False if in the future or invalid.
    """
    if not sched_str:
        return True
    try:
        clean_ts = str(sched_str).strip().replace("Z", "+00:00")
        sched_dt = datetime.fromisoformat(clean_ts)
        # If naive, assume UTC so offset-naive vs aware comparison never crashes
        if sched_dt.tzinfo is None:
            sched_dt = sched_dt.replace(tzinfo=timezone.utc)
        return sched_dt <= now_dt
    except Exception as e:
        print(f"[scheduler] Warning parsing scheduled_at '{sched_str}': {e}")
        return False

def check_and_publish_due_posts():
    """Checks for posts with status 'scheduled' or 'approved' that have reached their scheduled time."""
    sb_url = os.getenv("SUPABASE_URL") or "https://sqgxnhdsajinjhuiyntm.supabase.co"
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
    has_supabase = bool(sb_url and sb_key and not str(sb_url).startswith("https://your-project") and not str(sb_key).startswith("your_supabase"))

    dispatched = []
    now_dt = datetime.now(timezone.utc)

    # 1. Process local scheduled campaigns from local_campaigns_db.json
    try:
        local_camps = load_local_campaigns()
        for camp in local_camps:
            if camp.get("status") in ("scheduled", "approved"):
                sched_at = camp.get("scheduled_at")
                if is_post_due(sched_at, now_dt):
                    camp["status"] = "publishing"
                    save_local_campaign(camp)
                    try:
                        print(f"[scheduler] Publishing due local campaign: {camp.get('topic') or camp.get('title')}")
                        pub_res = dispatch_multi_channel_publish(camp)
                        camp["status"] = "published" if pub_res.get("published_platforms") else "failed"
                        camp["published_at"] = datetime.now(timezone.utc).isoformat()
                        camp["live_post_url"] = pub_res.get("live_post_url", "")
                        camp["platform_urls"] = pub_res.get("platform_urls", {})
                        save_local_campaign(camp)
                        dispatched.append(camp.get("id"))
                    except Exception as e_pub:
                        print(f"[scheduler] Local campaign error: {e_pub}")
                        camp["status"] = "failed"
                        save_local_campaign(camp)
    except Exception as e_loc:
        print(f"[scheduler] Local campaigns check error: {e_loc}")

    if not has_supabase:
        return dispatched

    headers = get_headers()
    dispatched = []
    now_dt = datetime.now(timezone.utc)

    # 0. Self-healing: Recover any campaigns stuck in 'publishing' for > 15 minutes due to abrupt restarts
    try:
        stale_cutoff = (now_dt - timedelta(minutes=15)).isoformat()
        stale_url = f"{SUPABASE_URL}/rest/v1/video_campaigns?status=eq.publishing&created_at=lt.{stale_cutoff}"
        stale_res = requests.get(stale_url, headers=headers, timeout=4)
        if stale_res.status_code == 200:
            for stale_p in stale_res.json():
                print(f"[scheduler] ⚠️ Auto-recovering interrupted campaign ID: {stale_p.get('id')} - resetting to 'failed'")
                requests.patch(
                    f"{SUPABASE_URL}/rest/v1/video_campaigns?id=eq.{stale_p.get('id')}",
                    headers=headers,
                    json={"status": "failed"},
                    timeout=4
                )
    except Exception:
        pass

    # 1. Check fresh table 'video_campaigns'
    try:
        url_vc = f"{SUPABASE_URL}/rest/v1/video_campaigns?status=eq.scheduled&order=created_at.asc"
        res_vc = requests.get(url_vc, headers=headers, timeout=5)
        if res_vc.status_code == 200:
            for p in res_vc.json():
                sched_str = p.get("scheduled_at")
                if is_post_due(sched_str, now_dt):
                    # Lock immediately to prevent duplicate execution across threads
                    try:
                        requests.patch(
                            f"{SUPABASE_URL}/rest/v1/video_campaigns?id=eq.{p.get('id')}",
                            headers=headers,
                            json={"status": "publishing"},
                            timeout=5
                        )
                    except Exception:
                        pass

                    # Extract metadata including target platforms and X caption
                    meta_json = p.get("social_post_ids") if isinstance(p.get("social_post_ids"), dict) else {}
                    target_platforms = meta_json.get("platforms") or ["facebook", "instagram"]
                    x_caption = meta_json.get("x_caption") or p.get("hook") or p.get("generated_content", "")
                    vid_url = p.get("video_url", "")
                    threads_caption = meta_json.get("threads_caption") or p.get("threads_caption")
                    if not threads_caption or (vid_url and vid_url not in threads_caption):
                        h_t = p.get("hook") or p.get("title") or ""
                        sum_t = p.get("summary") or p.get("generated_content", "")
                        link_t = f"\n\n👉 Watch: {vid_url}" if vid_url else ""
                        avail = 500 - len(h_t) - len(link_t) - 10
                        body_t = sum_t[:avail].rstrip() + ("..." if len(sum_t) > avail else "")
                        threads_caption = f"🔥 {h_t}\n\n{body_t}{link_t}" if h_t else f"{body_t}{link_t}"

                    print(f"[scheduler] Publishing due video campaign ID: {p.get('id')} - {p.get('title')} to {target_platforms}")
                    post_payload = {
                        "id": p.get("id"),
                        "topic": p.get("title"),
                        "hook": p.get("hook", p.get("title")),
                        "summary": p.get("summary", ""),
                        "generated_content": p.get("generated_content", ""),
                        "hashtags": p.get("hashtags", ""),
                        "call_to_action": p.get("call_to_action", ""),
                        "video_url": p.get("video_url", ""),
                        "image_url": p.get("image_url", ""),
                        "platform_captions": {
                            "facebook": p.get("facebook_caption"),
                            "instagram": p.get("instagram_caption"),
                            "threads": threads_caption,
                            "x": x_caption,
                            "linkedin": p.get("linkedin_caption")
                        },
                        "platforms": target_platforms
                    }
                    try:
                        pub_res = dispatch_multi_channel_publish(post_payload)
                        dispatched.append({"id": p.get("id"), "result": pub_res})
                    except Exception as pe:
                        print(f"[scheduler] Error dispatching campaign {p.get('id')}: {pe}")
                        try:
                            requests.patch(
                                f"{SUPABASE_URL}/rest/v1/video_campaigns?id=eq.{p.get('id')}",
                                headers=headers,
                                json={"status": "failed"},
                                timeout=5
                            )
                        except Exception:
                            pass
    except Exception as e:
        err_msg = str(e)
        if "NameResolutionError" in err_msg or "Failed to resolve" in err_msg:
            print("[scheduler] ℹ️ Supabase network notice: DNS/connection blip on video_campaigns; will retry on next poll.")
        else:
            print(f"[scheduler] Warning querying video_campaigns: {e}")

    # 2. Check legacy/fallback 'marketing_content'
    try:
        url = f"{SUPABASE_URL}/rest/v1/marketing_content?status=eq.approved&order=created_at.asc"
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            for p in res.json():
                key_points_raw = p.get("key_points") or ""
                meta = {}
                if key_points_raw.startswith("{") and key_points_raw.endswith("}"):
                    try:
                        meta = json.loads(key_points_raw)
                    except Exception:
                        pass

                scheduled_at_str = meta.get("scheduled_at")
                if is_post_due(scheduled_at_str, now_dt):
                    print(f"[scheduler] Publishing due post ID: {p.get('id')} - {p.get('topic')}")
                    post_payload = {
                        "id": p.get("id"),
                        "topic": p.get("topic"),
                        "business_name": p.get("business_name"),
                        "hook": meta.get("hook", p.get("topic")),
                        "summary": meta.get("summary", ""),
                        "generated_content": p.get("generated_content", ""),
                        "hashtags": p.get("hashtags", ""),
                        "call_to_action": p.get("call_to_action", ""),
                        "video_url": meta.get("video_url", ""),
                        "image_url": meta.get("image_url", ""),
                        "platform_captions": meta.get("platform_captions", {})
                    }
                    try:
                        pub_result = dispatch_multi_channel_publish(post_payload)
                        dispatched.append({"id": p.get("id"), "result": pub_result})
                    except Exception as pe:
                        print(f"[scheduler] Error dispatching marketing_content {p.get('id')}: {pe}")
    except Exception as e:
        err_msg = str(e)
        if "NameResolutionError" in err_msg or "Failed to resolve" in err_msg:
            print("[scheduler] ℹ️ Supabase network notice: DNS/connection blip on marketing_content; will retry on next poll.")
        else:
            print(f"[scheduler] Warning querying marketing_content: {e}")

    return dispatched

def clean_supabase_storage_retention(days: int = 0) -> dict:
    """
    Safely purges Supabase database records and banner files.
    When days <= 0 (default): Erases the entire bucket (100% of files) and all campaign records.
    CRITICAL: Does NOT delete or affect real live posts on Facebook, Instagram, Threads, LinkedIn, or YouTube!
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return {"success": False, "error": "Supabase not configured"}

    headers = get_headers()
    storage_headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }

    deleted_files = []
    deleted_records = 0
    bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "marketing-banners")

    # 1. Clean banner files from Supabase Storage bucket (erase full bucket when days <= 0)
    try:
        list_url = f"{SUPABASE_URL}/storage/v1/object/list/{bucket}"
        res_files = requests.post(list_url, headers=storage_headers, json={"prefix": "", "limit": 1000}, timeout=15)
        if res_files.status_code == 200:
            all_files = res_files.json() or []
            file_names_to_delete = []

            if days <= 0:
                # Erase full bucket
                file_names_to_delete = [f.get("name") for f in all_files if f.get("name")]
            else:
                cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
                for file_obj in all_files:
                    created_at_str = file_obj.get("created_at")
                    if created_at_str:
                        try:
                            clean_ts = created_at_str.replace("Z", "+00:00")
                            f_dt = datetime.fromisoformat(clean_ts)
                            if f_dt < cutoff_dt:
                                file_names_to_delete.append(file_obj.get("name"))
                        except Exception:
                            pass

            if file_names_to_delete:
                for i in range(0, len(file_names_to_delete), 100):
                    chunk = file_names_to_delete[i:i+100]
                    del_url = f"{SUPABASE_URL}/storage/v1/object/{bucket}"
                    del_res = requests.delete(del_url, headers=storage_headers, json={"prefixes": chunk}, timeout=15)
                    if del_res.status_code in (200, 204):
                        deleted_files.extend(chunk)
                print(f"[supabase_cleanup] Purged {len(deleted_files)} files from {bucket}")
    except Exception as e:
        print(f"[supabase_cleanup] Warning cleaning storage bucket: {e}")

    # 2. Clean campaign records from Supabase tables (leaves social media untouched!)
    cutoff_iso = datetime.now(timezone.utc).isoformat()
    clean_tables = [
        "video_campaigns",
        "marketing_published_archive",
        "marketing_queue"
    ]
    for table in clean_tables:
        try:
            if days <= 0:
                del_table_url = f"{SUPABASE_URL}/rest/v1/{table}?id=not.is.null"
            else:
                cutoff_old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
                del_table_url = f"{SUPABASE_URL}/rest/v1/{table}?created_at=lt.{cutoff_old}"
            r = requests.delete(del_table_url, headers=headers, timeout=10)
            if r.status_code in (200, 204):
                deleted_records += 1
        except Exception:
            pass

    # Clean marketing_content campaigns while strictly preserving ACTIVE_AI_PROMPT and NOTIFICATION_ALERT
    try:
        del_mc_url = f"{SUPABASE_URL}/rest/v1/marketing_content?topic=not.in.(ACTIVE_AI_PROMPT,NOTIFICATION_ALERT)"
        r_mc = requests.delete(del_mc_url, headers=headers, timeout=10)
        if r_mc.status_code in (200, 204):
            deleted_records += 1
    except Exception:
        pass

    # Clear local tracker cache
    try:
        tracker_path = os.path.join(os.path.dirname(__file__), "live_social_tracker.json")
        with open(tracker_path, "w", encoding="utf-8") as f:
            json.dump([], f)
    except Exception:
        pass

    return {
        "success": True,
        "deleted_files_count": len(deleted_files),
        "deleted_records_count": deleted_records,
        "live_social_posts_status": "UNTOUCHED_AND_ACTIVE",
        "message": f"Successfully erased {len(deleted_files)} bucket file(s) and cleared campaign database data. Real live social media posts remain 100% active and untouched."
    }

class BackgroundScheduler(threading.Thread):
    def __init__(self, interval_seconds=30):
        super().__init__(daemon=True)
        self.interval = interval_seconds
        self.running = True

    def run(self):
        print(f"[scheduler] Background scheduler started (polling every {self.interval}s)")
        while self.running:
            try:
                check_and_publish_due_posts()
            except Exception as e:
                print(f"[scheduler] Loop exception: {e}")

            time.sleep(self.interval)

    def stop(self):
        self.running = False

_scheduler_instance = None

def start_scheduler(interval_seconds=30):
    global _scheduler_instance
    if _scheduler_instance is None or not _scheduler_instance.is_alive():
        _scheduler_instance = BackgroundScheduler(interval_seconds)
        _scheduler_instance.start()
    return _scheduler_instance

if __name__ == "__main__":
    print("Testing scheduler check...")
    res = check_and_publish_due_posts()
    print("Dispatched posts:", len(res))

