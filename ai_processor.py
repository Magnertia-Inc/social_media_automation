"""
ai_processor.py
Generates viral social media promotional content referencing existing videos or shorts
using Groq LLM with configurable and database-persisted prompt engineering.
"""

import os
import json
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_SECONDARY_API_KEY = os.getenv("GROQ_SECONDARY_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
DEFAULT_BUSINESS_NAME = os.getenv("DEFAULT_BUSINESS_NAME", "")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")

DEFAULT_SYSTEM_PROMPT = (
    "# ROLE\n"
    "You are an expert short-form social media copywriter and content repurposing strategist.\n\n"
    "Your task is to transform YouTube video titles, descriptions and transcripts into short, engaging, attention-grabbing social media captions.\n\n"
    "The goal is to attract viewers from social media and encourage them to watch the full YouTube video.\n\n"
    "# ABSOLUTE RULES\n\n"
    "1. Never mention studio names, agency names, sponsors or creator names.\n"
    "2. Never use promotional introductions such as \"In this video\", \"The creator explains\" or \"Check out our latest video\".\n"
    "3. Write directly to the audience using \"you\" and \"your\".\n"
    "4. Generate content strictly based on the supplied video topic and transcript.\n"
    "5. Do not invent statistics, facts or claims.\n"
    "6. Keep every caption short, natural and easy to read.\n"
    "7. Avoid lengthy explanations, unnecessary paragraphs and excessive bullet points.\n"
    "8. Always include the exact YouTube video URL in every platform caption.\n"
    "9. Include relevant hashtags without overloading the caption.\n\n"
    "# HOOK GENERATION (HIGHEST PRIORITY)\n\n"
    "Generate ONE powerful, attention-grabbing sentence.\n\n"
    "The hook must:\n"
    "- Capture attention immediately.\n"
    "- Highlight a surprising insight, common mistake or overlooked problem.\n"
    "- Create curiosity.\n"
    "- Be relevant to the actual video topic.\n"
    "- Use simple, conversational English.\n"
    "- Avoid exaggerated or misleading clickbait.\n\n"
    "Example:\n"
    "\"Your business isn't failing because of a lack of effort. You might be tracking the wrong metrics!\"\n\n"
    "Do not generate multiple hooks. Select the strongest one.\n\n"
    "# CONTENT LENGTH\n\n"
    "Keep the content concise.\n\n"
    "- Hook: 1 sentence.\n"
    "- Body: Maximum 1–2 short sentences.\n"
    "- Key takeaways: Maximum 2 short points, only when useful.\n"
    "- CTA: 1 short sentence.\n"
    "- Hashtags: 3–5 relevant hashtags.\n\n"
    "Avoid repeating the same information in the hook, body and summary.\n\n"
    "# PLATFORM-SPECIFIC FORMATTING\n\n"
    "1. INSTAGRAM\n"
    "- Strong opening hook.\n"
    "- One or two short sentences.\n"
    "- Maximum 2 short takeaway points.\n"
    "- YouTube video link with a clear watch CTA.\n"
    "- 3–5 relevant hashtags.\n"
    "- Make it visually readable with line breaks.\n\n"
    "2. FACEBOOK\n"
    "- Conversational and engaging.\n"
    "- Strong opening hook.\n"
    "- One short paragraph explaining the insight.\n"
    "- Direct YouTube watch link.\n"
    "- 2–4 relevant hashtags.\n"
    "- Avoid long educational posts.\n\n"
    "3. THREADS\n"
    "- Short, conversational and curiosity-driven.\n"
    "- Strong one-sentence hook.\n"
    "- Maximum 2 short takeaway points.\n"
    "- Include the exact YouTube URL.\n"
    "- Maximum 500 characters, including spaces and hashtags.\n\n"
    "4. PINTEREST\n"
    "- Generate an interesting, searchable title.\n"
    "- Write a concise description explaining the value.\n"
    "- Naturally include relevant topic keywords.\n"
    "- Include the exact YouTube URL.\n"
    "- Add 3–5 relevant hashtags.\n"
    "- Make it suitable for Pinterest discovery.\n\n"
    "# CALL TO ACTION\n\n"
    "Use natural CTAs such as:\n"
    "- \"Watch the full breakdown on YouTube 👇\"\n"
    "- \"Discover the full strategy here 👇\"\n"
    "- \"Watch the complete explanation 👇\"\n\n"
    "Always include the exact supplied YouTube URL.\n\n"
    "# OUTPUT REQUIREMENTS\n\n"
    "Return ONLY valid JSON.\n\n"
    "Follow this exact schema:\n"
    "{\n"
    '  "hook": "One powerful attention-grabbing sentence",\n'
    '  "summary": [\n'
    '    "Key insight 1",\n'
    '    "Key insight 2"\n'
    "  ],\n"
    '  "generated_content": "One or two concise sentences explaining the main value of the video",\n'
    '  "hashtags": "#RelevantTag1 #RelevantTag2 #RelevantTag3",\n'
    '  "call_to_action": "Short CTA with the exact YouTube URL",\n'
    '  "platform_captions": {\n'
    '    "instagram": "Short Instagram caption with hook, concise body, YouTube URL and hashtags",\n'
    '    "facebook": "Short conversational Facebook caption with hook, body, YouTube URL and hashtags",\n'
    '    "threads": "Short Threads caption under 500 characters with hook, key insights, YouTube URL and hashtags",\n'
    '    "pinterest": "SEO-friendly Pinterest title, concise description, YouTube URL and hashtags"\n'
    "  }\n"
    "}\n\n"
    "IMPORTANT:\n"
    "- Do not generate lengthy content.\n"
    "- Do not repeat the same paragraphs across platforms.\n"
    "- Do not omit the YouTube link.\n"
    "- Do not add unnecessary introductions.\n"
    "- Prioritize attention, clarity, curiosity and clicks.\n"
    "- Ensure the output is valid JSON without Markdown code fences."
)

PROMPT_TEMPLATES = {
    "viral_pattern_interrupt": {
        "title": "Viral Pattern-Interrupt",
        "description": "Scroll-stopping contrarian truths and false-cause reframes that drive organic reach.",
        "prompt": DEFAULT_SYSTEM_PROMPT
    },
    "b2b_thought_leadership": {
        "title": "B2B Thought Leadership",
        "description": "Executive frameworks, strategic ROI analysis, and authoritative insights for decision makers.",
        "prompt": (
            "You are an elite B2B growth strategist and executive ghostwriter. "
            "Your objective is to translate video content into authoritative, high-credibility insights for founders, directors, and executives.\n\n"
            "RULES:\n"
            "1. Focus on operational leverage, ROI, systems thinking, and strategic bottlenecks.\n"
            "2. Zero self-promotion, fluff, or clichés. Write with calm confidence and high analytical rigor.\n"
            "3. Hook must be a thought-provoking industry insight or counter-intuitive management thesis.\n"
            "4. Body: 2 concise paragraphs unpacking the strategic friction and actionable framework.\n"
            "5. Summary: 3 bullet takeaways structured as operational action items.\n"
            "6. Call to Action: Professional invitation to examine the full video breakdown.\n\n"
            "Respond ONLY with a valid JSON object matching this schema:\n"
            "{\n"
            '  "hook": "Authoritative, thought-provoking executive hook",\n'
            '  "summary": ["Actionable framework takeaway 1", "Takeaway 2", "Takeaway 3"],\n'
            '  "generated_content": "2 analytical paragraphs diagnosing the strategic bottleneck and outlining the framework",\n'
            '  "hashtags": "#Leadership #Strategy #Operations #ExecutiveGrowth",\n'
            '  "call_to_action": "Watch the full masterclass here",\n'
            '  "platform_captions": {\n'
            '    "linkedin": "Full B2B post with hook, analytical body, bullets, CTA, hashtags",\n'
            '    "facebook": "Engaging conversational analysis with video link",\n'
            '    "instagram": "Key visual insights, takeaway bullets, and link in bio CTA",\n'
            '    "threads": "Executive hook, key takeaway bullet, and video link under 420 chars",\n'
            '    "x": "Punchy executive insight under 260 chars with link"\n'
            "  }\n"
            "}"
        )
    },
    "contrarian_mythbuster": {
        "title": "Contrarian Mythbuster",
        "description": "Directly attacks industry misconceptions, expensive mistakes, and conventional bad advice.",
        "prompt": (
            "You are a provocative, contrarian direct-response marketing master. "
            "Your mission is to dismantle outdated conventional advice and reveal the hidden cost of common mistakes based on the video topic.\n\n"
            "RULES:\n"
            "1. The hook must challenge a deeply held belief or highlight an expensive myth people blindly follow.\n"
            "2. Tone: Bold, uncompromising, highly persuasive, and radically transparent.\n"
            "3. Body: Break down why the common approach fails and reveal the counter-intuitive reality.\n"
            "4. Summary: 3 punchy, eye-opening takeaways from the video.\n"
            "5. CTA: Direct viewers to watch the unfiltered breakdown at the video link.\n\n"
            "Respond ONLY with a valid JSON object matching this schema:\n"
            "{\n"
            '  "hook": "Bold, myth-busting statement exposing common bad advice",\n'
            '  "summary": ["Myth vs reality insight 1", "Insight 2", "Insight 3"],\n'
            '  "generated_content": "2 hard-hitting paragraphs exposing why standard advice fails and what actually works",\n'
            '  "hashtags": "#UnpopularTruth #Debunked #GrowthHacks #BusinessSecrets",\n'
            '  "call_to_action": "See the unfiltered evidence in the full video",\n'
            '  "platform_captions": {\n'
            '    "linkedin": "Full contrarian breakdown with hook, body, bullets, CTA, hashtags",\n'
            '    "facebook": "Compelling debate-sparking post with video link",\n'
            '    "instagram": "High-energy caption calling out the myth, with bullets & CTA",\n'
            '    "x": "Spicy contrarian tweet under 260 chars with link"\n'
            "  }\n"
            "}"
        )
    },
    "story_breakdown": {
        "title": "Story & Lessons Learned",
        "description": "Narrative-driven hook, emotional friction, turning point, and relatable tactical lessons.",
        "prompt": (
            "You are a master digital storyteller and audience retention expert. "
            "Your goal is to package video lessons into an emotionally compelling journey from struggle to breakthrough.\n\n"
            "RULES:\n"
            "1. Hook: Start right in the middle of a high-stakes realization or relatable moment of friction.\n"
            "2. Body: Tell the transition story—the turning point where everything clicked.\n"
            "3. Summary: 3 memorable moral/tactical lessons learned.\n"
            "4. CTA: Invite the audience to see the complete story unfold in the video.\n\n"
            "Respond ONLY with a valid JSON object matching this schema:\n"
            "{\n"
            '  "hook": "Narrative hook starting in the middle of the conflict or realization",\n'
            '  "summary": ["Lesson 1 from the journey", "Lesson 2", "Lesson 3"],\n'
            '  "generated_content": "2 narrative paragraphs capturing the struggle, turning point, and lesson",\n'
            '  "hashtags": "#BehindTheScenes #FounderJourney #LessonsLearned #Storytelling",\n'
            '  "call_to_action": "Watch the complete story unfold here",\n'
            '  "platform_captions": {\n'
            '    "linkedin": "Full storytelling post with narrative hook, body, key lessons, CTA",\n'
            '    "facebook": "Relatable emotional story post with video link",\n'
            '    "instagram": "Visual storytelling caption with key insights & CTA",\n'
            '    "x": "Snappy mini-story thread hook under 260 chars with link"\n'
            "  }\n"
            "}"
        )
    }
}

def get_active_prompt() -> dict:
    """
    Retrieves the active system prompt from Supabase database or local fallback cache.
    """
    # 1. Supabase marketing_content table
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json"
            }
            url = f"{SUPABASE_URL}/rest/v1/marketing_content?topic=eq.ACTIVE_AI_PROMPT&status=eq.prompt_template&order=created_at.desc&limit=1"
            res = requests.get(url, headers=headers, timeout=5)
            if res.status_code == 200 and res.json():
                row = res.json()[0]
                content = row.get("generated_content")
                if content and len(content.strip()) > 40:
                    return {
                        "prompt": content.strip(),
                        "tone": row.get("tone") or "Viral Pattern-Interrupt",
                        "source": "supabase",
                        "updated_at": row.get("created_at")
                    }
        except Exception as e:
            print(f"[ai_processor] Supabase prompt fetch error: {e}")

    # 2. Local fallback custom_prompt.json
    cache_path = os.path.join(os.path.dirname(__file__), "custom_prompt.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                d = json.load(f)
                if d.get("prompt"):
                    return {
                        "prompt": d["prompt"],
                        "tone": d.get("tone", "Viral Pattern-Interrupt"),
                        "source": "local_cache",
                        "updated_at": d.get("updated_at")
                    }
        except Exception:
            pass

    # 3. Default
    return {
        "prompt": DEFAULT_SYSTEM_PROMPT,
        "tone": "Viral Pattern-Interrupt",
        "source": "default",
        "updated_at": None
    }

def save_active_prompt(prompt_text: str, tone: str = "Viral Pattern-Interrupt") -> dict:
    """
    Persists the custom system prompt into Supabase database and local custom_prompt.json.
    """
    clean_prompt = (prompt_text or "").strip()
    if not clean_prompt:
        clean_prompt = DEFAULT_SYSTEM_PROMPT

    now_iso = datetime.now(timezone.utc).isoformat()
    record = {
        "prompt": clean_prompt,
        "tone": tone,
        "updated_at": now_iso
    }
    
    # Save local cache
    cache_path = os.path.join(os.path.dirname(__file__), "custom_prompt.json")
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
    except Exception as e:
        print(f"[ai_processor] Local prompt save error: {e}")

    # Save to Supabase marketing_content
    db_saved = False
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json"
            }
            # Clean previous active prompt
            requests.delete(f"{SUPABASE_URL}/rest/v1/marketing_content?topic=eq.ACTIVE_AI_PROMPT", headers=headers, timeout=5)
            # Insert new
            payload = {
                "topic": "ACTIVE_AI_PROMPT",
                "business_name": "OmniPost AI Studio",
                "audience": "Viral Social Media",
                "tone": tone,
                "key_points": json.dumps({"active": True, "saved_at": now_iso}),
                "language": "English",
                "generated_content": clean_prompt,
                "hashtags": "#AIPrompt",
                "call_to_action": "Saved to database",
                "status": "prompt_template"
            }
            r = requests.post(f"{SUPABASE_URL}/rest/v1/marketing_content", headers=headers, json=payload, timeout=6)
            if r.status_code in (200, 201, 204):
                db_saved = True
        except Exception as e:
            print(f"[ai_processor] Supabase prompt insert error: {e}")

    return {
        "success": True,
        "prompt": clean_prompt,
        "tone": tone,
        "source": "supabase" if db_saved else "local_cache",
        "db_saved": db_saved,
        "updated_at": now_iso
    }

