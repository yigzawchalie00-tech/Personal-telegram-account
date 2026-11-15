"""
YouTube Downloader Bridge
--------------------------
Uses your Telegram account (Telethon) to:
1. Receive a YouTube URL from your YouTube Shorts Scheduler bot
2. Send the URL to @Gozilla_bot
3. Wait for quality buttons
4. Click 854p automatically
5. Wait for the video file
6. Forward it to your YouTube Shorts Scheduler bot

This script runs alongside auto_away.py on Render.

ENV VARS (same as auto_away.py):
  TG_API_ID=
  TG_API_HASH=
  TG_SESSION_STRING=
  YT_SCHEDULER_BOT=   Your YouTube scheduler bot username e.g. @MyYTSchedulerBot
  DOWNLOADER_BOT=     @Gozilla_bot (default)
  QUALITY=            Video quality to pick: 640p, 854p, 1024p (default: 854p)
"""

import asyncio
import os
import logging
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import KeyboardButtonCallback

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_ID       = int(os.environ["TG_API_ID"])
API_HASH     = os.environ["TG_API_HASH"]
SESSION      = os.environ["TG_SESSION_STRING"]

YT_SCHEDULER_BOT = os.environ.get("YT_SCHEDULER_BOT", "").lstrip("@")
DOWNLOADER_BOT   = os.environ.get("DOWNLOADER_BOT", "Gozilla_bot").lstrip("@")
QUALITY          = os.environ.get("QUALITY", "854p")

# Track pending downloads: {gozilla_msg_id: original_url}
pending = {}

client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)


async def download_video(url: str):
    """Send URL to Gozilla_bot and wait for video."""
    logger.info(f"[bridge] Sending URL to @{DOWNLOADER_BOT}: {url}")
    msg = await client.send_message(DOWNLOADER_BOT, url)
    pending[msg.id] = url
    logger.info(f"[bridge] Sent! Waiting for quality buttons...")


@client.on(events.NewMessage(from_users=DOWNLOADER_BOT))
async def handle_gozilla_reply(event):
    """Handle reply from Gozilla_bot — click quality button or receive video."""
    msg = event.message

    # Check if it has inline buttons (quality selection)
    if msg.buttons:
        logger.info(f"[bridge] Got quality buttons from Gozilla_bot")
        clicked = False
        for row in msg.buttons:
            for btn in row:
                if QUALITY.lower() in btn.text.lower():
                    logger.info(f"[bridge] Clicking: {btn.text}")
                    await btn.click()
                    clicked = True
                    break
            if clicked:
                break

        # If preferred quality not found, click first video button
        if not clicked:
            for row in msg.buttons:
                for btn in row:
                    if "p -" in btn.text or "MB" in btn.text:
                        logger.info(f"[bridge] Clicking first available: {btn.text}")
                        await btn.click()
                        break

    # Check if it has a video file
    elif msg.video or msg.document:
        logger.info(f"[bridge] Got video from Gozilla_bot! Forwarding to scheduler bot...")
        try:
            scheduler = await client.get_entity(YT_SCHEDULER_BOT)
            await client.forward_messages(scheduler, msg)
            logger.info(f"[bridge] ✅ Video forwarded to @{YT_SCHEDULER_BOT}")
        except Exception as e:
            logger.error(f"[bridge] Failed to forward: {e}")


@client.on(events.NewMessage(from_users=YT_SCHEDULER_BOT))
async def handle_scheduler_request(event):
    """
    Listen for YouTube URLs sent from the scheduler bot to your account.
    The scheduler bot sends a URL to your personal account to trigger download.
    """
    text = event.message.text or ""
    if text.startswith("DOWNLOAD:"):
        url = text.replace("DOWNLOAD:", "").strip()
        logger.info(f"[bridge] Download request received: {url}")
        await download_video(url)


async def main():
    await client.start()
    me = await client.get_me()
    logger.info(f"[bridge] ✅ Logged in as {me.first_name} (@{me.username})")
    logger.info(f"[bridge] Watching @{DOWNLOADER_BOT} → @{YT_SCHEDULER_BOT}")
    logger.info(f"[bridge] Preferred quality: {QUALITY}")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
