# Video compression worker

This service accepts authorized video URLs, downloads one video, transcodes it with
real video compression (`libx265`), uploads the result to a private Telegram
channel, and removes temporary files.

## Free fallback options

The same standalone script can run without the VPS:

### GitHub Actions

1. Add repository secrets named `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
2. Open **Actions → Compress video and send to Telegram → Run workflow**.
3. Paste one or more video URLs in the **URLs** field, one per line, and optionally adjust CRF.

The workflow processes URLs sequentially, continues after an individual failure,
and reports failures in the job log. It uses an ephemeral GitHub-hosted runner
and deletes its files when the job ends; runner time, storage, and Telegram
upload limits still apply.

### Google Colab

Paste this into one Colab cell and run it:

```python
!apt-get -qq update && apt-get -qq install -y ffmpeg
!pip -q install httpx yt-dlp

import getpass, os, subprocess, sys, urllib.request

os.environ["VIDEO_URLS"] = """
https://example.com/first-video
https://example.com/second-video
""".strip()
os.environ["TELEGRAM_BOT_TOKEN"] = getpass.getpass("Telegram bot token: ")
os.environ["TELEGRAM_CHAT_ID"] = input("Telegram channel ID: ").strip()
urllib.request.urlretrieve(
    "https://raw.githubusercontent.com/obinna-okpolu/earthlydelights/main/video_to_telegram.py",
    "video_to_telegram.py",
)
subprocess.run([sys.executable, "-c", """
import sys
sys.argv = ["video_to_telegram.py"]
import runpy
runpy.run_path("video_to_telegram.py", run_name="__main__")
"""], check=True)
```

Do not save a notebook containing the bot token. If the repository is private,
upload `video_to_telegram.py` with Colab's folder icon instead of using the raw
GitHub URL.

## Deploy

On a VPS with Docker:

```sh
cp .env.example .env
openssl rand -hex 32
# Put the generated value in API_KEY and fill in the Telegram values.
docker compose up -d --build
```

Create a Telegram bot with BotFather, add it as an administrator to the private
channel, and set `TELEGRAM_CHAT_ID` to the channel ID (typically a negative
`-100...` value). Keep `.env` private.

Submit a URL:

```sh
curl -X POST http://127.0.0.1:8000/jobs \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/video"}'
```

Check a job with `GET /jobs/{id}` using the same authorization header. The
`/healthz` endpoint does not require authentication.

The service is intentionally bound to localhost by Compose. Put it behind an
authenticated VPN or reverse proxy if it must be reachable remotely; do not
expose the unauthenticated Docker port directly. Set `VIDEO_CRF` higher for
smaller files/lower quality, or lower it for better quality. `MAX_DOWNLOAD_BYTES`
and the tmpfs size provide resource limits. Only process content you are
authorized to download, and do not use this to bypass DRM or access controls.