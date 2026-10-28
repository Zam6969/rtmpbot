"""Discord bot that runs the whole DJ recording flow.

/register gives a DJ their stream key and saves their username, timezone and video privacy.
nginx-rtmp records each stream and calls the small web server in here:
  /rtmp/publish      -> accept or reject the stream key
  /rtmp/record_done  -> a recording finished
When a recording finishes, the bot DMs the DJ asking for a custom title (default after 30 minutes),
uploads it to YouTube, DMs them the link, and deletes the local file.
"""
import asyncio
import datetime as dt
import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

import discord
from aiohttp import web
from discord import app_commands

import youtube
from youtube import upload_video

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dj-recorder")

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"]) if os.environ.get("GUILD_ID") else None
RTMP_PUBLIC_URL = os.environ.get("RTMP_PUBLIC_URL", "rtmp://YOUR.SERVER.IP/live")
MIN_RECORDING_SECONDS = int(os.environ.get("MIN_RECORDING_SECONDS", "120"))
# Discord user IDs allowed to use the admin commands (comma-separated in .env to add more).
OWNER_IDS = {int(i) for i in os.environ.get("OWNER_IDS", "464865517354614805").split(",") if i.strip()}
TITLE_WAIT_MINUTES = int(os.environ.get("TITLE_WAIT_MINUTES", "30"))

CONFIG_DIR = Path("/config")
RECORDINGS_DIR = Path("/recordings")
TOKEN_PATH = CONFIG_DIR / "token.json"
CLIENT_PATH = CONFIG_DIR / "client_secret.json"
# Shared with update.sh on the server (see that file).
BUSY_PATH = CONFIG_DIR / "busy"
UPDATE_REQUEST_PATH = CONFIG_DIR / "update-requested"
UPDATE_RESULT_PATH = CONFIG_DIR / "update-result"
VERSION_PATH = CONFIG_DIR / "version"

# Recording files look like <streamkey>-YYYYmmdd-HHMMSS.flv, stamped in UTC (see rtmp/nginx.conf).
FILE_RE = re.compile(r"^(?P<key>[0-9a-f]+)-(?P<stamp>\d{8}-\d{6})\.flv$")

ALL_TIMEZONES = sorted(available_timezones())
COMMON_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Phoenix",
    "America/Anchorage", "Pacific/Honolulu", "America/Toronto", "America/Sao_Paulo", "America/Mexico_City",
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Amsterdam", "Europe/Madrid", "Europe/Stockholm",
    "Europe/Moscow", "Asia/Tokyo", "Asia/Seoul", "Asia/Shanghai", "Asia/Singapore", "Asia/Kolkata",
    "Australia/Sydney", "Australia/Perth", "Pacific/Auckland",
]


