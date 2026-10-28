import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import yt_dlp


def download(url: str, directory: Path, max_bytes: int) -> Path:
    options = {
        "format": "bv*+ba/b",
        "outtmpl": str(directory / "source.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "restrictfilenames": True,
        "max_filesize": max_bytes,
        "quiet": False,
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        downloader.download([url])
    files = [p for p in directory.iterdir() if p.name.startswith("source.") and p.is_file()]
    if not files:
        raise RuntimeError("The downloader produced no video file")
    return max(files, key=lambda p: p.stat().st_size)


def compress(source: Path, destination: Path, crf: str, preset: str) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx265",
            "-preset", preset, "-crf", crf, "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", "-y", str(destination),
        ],
        check=True,
    )
    if not destination.exists() or destination.stat().st_size == 0:
        raise RuntimeError("ffmpeg produced no output")


def upload(video: Path, token: str, chat_id: str) -> None:
    with video.open("rb") as handle:
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendVideo",
            data={"chat_id": chat_id, "supports_streaming": "true"},
            files={"video": (video.name, handle, "video/mp4")},
            timeout=3600,
        )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram rejected the upload: {result}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download, compress, and send one video to Telegram")
    parser.add_argument("--url", default=os.getenv("VIDEO_URL"))
    parser.add_argument("--token", default=os.getenv("TELEGRAM_BOT_TOKEN"))
    parser.add_argument("--chat-id", default=os.getenv("TELEGRAM_CHAT_ID"))
    parser.add_argument("--crf", default=os.getenv("VIDEO_CRF", "28"))
    parser.add_argument("--preset", default=os.getenv("VIDEO_PRESET", "medium"))
    parser.add_argument("--max-bytes", type=int, default=int(os.getenv("MAX_DOWNLOAD_BYTES", str(2 * 1024**3))))
    args = parser.parse_args()
    if not all((args.url, args.token, args.chat_id)):
        parser.error("--url, --token, and --chat-id are required (or set their environment variables)")
    if shutil.which("ffmpeg") is None:
        parser.error("ffmpeg is required but was not found on PATH")

    with tempfile.TemporaryDirectory(prefix="video-to-telegram-") as temporary:
        directory = Path(temporary)
        source = download(args.url, directory, args.max_bytes)
        compressed = directory / "compressed.mp4"
        print(f"Downloaded {source.stat().st_size / 1024**2:.1f} MiB")
        compress(source, compressed, args.crf, args.preset)
        print(f"Compressed to {compressed.stat().st_size / 1024**2:.1f} MiB")
        upload(compressed, args.token, args.chat_id)
        print("Uploaded successfully; temporary files removed.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("Interrupted")
