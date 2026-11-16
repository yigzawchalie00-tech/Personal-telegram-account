"""
Telegram Personal-Account Auto-Away Responder + Math Calculator + YouTube Bridge
-----------------------------------------------
Logs in as YOUR Telegram account (not a bot) using Telethon.

FEATURES:
  - Away mode (toggle with .away / .back in Saved Messages)
  - Robot image sent before away text reply
  - Math calculator (any private chat)
  - Auto-forward from specific private chats
  - Group mirror: forward EVERYTHING from one group to another
  - Media auto-save
  - Daily digest
  - Offline status when away
  - Session keepalive (get_me every 3 days to prevent session expiry)
  - YouTube downloader bridge (built in, same client/session):
      scheduler bot -> "DOWNLOAD:<url>[|<row_id>]" -> @Gozilla_bot -> video back

ENV VARS:
  TG_API_ID=
  TG_API_HASH=
  TG_SESSION_STRING=
  AWAY_MESSAGE=Hi {name}! Chalie is currently not active, will reply soon.
  AWAY_IMAGE_PATH=./away_robot.jpg
  FORWARD_TARGET=
  FORWARD_ALL_FROM=id1,id2,@username3
  FORWARD_KEYWORDS=urgent,invoice
  FORWARD_ON_MEDIA=true
  MEDIA_SAVE_DIR=./saved_media
  DIGEST_ENABLED=true
  DIGEST_TIME=20:00
  GROUP_SOURCE=  (comma-separated group usernames or IDs to mirror FROM)
  GROUP_TARGET=  (group username or ID to mirror TO)

  --- YouTube bridge (leave YT_SCHEDULER_BOT empty to disable) ---
  YT_SCHEDULER_BOT=   your scheduler bot username e.g. @MyYTSchedulerBot
  DOWNLOADER_BOT=     default Gozilla_bot
  QUALITY=            default 854p
  JOB_TIMEOUT=        seconds to wait per download (default 300)
"""

import os
import re
import math as _math
import asyncio
import threading
from datetime import datetime, date
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.tl.types import User
from telethon.tl.functions.account import UpdateStatusRequest

load_dotenv()

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION_STRING = os.environ.get("TG_SESSION_STRING", "")

AWAY_MESSAGE = os.environ.get(
    "AWAY_MESSAGE",
    "Hi {name}! Chalie is currently not active, will reply soon."
)

AWAY_IMAGE_PATH = os.environ.get("AWAY_IMAGE_PATH", "./away_robot.jpg")

# --- Auto-forward config ---
FORWARD_TARGET = os.environ.get("FORWARD_TARGET", "")
FORWARD_KEYWORDS = [
    k.strip().lower() for k in os.environ.get("FORWARD_KEYWORDS", "").split(",") if k.strip()
]
FORWARD_ON_MEDIA = os.environ.get("FORWARD_ON_MEDIA", "false").lower() == "true"
FORWARD_ALL_FROM_RAW = [
    x.strip() for x in os.environ.get("FORWARD_ALL_FROM", "").split(",") if x.strip()
]

# --- Forward exclude list (never forward messages from these IDs/usernames) ---
FORWARD_EXCLUDE_RAW = [
    x.strip() for x in os.environ.get("FORWARD_EXCLUDE", "609517172").split(",") if x.strip()
]

# --- Group mirror config ---
# GROUP_SOURCE supports multiple comma-separated channels: @chan1,@chan2,123456
GROUP_SOURCE_RAW = [
    x.strip() for x in os.environ.get("GROUP_SOURCE", "").split(",") if x.strip()
]
GROUP_TARGET = os.environ.get("GROUP_TARGET", "")

# GROUP_MIRROR_PAIRS: comma-separated @source>@target pairs
# e.g. @chan1>@dest1,@chan2>@dest2
GROUP_MIRROR_PAIRS_RAW = [
    x.strip() for x in os.environ.get("GROUP_MIRROR_PAIRS", "").split(",") if x.strip()
]

# --- Media organizer config ---
MEDIA_SAVE_DIR = os.environ.get("MEDIA_SAVE_DIR", "./saved_media")