def default_title(username: str, when: dt.datetime) -> str:
    return f"@{username} on {when:%m/%d/%Y} VRCHAT"


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS djs (
                discord_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                stream_key TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS uploads (
                file TEXT PRIMARY KEY,
                dj_name TEXT NOT NULL,
                status TEXT NOT NULL,
                video_id TEXT,
                error TEXT,
                updated TEXT NOT NULL
            );
            """
        )
        # Columns added after the first version; add them to existing databases.
        self._add_column("djs", "timezone", "TEXT NOT NULL DEFAULT 'UTC'")
        self._add_column("djs", "privacy", "TEXT NOT NULL DEFAULT 'unlisted'")
        self._add_column("djs", "channel_id", "INTEGER")  # where to post if their DMs are closed
        self._add_column("uploads", "discord_id", "INTEGER")
        self._add_column("uploads", "title", "TEXT")

    def _add_column(self, table: str, column: str, spec: str):
        cols = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            with self.db:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")

    def dj_by_id(self, discord_id: int):
        return self.db.execute("SELECT * FROM djs WHERE discord_id = ?", (discord_id,)).fetchone()

    def dj_by_key(self, key: str):
        return self.db.execute("SELECT * FROM djs WHERE stream_key = ?", (key,)).fetchone()

    def save_dj(self, discord_id: int, name: str, key: str, timezone: str, privacy: str):
        with self.db:
            self.db.execute(
                "INSERT INTO djs (discord_id, name, stream_key, timezone, privacy) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(discord_id) DO UPDATE SET name = excluded.name, stream_key = excluded.stream_key, "
                "timezone = excluded.timezone, privacy = excluded.privacy",
                (discord_id, name, key, timezone, privacy),
            )

    def set_channel(self, discord_id: int, channel_id: int | None):
        if channel_id:
            with self.db:
                self.db.execute("UPDATE djs SET channel_id = ? WHERE discord_id = ?", (channel_id, discord_id))

    def upload(self, file: str):
        return self.db.execute("SELECT * FROM uploads WHERE file = ?", (file,)).fetchone()

    def set_upload(self, file: str, dj, status: str, **fields):
        row = self.upload(file)
        values = {
            "dj_name": dj["name"],
            "discord_id": dj["discord_id"],
            "status": status,
            "video_id": row["video_id"] if row else None,
            "error": None,
            "title": row["title"] if row else None,
            "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        values.update(fields)
        with self.db:
            self.db.execute(
                "INSERT INTO uploads (file, dj_name, discord_id, status, video_id, error, title, updated) "
                "VALUES (:file, :dj_name, :discord_id, :status, :video_id, :error, :title, :updated) "
                "ON CONFLICT(file) DO UPDATE SET dj_name = excluded.dj_name, discord_id = excluded.discord_id, "
                "status = excluded.status, video_id = excluded.video_id, error = excluded.error, "
                "title = excluded.title, updated = excluded.updated",
                {"file": file, **values},
            )

    def uploads(self, statuses):
        marks = ",".join("?" * len(statuses))
        return self.db.execute(
            f"SELECT * FROM uploads WHERE status IN ({marks}) ORDER BY updated DESC", tuple(statuses)
        ).fetchall()


store = Store(CONFIG_DIR / "bot.db")
upload_lock = asyncio.Lock()  # one upload at a time keeps bandwidth and YouTube quota sane
live_keys: set[str] = set()  # keys streaming right now; their files are still being written
queued: set[str] = set()  # file names already being handled
uploading = False  # True while a set is being sent to YouTube


def new_key() -> str:
    return secrets.token_hex(16)


def clean_title(text: str) -> str:
    # YouTube rejects < and > in titles and caps them at 100 characters.
    return re.sub(r"[<>]", "", text).strip()[:100]


def duration_seconds(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return 0.0


def remux_to_mp4(src: Path) -> Path:
    dst = src.with_suffix(".mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(dst)],
        check=True,
    )
    return dst


# ---- Asking the DJ for a title ------------------------------------------------

class TitleModal(discord.ui.Modal, title="Custom video title"):
    def __init__(self, view: "TitleView"):
        super().__init__()
        self.parent_view = view
        self.title_input = discord.ui.TextInput(
            label="Title", default=view.default, max_length=100, min_length=1
        )
        self.add_item(self.title_input)

    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.chosen = clean_title(self.title_input.value) or self.parent_view.default
        await interaction.response.edit_message(
            content=f"Got it! Uploading as **{self.parent_view.chosen}**. I'll send you the link when it's up.",
            view=None,
        )
        self.parent_view.stop()


class TitleView(discord.ui.View):
    def __init__(self, default: str, dj_id: int):
        super().__init__(timeout=None)  # ask_for_title enforces the deadline itself
        self.default = default
        self.dj_id = dj_id
        self.chosen: str | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # When posted in a channel (DMs closed), only the DJ whose set it is may pick the title.
        if interaction.user.id != self.dj_id:
            await interaction.response.send_message("This isn't your set.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Custom title", style=discord.ButtonStyle.primary)
    async def custom(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TitleModal(self))

    @discord.ui.button(label="Use default title", style=discord.ButtonStyle.secondary)
    async def use_default(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.chosen = self.default
        await interaction.response.edit_message(
            content=f"Got it! Uploading as **{self.default}**. I'll send you the link when it's up.", view=None
        )
        self.stop()


async def dm(discord_id: int, content: str, view: discord.ui.View | None = None):
    """DM a user. Returns the message, or None if their DMs are closed."""
    try:
        user = await bot.fetch_user(discord_id)
        if view is None:
            return await user.send(content)
        return await user.send(content, view=view)
    except discord.HTTPException as e:
        log.warning("Couldn't DM %s: %s", discord_id, e)
        return None


async def notify(dj, content: str, view: discord.ui.View | None = None):
    """DM a DJ, or post in the channel they last used the bot in if their DMs are closed."""
    msg = await dm(dj["discord_id"], content, view)
    if msg is not None or not dj["channel_id"]:
        return msg
    try:
        channel = bot.get_channel(dj["channel_id"]) or await bot.fetch_channel(dj["channel_id"])
        kwargs = {"allowed_mentions": discord.AllowedMentions(users=True)}
        if view is not None:
            kwargs["view"] = view
        return await channel.send(
            f"<@{dj['discord_id']}> {content}\n"
            "-# I couldn't DM you, so I'm posting here. To get these privately, turn on "
            "**Direct Messages** in this server's Privacy Settings.",
            **kwargs,
        )
    except discord.HTTPException as e:
        log.warning("Couldn't post for %s in channel %s either: %s", dj["discord_id"], dj["channel_id"], e)
        return None


async def ask_for_title(dj, default: str, minutes: float) -> str:
    view = TitleView(default, dj["discord_id"])
    msg = await notify(
        dj,
        f"🎧 Your set ({minutes:.0f} min) finished recording and is ready to upload to YouTube.\n"
        f"Default title: **{default}**\n"
        f"Want a custom title? If you don't pick within {TITLE_WAIT_MINUTES:g} minutes, I'll use the default.",
        view,
    )
    if msg is None:
        return default
    try:
        await asyncio.wait_for(view.wait(), TITLE_WAIT_MINUTES * 60)
    except asyncio.TimeoutError:
        view.stop()
        try:
            await msg.edit(content=f"No answer, so I'm uploading it as **{default}**. Link coming soon.", view=None)
        except discord.HTTPException:
            pass
    return view.chosen or default


# ---- Recording pipeline ---------------------------------------------------------

def queue_recording(flv: Path):
    if flv.name not in queued:
        queued.add(flv.name)
        asyncio.create_task(process_recording(flv))


async def process_recording(flv: Path):
    try:
        await _process_recording(flv)
    except Exception:
        log.exception("Unexpected error handling %s", flv.name)
    finally:
        queued.discard(flv.name)


async def _process_recording(flv: Path):
    m = FILE_RE.match(flv.name)
    if not m or not flv.exists():
        log.warning("Ignoring unexpected file %s", flv)
        return

    # Prefer the DJ remembered for this file, so a key reset mid-upload doesn't lose track of whose set it is.
    row = store.upload(flv.name)
    dj = store.dj_by_id(row["discord_id"]) if row and row["discord_id"] else store.dj_by_key(m["key"])
    if dj is None:
        log.warning("No DJ for %s; leaving it on disk", flv.name)
        return

    seconds = await asyncio.to_thread(duration_seconds, flv)
    if seconds < MIN_RECORDING_SECONDS:
        log.info("%s is only %.0fs; deleting without upload", flv.name, seconds)
        flv.unlink(missing_ok=True)
        store.set_upload(flv.name, dj, "skipped", error=f"too short ({seconds:.0f}s)")
        return

    started_utc = dt.datetime.strptime(m["stamp"], "%Y%m%d-%H%M%S").replace(tzinfo=dt.timezone.utc)
    try:
        started = started_utc.astimezone(ZoneInfo(dj["timezone"]))
    except Exception:
        started = started_utc

    title = row["title"] if row and row["title"] else None
    if title is None:
        store.set_upload(flv.name, dj, "awaiting_title")
        title = clean_title(await ask_for_title(dj, clean_title(default_title(dj["name"], started)), seconds / 60))
        store.set_upload(flv.name, dj, "queued", title=title)

    description = f"Live set by @{dj['name']}, recorded {started:%m/%d/%Y %I:%M %p %Z}."

    global uploading
    async with upload_lock:
        uploading = True
        refresh_busy()
        store.set_upload(flv.name, dj, "uploading")
        mp4 = flv.with_suffix(".mp4")
        try:
            await asyncio.to_thread(remux_to_mp4, flv)
            video_id = await asyncio.to_thread(upload_video, mp4, title, description, dj["privacy"], TOKEN_PATH)
        except Exception as e:
            log.exception("Upload of %s failed; keeping it for /retryuploads", flv.name)
            store.set_upload(flv.name, dj, "failed", error=str(e)[:500])
            await notify(dj, f"⚠️ Uploading **{title}** failed. It's saved and will be retried.")
            return
        finally:
            mp4.unlink(missing_ok=True)
            uploading = False
            refresh_busy()

    flv.unlink(missing_ok=True)
    store.set_upload(flv.name, dj, "done", video_id=video_id)
    log.info("Uploaded %s as https://youtu.be/%s (%s)", flv.name, video_id, title)
    await notify(
        dj,
        f"✅ **{title}** is on YouTube ({dj['privacy']}):\nhttps://youtu.be/{video_id}\n"
        "It may take a few minutes for YouTube to finish processing HD.",
    )


def refresh_busy():
    """Tell update.sh not to restart us while someone is live or an upload is running."""
    if live_keys or uploading:
        BUSY_PATH.touch()
    else:
        BUSY_PATH.unlink(missing_ok=True)


async def housekeeping():
    """Keep the busy marker fresh and pass update results from update.sh on to the admin who asked."""
    await bot.wait_until_ready()
    while True:
        refresh_busy()
        if UPDATE_RESULT_PATH.exists():
            lines = UPDATE_RESULT_PATH.read_text().splitlines()
            UPDATE_RESULT_PATH.unlink(missing_ok=True)
            requester = lines[0].strip() if lines else ""
            message = "\n".join(lines[1:]).strip()
            log.info("Update: %s", message)
            if requester.isdigit():
                await dm(int(requester), f"🔄 {message}")
        await asyncio.sleep(30)


def current_version() -> str:
    try:
        return VERSION_PATH.read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def queue_leftovers():
    """Queue every recording still on disk (failed uploads, or ones interrupted by a restart)."""
    files = [
        f for f in sorted(RECORDINGS_DIR.glob("*.flv"))
        if (m := FILE_RE.match(f.name)) and m["key"] not in live_keys
    ]
    for flv in files:
        queue_recording(flv)
    return len(files)


# ---- nginx-rtmp callbacks ---------------------------------------------------

async def on_publish(request: web.Request):
    form = await request.post()
    key = form.get("name", "")
    dj = store.dj_by_key(key)
    if dj is None:
        log.info("Rejected stream with unknown key from %s", form.get("addr"))
        return web.Response(status=403)
    live_keys.add(key)
    refresh_busy()
    log.info("%s started streaming", dj["name"])
    return web.Response(text="ok")


async def on_publish_done(request: web.Request):
    form = await request.post()
    live_keys.discard(form.get("name", ""))
    refresh_busy()
    return web.Response(text="ok")


async def on_record_done(request: web.Request):
    form = await request.post()
    path = Path(form.get("path", ""))
    if path.parent == RECORDINGS_DIR:
        queue_recording(path)
    return web.Response(text="ok")


# ---- Discord commands ---------------------------------------------------------

class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.leftovers_queued = False

    async def setup_hook(self):
        app = web.Application()
        app.router.add_post("/rtmp/publish", on_publish)
        app.router.add_post("/rtmp/publish_done", on_publish_done)
        app.router.add_post("/rtmp/record_done", on_record_done)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", 8080).start()

        asyncio.create_task(housekeeping())

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)  # replaces every old command in the server with the current set
            # Remove commands an earlier version registered globally, which would show up as duplicates.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            await self.tree.sync()

    async def on_ready(self):
        # Leftovers need the Discord connection (to DM DJs), so start them once we're logged in.
        # on_ready fires again after reconnects; only do this the first time.
        if self.leftovers_queued:
            return
        self.leftovers_queued = True
        n = queue_leftovers()
        if n:
            log.info("Queued %d recording(s) left over from before", n)


bot = Bot()


def owner_only():
    return app_commands.check(lambda interaction: interaction.user.id in OWNER_IDS)


@bot.tree.error
async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        msg = "Only the bot owner can use this command."
    else:
        log.error("Command /%s failed", interaction.command.name if interaction.command else "?", exc_info=error)
        msg = "Something went wrong running that command."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)

PRIVACY_CHOICES = [
    app_commands.Choice(name="Public", value="public"),
    app_commands.Choice(name="Unlisted", value="unlisted"),
]


async def timezone_autocomplete(interaction: discord.Interaction, current: str):
    current = current.strip().lower().replace(" ", "_")
    if not current:
        matches = COMMON_TIMEZONES
    else:
        matches = [tz for tz in ALL_TIMEZONES if current in tz.lower()]
    return [app_commands.Choice(name=tz, value=tz) for tz in matches[:25]]


def clean_username(name: str) -> str:
    return name.strip().lstrip("@").strip()[:60]


def settings_message(dj) -> str:
    return (
        f"**Username:** @{dj['name']}\n"
        f"**Timezone:** {dj['timezone']}\n"
        f"**Videos:** {dj['privacy']}\n"
        f"**VRCDN Custom RTMP key:** ||`{RTMP_PUBLIC_URL.rstrip('/')}/{dj['stream_key']}`||\n"
        "Add it here: <https://panel.vrcdn.live/live.php>. In the **Stream Forwarding** section, select "
        "**Custom RTMP** as the provider and paste the key above into the **Key** box, then click **Add**. "
        "Don't share your key.\n\n"
        "When you finish a set, I'll DM you to pick a title, then send you the YouTube link. "
        "Make sure your DMs are open for this server."
    )


@bot.tree.command(description="Sign up to stream and get your personal stream key")
@app_commands.describe(
    username="The name your videos are posted under",
    timezone="Your timezone (start typing your city, e.g. New_York, London, Tokyo)",
    privacy="Should your videos be public or unlisted on YouTube?",
)
@app_commands.autocomplete(timezone=timezone_autocomplete)
@app_commands.choices(privacy=PRIVACY_CHOICES)
async def register(
    interaction: discord.Interaction, username: str, timezone: str, privacy: app_commands.Choice[str]
):
    if timezone not in ALL_TIMEZONES:
        await interaction.response.send_message(
            "I don't know that timezone. Start typing your city and pick one from the list.", ephemeral=True
        )
        return
    name = clean_username(username)
    if not name:
        await interaction.response.send_message("Please give a username.", ephemeral=True)
        return
    existing = store.dj_by_id(interaction.user.id)
    key = existing["stream_key"] if existing else new_key()
    store.save_dj(interaction.user.id, name, key, timezone, privacy.value)
    store.set_channel(interaction.user.id, interaction.channel_id)
    await interaction.response.send_message(
        ("You're all set!\n\n" if not existing else "Updated!\n\n") + settings_message(store.dj_by_id(interaction.user.id)),
        ephemeral=True,
    )
    # Check now that DMs work, so DJs find out before their first set.
    if await dm(interaction.user.id, "👋 You're signed up! This is where I'll ask you for titles and send your YouTube links.") is None:
        await interaction.followup.send(
            "⚠️ I couldn't DM you. I'll post your title questions and YouTube links in this channel instead. "
            "To get them privately, turn on **Direct Messages** in this server's Privacy Settings.",
            ephemeral=True,
        )


@bot.tree.command(description="See or change your username, timezone or video privacy")
@app_commands.describe(
    username="New username for your videos",
    timezone="New timezone",
    privacy="Public or unlisted",
)
@app_commands.autocomplete(timezone=timezone_autocomplete)
@app_commands.choices(privacy=PRIVACY_CHOICES)
async def settings(
    interaction: discord.Interaction,
    username: str | None = None,
    timezone: str | None = None,
    privacy: app_commands.Choice[str] | None = None,
):
    dj = store.dj_by_id(interaction.user.id)
    if dj is None:
        await interaction.response.send_message("You're not signed up yet. Use /register first.", ephemeral=True)
        return
    if timezone and timezone not in ALL_TIMEZONES:
        await interaction.response.send_message(
            "I don't know that timezone. Start typing your city and pick one from the list.", ephemeral=True
        )
        return
    store.save_dj(
        interaction.user.id,
        clean_username(username) if username and clean_username(username) else dj["name"],
        dj["stream_key"],
        timezone or dj["timezone"],
        privacy.value if privacy else dj["privacy"],
    )
    store.set_channel(interaction.user.id, interaction.channel_id)
    await interaction.response.send_message(settings_message(store.dj_by_id(interaction.user.id)), ephemeral=True)


@bot.tree.command(description="Get a new stream key (use this if your key leaked)")
async def resetkey(interaction: discord.Interaction):
    dj = store.dj_by_id(interaction.user.id)
    if dj is None:
        await interaction.response.send_message("You're not signed up yet. Use /register first.", ephemeral=True)
        return
    if dj["stream_key"] in live_keys:
        await interaction.response.send_message("You're live right now. Reset your key after you finish.", ephemeral=True)
        return
    store.save_dj(interaction.user.id, dj["name"], new_key(), dj["timezone"], dj["privacy"])
    store.set_channel(interaction.user.id, interaction.channel_id)
    await interaction.response.send_message(
        "New key made. The old one no longer works.\n\n" + settings_message(store.dj_by_id(interaction.user.id)),
        ephemeral=True,
    )


# ---- /youtube: admins connect the YouTube channel from Discord -------------------

LOGIN_STEPS = (
    "**1.** Open this link and sign in with the Google account that owns the YouTube channel: "
    "[Sign in with Google]({url})\n"
    "**2.** If Google says the app isn't verified, click **Advanced → Go to … (unsafe)**, then **Continue**/**Allow**.\n"
    "**3.** You'll end up on a page that **won't load** (localhost). That's expected. "
    "Copy the **whole address** from your browser's address bar.\n"
    "**4.** Click the button below and paste it."
)


class ClientModal(discord.ui.Modal, title="Google OAuth client"):
    client_id = discord.ui.TextInput(label="Client ID", placeholder="xxxx.apps.googleusercontent.com")
    client_secret = discord.ui.TextInput(label="Client secret", placeholder="GOCSPX-...")

    async def on_submit(self, interaction: discord.Interaction):
        youtube.save_client(CLIENT_PATH, self.client_id.value, self.client_secret.value)
        await send_login_link(interaction)


class CodeModal(discord.ui.Modal, title="Finish connecting YouTube"):
    pasted = discord.ui.TextInput(
        label="Address from your browser", style=discord.TextStyle.paragraph,
        placeholder="http://localhost/?state=...&code=...", max_length=2000,
    )

    def __init__(self, flow):
        super().__init__()
        self.flow = flow

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await asyncio.to_thread(youtube.finish_login, self.flow, self.pasted.value, TOKEN_PATH)
            name = await asyncio.to_thread(youtube.channel_name, TOKEN_PATH)
        except Exception as e:
            log.warning("YouTube login failed: %s", e)
            await interaction.followup.send(
                f"That didn't work: `{str(e)[:300]}`\nRun `/youtube connect` again and paste the address "
                "right after signing in (it expires after a few minutes).", ephemeral=True,
            )
            return
        n = queue_leftovers()
        await interaction.followup.send(
            f"✅ Connected to YouTube channel **{name}**. Sets will upload there."
            + (f"\nUploading {n} recording(s) that were waiting." if n else ""),
            ephemeral=True,
        )


class PasteView(discord.ui.View):
    def __init__(self, flow):
        super().__init__(timeout=900)
        self.flow = flow

    @discord.ui.button(label="Paste the address", style=discord.ButtonStyle.primary)
    async def paste(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CodeModal(self.flow))


async def send_login_link(interaction: discord.Interaction):
    try:
        flow, url = await asyncio.to_thread(youtube.start_login, CLIENT_PATH)
    except Exception as e:
        await interaction.response.send_message(
            f"The saved Google client looks wrong: `{str(e)[:300]}`\n"
            "Run `/youtube connect new_client:True` to enter it again.", ephemeral=True,
        )
        return
    await interaction.response.send_message(LOGIN_STEPS.format(url=url), view=PasteView(flow), ephemeral=True)


youtube_group = app_commands.Group(
    name="youtube", description="Admin: connect the YouTube channel sets upload to",
    default_permissions=discord.Permissions(manage_guild=True),
)


@owner_only()
@youtube_group.command(name="connect", description="Connect (or switch) the YouTube channel sets upload to")
@app_commands.describe(new_client="Enter a new Google Client ID/secret instead of the saved one")
async def youtube_connect(interaction: discord.Interaction, new_client: bool = False):
    if new_client or not CLIENT_PATH.exists():
        await interaction.response.send_modal(ClientModal())
    else:
        await send_login_link(interaction)


@owner_only()
@youtube_group.command(name="status", description="Show which YouTube channel is connected")
async def youtube_status(interaction: discord.Interaction):
    if not TOKEN_PATH.exists():
        await interaction.response.send_message("YouTube isn't connected. Use `/youtube connect`.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        name = await asyncio.to_thread(youtube.channel_name, TOKEN_PATH)
        msg = f"✅ Connected to **{name}**."
    except Exception as e:
        msg = f"⚠️ The YouTube connection isn't working (`{str(e)[:300]}`). Run `/youtube connect` again."
    await interaction.followup.send(msg, ephemeral=True)


bot.tree.add_command(youtube_group)


@bot.tree.command(description="Admin: show recent uploads and any that failed")
@app_commands.default_permissions(manage_guild=True)
@owner_only()
async def uploads(interaction: discord.Interaction):
    rows = store.uploads(["awaiting_title", "queued", "uploading", "failed", "done"])[:15]
    if not rows:
        await interaction.response.send_message("No uploads yet.", ephemeral=True)
        return
    lines = []
    for r in rows:
        extra = f"https://youtu.be/{r['video_id']}" if r["video_id"] else (r["error"] or "")
        lines.append(f"`{r['status']}` **@{r['dj_name']}** {r['title'] or ''} ({r['updated'][:16]}) {extra}")
    await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)


@bot.tree.command(description="Admin: update the bot to the latest version from GitHub")
@app_commands.default_permissions(manage_guild=True)
@owner_only()
async def update(interaction: discord.Interaction):
    UPDATE_REQUEST_PATH.write_text(str(interaction.user.id))
    busy = " Someone is live or uploading right now, so it will wait until they're done." if (
        live_keys or uploading) else ""
    await interaction.response.send_message(
        f"Checking GitHub for updates (current version: `{current_version()}`). "
        f"This takes up to a minute, plus a few minutes if there's something to install.{busy} "
        "I'll DM you the result.", ephemeral=True,
    )


@bot.tree.command(description="Admin: retry uploading recordings still on the server")
@app_commands.default_permissions(manage_guild=True)
@owner_only()
async def retryuploads(interaction: discord.Interaction):
    n = queue_leftovers()
    await interaction.response.send_message(f"Retrying {n} recording(s).", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN_PATH.exists():
        log.warning("YouTube isn't connected - recordings will be kept until an admin runs /youtube connect")
    bot.run(DISCORD_TOKEN, log_handler=None)
