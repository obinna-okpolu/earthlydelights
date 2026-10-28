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


def main() -> int:
    parser = argparse.ArgumentParser(description="Download, compress, and send videos to Telegram")
    parser.add_argument("--url", action="append", help="Video URL; repeat for multiple URLs")
    parser.add_argument("--urls-file", help="Text file containing one video URL per line")
    parser.add_argument("--token", default=os.getenv("TELEGRAM_BOT_TOKEN"))
    parser.add_argument("--chat-id", default=os.getenv("TELEGRAM_CHAT_ID"))
    parser.add_argument("--crf", default=os.getenv("VIDEO_CRF", "28"))
    parser.add_argument("--preset", default=os.getenv("VIDEO_PRESET", "medium"))
    parser.add_argument("--max-bytes", type=int, default=int(os.getenv("MAX_DOWNLOAD_BYTES", str(2 * 1024**3))))
    args = parser.parse_args()
    urls = list(args.url or [])
    if args.urls_file:
        urls.extend(Path(args.urls_file).read_text().splitlines())
    urls.extend(os.getenv("VIDEO_URLS", "").splitlines())
    if not urls and os.getenv("VIDEO_URL"):
        urls.append(os.environ["VIDEO_URL"])
    urls = [url.strip() for url in urls if url.strip() and not url.strip().startswith("#")]
    if not urls or not all((args.token, args.chat_id)):
        parser.error("at least one URL, --token, and --chat-id are required")
    if shutil.which("ffmpeg") is None:
        parser.error("ffmpeg is required but was not found on PATH")

    failures = 0
    for index, url in enumerate(urls, 1):
        try:
            with tempfile.TemporaryDirectory(prefix="video-to-telegram-") as temporary:
                directory = Path(temporary)
                source = download(url, directory, args.max_bytes)
                compressed = directory / "compressed.mp4"
                print(f"[{index}/{len(urls)}] Downloaded {source.stat().st_size / 1024**2:.1f} MiB")
                compress(source, compressed, args.crf, args.preset)
                print(f"[{index}/{len(urls)}] Compressed to {compressed.stat().st_size / 1024**2:.1f} MiB")
                upload(compressed, args.token, args.chat_id)
                print(f"[{index}/{len(urls)}] Uploaded successfully; temporary files removed.")
        except Exception as error:
            failures += 1
            print(f"[{index}/{len(urls)}] FAILED: {url}: {error}", file=sys.stderr)
    if failures:
        print(f"Completed with {failures} failure(s) out of {len(urls)}.", file=sys.stderr)
        return 1
    print(f"Completed {len(urls)} upload(s).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("Interrupted")
