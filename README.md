# DJ Stream Recorder

A Discord bot plus an RTMP server for recording DJ sets (e.g. forwarded from VRCDN) and posting them to YouTube.
The bot runs everything:

1. A DJ runs **`/register`** and gives a username, their timezone, and whether their videos should be
   **public** or **unlisted**. The bot privately replies with their personal RTMP key and where to paste it in VRCDN.
2. The DJ streams. Several DJs can stream at the same time; each set is recorded with video and audio.
3. When a set ends, the bot **DMs the DJ** asking whether they want a custom title. If they don't answer within
   30 minutes, it uses the default: **`@username on mm/dd/yyyy VRCHAT`**, dated in the DJ's own timezone.
4. The bot uploads the set to YouTube, **DMs the DJ the link**, and deletes the recording from the server.

If a DJ's DMs are closed, the bot posts the title question and the link in the channel where they last used a bot command, mentioning them; only that DJ can press the buttons. `/register` sends a test DM and warns them if it can't reach them.

## Discord commands

| Command | Who | What it does |
|---|---|---|
| `/register username timezone privacy` | anyone | Signs you up and gives you your RTMP key. Run it again to change your answers. |
| `/settings [username] [timezone] [privacy]` | DJs | Shows your settings and key, or changes any of them. |
| `/resetkey` | DJs | Makes a new key. The old one stops working. |
| `/youtube connect` | admins | Links the YouTube channel that sets upload to (see below). |
| `/youtube status` | admins | Shows which YouTube channel is connected. |
| `/update` | admins | Updates the bot from GitHub right away and DMs you the result. |
| `/status` | admins | Shows who's live (and how much is recorded), pending titles, upload progress, the daily upload count, server storage and the last uploads. |
| `/vodchannel [channel]` | admins | Posts every uploaded set in that channel. Run it with no channel to turn it off. |
| `/uploads` | admins | Shows recent uploads and any that failed. |
| `/retryuploads` | admins | Retries recordings still on the server (for example, when YouTube quota ran out). |

"Admins" means the bot owner: only the Discord user IDs in `OWNER_IDS` (default `464865517354614805`) can use these, and Discord only shows them to people with **Manage Server**. All command replies are private: only the person who ran the command sees them.

## Install

You need a Linux server (Ubuntu) with port **1935/TCP** reachable, and a Discord bot:

1. https://discord.com/developers/applications → **New Application** → **Bot** → **Reset Token** → copy the token.
   Turn **Public Bot** off.
2. **OAuth2 → URL Generator**: tick `bot` and `applications.commands`, open the URL and add the bot to your server.
3. In Discord, turn on **Developer Mode** (User Settings → Advanced), then right-click your server → **Copy Server ID**.

Then run this on the server. It asks for the token and server ID:
```
bash <(curl -fsSL https://raw.githubusercontent.com/Zam6969/rtmpbot/main/install.sh)
```

## Connecting YouTube (`/youtube connect`)

First create the Google credentials (one time only):
1. https://console.cloud.google.com → create a project → **APIs & Services → Library** → enable **YouTube Data API v3**.
2. **OAuth consent screen**: choose External, fill in the name and email, and add the channel owner's Google
   account as a **test user**. Then click **Publish app**. If you skip this, the login stops working after 7 days.
3. **Credentials → Create credentials → OAuth client ID → Desktop app**. Copy the **Client ID** and **Client secret**.

Then in Discord, run **`/youtube connect`**:
1. Paste the Client ID and secret.
2. Open the sign-in link and sign in with the Google account that owns the channel.
3. Copy the address of the page that doesn't load (`http://localhost/...`) and paste it back into the bot.

Run `/youtube connect` again at any time to switch channels. Use `/youtube connect new_client:True` to enter different Google credentials.

## Updates

The server checks this GitHub repository every 15 minutes and installs new versions by itself.
`/update` does it right away. Updates wait until nobody is live and no upload is running.
The update log is in `config/update.log`.

## Good to know
* **YouTube limits:** the free API quota allows about **6 uploads a day**, resetting at midnight Pacific time.
  Once the day's uploads are used up (or YouTube reports the quota exhausted), further sets wait in a first-come,
  first-served queue. Each DJ is told their place in line and when it will upload, and the queue uploads by itself
  after the reset. `/status` shows the queue. Change the limit with `DAILY_UPLOAD_LIMIT` in `.env`
  (for example after Google grants more quota).
* **New Google projects can only upload private videos** until Google approves them. Request this with the
  "YouTube API Services audit" form (free). Until then every set uploads as private, whatever the DJ picked.
* The default title format is set in `default_title()` in `bot/bot.py`, and the wait time in `TITLE_WAIT_MINUTES` in `.env`.
* Recordings shorter than `MIN_RECORDING_SECONDS` (default 2 minutes) count as test or accidental streams. They are deleted without uploading.
* If a DJ's connection drops and they reconnect, each part is uploaded as a separate video.
* Recordings are kept on the server only until their upload succeeds.
* Each hour of a 1080p set takes about 2–3 GB of disk while it waits to upload.
* Useful server commands, run in `~/rtmp-recorder`:
  * `sudo docker compose logs -f bot`: watch the bot's log.
  * `sudo docker compose down`: stop everything.
  * `sudo docker compose up -d`: start everything again.
