#!/usr/bin/env python3
"""Video -> MP3: dán link video hoặc upload file, server tải về, chuyển thành MP3, lưu trên web để tải xuống."""
import os
import re
import time
import uuid
import shutil
import threading
import subprocess
from pathlib import Path

import yt_dlp
from flask import Flask, request, jsonify, send_from_directory, abort

BASE = Path(__file__).resolve().parent
DOWNLOADS = BASE / "downloads"
TMP = BASE / "tmp"
COOKIES_FILE = BASE / "cookies.txt"
DOWNLOADS.mkdir(exist_ok=True)
TMP.mkdir(exist_ok=True)

MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500 MB

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

jobs = {}
jobs_lock = threading.Lock()

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".wma", ".opus"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv", ".m4v", ".3gp", ".ts", ".mpg", ".mpeg"}


def new_job(kind):
    jid = uuid.uuid4().hex[:12]
    job = {
        "id": jid, "kind": kind, "status": "queued", "progress": 0,
        "message": "Đang chờ xử lý...", "title": "", "filename": None,
        "error": None, "created": time.time(),
    }
    with jobs_lock:
        jobs[jid] = job
    return job


def set_job(jid, **kw):
    with jobs_lock:
        if jid in jobs:
            jobs[jid].update(kw)


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", (name or "").strip())
    name = re.sub(r"_+", "_", name).strip("._ ")
    return name[:120] or "audio"


def unique_mp3_name(title):
    base = safe_name(title)
    candidate = f"{base}.mp3"
    i = 1
    while (DOWNLOADS / candidate).exists():
        i += 1
        candidate = f"{base} ({i}).mp3"
    return candidate


def get_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30)
        return max(float(r.stdout.strip()), 0.1)
    except Exception:
        return 0


def ffmpeg_to_mp3(src, dst, jid, base_progress, span):
    """Chuyển src -> mp3, cập nhật progress của job trong khoảng [base, base+span]."""
    duration = get_duration(src)
    cmd = ["ffmpeg", "-y", "-i", str(src), "-vn",
           "-acodec", "libmp3lame", "-b:a", "192k",
           "-progress", "pipe:1", "-nostats", str(dst)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, bufsize=1)
    try:
        for line in p.stdout:
            line = line.strip()
            if line.startswith("out_time_ms=") and duration:
                try:
                    ms = int(line.split("=", 1)[1])
                    frac = min(max(ms / 1_000_000 / duration, 0), 1)
                    set_job(jid, progress=round(base_progress + frac * span, 1))
                except ValueError:
                    pass
            elif line == "progress=end":
                break
        p.wait(timeout=600)
    finally:
        if p.poll() is None:
            p.kill()
    if p.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError("ffmpeg chuyển đổi thất bại")


def process_url(jid, url):
    workdir = TMP / jid
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        set_job(jid, status="downloading", message="Đang tải video...", progress=2)

        def hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                frac = (done / total) if total else 0
                set_job(jid, progress=round(2 + min(frac, 1) * 68, 1),
                        message="Đang tải video...")
            elif d.get("status") == "finished":
                set_job(jid, progress=72, message="Tải xong, chuẩn bị chuyển đổi...")

        ydl_opts = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": str(workdir / "%(title).80s [%(id)s].%(ext)s"),
            "progress_hooks": [hook],
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 30, "retries": 3,
            # Vượt qua kiểm tra bot mới của YouTube (2025)
            "extractor_args": {"youtube": {"player_client": ["web", "android"]}},
        }
        # Dùng cookies YouTube nếu có để vượt qua kiểm tra "not a bot"
        if COOKIES_FILE.exists():
            ydl_opts["cookiefile"] = str(COOKIES_FILE)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        title = (info or {}).get("title") or "audio"

        files = [f for f in workdir.iterdir() if f.is_file()]
        if not files:
            raise RuntimeError("Không tải được file nào từ link này")
        src = max(files, key=lambda f: f.stat().st_size)

        set_job(jid, status="converting", title=safe_name(title),
                message="Đang chuyển sang MP3...", progress=75)
        mp3_name = unique_mp3_name(title)
        ffmpeg_to_mp3(src, DOWNLOADS / mp3_name, jid, 75, 24)

        set_job(jid, status="done", progress=100, filename=mp3_name,
                message="Hoàn tất!")
    except Exception as e:
        msg = str(e)
        if "Unsupported URL" in msg or "not a valid URL" in msg.lower():
            msg = "Link không được hỗ trợ. Hãy thử link video trực tiếp."
        set_job(jid, status="error", error=msg[:300], message="Có lỗi xảy ra")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def process_upload(jid, src_path, orig_name):
    try:
        stem = Path(orig_name).stem
        set_job(jid, status="converting", title=safe_name(stem),
                message="Đang chuyển sang MP3...", progress=10)
        mp3_name = unique_mp3_name(stem)
        ffmpeg_to_mp3(src_path, DOWNLOADS / mp3_name, jid, 10, 89)
        set_job(jid, status="done", progress=100, filename=mp3_name,
                message="Hoàn tất!")
    except Exception as e:
        set_job(jid, status="error", error=str(e)[:300], message="Có lỗi xảy ra")
    finally:
        try:
            os.remove(src_path)
        except OSError:
            pass


