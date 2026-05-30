import asyncio
import json
import logging
import os
import subprocess
import tempfile
import textwrap
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, UTC
from pathlib import Path

import httpx
from fastapi import FastAPI
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DB_PATH      = Path(os.environ.get("DB_PATH", "/data/agency.db"))
DISCORD_URL  = os.environ.get("DISCORD_BOT_URL", "http://agency-discord:8103/alert")
OLLAMA_URL   = os.environ.get("OLLAMA_BASE_URL", "http://host.containers.internal:11434")
BK_MODEL     = os.environ.get("BACKEND_MODEL", "gemma4:26b")
TTS_URL      = os.environ.get("SPEACHES_URL", "http://agency-kokoro:8880")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")
GCP_TTS_KEY      = os.environ.get("GCP_TTS_KEY", os.environ.get("GEMINI_API_KEY", ""))
GCP_TTS_VOICE    = os.environ.get("GCP_TTS_VOICE", "en-US-Journey-F")
ELEVENLABS_KEY   = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
PEXELS_KEY   = os.environ.get("PEXELS_API_KEY", "")
AGENT        = "agency-video"
VIDEO_DIR    = Path("/data/videos")
FONT_PATH    = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_SMALL   = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

NICHE_QUERIES = {
    "HVAC": "hvac air conditioning technician repair",
    "Plumbing": "plumber pipe repair water",
    "Dental": "dentist dental office smile",
    "Auto Repair": "car mechanic auto repair garage",
    "Law Firm": "lawyer attorney office professional",
    "Property Management": "apartment building property management",
    "Landscaping": "landscaping lawn garden outdoor",
    "Roofing": "roofing contractor roof house",
    "Pest Control": "pest control exterminator home",
    "Electrician": "electrician electrical wiring professional",
}

# YouTube OAuth
YT_CLIENT_ID      = os.environ.get("YOUTUBE_CLIENT_ID", "")
YT_CLIENT_SECRET  = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
YT_REFRESH_TOKEN  = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")

# TikTok
TIKTOK_CLIENT_KEY    = os.environ.get("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "")
TIKTOK_ACCESS_TOKEN  = os.environ.get("TIKTOK_ACCESS_TOKEN", "")
TIKTOK_REFRESH_TOKEN = os.environ.get("TIKTOK_REFRESH_TOKEN", "")

W, H     = 1080, 1920
BG_COLOR = (11, 11, 20)
AC_COLOR = (34, 211, 238)
TX_COLOR = (255, 255, 255)
DIM_COLOR= (150, 150, 170)

videos_made_today = 0
start_time = datetime.now(UTC)

# Only one FFmpeg job at a time — prevents CPU saturation on low-end hardware
_video_sem = asyncio.Semaphore(1)

# Mutable token store (refreshed at runtime without restarting)
_tiktok_token = {"access_token": TIKTOK_ACCESS_TOKEN, "refresh_token": TIKTOK_REFRESH_TOKEN}


async def send_discord(msg: str):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(DISCORD_URL, json={"content": f"🎬 **Video**: {msg}"})
    except Exception as e:
        logger.warning(f"Discord failed: {e}")


async def _llm(prompt: str) -> str:
    # Gemini 2.5 Flash — best for creative marketing scripts
    if GEMINI_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}",
                    json={"contents": [{"parts": [{"text": prompt}]}]},
                )
            if r.status_code == 200:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            logger.warning(f"Gemini failed: {e}")
    # Groq fallback
    if GROQ_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 600},
                )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"Groq failed: {e}")
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{OLLAMA_URL}/api/generate",
                         json={"model": BK_MODEL, "prompt": prompt, "stream": False})
        return r.json().get("response", "").strip()


