"""
notifications.py
Database-backed notification engine for OmniPost.
Persists publishing failures, fallbacks, and alerts to Supabase DB and local storage.
"""

import os
import json
import uuid
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
NOTIFICATIONS_FILE = os.path.join(os.path.dirname(__file__), "notifications.json")

def _get_supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def _read_local_notifications() -> list:
    if not os.path.exists(NOTIFICATIONS_FILE):
        return []
    try:
        with open(NOTIFICATIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []

def _write_local_notifications(items: list):
    try:
        with open(NOTIFICATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)
    except Exception as e:
        print(f"[notifications] Warning saving local notifications: {e}")

def create_notification(
    platform: str,
    title: str,
    message: str,
    type: str = "error",
    post_id: str = None,
    error_details: str = None
) -> dict:
    """
    Creates a new notification record and stores it into:
    1. Supabase database (marketing_content table with topic='NOTIFICATION_ALERT')
    2. Local persistent store (notifications.json)
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    notif_id = str(uuid.uuid4())
    
    meta_data = {
        "title": title,
        "platform": platform.lower().strip() if platform else "system",
        "error_details": (error_details or "")[:500],
        "post_id": post_id or "",
        "type": type,
        "is_read": False,
        "created_at": now_iso
    }

    item = {
        "id": notif_id,
        "platform": meta_data["platform"],
        "title": title,
        "message": message,
        "error_details": meta_data["error_details"],
        "post_id": meta_data["post_id"],
        "type": type,
        "is_read": False,
        "created_at": now_iso
    }

    # 1. Save to Supabase DB
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            headers = _get_supabase_headers()
            payload = {
                "id": notif_id,
                "topic": "NOTIFICATION_ALERT",
                "business_name": meta_data["platform"],
                "tone": type,
                "language": "English",
                "status": "unread",
                "key_points": json.dumps(meta_data),
                "generated_content": message,
                "call_to_action": meta_data["error_details"][:200] if meta_data["error_details"] else "View in Notifications"
            }
            res = requests.post(f"{SUPABASE_URL}/rest/v1/marketing_content", headers=headers, json=payload, timeout=8)
            if res.status_code in (200, 201) and res.json():
                saved_row = res.json()[0]
                item["id"] = saved_row.get("id", notif_id)
        except Exception as e:
            print(f"[notifications] Warning writing to Supabase: {e}")

    # 2. Save to local notifications.json
    local_items = _read_local_notifications()
    local_items.insert(0, item)
    # Keep up to 200 notifications
    local_items = local_items[:200]
    _write_local_notifications(local_items)

    print(f"[notifications] Created [{type.upper()}] notification for {platform}: {title}")
    return item

def get_notifications(limit: int = 50, unread_only: bool = False) -> dict:
    """
    Retrieves notifications from Supabase DB, falling back to local notifications.json.
    """
    notifications = []
    
    # 1. Try reading from Supabase DB
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}"
            }
            query = f"{SUPABASE_URL}/rest/v1/marketing_content?topic=eq.NOTIFICATION_ALERT&order=created_at.desc&limit={limit}"
            if unread_only:
                query += "&status=eq.unread"
            
            res = requests.get(query, headers=headers, timeout=6)
            if res.status_code == 200:
                for row in res.json():
                    kp_raw = row.get("key_points") or "{}"
                    meta = {}
                    if isinstance(kp_raw, str) and kp_raw.startswith("{"):
                        try:
                            meta = json.loads(kp_raw)
                        except Exception:
                            pass
                    elif isinstance(kp_raw, dict):
                        meta = kp_raw

                    notifications.append({
                        "id": row.get("id"),
                        "platform": meta.get("platform") or row.get("business_name") or "system",
                        "title": meta.get("title") or "System Notification",
                        "message": row.get("generated_content") or "",
                        "error_details": meta.get("error_details") or "",
                        "post_id": meta.get("post_id") or "",
                        "type": row.get("tone") or meta.get("type") or "error",
                        "is_read": row.get("status") == "read" or meta.get("is_read") is True,
                        "created_at": row.get("created_at") or meta.get("created_at")
                    })
        except Exception as e:
            print(f"[notifications] Query warning: {e}")

    # Fallback / merge with local notifications if Supabase returned nothing or offline
    if not notifications:
        local_items = _read_local_notifications()
        if unread_only:
            local_items = [n for n in local_items if not n.get("is_read")]
        notifications = local_items[:limit]

    unread_count = sum(1 for n in notifications if not n.get("is_read"))

    return {
        "success": True,
        "notifications": notifications,
        "unread_count": unread_count,
        "total_count": len(notifications)
    }

def mark_notification_read(notification_id: str = None, mark_all: bool = False) -> bool:
    """Marks a single notification or all notifications as read."""
    # Update Supabase
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            headers = _get_supabase_headers()
            if mark_all:
                requests.patch(
                    f"{SUPABASE_URL}/rest/v1/marketing_content?topic=eq.NOTIFICATION_ALERT",
                    headers=headers,
                    json={"status": "read"},
                    timeout=8
                )
            elif notification_id:
                requests.patch(
                    f"{SUPABASE_URL}/rest/v1/marketing_content?id=eq.{notification_id}&topic=eq.NOTIFICATION_ALERT",
                    headers=headers,
                    json={"status": "read"},
                    timeout=8
                )
        except Exception as e:
            print(f"[notifications] Update read status warning: {e}")

    # Update local notifications.json
    local_items = _read_local_notifications()
    for item in local_items:
        if mark_all or (notification_id and str(item.get("id")) == str(notification_id)):
            item["is_read"] = True
    _write_local_notifications(local_items)
    return True

def clear_all_notifications() -> bool:
    """Deletes all notification records from DB and local store."""
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            headers = _get_supabase_headers()
            requests.delete(
                f"{SUPABASE_URL}/rest/v1/marketing_content?topic=eq.NOTIFICATION_ALERT",
                headers=headers,
                timeout=8
            )
        except Exception as e:
            print(f"[notifications] Clear DB warning: {e}")

    _write_local_notifications([])
    return True
