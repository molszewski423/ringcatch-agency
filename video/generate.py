#!/usr/bin/env python3
"""
Video generation script — runs on MikeNixPC (RTX 5060 Ti), rsyncs result to archbox.
Run manually:  python3 generate.py --niche HVAC
Systemd timer: runs weekly per niche automatically.

GPU features used:
- NVENC (h264_nvenc) for fast GPU-accelerated video encoding
- faster-whisper (CPU mode, ctranslate2) for word-level caption timestamps
"""
import argparse
import asyncio
import base64
import ctypes
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, UTC
from pathlib import Path

# Pre-load NixOS shared libraries required by faster-whisper's av dependency.
# Must happen before any import of av or faster_whisper.
for _lib in [
    "/nix/store/ixhlv41i2wpl84xgjcks061dz4yssbg3-zlib-1.3.2/lib/libz.so.1",
    "/nix/store/ybp235ps7m4yd85v0pgvqkhd4xmxf6jq-gcc-14.3.0-lib/lib/libstdc++.so.6",
]:
    try:
        ctypes.CDLL(_lib)
    except Exception:
        pass

# ── Config ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ENV_FILE = Path(__file__).parent.parent / ".env"

def _load_env():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL      = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GCP_TTS_KEY     = os.environ.get("GCP_TTS_KEY", os.environ.get("GEMINI_API_KEY", ""))
GCP_TTS_VOICE   = os.environ.get("GCP_TTS_VOICE", "en-US-Journey-F")
PEXELS_KEY      = os.environ.get("PEXELS_API_KEY", "")
YT_CLIENT_ID    = os.environ.get("YOUTUBE_CLIENT_ID", "")
YT_CLIENT_SECRET= os.environ.get("YOUTUBE_CLIENT_SECRET", "")
YT_REFRESH_TOKEN= os.environ.get("YOUTUBE_REFRESH_TOKEN", "")
DISCORD_URL     = os.environ.get("DISCORD_BOT_URL", "http://agency-discord:8103/alert")
ARCHBOX_DATA    = "mike@100.96.122.27:/home/mike/.local/share/containers/storage/volumes/agency-data/_data/videos/"

# Font paths (NixOS nix store)
_FONT_BOLD  = subprocess.run(["fc-match", "DejaVu Sans:bold", "--format=%{file}"],
                              capture_output=True, text=True).stdout.strip()
_FONT_REG   = subprocess.run(["fc-match", "DejaVu Sans:regular", "--format=%{file}"],
                              capture_output=True, text=True).stdout.strip()
FONT_PATH   = _FONT_BOLD or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_SMALL  = _FONT_REG  or "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

VIDEO_DIR = Path(os.environ.get("VIDEO_DIR", Path.home() / "agency" / "videos"))
VIDEO_DIR.mkdir(parents=True, exist_ok=True)

W, H      = 1080, 1920
BG_COLOR  = (11, 11, 20)
AC_COLOR  = (34, 211, 238)
TX_COLOR  = (255, 255, 255)
DIM_COLOR = (150, 150, 170)

NICHE_QUERIES = {
    "HVAC":                "hvac air conditioning technician repair",
    "Plumbing":            "plumber pipe repair water",
    "Dental":              "dentist dental office smile",
    "Auto Repair":         "car mechanic auto repair garage",
    "Law Firm":            "lawyer attorney office professional",
    "Property Management": "apartment building property management",
    "Landscaping":         "landscaping lawn garden outdoor",
    "Roofing":             "roofing contractor roof house",
    "Pest Control":        "pest control exterminator home",
    "Electrician":         "electrician electrical wiring professional",
    "Hair Salon":          "hair salon stylist haircut professional",
    "Veterinary":          "veterinarian vet clinic pet animal care",
    "Chiropractic":        "chiropractor spine adjustment clinic",
    "Physical Therapy":    "physical therapy rehabilitation exercise clinic",
    "Moving Company":      "moving company movers truck boxes",
    "House Painting":      "house painter painting contractor interior exterior",
    "Home Cleaning":       "house cleaning maid service home spotless",
    "Pool Service":        "swimming pool cleaning maintenance backyard",
    "Tree Service":        "tree trimming arborist chainsaw outdoor",
    "Locksmith":           "locksmith keys door lock security",
    "Daycare":             "daycare child care children play learning",
    "Towing":              "tow truck towing service roadside assistance",
    "Personal Training":   "personal trainer gym workout fitness",
    "Tax Preparation":     "tax accountant financial office professional",
    "Restaurant":          "restaurant kitchen chef food service dining",
}

