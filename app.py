import asyncio
import hashlib
import logging
import os
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yt_dlp
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, HttpUrl

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("video-worker")

DB_PATH = Path(os.getenv("DB_PATH", "/data/jobs.sqlite3"))
WORK_DIR = Path(os.getenv("WORK_DIR", "/tmp/video-worker"))
API_KEY = os.environ["API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_BYTES", str(2 * 1024**3)))
MAX_CONCURRENT_JOBS = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "1")))
CRF = os.getenv("VIDEO_CRF", "28")
VIDEO_PRESET = os.getenv("VIDEO_PRESET", "medium")

app = FastAPI(title="Video compression worker")
job_queue: asyncio.Queue[str] = asyncio.Queue()
worker_tasks: list[asyncio.Task] = []


class JobRequest(BaseModel):
    url: HttpUrl


class JobResponse(BaseModel):
    id: str
    status: str


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db() -> None:
    with connect_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        db.execute(
            "UPDATE jobs SET status = 'queued', updated_at = ? WHERE status = 'processing'",
            (int(time.time()),),
        )


def update_job(job_id: str, status_value: str, error: str | None = None) -> None:
    with connect_db() as db:
        db.execute(
            "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (status_value, error, int(time.time()), job_id),
        )


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {API_KEY}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Only HTTP(S) URLs are accepted")


@app.on_event("startup")
async def startup() -> None:
    init_db()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    with connect_db() as db:
        rows = db.execute("SELECT id FROM jobs WHERE status = 'queued'").fetchall()
    for row in rows:
        await job_queue.put(row["id"])
    worker_tasks.extend(asyncio.create_task(worker()) for _ in range(MAX_CONCURRENT_JOBS))


@app.on_event("shutdown")
async def shutdown() -> None:
    for task in worker_tasks:
        task.cancel()
    await asyncio.gather(*worker_tasks, return_exceptions=True)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/jobs", response_model=JobResponse, status_code=202, dependencies=[Depends(require_api_key)])
async def create_job(request: JobRequest) -> JobResponse:
    url = str(request.url)
    validate_url(url)
    job_id = hashlib.sha256(f"{url}:{time.time_ns()}".encode()).hexdigest()[:20]
    now = int(time.time())
    with connect_db() as db:
        db.execute(
            "INSERT INTO jobs (id, url, status, created_at, updated_at) VALUES (?, ?, 'queued', ?, ?)",
            (job_id, url, now, now),
        )
    await job_queue.put(job_id)
    return JobResponse(id=job_id, status="queued")


@app.get("/jobs/{job_id}", response_model=JobResponse, dependencies=[Depends(require_api_key)])
async def get_job(job_id: str) -> JobResponse:
    with connect_db() as db:
        row = db.execute("SELECT id, status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse(id=row["id"], status=row["status"])


async def worker() -> None:
    while True:
        job_id = await job_queue.get()
        try:
            await asyncio.to_thread(process_job, job_id)
        except Exception:
            log.exception("Job %s failed", job_id)
        finally:
            job_queue.task_done()


def process_job(job_id: str) -> None:
    with connect_db() as db:
        row = db.execute("SELECT url FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return
    update_job(job_id, "processing")
    job_dir = Path(tempfile.mkdtemp(prefix=f"{job_id}-", dir=WORK_DIR))
    try:
        source = download_video(row["url"], job_dir)
        compressed = job_dir / "compressed.mp4"
        compress_video(source, compressed)
        send_to_telegram(compressed)
        update_job(job_id, "completed")
    except Exception as exc:
        update_job(job_id, "failed", str(exc)[:1000])
        raise
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def download_video(url: str, job_dir: Path) -> Path:
    output = str(job_dir / "source.%(ext)s")
    options = {
        "format": "bv*+ba/b",
        "outtmpl": output,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "restrictfilenames": True,
        "max_filesize": MAX_DOWNLOAD_BYTES,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        downloader.download([url])
    candidates = [path for path in job_dir.iterdir() if path.is_file() and path.name.startswith("source.")]
    if not candidates:
        raise RuntimeError("Downloader produced no video file")
    return max(candidates, key=lambda path: path.stat().st_size)


def compress_video(source: Path, destination: Path) -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx265",
        "-preset",
        VIDEO_PRESET,
        "-crf",
        CRF,
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        "-y",
        str(destination),
    ]
    subprocess.run(command, check=True, timeout=3600)
    if not destination.exists() or destination.stat().st_size == 0:
        raise RuntimeError("ffmpeg produced no output")


def send_to_telegram(video: Path) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendVideo"
    with video.open("rb") as handle:
        response = httpx.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "supports_streaming": "true"},
            files={"video": (video.name, handle, "video/mp4")},
            timeout=3600,
        )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram rejected upload: {payload}")
