import asyncio
import json
import logging
import os

import discord
import httpx
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ORCHESTRATOR_URL  = os.environ.get("ORCHESTRATOR_URL", "http://127.0.0.1:8109")
COMMAND_URL       = os.environ.get("COMMAND_URL", "http://127.0.0.1:8100")
CHAT_CHANNEL_NAME = os.environ.get("CHAT_CHANNEL_NAME", "agency-alerts")
OWNER_DISCORD_ID  = int(os.environ.get("OWNER_DISCORD_ID", "0"))  # set to Mike's Discord user ID

# Shared session for the owner so all his Discord messages share history
def session_id(user_id: int) -> str:
    if OWNER_DISCORD_ID and user_id == OWNER_DISCORD_ID:
        return "discord-owner"
    return f"discord_{user_id}"


import re as _re

def _sanitize(text: str) -> str:
    text = _re.sub(r'<function[^>]*>.*?</function>', '', text, flags=_re.DOTALL)
    text = _re.sub(r'<function[^>]*>', '', text)
    text = _re.sub(r'\{["\s]*"?name"?\s*:\s*"[^"]+"\s*,\s*"?(?:parameters|arguments)"?\s*:\s*\{.*?\}\s*\}', '', text, flags=_re.DOTALL)
    return text.strip()


async def call_orchestrator(message: str, user_id: int) -> str:
    try:
        async with httpx.AsyncClient(timeout=240) as client:
            resp = await client.post(
                f"{ORCHESTRATOR_URL}/chat",
                json={"message": message, "session_id": session_id(user_id)}
            )
            reply = resp.json().get("response", "No response from orchestrator.")
            return _sanitize(reply) or "⚠️ Orchestrator returned an empty response."
    except Exception as e:
        return f"⚠️ Orchestrator unavailable: {e}"


async def get_activity_summary() -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            events = (await client.get(f"{COMMAND_URL}/api/activity?limit=20")).json()
        if not events:
            return "No recent activity."
        lines = ["**Recent Activity**"]
        for e in events[:15]:
            ts = (e.get("timestamp") or "")[:16]
            lines.append(f"`{ts}` **{e.get('agent','')}** {e.get('event_type','')}: {e.get('message','')[:80]}")
        return "\n".join(lines)
    except Exception as e:
        return f"Could not fetch activity: {e}"


async def reset_session(user_id: int) -> str:
    sid = session_id(user_id)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.delete(f"{ORCHESTRATOR_URL}/history/{sid}")
        return f"✅ Session `{sid}` cleared. Fresh start."
    except Exception:
        # Endpoint may not exist — just inform the user
        return f"Session ID: `{sid}` (history is stored in the orchestrator DB; ask the orchestrator to ignore prior context if needed)."


def split_message(text: str, limit: int = 1900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            if current:
                chunks.append(current.rstrip())
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip())
    return chunks or [text[:limit]]


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

_alerts_channel = None  # set in on_ready


