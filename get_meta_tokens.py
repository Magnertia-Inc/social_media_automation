"""
get_meta_tokens.py
Utility to generate 60-Day Long-Lived User Tokens and Never-Expiring Page/Instagram Tokens.
"""

import sys
import os
import requests
from dotenv import load_dotenv

load_dotenv()

def update_env(key: str, val: str):
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    found = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}=") or line.startswith(f"#{key}="):
            new_lines.append(f"{key}={val}\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={val}\n")
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

def exchange_token(app_id: str, app_secret: str, short_lived_token: str):
    print("\n[1/3] Exchanging short-lived token for 60-Day Long-Lived User Token...")
    url = "https://graph.facebook.com/v19.0/oauth/access_token"
    params = {
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": short_lived_token
    }
    
    r = requests.get(url, params=params)
    data = r.json()
    if "error" in data:
        print("❌ Error exchanging token:", data["error"]["message"])
        return None, None
        
    long_lived_token = data.get("access_token")
    expires_in = data.get("expires_in", 5184000) # ~60 days
    days = expires_in // 86400
    print(f"✅ Success! Long-Lived User Token obtained (Valid for ~{days} days / 60 days).")
    
    print("\n[2/3] Fetching Never-Expiring Page & Instagram Access Token from /me/accounts...")
    accounts_url = f"https://graph.facebook.com/v19.0/me/accounts?access_token={long_lived_token}"
    acc_res = requests.get(accounts_url).json()
    
    if "error" in acc_res:
        print("❌ Error fetching accounts:", acc_res["error"]["message"])
        return long_lived_token, None
        
    pages = acc_res.get("data", [])
    if not pages:
        fallback_page_id = os.getenv("FACEBOOK_PAGE_ID", "1485094014687108")
        print(f"⚠️ /me/accounts returned no direct pages. Checking configured page ID ({fallback_page_id})...")
        fallback_url = f"https://graph.facebook.com/v19.0/{fallback_page_id}?fields=id,name,access_token,instagram_business_account&access_token={long_lived_token}"
        f_res = requests.get(fallback_url).json()
        if f_res.get("access_token"):
            pages = [{
                "id": f_res.get("id"),
                "name": f_res.get("name"),
                "access_token": f_res.get("access_token"),
                "instagram_business_account": f_res.get("instagram_business_account")
            }]
        else:
            print("❌ Could not resolve Page access token. Error:", f_res)
            return long_lived_token, None

    print(f"Found {len(pages)} Page(s):")
    for i, p in enumerate(pages):
        print(f"  [{i+1}] {p.get('name')} (ID: {p.get('id')})")
        
    chosen_page = pages[0]
    page_token = chosen_page.get("access_token")
    page_id = chosen_page.get("id")
    page_name = chosen_page.get("name")
    
    print(f"\n[3/3] Inspecting Page Token for '{page_name}'...")
    debug_url = f"https://graph.facebook.com/debug_token?input_token={page_token}&access_token={page_token}"
    deb = requests.get(debug_url).json()
    expires_at = deb.get("data", {}).get("data_access_expires_at") or deb.get("data", {}).get("expires_at", 0)
    
    if expires_at == 0 or deb.get("data", {}).get("expires_at") == 0:
        print("🌟 AWESOME! This Page Access Token NEVER EXPIRES!")
    else:
        print(f"Token is valid with expiration timestamp: {expires_at}")
        
    # Check connected Instagram business account
    ig_check_url = f"https://graph.facebook.com/v19.0/{page_id}?fields=instagram_business_account&access_token={page_token}"
    ig_res = requests.get(ig_check_url).json()
    ig_id = ig_res.get("instagram_business_account", {}).get("id")
    if ig_id:
        print(f"📸 Connected Instagram Business Account ID: {ig_id}")
    else:
        print("⚠️ No Instagram Business Account linked to this Facebook Page.")
        
    return long_lived_token, page_token, page_id, ig_id

if __name__ == "__main__":
    app_id = os.getenv("FACEBOOK_APP_ID")
    app_secret = os.getenv("FACEBOOK_APP_SECRET")
    
    if not app_id or not app_secret:
        print("Please enter your Meta App ID and App Secret (from developers.facebook.com):")
        app_id = app_id or input("Meta App ID: ").strip()
        app_secret = app_secret or input("Meta App Secret: ").strip()
        
    short_token = input("Paste your Short-Lived User Access Token from Graph API Explorer: ").strip()
    
    if not short_token:
        print("No token provided. Exiting.")
        sys.exit(1)
        
    res = exchange_token(app_id, app_secret, short_token)
    if res and len(res) == 4 and res[1]:
        ll_user, page_tok, p_id, ig_id = res
        save = input("\nDo you want to save these tokens into .env automatically? (y/n): ").strip().lower()
        if save in ("y", "yes"):
            update_env("FACEBOOK_APP_ID", app_id)
            update_env("FACEBOOK_APP_SECRET", app_secret)
            update_env("FACEBOOK_USER_TOKEN", ll_user)
            update_env("FACEBOOK_TOKEN", page_tok)
            update_env("FACEBOOK_PAGE_ACCESS_TOKEN", page_tok)
            update_env("FACEBOOK_PAGE_ID", p_id)
            update_env("INSTAGRAM_ACCESS_TOKEN", page_tok)
            if ig_id:
                update_env("INSTAGRAM_BUSINESS_ACCOUNT_ID", ig_id)
                update_env("INSTAGRAM_ID", ig_id)
            print("🚀 Successfully updated .env with your permanent Page and Instagram tokens!")