def cleanup_old_jobs():
    while True:
        time.sleep(600)
        cutoff = time.time() - 3600
        with jobs_lock:
            for jid in [k for k, v in jobs.items() if v["created"] < cutoff]:
                del jobs[jid]


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/url", methods=["POST"])
def api_url():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return jsonify({"ok": False, "error": "Link phải bắt đầu bằng http:// hoặc https://"}), 400
    job = new_job("url")
    t = threading.Thread(target=process_url, args=(job["id"], url), daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job["id"]})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "Chưa chọn file"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in VIDEO_EXTS and ext not in AUDIO_EXTS:
        return jsonify({"ok": False, "error": f"Định dạng {ext or '?'} chưa được hỗ trợ"}), 400
    job = new_job("upload")
    tmp_path = TMP / f"{job['id']}{ext}"
    f.save(tmp_path)
    t = threading.Thread(target=process_upload,
                         args=(job["id"], str(tmp_path), f.filename), daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job["id"]})


@app.route("/api/cookies", methods=["POST"])
def api_cookies():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "Chưa chọn file"}), 400
    content = f.read().decode("utf-8", errors="ignore")
    if "youtube.com" not in content.lower() and "#HttpOnly" not in content:
        return jsonify({"ok": False, "error": "File không giống cookies YouTube"}), 400
    COOKIES_FILE.write_text(content, encoding="utf-8")
    return jsonify({"ok": True, "message": "Đã lưu cookies YouTube!"})


@app.route("/api/cookies", methods=["DELETE"])
def api_cookies_delete():
    if COOKIES_FILE.exists():
        COOKIES_FILE.unlink()
    return jsonify({"ok": True})


@app.route("/api/cookies/status")
def api_cookies_status():
    return jsonify({"ok": True, "has_cookies": COOKIES_FILE.exists()})


@app.route("/api/jobs/<jid>")
def api_job(jid):
    with jobs_lock:
        job = jobs.get(jid)
    if not job:
        return jsonify({"ok": False, "error": "Không tìm thấy job"}), 404
    return jsonify({"ok": True, "job": job})


@app.route("/api/files")
def api_files():
    items = []
    for p in sorted(DOWNLOADS.glob("*.mp3"), key=lambda x: x.stat().st_mtime, reverse=True):
        st = p.stat()
        items.append({
            "name": p.name, "size": st.st_size,
            "size_mb": round(st.st_size / 1024 / 1024, 2),
            "mtime": st.st_mtime,
        })
    return jsonify({"ok": True, "files": items})


@app.route("/api/download/<path:name>")
def api_download(name):
    if ".." in name or "/" in name:
        abort(400)
    p = DOWNLOADS / name
    if not p.is_file() or p.suffix.lower() != ".mp3":
        abort(404)
    return send_from_directory(DOWNLOADS, name, as_attachment=True)


@app.route("/api/files/<path:name>", methods=["DELETE"])
def api_delete(name):
    if ".." in name or "/" in name:
        abort(400)
    p = DOWNLOADS / name
    if p.is_file() and p.suffix.lower() == ".mp3":
        p.unlink()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Không tìm thấy file"}), 404


if __name__ == "__main__":
    threading.Thread(target=cleanup_old_jobs, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