async def generate_thumbnail(niche: str, title: str, out_dir: str) -> str | None:
    """Generate a YouTube thumbnail using Imagen 3 (Google AI Studio)."""
    if not GEMINI_API_KEY:
        return None
    import base64
    prompt = (
        f"Professional YouTube Short thumbnail for a video about AI chatbots helping {niche} businesses. "
        f"Bold and eye-catching. Dark blue/black background. Show a {niche} professional looking at their phone. "
        f"Large bold white text overlay. Modern, high-contrast design. No text in image — image only. "
        f"Style: clean tech marketing, not cheesy."
    )
    try:
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/imagen-3.0-generate-001:predict?key={GEMINI_API_KEY}",
                json={
                    "instances": [{"prompt": prompt}],
                    "parameters": {"sampleCount": 1, "aspectRatio": "16:9"},
                },
            )
        if r.status_code == 200:
            predictions = r.json().get("predictions", [])
            if predictions:
                img_data = base64.b64decode(predictions[0]["bytesBase64Encoded"])
                thumb_path = str(Path(out_dir) / f"thumb_{niche.replace(' ', '_').lower()}.jpg")
                Path(thumb_path).write_bytes(img_data)
                logger.info(f"Imagen thumbnail generated: {thumb_path}")
                return thumb_path
        else:
            logger.warning(f"Imagen API HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"Imagen thumbnail failed: {e}")
    return None


async def generate_script(niche: str) -> dict:
    prompt = f"""You are a viral short-form video scriptwriter for RingCatch, an AI chatbot service for local small businesses ($450 setup + $89/month).

Write a 45-60 second YouTube Shorts / TikTok script targeting {niche} business owners.

The script must:
- Open with a pattern-interrupt hook that stops the scroll (speak directly to the pain, be specific and bold)
- Use conversational, natural language — written to be SPOKEN aloud, not read
- Feel like advice from a friend who runs a business, not a sales pitch
- Include a real stat or relatable scenario specific to {niche}
- End with a low-friction CTA (free demo at ringcatch.io, no credit card)

Return ONLY valid JSON — no markdown, no explanation, just the JSON object:
{{
  "title": "Short punchy title for the video card (max 55 chars, no emoji)",
  "hook": "The first 3-5 seconds spoken aloud — bold, specific, attention-grabbing",
  "points": [
    "Point 1 — a specific pain or scenario (one conversational sentence)",
    "Point 2 — the consequence or cost of the problem (one sentence)",
    "Point 3 — how RingCatch solves it, simply stated (one sentence)"
  ],
  "cta": "The closing call to action spoken aloud (one natural sentence, mention ringcatch.io)",
  "caption": "Engaging social media caption written for {niche} owners, 2-3 sentences + 4-6 relevant hashtags"
}}"""
    raw = await _llm(prompt)
    try:
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        return json.loads(raw[start:end])
    except Exception:
        return {
            "title": f"How {niche} Businesses Lose Money After Hours",
            "hook": f"Every missed call after 5pm costs {niche} businesses real money.",
            "points": [
                "Customers call after hours and get voicemail — they call your competitor instead.",
                "An AI chatbot answers instantly, captures the lead, and books the job.",
                "RingCatch sets this up in 48 hours for $450 — no monthly tech headaches.",
            ],
            "cta": "See it live at ringcatch.io — free demo, no credit card.",
            "caption": f"AI chatbot for {niche} businesses | After hours lead capture | ringcatch.io #{niche.replace(' ','')} #SmallBusiness #AIchatbot #LeadGeneration",
        }


async def generate_tts(text: str, output_path: str) -> bool:
    import base64
    # Google Cloud TTS — Journey-F: warm, natural American female voice
    if GCP_TTS_KEY:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    f"https://texttospeech.googleapis.com/v1/text:synthesize?key={GCP_TTS_KEY}",
                    json={
                        "input": {"text": text},
                        "voice": {"languageCode": "en-US", "name": GCP_TTS_VOICE},
                        "audioConfig": {"audioEncoding": "MP3", "speakingRate": 1.05, "pitch": 0.0},
                    },
                )
            if r.status_code == 200:
                Path(output_path).write_bytes(base64.b64decode(r.json()["audioContent"]))
                logger.info(f"TTS: Google Cloud ({GCP_TTS_VOICE})")
                return True
            logger.warning(f"Google TTS error {r.status_code}: {r.text[:300]}")
        except Exception as e:
            logger.warning(f"Google TTS failed: {e}")
    # ElevenLabs fallback
    if ELEVENLABS_KEY:
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE}",
                    headers={"xi-api-key": ELEVENLABS_KEY, "Content-Type": "application/json"},
                    json={"text": text, "model_id": "eleven_turbo_v2_5",
                          "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.3}},
                )
            if r.status_code == 200:
                Path(output_path).write_bytes(r.content)
                logger.info("TTS: ElevenLabs")
                return True
        except Exception as e:
            logger.warning(f"ElevenLabs TTS failed: {e}")
    # Kokoro fallback
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{TTS_URL}/v1/audio/speech",
                             json={"model": "kokoro", "input": text, "voice": "af_heart", "response_format": "mp3"})
            if r.status_code == 200:
                Path(output_path).write_bytes(r.content)
                logger.info("TTS: Kokoro fallback")
                return True
    except Exception as e:
        logger.warning(f"Kokoro TTS failed: {e}")
    return False