# --- Daily digest config ---
DIGEST_ENABLED = os.environ.get("DIGEST_ENABLED", "false").lower() == "true"
DIGEST_TIME = os.environ.get("DIGEST_TIME", "20:00")

# --- YouTube bridge config ---
YT_SCHEDULER_BOT = os.environ.get("YT_SCHEDULER_BOT", "").strip().lstrip("@")
DOWNLOADER_BOT = os.environ.get("DOWNLOADER_BOT", "Gozilla_bot").strip().lstrip("@")
QUALITY = os.environ.get("QUALITY", "854p")
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "300"))
BRIDGE_ENABLED = bool(YT_SCHEDULER_BOT)

# In-memory state
is_away = False
already_replied_to = set()
forward_target_entity = None
forward_all_from_ids = set()
forward_exclude_ids = set()   # never forward from these IDs
group_source_ids = set()      # normalized marked chat ids for ALL source channels
group_target_entity = None
mirror_pairs = {}             # {source_chat_id: target_entity} from GROUP_MIRROR_PAIRS
daily_stats = {}
last_digest_date = None

# Bridge state: one download at a time so Gozilla replies can't get mixed up
bridge_jobs = None  # created in main() once the event loop is running
bridge_current = {"job": None, "done": None, "clicked": set()}

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


# --- Session keepalive (prevents Telegram from expiring the session) ----------

async def session_keepalive_loop():
    """
    Call get_me() every 3 days to prevent Telegram from expiring the session
    due to "terminate inactive sessions". This is the fix for weekly disconnects.
    """
    INTERVAL = 3 * 24 * 60 * 60  # 3 days in seconds
    while True:
        await asyncio.sleep(INTERVAL)
        try:
            me = await client.get_me()
            print(f"[keepalive] Session active — logged in as {me.first_name} (ID {me.id})")
        except Exception as e:
            print(f"[keepalive] get_me() failed: {e}")


# --- Offline-status control ---

async def set_telegram_offline(offline: bool):
    try:
        await client(UpdateStatusRequest(offline=offline))
        print(f"[presence] Set account -> {'offline' if offline else 'online'}")
    except Exception as e:
        print(f"[presence] Failed: {e}")


async def offline_keepalive_loop():
    while True:
        if is_away:
            await set_telegram_offline(True)
        await asyncio.sleep(30)


# --- Math calculator (scientific, full order of operations) ---

# Safe names allowed inside eval
_SAFE_GLOBALS = {
    "__builtins__": {},
    "abs": abs, "round": round, "pow": pow,
    "sqrt": _math.sqrt, "log": _math.log, "log10": _math.log10,
    "log2": _math.log2, "sin": _math.sin, "cos": _math.cos,
    "tan": _math.tan, "asin": _math.asin, "acos": _math.acos,
    "atan": _math.atan, "atan2": _math.atan2,
    "floor": _math.floor, "ceil": _math.ceil,
    "pi": _math.pi, "e": _math.e,
    "factorial": _math.factorial,
}


def _clean_expr(expr: str) -> str:
    """Normalise an expression string so Python eval can handle it."""
    expr = expr.strip()
    # Remove thousands-separating commas (e.g. 1,000 → 1000)
    expr = re.sub(r'(\d),(\d)', r'\1\2', expr)
    # Replace common alternate symbols
    expr = expr.replace("×", "*").replace("÷", "/").replace("^", "**")
    # Replace 'x' used as multiply only when surrounded by digits/spaces
    expr = re.sub(r'(?<=[\d\s])[xX](?=[\d\s])', '*', expr)
    return expr


def _safe_eval(expr: str):
    """Evaluate a mathematical expression safely. Returns (result, error_str)."""
    try:
        result = eval(expr, _SAFE_GLOBALS, {})   # noqa: S307
        return result, None
    except ZeroDivisionError:
        return None, "Division by zero"
    except Exception as e:
        return None, str(e)


def _fmt(value) -> str:
    """Format a number nicely — no trailing .0 for integers."""
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,}"
    if isinstance(value, float):
        # Up to 10 significant digits, strip trailing zeros
        s = f"{value:.10g}"
        return s
    return f"{value:,}"


