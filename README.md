# 🚀 Multi-Channel Marketing Workflow: API Setup Guide

This guide provides end-to-end instructions and direct developer portal links to obtain API keys, Client IDs, Secrets, and long-lived access tokens for all supported social media platforms in this automated marketing system.

---

## 📋 Quick Reference: Developer Portal Links

| Platform | Developer Portal / Direct Link | Token Type | Required Permissions / Scopes |
| :--- | :--- | :--- | :--- |
| **Facebook & Instagram** | [Meta Developer Portal](https://developers.facebook.com/apps/) | Permanent Page Token | `pages_show_list`, `pages_manage_posts`, `pages_read_engagement`, `instagram_basic`, `instagram_content_publish` |
| **Meta Threads** | [Meta Threads API Settings](https://developers.facebook.com/apps/1058454950588661/use_cases/customize/settings/?use_case_enum=THREADS_API) | 60-Day User Token (Auto-Renewed) | `threads_basic`, `threads_content_publish` |
| **YouTube (Google Cloud)** | [Google Cloud Console](https://console.cloud.google.com/apis/credentials) | OAuth 2.0 Refresh Token | `https://www.googleapis.com/auth/youtube`, `https://www.googleapis.com/auth/youtube.upload` |
| **X (Twitter)** | [X Developer Portal](https://developer.x.com/en/portal/dashboard) | OAuth 1.0a / 2.0 User Token | `tweet.read`, `tweet.write`, `users.read`, `offline.access` |
| **LinkedIn** | [LinkedIn Developer Portal](https://www.linkedin.com/developers/apps) | OAuth 2.0 Access Token | `w_member_social`, `openid`, `profile`, `email` |
| **Pinterest** | [Pinterest Developer Portal](https://developers.pinterest.com/apps/) | OAuth 2.0 Access Token | `boards:read`, `pins:read`, `pins:write` |
| **Groq (AI Engine)** | [Groq Console](https://console.groq.com/keys) | API Key | AI Summaries & Caption Generation |
| **Supabase (Database)** | [Supabase Dashboard](https://supabase.com/dashboard) | Project URL & Anon/Service Key | Database & Banner Image Storage |

---

## 1. 📘 Facebook & 📸 Instagram (Meta Graph API)

Facebook and Instagram share **one single permanent Page Access Token**.

### Direct Links:
* **Apps Dashboard:** [https://developers.facebook.com/apps/](https://developers.facebook.com/apps/)
* **Graph API Explorer:** [https://developers.facebook.com/tools/explorer/](https://developers.facebook.com/tools/explorer/)
* **Access Token Debugger:** [https://developers.facebook.com/tools/debug/accesstoken/](https://developers.facebook.com/tools/debug/accesstoken/)

### Prerequisites:
1. Ensure your Facebook Page is linked to your Instagram Professional/Business account in **Facebook Page Settings → Linked Accounts → Instagram**.

### Steps to get the Permanent Page Token:
1. Go to [Meta for Developers](https://developers.facebook.com/apps/) and create an app (Type: **Business**).
2. Note your **App ID** and **App Secret** (from **App Settings → Basic**).
3. Open the [Graph API Explorer](https://developers.facebook.com/tools/explorer/):
   - In **Meta App**, select your app.
   - In **User or Page**, select **User Token**.
   - Under **Permissions**, add:
     - `pages_show_list`
     - `pages_read_engagement`
     - `pages_manage_posts`
     - `instagram_basic`
     - `instagram_content_publish`
     - `instagram_manage_contents`
   - Click **Generate Access Token** and accept the permissions.
4. **Convert to a Permanent Page Token**:
   - In the **User or Page** dropdown, switch from *User Token* to your Facebook Page (e.g., **Founders Gateway**).
   - Copy the generated token.
   - Check it in the [Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/): it should say **Expires: Never**.
5. **Find your IDs**:
   - **Facebook Page ID**: Under Page Settings or query `GET /me?fields=id,name`.
   - **Instagram Business ID**: Query `GET /{page-id}?fields=instagram_business_account`.

### Save in `.env`:
```env
FACEBOOK_APP_ID=your_meta_app_id
FACEBOOK_APP_SECRET=your_meta_app_secret
FACEBOOK_PAGE_ID=your_facebook_page_id
FACEBOOK_PAGE_ACCESS_TOKEN=your_permanent_page_token
INSTAGRAM_BUSINESS_ACCOUNT_ID=your_instagram_account_id
INSTAGRAM_ACCESS_TOKEN=your_permanent_page_token
INSTAGRAM_USERNAME=founders_gateway
```

---

## 2. 🧵 Meta Threads (Official Threads API)

Threads operates on a dedicated endpoint (`graph.threads.net`).

### Direct Links:
* **Threads API Use Case:** [https://developers.facebook.com/apps/](https://developers.facebook.com/apps/) → Select App → **Use Cases** → **Access the Threads API**
* **Threads API Settings:** [Meta Threads Settings Direct Link](https://developers.facebook.com/apps/1058454950588661/use_cases/customize/settings/?use_case_enum=THREADS_API)
* **App Roles & Testers:** [Meta App Roles Direct Link](https://developers.facebook.com/apps/1058454950588661/roles/roles/)
* **Threads Website Permissions:** [https://www.threads.net/settings/website_permissions](https://www.threads.net/settings/website_permissions)

### Steps to get the Threads Token:
1. In your Meta App, go to **Use cases** → click **Add use case** → select **Access the Threads API**.
2. **Add Your Threads Account as a Tester**:
   - Go to **App roles → Roles** (or click *Add or Remove Threads Testers*).
   - Click **Add People** → select **Threads Tester**.
   - Enter your Threads handle (e.g. `founders_gateway`) and click **Add**.
3. **Accept the Invite in Threads**:
   - Go to [Threads Website Permissions](https://www.threads.net/settings/website_permissions) (or Threads mobile app: **Settings → Account → Website permissions**).
   - In the **Invites** tab, click **Accept**.
4. **Generate the Long-Lived Token**:
   - Return to [Threads API Settings](https://developers.facebook.com/apps/1058454950588661/use_cases/customize/settings/?use_case_enum=THREADS_API).
   - Scroll down to **User Token Generator**.
   - Your account will appear with a **"Generate Token"** button.
   - Click it, copy the token, and copy your **Threads app ID** from the top of the page.

### Save in `.env`:
```env
THREADS_APP_ID=1669350754539486
THREADS_APP_SECRET=your_threads_app_secret
THREADS_ACCESS_TOKEN=THAAXu...
THREADS_USER_ID=your_threads_user_id
THREADS_USERNAME=founders_gateway
```
*(Note: The background server automatically calls `refresh_access_token` so this token stays permanently active).*

---

## 3. ▶️ YouTube (Google Cloud Console & OAuth 2.0)

Used for uploading videos, pinning comments, and posting community updates.

### Direct Links:
* **Google Cloud Console:** [https://console.cloud.google.com/](https://console.cloud.google.com/)
* **API Library (YouTube Data API v3):** [https://console.cloud.google.com/apis/library/youtube.googleapis.com](https://console.cloud.google.com/apis/library/youtube.googleapis.com)
* **Credentials Manager:** [https://console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials)
* **OAuth Consent Screen:** [https://console.cloud.google.com/apis/credentials/consent](https://console.cloud.google.com/apis/credentials/consent)

### Steps to get YouTube Credentials:
1. Create a project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable **YouTube Data API v3** in the API Library.
3. In **OAuth consent screen**:
   - User Type: **External**.
   - Add Test Users: Add the Gmail address that owns your YouTube channel.
   - Scopes: Add `https://www.googleapis.com/auth/youtube` and `https://www.googleapis.com/auth/youtube.force-ssl`.
4. In **Credentials**:
   - Click **Create Credentials** → **OAuth client ID**.
   - Application Type: **Web application**.
   - Authorized redirect URIs: Add `http://localhost:8080/callback/youtube`.
   - Copy **Client ID** and **Client Secret**.
5. **Get Refresh Token**:
   - Start the local server (`python3 server.py`).
   - Visit: `http://localhost:8080/auth/youtube`.
   - Log in with your Google Account and click **Allow**.
   - The server automatically captures and saves the permanent `GOOGLE_REFRESH_TOKEN` to `.env`.

### Save in `.env`:
```env
GOOGLE_CLIENT_ID=your_google_client_id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-your_secret
GOOGLE_REFRESH_TOKEN=1//0gEL...
YOUTUBE_API_KEY=AIzaSy...
YOUTUBE_CHANNEL_ID=your_channel_id
```

---

## 4. 🐦 X / Twitter (X Developer Portal)

Used for posting short-form summaries, threads, and campaign announcements.

### Direct Links:
* **X Developer Portal:** [https://developer.x.com/en/portal/dashboard](https://developer.x.com/en/portal/dashboard)
* **X Projects & Apps:** [https://developer.x.com/en/portal/projects-and-apps](https://developer.x.com/en/portal/projects-and-apps)

### Steps to get X Credentials:
1. Log in to the [X Developer Portal](https://developer.x.com/en/portal/dashboard) (requires Free or Basic tier).
2. Create a **Project** and an **App**.
3. In your App Settings, configure **User authentication settings**:
   - App permissions: **Read and write** (or *Read, write, and Direct Messages*).
   - Type of App: **Web App, Automated App or Bot**.
   - Callback / Redirect URL: `http://localhost:8080/callback/x`.
   - Website URL: `https://foundersgateway.in` (or your domain).
4. Go to **Keys and Tokens**:
   - **API Key & Secret (Consumer Keys)**: Generate and copy.
   - **OAuth 2.0 Client ID & Client Secret**: Generate and copy.
   - **Access Token & Secret**: Generate with *Read and Write* permissions.

### Save in `.env`:
```env
X_API_KEY=your_x_api_key
X_API_SECRET=your_x_api_secret
X_ACCESS_TOKEN=your_user_access_token
X_ACCESS_TOKEN_SECRET=your_user_token_secret
X_CLIENT_ID=your_oauth2_client_id
X_CLIENT_SECRET=your_oauth2_client_secret
```

---

## 5. 💼 LinkedIn (LinkedIn Developer Portal)

Used for sharing professional commentary, carousel graphics, and video highlights.

### Direct Links:
* **LinkedIn Developers:** [https://www.linkedin.com/developers/apps](https://www.linkedin.com/developers/apps)
* **LinkedIn Token Generator:** [https://www.linkedin.com/developers/tools/oauth](https://www.linkedin.com/developers/tools/oauth)

### Steps to get LinkedIn Credentials:
1. Go to [LinkedIn Developer Portal](https://www.linkedin.com/developers/apps) and click **Create App**.
2. Associate the app with your verified LinkedIn Company Page.
3. Under the **Products** tab, request:
   - **Share on LinkedIn**
   - **Sign In with LinkedIn using OpenID Connect**
4. Under the **Auth** tab:
   - Copy **Client ID** and **Primary Client Secret**.
   - Add Authorized redirect URL: `http://localhost:8080/callback/linkedin`.
5. Under **OAuth 2.0 tools** (or by hitting `/auth/linkedin` on your local server):
   - Generate an Access Token with `w_member_social`, `openid`, `profile`.

### Save in `.env`:
```env
LINKEDIN_CLIENT_ID=your_linkedin_client_id
LINKEDIN_CLIENT_SECRET=your_linkedin_client_secret
LINKEDIN_ACCESS_TOKEN=your_access_token
```

---

## 6. 📌 Pinterest (Pinterest Developers)

Used for publishing campaign pins, banners, and infographics.

### Direct Links:
* **Pinterest Developers Portal:** [https://developers.pinterest.com/apps/](https://developers.pinterest.com/apps/)
* **Pinterest API Documentation:** [https://developers.pinterest.com/docs/getting-started/introduction/](https://developers.pinterest.com/docs/getting-started/introduction/)

### Steps to get Pinterest Credentials:
1. Convert your Pinterest account to a **Business Account** (free).
2. Go to [Pinterest Developers](https://developers.pinterest.com/apps/) and click **Create App**.
3. Under App Details, note your **App ID** and **App Secret**.
4. In **Redirect URIs**, add `http://localhost:8080/callback/pinterest`.
5. Under **User Token** or via OAuth exchange:
   - Request scopes: `boards:read`, `pins:read`, `pins:write`.
   - Generate your Access Token.
6. Find your Board ID via `GET /v5/boards` or from your board URL.

### Save in `.env`:
```env
PINTEREST_APP_ID=your_pinterest_app_id
PINTEREST_APP_SECRET=your_pinterest_app_secret
PINTEREST_ACCESS_TOKEN=pina_your_token
PINTEREST_BOARD_ID=your_board_id
```

---

## 7. 🧠 Groq & 🗄️ Supabase (Backend Infrastructure)

### Groq (AI Engine for Repurposing & Summaries)
* **Portal Link:** [https://console.groq.com/keys](https://console.groq.com/keys)
* **Setup:** Create a free account → API Keys → **Create API Key**.
* **Save in `.env`:**
  ```env
  GROQ_API_KEY=your_groq_api_key_here
  GROQ_MODEL=openai/gpt-oss-120b
  ```

### Supabase (Database & Banner Media Bucket)
* **Portal Link:** [https://supabase.com/dashboard](https://supabase.com/dashboard)
* **Setup:** Select your project → **Project Settings → API**.
* **Storage:** In **Storage**, ensure bucket `marketing-banners` is created and public.
* **Save in `.env`:**
  ```env
  SUPABASE_URL=https://your-project.supabase.co
  SUPABASE_KEY=your_supabase_service_role_key_here
  SUPABASE_STORAGE_BUCKET=marketing-banners
  ```

---

## 🛠️ Testing & Verifying Connections

Once your `.env` is populated, start the marketing server and open the live dashboard:

```bash
# Start the automation server
python3 server.py
```

1. **Dashboard:** Open [http://localhost:8080/dashboard](http://localhost:8080/dashboard)
2. **Token Health Modal:** Click **"Manage Tokens"** in the top navigation to see live status checks (🟢 Active / 🔴 Action Required) for every platform.
3. **API Status Endpoint:** Visit [http://localhost:8080/api/status?force=true](http://localhost:8080/api/status?force=true) to view real-time HTTP 200 checks for all connected accounts.