import httpx
from PIL import Image, ImageDraw, ImageFont


# ── LLM ───────────────────────────────────────────────────────────────────────

async def _llm(prompt: str) -> str:
    if GEMINI_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
                    json={"contents": [{"parts": [{"text": prompt}]}]},
                )
            if r.status_code == 200:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            logger.warning(f"Gemini failed: {e}")
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
    raise RuntimeError("All LLM backends failed")


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
        start = raw.find("{"); end = raw.rfind("}") + 1
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


# ── TTS ───────────────────────────────────────────────────────────────────────

async def generate_tts(text: str, output_path: str) -> bool:
    if GCP_TTS_KEY:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    f"https://texttospeech.googleapis.com/v1/text:synthesize?key={GCP_TTS_KEY}",
                    json={"input": {"text": text},
                          "voice": {"languageCode": "en-US", "name": GCP_TTS_VOICE},
                          "audioConfig": {"audioEncoding": "MP3", "speakingRate": 1.05, "pitch": 0.0}},
                )
            if r.status_code == 200:
                Path(output_path).write_bytes(base64.b64decode(r.json()["audioContent"]))
                logger.info(f"TTS: Google Cloud ({GCP_TTS_VOICE})")
                return True
            logger.warning(f"Google TTS {r.status_code}: {r.text[:200]}")
        except Exception as e:
            logger.warning(f"Google TTS failed: {e}")
    return False


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
        videos = r.json().get("videos", [])
        if not videos:
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
        files = sorted(video.get("video_files", []), key=lambda f: f.get("width", 0) * f.get("height", 0), reverse=True)
        if not files:
            return False
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            r = await c.get(files[0]["link"])
        Path(output_path).write_bytes(r.content)
        logger.info(f"Stock footage: {niche} ({len(r.content)//1024}KB)")
        return True
    except Exception as e:
        logger.warning(f"Pexels failed: {e}")
        return False