def parse_and_calculate(text: str):
    """
    Scientific calculator.

    Handles:
      • Full order-of-operations:  3+6*5+9  →  42
      • Multi-line with per-line results + grand total
      • Optional label prefix:  "Apples = 3*5"
      • Functions: sqrt(), sin(), log(), factorial(), …
      • Constants: pi, e
      • Exponent: 2^10 or 2**10
      • Percent: 200*15% → 30  (% converted to /100)
    """
    lines = text.strip().splitlines()
    results = []
    grand_total = 0.0
    found_any = False

    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        # Try to split "Label = expression" or just "expression"
        label = ""
        expr_part = raw_line
        label_match = re.match(r'^([^=\d(]+?)\s*=\s*(.+)$', raw_line)
        if label_match:
            possible_label = label_match.group(1).strip()
            possible_expr = label_match.group(2).strip()
            # Only treat as label if the left side has no digits (avoid "3+2 = ?" style)
            if not re.search(r'\d', possible_label):
                label = possible_label
                expr_part = possible_expr

        # Normalise percent signs before cleaning  e.g. 15% → 0.15
        expr_part = re.sub(r'(\d+(?:\.\d+)?)\s*%', lambda m: str(float(m.group(1)) / 100), expr_part)

        clean = _clean_expr(expr_part)

        # Only try eval if the expression contains a digit or known constant
        if not re.search(r'[\d]|pi|e\b', clean):
            results.append(raw_line)
            continue

        value, err = _safe_eval(clean)
        if err is not None or value is None:
            results.append(raw_line)
            continue

        found_any = True
        try:
            grand_total += float(value)
        except (TypeError, ValueError):
            pass

        fmt_val = _fmt(value)
        if label:
            results.append(f"{label} = {expr_part} = {fmt_val}")
        else:
            results.append(f"{expr_part} = {fmt_val}")

    if not found_any:
        return None

    if len([r for r in results if "=" in r]) > 1:
        # Only show grand total when there are multiple calculations
        results.append("")
        results.append(f"Total = {_fmt(grand_total)}")

    return "\n".join(results)


# --- Helpers ---

def sender_display_name(sender) -> str:
    if isinstance(sender, User):
        name = " ".join(filter(None, [sender.first_name, sender.last_name])).strip()
        if not name:
            name = sender.username or f"id{sender.id}"
        return name
    return getattr(sender, "title", None) or f"id{getattr(sender, 'id', 'unknown')}"


def build_away_message(sender) -> str:
    if isinstance(sender, User) and sender.first_name:
        name = sender.first_name
    else:
        name = sender_display_name(sender)
    try:
        return AWAY_MESSAGE.format(name=name)
    except (KeyError, IndexError):
        return AWAY_MESSAGE


def safe_filename_part(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_-]', "_", name)[:40]


# --- Resolve helpers ---

async def resolve_from_dialogs(target_id: int):
    """
    Search dialogs for a match by either the bare entity id OR the
    normalized 'marked' peer id (the form Telethon uses for event.chat_id,
    e.g. -100xxxxxxxxxx for supergroups/channels).
    """
    try:
        async for dialog in client.iter_dialogs():
            if dialog.entity.id == target_id or utils.get_peer_id(dialog.entity) == target_id:
                return dialog.entity
    except Exception as e:
        print(f"[resolve] Error: {e}")
    return None


async def resolve_any_chat(value: str):
    """
    Resolve a username or numeric ID (bare or marked/-100 form) to an entity.
    """
    value = value.strip()
    if not value:
        raise ValueError("empty value")

    if value.lstrip("-").isdigit():
        raw_id = int(value)
        found = await resolve_from_dialogs(raw_id)
        if found:
            return found
        try:
            return await client.get_entity(raw_id)
        except Exception:
            pass
        if str(raw_id).startswith("-100"):
            try:
                bare_id = int(str(raw_id)[4:])
                return await client.get_entity(bare_id)
            except Exception:
                pass
        raise ValueError(f"could not resolve ID {raw_id}")

    return await client.get_entity(value)