async def _http_alert(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        text = body.get("content") or body.get("message") or ""
    except Exception:
        return web.Response(status=400, text="invalid json")
    if not text:
        return web.Response(status=400, text="no content")
    if _alerts_channel is None:
        return web.Response(status=503, text="channel not ready")
    mention = f"<@{OWNER_DISCORD_ID}> " if OWNER_DISCORD_ID else ""
    chunks = split_message(str(text))
    for i, chunk in enumerate(chunks):
        await _alerts_channel.send((mention if i == 0 else "") + chunk)
    return web.Response(status=204)


async def _http_health(request: web.Request) -> web.Response:
    ready = _alerts_channel is not None
    return web.Response(
        status=200,
        content_type="application/json",
        text=json.dumps({"status": "ok" if ready else "starting", "channel_ready": ready}),
    )


async def _start_http() -> None:
    app = web.Application()
    app.router.add_post("/alert", _http_alert)
    app.router.add_get("/health", _http_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8103)
    await site.start()
    logger.info("Discord HTTP alert server listening on :8103")


@client.event
async def on_ready():
    global _alerts_channel
    for guild in client.guilds:
        ch = discord.utils.get(guild.text_channels, name=CHAT_CHANNEL_NAME)
        if ch:
            _alerts_channel = ch
            break
    logger.info(f"Discord bot online as {client.user} (alerts_channel={_alerts_channel}, owner_id={OWNER_DISCORD_ID or 'not set'})")


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return

    content = message.content.strip()
    in_chat_channel = hasattr(message.channel, "name") and message.channel.name == CHAT_CHANNEL_NAME
    is_dm           = isinstance(message.channel, discord.DMChannel)
    is_mention      = client.user in message.mentions

    if not (in_chat_channel or is_dm or is_mention):
        return

    # Strip bot mention
    text = content.replace(f"<@{client.user.id}>", "").strip()
    if not text:
        return

    cmd = text.lower()

    # ── Quick commands ────────────────────────────────────────────────────────
    if cmd in ("!help", "!h"):
        await message.channel.send(
            "**RingCatch Orchestrator — Full Agentic Mode**\n"
            "`!status`   `!s` — business snapshot\n"
            "`!report`   `!r` — comprehensive agency report\n"
            "`!health`   `!he` — full system diagnosis\n"
            "`!leads`    `!l` — pipeline breakdown\n"
            "`!activity` `!a` — last 20 system events\n"
            "`!reset` — clear your conversation history\n"
            "\n**Just talk to me in plain English:**\n"
            "• _\"Scrape electricians in Dallas and Austin\"_\n"
            "• _\"What's been happening since 2am?\"_\n"
            "• _\"Show me all scraped leads\"_\n"
            "• _\"Trigger outreach on the scraped leads\"_\n"
            "• _\"What did you do overnight?\"_\n"
            "• _\"Check the scraper logs\"_\n"
            "• _\"Update targets to plumbers in Chicago and NYC\"_"
        )
        return

    if cmd in ("!status", "!s"):
        await message.add_reaction("⏳")
        async with message.channel.typing():
            reply = await call_orchestrator(
                "Give me a quick business status snapshot: MRR, clients, pipeline stages, emails today, and agent health.",
                message.author.id
            )
        await message.remove_reaction("⏳", client.user)
        for chunk in split_message(reply):
            await message.channel.send(chunk)
        return

    if cmd in ("!report", "!r"):
        await message.add_reaction("⏳")
        async with message.channel.typing():
            reply = await call_orchestrator(
                "Generate a comprehensive RingCatch Agency report. Include business metrics, pipeline health, conversion analytics, and any critical issues that need attention.",
                message.author.id
            )
        await message.remove_reaction("⏳", client.user)
        for chunk in split_message(reply):
            await message.channel.send(chunk)
        return

    if cmd in ("!health", "!he"):
        await message.add_reaction("⏳")
        async with message.channel.typing():
            reply = await call_orchestrator(
                "Run a full system health check and diagnose any pipeline stalls. Specifically check if Ollama and other agents are online.",
                message.author.id
            )
        await message.remove_reaction("⏳", client.user)
        for chunk in split_message(reply):
            await message.channel.send(chunk)
        return

    if cmd in ("!leads", "!l", "!pipeline"):
        await message.add_reaction("⏳")
        async with message.channel.typing():
            reply = await call_orchestrator(
                "Show me the current pipeline breakdown by stage, with counts. Also show the 5 most recent leads.",
                message.author.id
            )
        await message.remove_reaction("⏳", client.user)
        for chunk in split_message(reply):
            await message.channel.send(chunk)
        return

    if cmd in ("!activity", "!a"):
        async with message.channel.typing():
            reply = await get_activity_summary()
        await message.channel.send(reply[:1900])
        return

    if cmd in ("!reset",):
        reply = await reset_session(message.author.id)
        await message.channel.send(reply)
        return

    # ── Full agentic request ──────────────────────────────────────────────────
    # Prefix with caller identity so the orchestrator knows who's asking
    display = message.author.display_name
    channel_ctx = f"DM" if is_dm else f"#{getattr(message.channel, 'name', 'unknown')}"
    prefixed = f"[Discord | {display} | {channel_ctx}] {text}"

    await message.add_reaction("⏳")
    async with message.channel.typing():
        reply = await call_orchestrator(prefixed, message.author.id)
    await message.remove_reaction("⏳", client.user)

    for chunk in split_message(reply):
        await message.channel.send(chunk)


async def _main():
    await _start_http()
    await client.start(DISCORD_BOT_TOKEN)


if not DISCORD_BOT_TOKEN:
    logger.error("DISCORD_BOT_TOKEN not set — bot cannot start")
else:
    asyncio.run(_main())