def get_audio_duration(path: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except Exception:
        return 45.0


def generate_captions(audio_path: str) -> list[dict]:
    """Word-level timestamps via faster-whisper (CPU mode, int8 quantized).
    Returns list of {word, start, end} dicts. Empty list on failure."""
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(audio_path, word_timestamps=True)
        words = []
        for seg in segments:
            if seg.words:
                for w in seg.words:
                    word = w.word.strip()
                    if word:
                        words.append({"word": word, "start": w.start, "end": w.end})
        logger.info(f"Captions: {len(words)} words transcribed")
        return words
    except Exception as e:
        logger.warning(f"Caption generation failed: {e}")
        return []


def write_ass_captions(words: list[dict], path: str) -> None:
    """Write an ASS subtitle file using phrase-grouped captions (5 words per line).
    Style: bold white text, thick black outline, center-bottom, semi-transparent box."""
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # White text, black outline (thickness 5), drop shadow 2, center-bottom, 200px margin
        "Style: Cap,Arial,68,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        "-1,0,0,0,100,100,1,0,1,5,2,2,60,60,200,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    def _t(secs: float) -> str:
        h = int(secs // 3600)
        m = int((secs % 3600) // 60)
        s = secs % 60
        cs = int(round((s % 1) * 100))
        return f"{h}:{m:02d}:{int(s):02d}.{cs:02d}"

    lines = []
    chunk = 5
    for i in range(0, len(words), chunk):
        group = words[i : i + chunk]
        start = group[0]["start"]
        end   = group[-1]["end"]
        text  = " ".join(w["word"] for w in group).replace(",", r"\,")
        lines.append(f"Dialogue: 0,{_t(start)},{_t(end)},Cap,,0,0,0,,{text}")

    Path(path).write_text(header + "\n".join(lines))


# ── Video assembly ────────────────────────────────────────────────────────────

def load_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH if bold else FONT_SMALL, size)
    except Exception:
        return ImageFont.load_default()


def draw_text_wrapped(draw, text, x, y, max_width, font, color, line_spacing=12):
    lines = []
    for paragraph in text.split("\n"):
        words, line = paragraph.split(), ""
        for word in words:
            test = f"{line} {word}".strip()
            if draw.textbbox((0, 0), test, font=font)[2] > max_width and line:
                lines.append(line); line = word
            else:
                line = test
        if line:
            lines.append(line)
    cy = y
    for line in lines:
        draw.text((x, cy), line, font=font, fill=color)
        cy += draw.textbbox((0, 0), line, font=font)[3] + line_spacing
    return cy


def make_text_overlay(text: str, slide_type: str = "body", accent: str = "") -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = 64

    def band(y, h, alpha=185):
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

    band(H - 72, 72, 170)
    draw.text((pad, H - 58), "RingCatch.io", font=load_font(30), fill=AC_COLOR)
    return img


def assemble_with_footage(footage_path, audio_path, output_path, script, duration,
                          words: list | None = None) -> bool:
    """Professional assembly: Ken Burns motion, color grade, vignette, GPU encode, burnt captions."""
    texts   = [script["hook"]] + script.get("points", [])[:3] + [script["cta"]]
    types   = ["hook"] + ["point"] * len(script.get("points", [])[:3]) + ["cta"]
    accents = [script.get("title", "")] + [""] * (len(texts) - 1)

    chars  = [max(len(t), 20) for t in texts]
    total  = sum(chars)
    usable = duration - 0.4
    times, t = [], 0.2
    for c in chars:
        seg = (c / total) * usable
        times.append((round(t, 2), round(t + seg, 2)))
        t += seg

    with tempfile.TemporaryDirectory() as tmpdir:
        overlay_paths = []
        for i, (text, stype, acc) in enumerate(zip(texts, types, accents)):
            ov = make_text_overlay(text, stype, acc)
            p = f"{tmpdir}/ov_{i}.png"; ov.save(p)
            overlay_paths.append(p)

        # Write ASS captions if word timestamps available
        ass_path = None
        if words:
            ass_path = f"{tmpdir}/captions.ass"
            write_ass_captions(words, ass_path)

        inputs = ["-stream_loop", "-1", "-t", str(duration + 1), "-i", footage_path, "-i", audio_path]
        for p in overlay_paths:
            inputs += ["-loop", "1", "-i", p]

        # Ken Burns: scale 10% larger than frame, drift slowly via sin-wave crop offset
        ken_burns = (
            "scale=1188:2112:force_original_aspect_ratio=increase,"
            "crop=1080:1920:"
            "x='(iw-ow)/2+22*sin(t*0.18)':"
            "y='(ih-oh)/2+14*sin(t*0.13+0.7)',"
            "setsar=1"
        )

        fc = f"[0:v]{ken_burns}[bg];"
        prev = "bg"
        for i, (start, end) in enumerate(times):
            nxt = f"v{i}"
            fc += f"[{prev}][{i+2}:v]overlay=0:0:enable='between(t,{start},{end})'[{nxt}];"
            prev = nxt

        # Color grade: lifted contrast, saturation boost, slight brightness dip; vignette
        fc += f"[{prev}]eq=contrast=1.12:saturation=1.18:brightness=-0.03,vignette=angle=PI/5[graded];"
        prev = "graded"

        # Burn captions
        if ass_path:
            safe_path = ass_path.replace("\\", "/").replace(":", r"\:")
            fc += f"[{prev}]ass='{safe_path}'[captioned]"
            prev = "captioned"

        fc = fc.rstrip(";")

        cmd = [
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", fc,
            "-map", f"[{prev}]", "-map", "1:a",
            "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "22",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart",
            output_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            logger.error(f"FFmpeg professional error: {r.stderr[-600:]}")
            # Fallback: CPU encode without captions
            fc_simple = (
                f"[0:v]{ken_burns}[bg];"
            )
            prev_s = "bg"
            for i, (start, end) in enumerate(times):
                nxt = f"v{i}"
                fc_simple += f"[{prev_s}][{i+2}:v]overlay=0:0:enable='between(t,{start},{end})'[{nxt}];"
                prev_s = nxt
            fc_simple = fc_simple.rstrip(";")
            cmd_fb = [
                "ffmpeg", "-y", *inputs, "-filter_complex", fc_simple,
                "-map", f"[{prev_s}]", "-map", "1:a",
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart",
                output_path,
            ]
            r = subprocess.run(cmd_fb, capture_output=True, text=True)
            if r.returncode != 0:
                logger.error(f"FFmpeg fallback error: {r.stderr[-400:]}")
        return r.returncode == 0


# ── YouTube upload ────────────────────────────────────────────────────────────

def upload_to_youtube(video_path: str, title: str, description: str, caption: str, niche: str = "") -> str | None:
    if not all([YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN]):
        logger.warning("YouTube credentials not set")
        return None
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError:
        logger.error("google-api-python-client not installed")
        return None

    creds = Credentials(
        token=None, refresh_token=YT_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YT_CLIENT_ID, client_secret=YT_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/youtube.upload",
                "https://www.googleapis.com/auth/youtube.force-ssl"],
    )
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {
        "snippet": {
            "title": title[:100],
            "description": f"{description}\n\n{caption}\n\nLearn more: https://ringcatch.io",
            "tags": ["AI chatbot", "small business", "RingCatch", "lead generation", "after hours"],
            "categoryId": "22",
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    req = yt.videos().insert(part=",".join(body.keys()), body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    video_id = resp.get("id", "")

    # Post pinned comment
    comment_text = (
        f"Want an AI chatbot like this for your {niche} business? "
        f"Get a free demo at ringcatch.io — live in 48 hours 🤖"
        if niche else "Get a free AI chatbot demo → ringcatch.io 🤖"
    )
    try:
        yt.commentThreads().insert(
            part="snippet",
            body={"snippet": {"videoId": video_id,
                              "topLevelComment": {"snippet": {"textOriginal": comment_text}}}},
        ).execute()
        logger.info(f"Comment posted on {video_id}")
    except Exception as e:
        logger.warning(f"Comment failed: {e}")

    url = f"https://www.youtube.com/shorts/{video_id}"
    logger.info(f"YouTube upload complete: {url}")
    return url


# ── Rsync to archbox ──────────────────────────────────────────────────────────

def rsync_to_archbox(json_path: str) -> bool:
    key = Path.home() / ".ssh" / "id_video_automation"
    ssh_opts = f"-i {key} -o StrictHostKeyChecking=no" if key.exists() else "-o StrictHostKeyChecking=no"
    r = subprocess.run(
        ["rsync", "-az", "--no-perms", "-e", f"ssh {ssh_opts}", json_path, ARCHBOX_DATA],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        logger.info(f"Rsynced {Path(json_path).name} to archbox")
    else:
        logger.warning(f"Rsync failed: {r.stderr[:200]}")
    return r.returncode == 0


# ── Pending upload retry ──────────────────────────────────────────────────────

async def _retry_pending_uploads() -> None:
    """Upload any .mp4 files in VIDEO_DIR that have no matching .json (prior upload failure)."""
    pending = [
        p for p in VIDEO_DIR.glob("*.mp4")
        if not p.with_suffix(".json").exists()
    ]
    if not pending:
        logger.info("No pending uploads found")
        return
    for mp4 in pending:
        # Derive niche from filename: plumbing_20260520_222415.mp4 → "Plumbing"
        stem_parts = mp4.stem.split("_")
        raw_niche = stem_parts[0].replace("-", " ").title()
        # Find matching canonical niche
        niche = next(
            (k for k in NICHE_QUERIES if k.lower().replace(" ", "_") == raw_niche.lower().replace(" ", "_")),
            raw_niche,
        )
        logger.info(f"Retrying upload: {mp4.name} (niche: {niche})")
        title = f"How {niche} Businesses Lose Jobs After Hours"
        description = f"AI chatbot for {niche} businesses — answers calls, captures leads, books appointments 24/7. Live on your site in 48 hours."
        caption = f"AI chatbot for {niche} | After-hours lead capture | ringcatch.io #{niche.replace(' ','')} #SmallBusiness #AIchatbot"
        url = upload_to_youtube(str(mp4), title=title, description=description,
                                caption=caption, niche=niche)
        if url:
            meta = {
                "title": title, "caption": caption, "niche": niche,
                "path": str(mp4),
                "created_at": datetime.fromtimestamp(mp4.stat().st_mtime, UTC).isoformat(),
                "youtube_url": url,
            }
            mp4.with_suffix(".json").write_text(json.dumps(meta, indent=2))
            logger.info(f"Retry success: {url}")
        else:
            logger.warning(f"Retry failed for {mp4.name} — will try again next night")


# ── Cleanup ───────────────────────────────────────────────────────────────────

def cleanup_uploaded_videos(days: int = 7) -> None:
    """Delete mp4+json pairs that were successfully uploaded and are older than `days`."""
    import time
    cutoff = time.time() - days * 86400
    removed = 0
    for json_file in sorted(VIDEO_DIR.glob("*.json")):
        try:
            meta = json.loads(json_file.read_text())
            uploaded = bool(meta.get("youtube_url") or meta.get("tiktok_url"))
            if not uploaded:
                continue
            if json_file.stat().st_mtime >= cutoff:
                continue
            mp4 = json_file.with_suffix(".mp4")
            if mp4.exists():
                mp4.unlink()
                logger.info(f"Deleted (uploaded {days}d ago): {mp4.name}")
            json_file.unlink()
            removed += 1
        except Exception as e:
            logger.warning(f"Cleanup error {json_file.name}: {e}")
    logger.info(f"Cleanup: {removed} uploaded video(s) removed")


# ── Main ──────────────────────────────────────────────────────────────────────

async def make_video(niche: str) -> str | None:
    logger.info(f"Generating video: {niche}")
    script = await generate_script(niche)
    logger.info(f"Script: {script.get('title')}")

    full_text = f"{script['hook']} {' '.join(script['points'])} {script['cta']}"
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_path = str(VIDEO_DIR / f"{niche.replace(' ','_').lower()}_{ts}.mp4")

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = f"{tmpdir}/voice.mp3"
        if not await generate_tts(full_text, audio_path):
            logger.error("TTS failed — aborting")
            return None

        duration = get_audio_duration(audio_path)

        # GPU-assisted word-level caption timestamps
        words = generate_captions(audio_path)

        footage_path = f"{tmpdir}/footage.mp4"
        has_footage = await get_stock_footage(niche, footage_path)

        ok = False
        if has_footage:
            ok = assemble_with_footage(footage_path, audio_path, out_path, script, duration, words)

        if not ok:
            logger.error("Video assembly failed")
            return None

    if not Path(out_path).exists():
        return None

    logger.info(f"Video saved: {out_path}")

    yt_url = upload_to_youtube(
        out_path, title=script["title"],
        description=script["hook"] + "\n\n" + "\n".join(script.get("points", [])),
        caption=script.get("caption", ""), niche=niche,
    )

    meta = {
        "title": script["title"], "caption": script.get("caption", ""),
        "niche": niche, "path": out_path,
        "created_at": datetime.now(UTC).isoformat(),
        "youtube_url": yt_url,
    }
    json_path = out_path.replace(".mp4", ".json")
    Path(json_path).write_text(json.dumps(meta, indent=2))

    rsync_to_archbox(json_path)
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--niche", help="Niche to generate video for")
    parser.add_argument("--cleanup", action="store_true",
                        help="Delete uploaded videos older than --cleanup-days (default 7)")
    parser.add_argument("--cleanup-days", type=int, default=7, dest="cleanup_days")
    parser.add_argument("--retry-pending", action="store_true", dest="retry_pending",
                        help="Upload any .mp4 files that have no .json (failed previous upload)")
    args = parser.parse_args()

    if args.cleanup:
        cleanup_uploaded_videos(args.cleanup_days)
        sys.exit(0)

    if args.retry_pending:
        asyncio.run(_retry_pending_uploads())
        sys.exit(0)

    if not args.niche:
        parser.error("--niche is required unless --cleanup or --retry-pending is set")

    result = asyncio.run(make_video(args.niche))
    sys.exit(0 if result else 1)