async def resolve_forward_target():
    global forward_target_entity
    if not FORWARD_TARGET:
        print("[forward] No FORWARD_TARGET set.")
        return
    try:
        forward_target_entity = await resolve_any_chat(FORWARD_TARGET)
        print(f"[forward] Target resolved: {sender_display_name(forward_target_entity)}")
    except Exception as e:
        print(f"[forward] Could not resolve FORWARD_TARGET: {e}")


async def resolve_forward_all_from():
    global forward_all_from_ids
    if not FORWARD_ALL_FROM_RAW:
        print("[forward] No FORWARD_ALL_FROM set.")
        return
    print(f"[forward] Resolving {len(FORWARD_ALL_FROM_RAW)} FORWARD_ALL_FROM entries...")
    for entry in FORWARD_ALL_FROM_RAW:
        try:
            if entry.lstrip("-").isdigit():
                target_id = int(entry)
                entity = await resolve_from_dialogs(target_id)
                forward_all_from_ids.add(target_id)
                name = sender_display_name(entity) if entity else str(target_id)
                print(f"[forward] ✅ {name} → ID {target_id}")
            else:
                entity = await client.get_entity(entry)
                forward_all_from_ids.add(entity.id)
                print(f"[forward] ✅ {entry} → ID {entity.id}")
        except Exception as e:
            print(f"[forward] ❌ Could not resolve '{entry}': {e}")
    print(f"[forward] Active IDs: {forward_all_from_ids}")


async def resolve_forward_exclude():
    global forward_exclude_ids
    for entry in FORWARD_EXCLUDE_RAW:
        try:
            if entry.lstrip("-").isdigit():
                forward_exclude_ids.add(int(entry))
                print(f"[forward] 🚫 Excluded ID: {entry}")
            else:
                entity = await client.get_entity(entry)
                forward_exclude_ids.add(entity.id)
                print(f"[forward] 🚫 Excluded: {entry} → ID {entity.id}")
        except Exception as e:
            print(f"[forward] ❌ Could not resolve exclude '{entry}': {e}")


async def resolve_group_mirror():
    """
    Resolves all GROUP_SOURCE entries (comma-separated) and GROUP_TARGET.
    group_source_ids holds the normalized 'marked' peer ids for direct
    comparison against event.chat_id.
    """
    global group_source_ids, group_target_entity

    print(f"[mirror] GROUP_SOURCE_RAW = {GROUP_SOURCE_RAW}")
    print(f"[mirror] GROUP_TARGET = {GROUP_TARGET!r}")

    if not GROUP_SOURCE_RAW or not GROUP_TARGET:
        print("[mirror] GROUP_SOURCE or GROUP_TARGET not set — mirror disabled.")
        return

    print(f"[mirror] Resolving {len(GROUP_SOURCE_RAW)} source channel(s)...")
    for entry in GROUP_SOURCE_RAW:
        try:
            print(f"[mirror] Trying to resolve '{entry}'...")
            source_entity = await resolve_any_chat(entry)
            marked_id = utils.get_peer_id(source_entity)
            group_source_ids.add(marked_id)
            title = getattr(source_entity, 'title', entry)
            print(f"[mirror] ✅ Source: {title} → chat_id {marked_id}")
        except Exception as e:
            print(f"[mirror] ❌ Could not resolve source '{entry}': {type(e).__name__}: {e}")

    if not group_source_ids:
        print("[mirror] ❌ No sources resolved — mirror disabled.")
        return

    try:
        print(f"[mirror] Trying to resolve target '{GROUP_TARGET}'...")
        group_target_entity = await resolve_any_chat(GROUP_TARGET)
        print(f"[mirror] ✅ Target: {getattr(group_target_entity, 'title', GROUP_TARGET)}")
    except Exception as e:
        print(f"[mirror] ❌ Could not resolve GROUP_TARGET: {type(e).__name__}: {e}")