def optimize_prompt(current_prompt: str, goal: str = "viral_engagement") -> dict:
    """
    Uses Groq LLM to intelligently refine and optimize the prompt text.
    """
    meta_system = (
        "You are an elite AI Prompt Engineer specializing in viral direct-response social media copywriting. "
        "Your task is to optimize and sharpen the given system prompt. "
        "Elevate the hook rules, pattern interrupts, and psychological drivers. "
        "CRITICAL: Keep the JSON response schema and required output structure intact. "
        "Return ONLY the complete, refined prompt text. Do NOT wrap in markdown fences (```) or include introductory pleasantries."
    )
    meta_user = (
        f"Optimization Goal: {goal or 'Maximize viral engagement, CTR back to the video, and pattern interrupts'}\n\n"
        f"CURRENT PROMPT TO OPTIMIZE:\n\"\"\"\n{current_prompt}\n\"\"\"\n\n"
        "Output the optimized system prompt now:"
    )
    messages = [
        {"role": "system", "content": meta_system},
        {"role": "user", "content": meta_user}
    ]
    keys_to_try = [
        (GROQ_API_KEY, GROQ_MODEL),
        (GROQ_API_KEY, "openai/gpt-oss-20b"),
        (GROQ_SECONDARY_API_KEY, GROQ_MODEL)
    ]
    for key, model in keys_to_try:
        if not key:
            continue
        try:
            res = call_groq_chat(messages, key, model, json_format=False)
            opt_text = res["choices"][0]["message"]["content"].strip()
            # Clean any accidental outer markdown fences
            if opt_text.startswith("```"):
                lines = opt_text.splitlines()
                if len(lines) > 2 and lines[0].startswith("```"):
                    opt_text = "\n".join(lines[1:-1]).strip()
            return {"success": True, "optimized_prompt": opt_text}
        except Exception as e:
            print(f"[ai_processor] Optimize prompt failed with {model}: {e}")
            continue

    return {"success": False, "error": "LLM prompt optimization unavailable. Please check Groq API key."}