def load_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    path = FONT_PATH if bold else FONT_SMALL
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def draw_text_wrapped(draw: ImageDraw.Draw, text: str, x: int, y: int,
                      max_width: int, font: ImageFont.FreeTypeFont,
                      color: tuple, line_spacing: int = 12) -> int:
    lines = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        line = ""
        for word in words:
            test = f"{line} {word}".strip()
            bbox = draw.textbbox((0, 0), test, font=font)
            if bbox[2] > max_width and line:
                lines.append(line)
                line = word
            else:
                line = test
        if line:
            lines.append(line)
    cy = y
    for line in lines:
        draw.text((x, cy), line, font=font, fill=color)
        bbox = draw.textbbox((0, 0), line, font=font)
        cy += bbox[3] - bbox[1] + line_spacing
    return cy


def make_slide(text: str, subtitle: str = "", accent_line: str = "",
               progress: float = 0.0, slide_type: str = "body") -> Image.Image:
    img  = Image.new("RGB", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    for i in range(H):
        alpha = int(15 * (i / H))
        draw.line([(0, i), (W, i)], fill=(34, 211, 238, alpha))

    draw.rectangle([(0, 0), (W, 8)], fill=AC_COLOR)
    draw.rectangle([(0, H - 12), (W, H)], fill=(30, 30, 50))
    draw.rectangle([(0, H - 12), (int(W * progress), H)], fill=AC_COLOR)

    font_logo = load_font(32)
    draw.text((48, 32), "RINGCATCH", font=font_logo, fill=AC_COLOR)
    draw.text((48, 70), "ringcatch.io", font=load_font(22, bold=False), fill=DIM_COLOR)

    pad = 72
    if slide_type == "hook":
        font_big = load_font(88)
        cy = H // 2 - 200
        cy = draw_text_wrapped(draw, text, pad, cy, W - pad * 2, font_big, TX_COLOR, 16)
        if subtitle:
            draw_text_wrapped(draw, subtitle, pad, cy + 32, W - pad * 2,
                              load_font(48, bold=False), DIM_COLOR)
    elif slide_type == "point":
        num = accent_line
        cx_circ, cy_circ = pad + 40, H // 2 - 80
        draw.ellipse([(cx_circ - 40, cy_circ - 40), (cx_circ + 40, cy_circ + 40)], fill=AC_COLOR)
        draw.text((cx_circ - 14, cy_circ - 22), num, font=load_font(44), fill=BG_COLOR)
        font_pt = load_font(64)
        draw_text_wrapped(draw, text, pad, cy_circ + 70, W - pad * 2, font_pt, TX_COLOR, 14)
    elif slide_type == "cta":
        draw.rectangle([(pad - 20, H // 2 - 60), (W - pad + 20, H // 2 + 180)],
                       fill=(20, 60, 70))
        draw.rectangle([(pad - 20, H // 2 - 60), (W - pad + 20, H // 2 - 52)],
                       fill=AC_COLOR)
        font_cta = load_font(60)
        draw_text_wrapped(draw, text, pad, H // 2 - 20, W - pad * 2, font_cta, TX_COLOR, 12)

    return img


def images_to_video(image_paths: list, audio_path: str, output_path: str,
                    durations: list) -> bool:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        concat_file = f.name
        for img_path, dur in zip(image_paths, durations):
            f.write(f"file '{img_path}'\nduration {dur}\n")
        f.write(f"file '{image_paths[-1]}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", concat_file,
        "-i", audio_path,
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", "-movflags", "+faststart",
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    Path(concat_file).unlink(missing_ok=True)
    if result.returncode != 0:
        logger.error(f"FFmpeg error: {result.stderr[-500:]}")
    return result.returncode == 0


# ── YouTube upload ────────────────────────────────────────────────────────────

def _post_yt_comment(yt, video_id: str, niche: str) -> None:
    text = (
        f"Want an AI chatbot like this for your {niche} business? "
        f"Get a free demo tailored to your business at ringcatch.io — live in 48 hours 🤖"
        if niche else
        "Get a free AI chatbot demo for your business → ringcatch.io 🤖"
    )
    yt.commentThreads().insert(
        part="snippet",
        body={"snippet": {"videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": text}}}},
    ).execute()


async def upload_to_youtube(video_path: str, title: str, description: str, caption: str, niche: str = "", thumbnail_path: str | None = None) -> str | None:
    if not all([YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN]):
        logger.info("YouTube credentials not configured — skipping upload")
        return None
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError:
        logger.error("google-api-python-client not installed")
        return None

    creds = Credentials(
        token=None,
        refresh_token=YT_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YT_CLIENT_ID,
        client_secret=YT_CLIENT_SECRET,
        scopes=[
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.force-ssl",
        ],
    )

    body = {
        "snippet": {
            "title": title[:100],
            "description": f"{description}\n\n{caption}\n\nLearn more: https://ringcatch.io",
            "tags": ["AI chatbot", "small business", "RingCatch", "lead generation", "after hours"],
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    def _do_upload():
        yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
        req = yt.videos().insert(part=",".join(body.keys()), body=body, media_body=media)
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
        vid_id = resp.get("id", "")
        try:
            _post_yt_comment(yt, vid_id, niche)
            logger.info(f"Pinned comment posted on {vid_id}")
        except Exception as ce:
            logger.warning(f"Comment post failed (re-auth with force-ssl scope to fix): {ce}")
        if thumbnail_path and Path(thumbnail_path).exists():
            try:
                thumb_media = MediaFileUpload(thumbnail_path, mimetype="image/jpeg")
                yt.thumbnails().set(videoId=vid_id, media_body=thumb_media).execute()
                logger.info(f"Thumbnail set for {vid_id}: {thumbnail_path}")
            except Exception as te:
                logger.warning(f"Thumbnail upload failed: {te}")
        return vid_id

    try:
        loop = asyncio.get_event_loop()
        video_id = await loop.run_in_executor(None, _do_upload)
        url = f"https://www.youtube.com/shorts/{video_id}"
        logger.info(f"YouTube upload complete: {url}")
        return url
    except Exception as e:
        logger.error(f"YouTube upload failed: {e}")
        return None


# ── TikTok upload ─────────────────────────────────────────────────────────────

async def refresh_tiktok_token() -> bool:
    if not TIKTOK_CLIENT_KEY or not _tiktok_token["refresh_token"]:
        return False
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                "https://open.tiktokapis.com/v2/oauth/token/",
                data={
                    "client_key": TIKTOK_CLIENT_KEY,
                    "client_secret": TIKTOK_CLIENT_SECRET,
                    "grant_type": "refresh_token",
                    "refresh_token": _tiktok_token["refresh_token"],
                },
            )
            data = r.json()
            if "access_token" in data:
                _tiktok_token["access_token"] = data["access_token"]
                if "refresh_token" in data:
                    _tiktok_token["refresh_token"] = data["refresh_token"]
                logger.info("TikTok token refreshed")
                return True
            logger.error(f"TikTok refresh failed: {data}")
            return False
    except Exception as e:
        logger.error(f"TikTok refresh error: {e}")
        return False


async def upload_to_tiktok(video_path: str, caption: str) -> str | None:
    if not _tiktok_token["access_token"]:
        logger.info("TikTok credentials not configured — skipping upload")
        return None

    video_size = Path(video_path).stat().st_size
    headers = {
        "Authorization": f"Bearer {_tiktok_token['access_token']}",
        "Content-Type": "application/json; charset=UTF-8",
    }

    async with httpx.AsyncClient(timeout=120) as c:
        # Step 1: initialize
        init = await c.post(
            "https://open.tiktokapis.com/v2/post/publish/video/init/",
            headers=headers,
            json={
                "post_info": {
                    "title": caption[:2200],
                    "privacy_level": "PUBLIC_TO_EVERYONE",
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                    "video_cover_timestamp_ms": 1000,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": video_size,
                    "chunk_size": video_size,
                    "total_chunk_count": 1,
                },
            },
        )

        if init.status_code == 401:
            # Try refreshing token once
            if await refresh_tiktok_token():
                headers["Authorization"] = f"Bearer {_tiktok_token['access_token']}"
                init = await c.post(
                    "https://open.tiktokapis.com/v2/post/publish/video/init/",
                    headers=headers,
                    json=init.request.content,
                )

        if init.status_code != 200:
            logger.error(f"TikTok init failed ({init.status_code}): {init.text[:300]}")
            return None

        data = init.json().get("data", {})
        publish_id  = data.get("publish_id")
        upload_url  = data.get("upload_url")

        # Step 2: upload file
        video_bytes = Path(video_path).read_bytes()
        up = await c.put(
            upload_url,
            content=video_bytes,
            headers={
                "Content-Range": f"bytes 0-{video_size - 1}/{video_size}",
                "Content-Type": "video/mp4",
            },
        )
        if up.status_code not in (200, 201, 206, 204):
            logger.error(f"TikTok upload chunk failed: {up.status_code}")
            return None

        logger.info(f"TikTok upload complete, publish_id: {publish_id}")
        return publish_id


# ── Stock footage ─────────────────────────────────────────────────────────────

async def get_stock_footage(niche: str, output_path: str) -> bool:
    if not PEXELS_KEY:
        return False
    import random
    query = NICHE_QUERIES.get(niche, f"{niche} small business professional")
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(
                "https://api.pexels.com/videos/search",
                params={"query": query, "orientation": "portrait", "size": "medium", "per_page": 8},
                headers={"Authorization": PEXELS_KEY},
            )
        if r.status_code != 200:
            logger.warning(f"Pexels error {r.status_code}")
            return False
        videos = r.json().get("videos", [])
        if not videos:
            # Fallback: try landscape and we'll crop
            r2_data = None
            async with httpx.AsyncClient(timeout=30) as c:
                r2 = await c.get(
                    "https://api.pexels.com/videos/search",
                    params={"query": query, "size": "medium", "per_page": 8},
                    headers={"Authorization": PEXELS_KEY},
                )
            videos = r2.json().get("videos", []) if r2.status_code == 200 else []
        if not videos:
            return False
        video = random.choice(videos[:5])
        # Pick highest quality file
        files = sorted(video.get("video_files", []), key=lambda f: f.get("width", 0) * f.get("height", 0), reverse=True)
        if not files:
            return False
        url = files[0]["link"]
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            r = await c.get(url)
        Path(output_path).write_bytes(r.content)
        logger.info(f"Stock footage: {niche} ({len(r.content)//1024}KB)")
        return True
    except Exception as e:
        logger.warning(f"Pexels fetch failed: {e}")
        return False


def get_audio_duration(audio_path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except Exception:
        return 45.0


def make_text_overlay(text: str, slide_type: str = "body", accent: str = "") -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = 64

    def band(y: int, h: int, alpha: int = 185):
        b = Image.new("RGBA", (W, h), (0, 0, 0, alpha))
        img.paste(b, (0, y), b)

    if slide_type == "hook":
        band(0, int(H * 0.46), 200)
        draw_text_wrapped(draw, text, pad, 72, W - pad * 2, load_font(78), TX_COLOR, 14)
        if accent:
            draw_text_wrapped(draw, accent, pad, 380, W - pad * 2, load_font(44, bold=False), (190, 210, 230), 10)
    elif slide_type == "point":
        by = int(H * 0.38)
        band(by, int(H * 0.28))
        draw_text_wrapped(draw, text, pad, by + 44, W - pad * 2, load_font(60), TX_COLOR, 12)
    elif slide_type == "cta":
        by = int(H * 0.58)
        band(by, int(H * 0.38), 210)
        draw.rectangle([(pad - 20, by), (W - pad + 20, by + 6)], fill=AC_COLOR)
        draw_text_wrapped(draw, text, pad, by + 28, W - pad * 2, load_font(58), TX_COLOR, 12)
        draw.text((pad, by + 220), "ringcatch.io  →", font=load_font(46), fill=AC_COLOR)

    # Persistent bottom branding bar
    band(H - 72, 72, 170)
    draw.text((pad, H - 58), "RingCatch.io", font=load_font(30), fill=AC_COLOR)
    return img


def assemble_with_footage(footage_path: str, audio_path: str,
                          output_path: str, script: dict, duration: float) -> bool:
    texts = [script["hook"]] + script.get("points", [])[:3] + [script["cta"]]
    types = ["hook"] + ["point"] * len(script.get("points", [])[:3]) + ["cta"]
    accents = [script.get("title", "")] + [""] * (len(texts) - 1)

    # Estimate per-segment timing proportional to char count
    chars = [max(len(t), 20) for t in texts]
    total_chars = sum(chars)
    usable = duration - 0.4
    times: list[tuple[float, float]] = []
    t = 0.2
    for c in chars:
        seg_dur = (c / total_chars) * usable
        times.append((round(t, 2), round(t + seg_dur, 2)))
        t += seg_dur

    with tempfile.TemporaryDirectory() as tmpdir:
        overlay_paths = []
        for i, (text, stype, acc) in enumerate(zip(texts, types, accents)):
            ov = make_text_overlay(text, stype, acc)
            p = f"{tmpdir}/ov_{i}.png"
            ov.save(p)
            overlay_paths.append(p)

        # Build filter_complex
        inputs = ["-stream_loop", "-1", "-t", str(duration + 1), "-i", footage_path,
                  "-i", audio_path]
        for p in overlay_paths:
            inputs += ["-loop", "1", "-i", p]

        # Scale/crop footage to 9:16 portrait
        fc = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[bg];"
        prev = "bg"
        for i, (start, end) in enumerate(times):
            nxt = f"v{i}"
            fc += f"[{prev}][{i+2}:v]overlay=0:0:enable='between(t,{start},{end})'[{nxt}];"
            prev = nxt
        fc = fc.rstrip(";")

        cmd = [
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", fc,
            "-map", f"[{prev}]", "-map", "1:a",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-movflags", "+faststart",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"FFmpeg footage error: {result.stderr[-600:]}")
        return result.returncode == 0


# ── Video generation ──────────────────────────────────────────────────────────

async def make_video(niche: str) -> str | None:
    global videos_made_today
    async with _video_sem:
        return await _make_video_inner(niche)


async def _make_video_inner(niche: str) -> str | None:
    global videos_made_today
    logger.info(f"Generating video for niche: {niche}")
    script = await generate_script(niche)
    logger.info(f"Script: {script.get('title')}")

    full_text = f"{script['hook']} {' '.join(script['points'])} {script['cta']}"
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_path = str(VIDEO_DIR / f"{niche.replace(' ','_').lower()}_{ts}.mp4")

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = f"{tmpdir}/voice.mp3"
        tts_ok = await generate_tts(full_text, audio_path)
        if not tts_ok:
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                            "anullsrc=r=44100:cl=stereo", "-t", "50",
                            "-q:a", "9", "-acodec", "libmp3lame", audio_path],
                           capture_output=True)

        duration = get_audio_duration(audio_path)
        footage_path = f"{tmpdir}/footage.mp4"
        has_footage = await get_stock_footage(niche, footage_path)

        if has_footage:
            ok = assemble_with_footage(footage_path, audio_path, out_path, script, duration)
            if not ok:
                has_footage = False

        if not has_footage:
            # Fallback: PIL slides
            slides, durations_list = [], []
            s = make_slide(script["hook"], script["title"], slide_type="hook", progress=0.05)
            p = f"{tmpdir}/slide_0.jpg"; s.save(p, quality=95)
            slides.append(p); durations_list.append(5)
            for i, point in enumerate(script.get("points", [])[:3], 1):
                s = make_slide(point, accent_line=str(i), slide_type="point", progress=0.1 + i * 0.25)
                p = f"{tmpdir}/slide_{i}.jpg"; s.save(p, quality=95)
                slides.append(p); durations_list.append(6)
            s = make_slide(script["cta"], slide_type="cta", progress=1.0)
            p = f"{tmpdir}/slide_cta.jpg"; s.save(p, quality=95)
            slides.append(p); durations_list.append(8)
            ok = images_to_video(slides, audio_path, out_path, durations_list)

    if not Path(out_path).exists():
        return None

    videos_made_today += 1
    meta = {
        "title": script["title"],
        "caption": script.get("caption", ""),
        "niche": niche,
        "path": out_path,
        "created_at": datetime.now(UTC).isoformat(),
    }
    Path(out_path.replace(".mp4", ".json")).write_text(json.dumps(meta, indent=2))
    logger.info(f"Video saved: {out_path}")

    # Generate Imagen thumbnail (non-blocking — falls back gracefully if unavailable)
    thumbnail_path = await generate_thumbnail(niche, script["title"], str(VIDEO_DIR))

    # Upload to platforms
    yt_url = await upload_to_youtube(
        out_path,
        title=script["title"],
        description=script["hook"] + "\n\n" + "\n".join(script.get("points", [])),
        caption=script.get("caption", ""),
        niche=niche,
        thumbnail_path=thumbnail_path,
    )
    tt_id = await upload_to_tiktok(out_path, script.get("caption", script["title"]))

    upload_status = []
    if yt_url:
        upload_status.append(f"YouTube: {yt_url}")
    else:
        upload_status.append("YouTube: not configured (run youtube_auth.py)")
    if tt_id:
        upload_status.append(f"TikTok: publish_id={tt_id}")
    else:
        upload_status.append("TikTok: not configured (run tiktok_auth.py)")

    await send_discord(
        f"New video: **{niche}** — {script['title']}\n"
        f"Path: `{out_path}`\n" +
        "\n".join(upload_status)
    )
    return out_path


async def nightly_video_loop():
    niches = ["HVAC", "Plumbing", "Dental", "Auto Repair", "Law Firm",
              "Property Management", "Landscaping", "Roofing", "Pest Control", "Electrician"]
    idx = 0
    await asyncio.sleep(30)
    while True:
        now = datetime.now()
        target = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        logger.info(f"Next video in {wait/3600:.1f}h at 2 AM")
        await asyncio.sleep(wait)
        for attempt in range(6):
            try:
                async with httpx.AsyncClient(timeout=5) as c:
                    r = await c.get(f"{OLLAMA_URL}/api/tags")
                if r.status_code == 200:
                    break
            except Exception:
                pass
            logger.info(f"GPU busy, retrying in 30 min (attempt {attempt+1}/6)")
            await asyncio.sleep(1800)
        niche = niches[idx % len(niches)]
        try:
            await make_video(niche)
        except Exception as e:
            logger.error(f"Video generation failed for {niche}: {e}")
            await send_discord(f"Video generation error for {niche}: {e}")
        idx += 1


@asynccontextmanager
async def lifespan(app: FastAPI):
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    asyncio.create_task(nightly_video_loop())
    logger.info("Video agent started")
    yield


app = FastAPI(title="Agency Video", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "videos_made_today": videos_made_today}


@app.get("/status")
def status():
    videos = sorted(VIDEO_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    yt_configured = bool(YT_CLIENT_ID and YT_CLIENT_SECRET and YT_REFRESH_TOKEN)
    tt_configured = bool(_tiktok_token["access_token"])
    return {
        "status": "ok",
        "tts_url": TTS_URL,
        "videos_made_today": videos_made_today,
        "total_videos": len(videos),
        "latest": str(videos[0]) if videos else None,
        "youtube_configured": yt_configured,
        "tiktok_configured": tt_configured,
    }


@app.post("/generate")
async def generate_endpoint(payload: dict):
    niche = payload.get("niche", "HVAC")
    path = await make_video(niche)
    if path:
        return {"status": "ok", "path": path}
    return {"status": "error", "detail": "Video generation failed"}


@app.post("/upload-pending")
async def upload_pending():
    results = []
    for json_file in sorted(VIDEO_DIR.glob("*.json")):
        meta = json.loads(json_file.read_text())
        if meta.get("youtube_url") or meta.get("tiktok_id"):
            continue  # already uploaded
        video_path = json_file.with_suffix(".mp4")
        if not video_path.exists():
            continue
        yt_url = await upload_to_youtube(
            str(video_path), meta.get("title", video_path.stem),
            meta.get("caption", ""), meta.get("caption", ""),
            niche=meta.get("niche", ""),
        )
        tt_id = await upload_to_tiktok(str(video_path), meta.get("caption", meta.get("title", "")))
        if yt_url:
            meta["youtube_url"] = yt_url
        if tt_id:
            meta["tiktok_id"] = tt_id
        json_file.write_text(json.dumps(meta, indent=2))
        results.append({"video": video_path.name, "youtube": yt_url, "tiktok": tt_id})
        await send_discord(
            f"📤 Uploaded pending video: **{meta.get('title')}**\n"
            + (f"YouTube: {yt_url}\n" if yt_url else "")
            + (f"TikTok publish_id: {tt_id}" if tt_id else "")
        )
    return {"uploaded": len(results), "results": results}