async def resolve_mirror_pairs():
    """
    Resolves GROUP_MIRROR_PAIRS entries like @source>@target
    into {source_chat_id: target_entity} stored in mirror_pairs.
    """
    global mirror_pairs
    if not GROUP_MIRROR_PAIRS_RAW:
        print("[pairs] No GROUP_MIRROR_PAIRS set.")
        return
    print(f"[pairs] Resolving {len(GROUP_MIRROR_PAIRS_RAW)} mirror pair(s)...")
    for pair in GROUP_MIRROR_PAIRS_RAW:
        if ">" not in pair:
            print(f"[pairs] ❌ Invalid pair (no '>'): {pair!r}")
            continue
        src_raw, _, tgt_raw = pair.partition(">")
        src_raw = src_raw.strip()
        tgt_raw = tgt_raw.strip()
        try:
            src_entity = await resolve_any_chat(src_raw)
            src_id = utils.get_peer_id(src_entity)
        except Exception as e:
            print(f"[pairs] ❌ Could not resolve source '{src_raw}': {e}")
            continue
        try:
            tgt_entity = await resolve_any_chat(tgt_raw)
        except Exception as e:
            print(f"[pairs] ❌ Could not resolve target '{tgt_raw}': {e}")
            continue
        mirror_pairs[src_id] = tgt_entity
        src_title = getattr(src_entity, 'title', src_raw)
        tgt_title = getattr(tgt_entity, 'title', tgt_raw)
        print(f"[pairs] ✅ {src_title} → {tgt_title} (src_id={src_id})")
    print(f"[pairs] Active pairs: {len(mirror_pairs)}")


# --- Handlers ---

@client.on(events.NewMessage(outgoing=True, chats="me"))
async def toggle_handler(event):
    global is_away, already_replied_to
    text = (event.raw_text or "").strip().lower()
    if text == ".away":
        is_away = True
        already_replied_to.clear()
        await set_telegram_offline(True)
        await event.reply(f"✅ Away mode ON ({datetime.now().strftime('%H:%M')})")
        print("[state] Away mode enabled")
    elif text == ".back":
        is_away = False
        already_replied_to.clear()
        await set_telegram_offline(False)
        await event.reply(f"✅ Away mode OFF ({datetime.now().strftime('%H:%M')})")
        print("[state] Away mode disabled")