def call_groq_chat(messages: list, api_key: str, model: str, json_format: bool = True) -> dict:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1400
    }
    if json_format:
        payload["response_format"] = {"type": "json_object"}

    response = requests.post(url, headers=headers, json=payload, timeout=25)
    response.raise_for_status()
    return response.json()

def sanitize_generated_copy(text: str) -> str:
    """Removes any unwanted studio/agency self-references or legacy name mentions."""
    if not text:
        return ""
    import re
    patterns = [
        r"(?i)\bPrem\s+Marketing(\s+Studio)?\b",
        r"(?i)Prem Marketing Studio pulls back the curtain\.{0,3}",
        r"(?i)Prem Marketing Studio reveals\.{0,3}",
        r"(?i)pulls back the curtain\.{0,3}",
        r"(?i)In this video, the creator reveals\.{0,3}",
    ]
    cleaned = str(text)
    for pat in patterns:
        cleaned = re.sub(pat, "", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned)
    return cleaned.strip()

def repurpose_video_content(video_context: dict, business_name: str = None, custom_prompt: str = None) -> dict:
    """
    Transforms video context (title, description, transcript, link) into
    engaging promotional copy pointing followers back to the video.
    """
    brand = business_name or DEFAULT_BUSINESS_NAME or ""
    title = video_context.get("title", "Featured Video")
    description = video_context.get("description", "")
    transcript = video_context.get("transcript", "")
    clean_url = video_context.get("clean_url", "https://youtube.com")
    platform = video_context.get("platform", "video")

    # Use custom prompt if provided, else fetch active saved prompt from DB / cache
    if custom_prompt and len(custom_prompt.strip()) > 40:
        system_prompt = custom_prompt.strip()
    else:
        system_prompt = get_active_prompt()["prompt"]

    transcript_section = (
        f"Full Spoken Video Transcript / Dialogue:\n\"\"\"\n{transcript[:15000]}\n\"\"\""
        if transcript and transcript.strip()
        else "Spoken Transcript: Not available on YouTube (captions disabled). Rely on Video Title and Description."
    )

    user_prompt = (
        f"Video Title: {title}\n"
        f"Video Link: {clean_url}\n"
        f"Platform Type: {platform}\n"
        f"Video Description: {description[:3000]}\n"
        f"{transcript_section}\n\n"
        f"CRITICAL RULES (MUST FOLLOW):\n"
        f"1. Zero self-referencing. Do NOT mention any studio name, agency name, sponsor, or creator name anywhere in the post!\n"
        f"2. HOOK FORMAT: Write a powerful 1-sentence pattern-interrupt hook based on the video topic.\n"
        f"   Target Style: \"Your business isn't failing because of a lack of effort. It may be failing because you're tracking the wrong metrics!\"\n"
        f"3. ZERO 3rd-PERSON PROMOTIONAL INTROS: Do NOT say 'In this video...', 'The creator explains...', or 'Check out our new video'.\n"
        f"4. Speak directly to the viewer ('you' / 'your business'), addressing the real friction point and the breakthrough from the video.\n"
        f"5. End with an irresistible call-to-action directing them to watch the full video at {clean_url}.\n\n"
        f"Generate the optimized JSON caption package now in valid JSON format!"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]

    keys_to_try = [
        (GROQ_API_KEY, GROQ_MODEL),
        (GROQ_API_KEY, "openai/gpt-oss-20b"),
        (GROQ_API_KEY, "qwen/qwen3.8-27b"),
        (GROQ_SECONDARY_API_KEY, GROQ_MODEL),
        (GROQ_SECONDARY_API_KEY, "openai/gpt-oss-20b")
    ]

    last_error = None
    for key, model in keys_to_try:
        if not key:
            continue
        try:
            data = call_groq_chat(messages, key, model, json_format=True)
            content_str = data["choices"][0]["message"]["content"]
            parsed = json.loads(content_str)
            
            # Format summary if list
            summary_val = parsed.get("summary")
            if isinstance(summary_val, list):
                summary_text = "\n".join([f"• {sanitize_generated_copy(item)}" for item in summary_val])
            else:
                summary_text = sanitize_generated_copy(str(summary_val or ""))

            cta = parsed.get("call_to_action", f"Watch now: {clean_url}")
            if clean_url not in cta:
                cta = f"{cta} 👉 {clean_url}"
            cta = sanitize_generated_copy(cta)

            raw_captions = parsed.get("platform_captions", {})
            clean_captions = {
                k: sanitize_generated_copy(v) for k, v in raw_captions.items()
            } if isinstance(raw_captions, dict) else {}

            hook_clean = sanitize_generated_copy(parsed.get("hook", f"Watch: {title}"))
            body_clean = sanitize_generated_copy(parsed.get("generated_content", description or title))
            tags_clean = sanitize_generated_copy(parsed.get("hashtags", "#Video #Marketing #Insights"))

            th_body = summary_text if (summary_text and len(summary_text) > 20) else body_clean
            th_link = f"\n\n👉 Watch: {clean_url}"
            th_avail = 500 - len(hook_clean) - len(th_link) - 10
            th_body_fmt = th_body[:th_avail].rstrip() + ("..." if len(th_body) > th_avail else "")
            threads_formatted = f"🔥 {hook_clean}\n\n{th_body_fmt}{th_link}"

            if not clean_captions:
                clean_captions = {
                    "instagram": f"{hook_clean}\n\n{body_clean}\n\n{cta}\n\n{tags_clean}",
                    "facebook": f"{hook_clean}\n\n{body_clean}\n\n{cta}",
                    "threads": threads_formatted,
                    "pinterest": f"{hook_clean}\n\n{body_clean}\n\n{cta}\n\n{tags_clean}",
                    "linkedin": f"{hook_clean}\n\n{body_clean}\n\n{cta}\n\n{tags_clean}",
                    "x": f"{hook_clean}\n\n{cta}"[:280]
                }
            elif "threads" not in clean_captions or clean_url not in clean_captions["threads"]:
                clean_captions["threads"] = threads_formatted

            return {
                "hook": hook_clean,
                "summary": summary_text,
                "generated_content": body_clean,
                "hashtags": tags_clean,
                "call_to_action": cta,
                "platform_captions": clean_captions
            }
        except Exception as e:
            last_error = e
            print(f"[ai_processor] Attempt with model {model} failed: {e}")
            continue

    # Fallback if Groq unavailable
    print(f"[ai_processor] All LLM calls failed, falling back to structured templates: {last_error}")
    topic_ref = title.strip() if title and title != "Featured Video" else "tracking the wrong metrics"
    fallback_hook = f"Your business isn't stuck because of a lack of effort. Here is the breakthrough strategy behind {topic_ref}!" if title and title != "Featured Video" else "Your business isn't failing because of a lack of effort. It may be failing because you're tracking the wrong metrics!"
    fallback_content = (
        f"Working harder in the wrong direction only leads to burnout. In \"{topic_ref}\", the key is identifying "
        "the few critical levers that actually drive real growth and compound results.\n\n"
        "Shift your focus to high-leverage execution. Stop guessing and realign your strategy."
    )
    fallback_summary = f"• Uncover the core bottlenecks holding back growth\n• Shift focus from vanity metrics to compound ROI\n• Execute on high-leverage business fundamentals"
    fallback_cta = f"Watch the full strategy breakdown here 👉 {clean_url}"
    hashtags = "#BusinessGrowth #Strategy #Leadership #ExecutiveInsights"

    return {
        "hook": fallback_hook,
        "summary": fallback_summary,
        "generated_content": fallback_content,
        "hashtags": hashtags,
        "call_to_action": fallback_cta,
        "platform_captions": {
            "instagram": f"🎯 {fallback_hook}\n\n{fallback_summary}\n\n🔗 Watch here: {clean_url}\n\n{hashtags}",
            "facebook": f"🚨 {fallback_hook}\n\n{fallback_content}\n\n👉 Full video: {clean_url}",
            "threads": f"🔥 {fallback_hook}\n\n{fallback_content[:240]}\n\n👉 {clean_url}"[:500],
            "pinterest": f"📌 {fallback_hook}\n\n{fallback_content}\n\n👉 Watch: {clean_url}\n\n{hashtags}",
            "linkedin": f"💡 {fallback_hook}\n\n{fallback_content}\n\n{fallback_summary}\n\n{fallback_cta}\n\n{hashtags}",
            "x": f"🔥 {fallback_hook}\n\n👉 Watch here: {clean_url}\n#Business"
        }
    }

if __name__ == "__main__":
    from video_extractor import get_complete_video_context
    ctx = get_complete_video_context("ic4HSdThFc8")
    result = repurpose_video_content(ctx)
    print("Hook:", result["hook"])
    print("CTA:", result["call_to_action"])
    print("LinkedIn Caption:", result["platform_captions"]["linkedin"][:120])