@client.on(events.NewMessage(incoming=True))
async def message_handler(event):
    """Handle ALL incoming messages — private and group."""

    # ── GROUP MIRROR ──────────────────────────────────────────────────────
    if not event.is_private:
        # Simple GROUP_SOURCE → GROUP_TARGET mirror
        if group_source_ids and group_target_entity:
            if event.chat_id in group_source_ids:
                try:
                    await client.forward_messages(group_target_entity, event.message)
                    print(f"[mirror] ✅ Forwarded from chat_id {event.chat_id} to target")
                except Exception as e:
                    print(f"[mirror] ❌ Failed: {e}")

        # GROUP_MIRROR_PAIRS: @source>@target pairs
        if event.chat_id in mirror_pairs:
            try:
                await client.forward_messages(mirror_pairs[event.chat_id], event.message)
                print(f"[pairs] ✅ Forwarded from chat_id {event.chat_id}")
            except Exception as e:
                print(f"[pairs] ❌ Failed: {e}")

        return

    # ── PRIVATE CHAT ──────────────────────────────────────────────────────
    sender = await event.get_sender()
    # Bots (including the scheduler bot and Gozilla) are handled by the
    # bridge handlers below, never by the away/calculator logic.
    if sender is None or getattr(sender, "bot", False):
        return

    sender_id = sender.id
    message_text = event.raw_text or ""

    # Daily stats
    name = sender_display_name(sender)
    entry = daily_stats.setdefault(sender_id, {"name": name, "count": 0})
    entry["count"] += 1
    entry["name"] = name

    # Media organizer
    if event.message.media:
        try:
            os.makedirs(MEDIA_SAVE_DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            prefix = f"{safe_filename_part(name)}_{ts}"
            saved_path = await client.download_media(
                event.message, file=os.path.join(MEDIA_SAVE_DIR, prefix)
            )
            if saved_path:
                print(f"[media] Saved from {name} -> {saved_path}")
        except Exception as e:
            print(f"[media] Failed to save media from {name}: {e}")

    # Auto-forward
    should_forward = False
    if forward_target_entity is not None and sender_id not in forward_exclude_ids:
        if forward_all_from_ids and sender_id in forward_all_from_ids:
            should_forward = True
            print(f"[forward] Matched FORWARD_ALL_FROM: {name}")
        elif event.message.media and FORWARD_ON_MEDIA:
            should_forward = True
        elif FORWARD_KEYWORDS and message_text:
            if any(kw in message_text.lower() for kw in FORWARD_KEYWORDS):
                should_forward = True
    elif sender_id in forward_exclude_ids:
        print(f"[forward] Skipped (excluded): {name}")

    if should_forward:
        try:
            await client.forward_messages(forward_target_entity, event.message)
            print(f"[forward] ✅ Forwarded from {name}")
        except Exception as e:
            print(f"[forward] ❌ Failed: {e}")

    # Math calculator
    calc_result = parse_and_calculate(message_text)
    if calc_result:
        await event.reply(calc_result)
        print(f"[math] Replied with calculation to {name}")
        return

    # Auto-away reply
    if not is_away:
        return
    if sender_id in already_replied_to:
        return

    # Send robot image first, then away text
    if AWAY_IMAGE_PATH and os.path.exists(AWAY_IMAGE_PATH):
        try:
            await client.send_file(sender_id, AWAY_IMAGE_PATH)
        except Exception as e:
            print(f"[auto-reply] Failed to send image to {name}: {e}")
    else:
        print(f"[auto-reply] Image not found ({AWAY_IMAGE_PATH}), skipping.")

    await event.reply(build_away_message(sender))
    already_replied_to.add(sender_id)
    print(f"[auto-reply] Sent image + away message to {name}")


# --- YouTube downloader bridge (runs on the same client as everything above) ---

async def bridge_worker():
    """Process download jobs one at a time so Gozilla replies can't get mixed up."""
    while True:
        url, row_id = await bridge_jobs.get()
        done = asyncio.Event()
        bridge_current.update(job=(url, row_id), done=done, clicked=set())
        try:
            print(f"[bridge] Sending to @{DOWNLOADER_BOT}: {url} (row={row_id})")
            await client.send_message(DOWNLOADER_BOT, url)
            await asyncio.wait_for(done.wait(), JOB_TIMEOUT)
        except asyncio.TimeoutError:
            print(f"[bridge] ❌ Timed out waiting for video: {url}")
        except Exception as e:
            print(f"[bridge] ❌ Job failed: {e}")
        finally:
            bridge_current.update(job=None, done=None)


async def bridge_click_quality(msg):
    if msg.id in bridge_current["clicked"]:
        return
    buttons = [b for row in msg.buttons for b in row]
    choice = next((b for b in buttons if QUALITY.lower() in (b.text or "").lower()), None)
    if choice is None:
        choice = next(
            (b for b in buttons if "p -" in (b.text or "") or "MB" in (b.text or "")), None
        )
    if choice:
        bridge_current["clicked"].add(msg.id)
        print(f"[bridge] Clicking: {choice.text}")
        await choice.click()


async def bridge_gozilla_handler(event):
    """Gozilla replies: click the quality button, then pass the video on."""
    msg = event.message
    job = bridge_current["job"]
    if not job:
        return  # not expecting anything

    if msg.buttons:
        await bridge_click_quality(msg)
        return

    if msg.video or msg.document:
        url, row_id = job
        try:
            scheduler = await client.get_entity(YT_SCHEDULER_BOT)
            if row_id:
                # Auto-repost: re-send with ROW:<id> caption so the bot can match it
                await client.send_file(scheduler, msg.media, caption=f"ROW:{row_id}")
            else:
                # Manual flow: plain forward
                await client.forward_messages(scheduler, msg)
            print(f"[bridge] ✅ Video sent to @{YT_SCHEDULER_BOT} (row={row_id})")
        except Exception as e:
            print(f"[bridge] ❌ Failed to send video: {e}")
        finally:
            if bridge_current["done"]:
                bridge_current["done"].set()


async def bridge_scheduler_handler(event):
    """Download requests from the scheduler bot: DOWNLOAD:<url>[|<row_id>]."""
    text = (event.message.text or "").strip()
    if not text.startswith("DOWNLOAD:"):
        return
    payload = text[len("DOWNLOAD:"):].strip()
    url, _, row = payload.partition("|")
    row_id = int(row) if row.strip().isdigit() else None
    print(f"[bridge] Queued: {url.strip()} (row={row_id})")
    await bridge_jobs.put((url.strip(), row_id))


def register_bridge_handlers():
    client.add_event_handler(
        bridge_gozilla_handler, events.NewMessage(from_users=DOWNLOADER_BOT)
    )
    client.add_event_handler(
        bridge_gozilla_handler, events.MessageEdited(from_users=DOWNLOADER_BOT)
    )
    client.add_event_handler(
        bridge_scheduler_handler, events.NewMessage(from_users=YT_SCHEDULER_BOT)
    )
    print(f"[bridge] ✅ Enabled: @{YT_SCHEDULER_BOT} ⇄ @{DOWNLOADER_BOT} (quality {QUALITY})")


# --- Daily digest ---

async def digest_loop():
    global last_digest_date, daily_stats
    if not DIGEST_ENABLED:
        print("[digest] Disabled.")
        return
    try:
        target_hour, target_minute = (int(p) for p in DIGEST_TIME.split(":"))
    except Exception:
        print(f"[digest] Invalid DIGEST_TIME. Disabled.")
        return
    print(f"[digest] Will send at {DIGEST_TIME} each day.")
    while True:
        now = datetime.now()
        today = date.today()
        if now.hour == target_hour and now.minute == target_minute and last_digest_date != today:
            await send_digest()
            last_digest_date = today
        await asyncio.sleep(30)


async def send_digest():
    if not daily_stats:
        summary = "📊 Daily digest: no incoming private messages today."
    else:
        lines = ["📊 Daily digest — incoming private messages:"]
        total = 0
        for stats in sorted(daily_stats.values(), key=lambda s: -s["count"]):
            lines.append(f"  • {stats['name']}: {stats['count']}")
            total += stats["count"]
        lines.append("")
        lines.append(f"Total messages: {total}")
        lines.append(f"Unique senders: {len(daily_stats)}")
        summary = "\n".join(lines)
    try:
        await client.send_message("me", summary)
        print("[digest] Sent daily digest")
    except Exception as e:
        print(f"[digest] Failed: {e}")
    daily_stats.clear()


# --- HTTP keep-alive ---

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        status = "AWAY" if is_away else "ACTIVE"
        fwd_ids = ",".join(str(i) for i in forward_all_from_ids) or "none"
        if group_source_ids and group_target_entity:
            src_list = ",".join(str(i) for i in group_source_ids)
            mirror = f"{src_list}→{getattr(group_target_entity, 'id', '?')}"
        else:
            mirror = "disabled"
        pairs = f"{len(mirror_pairs)} pairs" if mirror_pairs else "disabled"
        bridge = "on" if BRIDGE_ENABLED else "off"
        self.wfile.write(
            f"OK - status: {status} - forward_all_from: {fwd_ids} - mirror: {mirror} - pairs: {pairs} - bridge: {bridge}".encode()
        )

    def log_message(self, format, *args):
        pass


def start_ping_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    print(f"[keep-alive] Ping server on port {port}")
    server.serve_forever()


# --- Main ---

async def main():
    global bridge_jobs
    threading.Thread(target=start_ping_server, daemon=True).start()
    print("Logging in...")
    await client.start()
    print("Logged in. Listening for messages.")
    print("Send '.away' or '.back' to your Saved Messages to toggle.")

    if not SESSION_STRING:
        print("\n[!] Save this to TG_SESSION_STRING:\n")
        print(client.session.save())
        print()

    await resolve_forward_target()
    await resolve_forward_all_from()
    await resolve_forward_exclude()
    await resolve_group_mirror()
    await resolve_mirror_pairs()

    if is_away:
        await set_telegram_offline(True)

    asyncio.create_task(offline_keepalive_loop())
    asyncio.create_task(session_keepalive_loop())   # <-- prevents session expiry

    if BRIDGE_ENABLED:
        bridge_jobs = asyncio.Queue()
        register_bridge_handlers()
        asyncio.create_task(bridge_worker())
    else:
        print("[bridge] YT_SCHEDULER_BOT not set — bridge disabled.")

    if DIGEST_ENABLED:
        asyncio.create_task(digest_loop())

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
