import os
import re
import shutil
import subprocess
import threading
import time
from flask import Flask, render_template, request, jsonify, send_from_directory
import yt_dlp
from yt_dlp.utils import download_range_func

app = Flask(__name__)

# Folders in VS Code
NOT_UPLOADED_FOLDER = os.path.join(os.getcwd(), "not_uploaded")
UPLOADED_FOLDER = os.path.join(os.getcwd(), "uploaded")
TEMP_UPLOAD_FOLDER = os.path.join(os.getcwd(), "temp_uploads")
THUMBNAILS_FOLDER = os.path.join(os.getcwd(), "thumbnails")

for folder in [NOT_UPLOADED_FOLDER, UPLOADED_FOLDER, TEMP_UPLOAD_FOLDER, THUMBNAILS_FOLDER]:
    os.makedirs(folder, exist_ok=True)

download_status = {"state": "idle", "percent": 0, "message": "", "filename": ""}
edit_status = {"state": "idle", "percent": 0, "message": "", "filename": ""}


def time_to_seconds(t_str):
    parts = [float(p) for p in t_str.strip().split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    elif len(parts) == 2:
        return parts[0] * 60 + parts[1]
    elif len(parts) == 1:
        return parts[0]
    return 0.0


def get_video_duration(filepath):
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", filepath
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        return float(res.stdout.strip())
    except Exception:
        return 0.0


def build_atempo_filter(speed):
    filters = []
    remaining = speed
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    filters.append(f"atempo={remaining:.3f}")
    return ",".join(filters)


@app.route("/")
def index():
    return render_template("index.html")


# Stream video for built-in player
@app.route("/video/<path:filename>")
def serve_video(filename):
    return send_from_directory(NOT_UPLOADED_FOLDER, filename)


# Serve grabbed thumbnail images
@app.route("/thumbnail/<path:filename>")
def serve_thumbnail(filename):
    return send_from_directory(THUMBNAILS_FOLDER, filename)


@app.route("/get_formats", methods=["POST"])
def get_formats():
    url = request.json.get("url")
    if not url:
        return jsonify({"error": "Please provide a valid URL"}), 400
    try:
        ydl_opts = {"quiet": True, "no_warnings": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            resolutions = set()
            for f in info.get("formats", []):
                if f.get("vcodec") != "none" and f.get("height"):
                    resolutions.add(f["height"])
            return jsonify({
                "title": info.get("title", "Unknown Video"),
                "thumbnail": info.get("thumbnail", ""),
                "resolutions": sorted(list(resolutions), reverse=True)
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def run_download_thread(url, choice, start_time, end_time):
    global download_status
    download_status = {"state": "downloading", "percent": 0, "message": "Starting download...", "filename": ""}

    def progress_hook(d):
        if d["status"] == "downloading":
            p_str = re.sub(r"\x1b\[[0-9;]*m", "", d.get("_percent_str", "0%")).replace("%", "").strip()
            speed = re.sub(r"\x1b\[[0-9;]*m", "", d.get("_speed_str", ""))
            eta = re.sub(r"\x1b\[[0-9;]*m", "", d.get("_eta_str", ""))
            try:
                pct = float(p_str)
            except ValueError:
                pct = download_status["percent"]
            download_status["percent"] = round(pct, 1)
            download_status["message"] = f"Downloading... {pct:.1f}% | Speed: {speed} | ETA: {eta}"
        elif d["status"] == "finished":
            download_status["percent"] = 95
            download_status["message"] = "Merging audio & video..."

    try:
        clip_suffix = ""
        clip_opts = {}

        if start_time and end_time:
            start_sec = time_to_seconds(start_time)
            end_sec = time_to_seconds(end_time)
            if end_sec <= start_sec:
                download_status = {"state": "error", "percent": 0, "message": "❌ End time must be greater than start time!", "filename": ""}
                return
            clip_opts["download_ranges"] = download_range_func(None, [(start_sec, end_sec)])
            clip_suffix = f"_{int(start_sec)}s_to_{int(end_sec)}s"
            download_status["message"] = "Downloading clip... (live speed terminal mein chal rahi hai)"

        if choice == "mp3":
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": os.path.join(NOT_UPLOADED_FOLDER, f"%(title)s{clip_suffix}.%(ext)s"),
                "restrictfilenames": True,
                "progress_hooks": [progress_hook],
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
                }],
                **clip_opts,
            }
            ext = ".mp3"
        else:
            ydl_opts = {
                "format": f"bestvideo[height<={choice}]+bestaudio/best[height<={choice}]",
                "merge_output_format": "mp4",
                "outtmpl": os.path.join(NOT_UPLOADED_FOLDER, f"%(title)s_{choice}p{clip_suffix}.%(ext)s"),
                "restrictfilenames": True,
                "progress_hooks": [progress_hook],
                **clip_opts,
            }
            ext = ".mp4"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            base, _ = os.path.splitext(filename)
            final_name = os.path.basename(base + ext)

        download_status = {
            "state": "done",
            "percent": 100,
            "message": f"✅ Done! 'not_uploaded' folder check karo ({final_name})",
            "filename": final_name
        }
    except Exception as e:
        download_status = {"state": "error", "percent": 0, "message": f"❌ Error: {str(e)}", "filename": ""}


@app.route("/start_download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    choice = data.get("resolution", "").strip()
    start_time = data.get("start_time", "").strip()
    end_time = data.get("end_time", "").strip()

    if not url or not choice:
        return jsonify({"error": "Please select a quality first!"}), 400

    t = threading.Thread(target=run_download_thread, args=(url, choice, start_time, end_time))
    t.start()
    return jsonify({"status": "started"})


@app.route("/download_status")
def get_download_status():
    return jsonify(download_status)


@app.route("/list_videos")
def list_videos():
    not_up = [f for f in os.listdir(NOT_UPLOADED_FOLDER) if f.lower().endswith((".mp4", ".mkv", ".webm", ".mov", ".mp3"))]
    not_up.sort(key=lambda x: os.path.getmtime(os.path.join(NOT_UPLOADED_FOLDER, x)), reverse=True)

    up = [f for f in os.listdir(UPLOADED_FOLDER) if f.lower().endswith((".mp4", ".mkv", ".webm", ".mov", ".mp3"))]
    up.sort(key=lambda x: os.path.getmtime(os.path.join(UPLOADED_FOLDER, x)), reverse=True)

    return jsonify({"not_uploaded": not_up, "uploaded": up})


@app.route("/rename_video", methods=["POST"])
def rename_video():
    old_name = request.json.get("old_name", "").strip()
    new_title = request.json.get("new_title", "").strip()
    if not old_name or not new_title:
        return jsonify({"error": "Invalid filename"}), 400

    # Clean invalid Windows filename characters
    clean_title = re.sub(r'[<>:"/\\|?*]', "", new_title).strip()
    if not clean_title:
        return jsonify({"error": "Invalid characters in name"}), 400

    ext = os.path.splitext(old_name)[1]
    new_name = clean_title if clean_title.lower().endswith(ext.lower()) else clean_title + ext

    src = os.path.join(NOT_UPLOADED_FOLDER, old_name)
    dst = os.path.join(NOT_UPLOADED_FOLDER, new_name)
    if not os.path.exists(src):
        return jsonify({"error": "File not found"}), 404
    if os.path.exists(dst):
        return jsonify({"error": "A file with this name already exists"}), 400

    os.rename(src, dst)
    return jsonify({"status": "renamed", "new_name": new_name})


@app.route("/mark_uploaded", methods=["POST"])
def mark_uploaded():
    filename = request.json.get("filename", "").strip()
    src = os.path.join(NOT_UPLOADED_FOLDER, filename)
    dst = os.path.join(UPLOADED_FOLDER, filename)
    if not filename or not os.path.exists(src):
        return jsonify({"error": "File not found in not_uploaded folder!"}), 404
    shutil.move(src, dst)
    return jsonify({"status": "moved", "message": f"✅ '{filename}' moved to 'uploaded' folder!"})


@app.route("/open_not_uploaded_folder", methods=["POST"])
def open_not_uploaded_folder():
    try:
        os.startfile(NOT_UPLOADED_FOLDER)
        return jsonify({"status": "opened"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/open_thumbnails_folder", methods=["POST"])
def open_thumbnails_folder():
    try:
        os.startfile(THUMBNAILS_FOLDER)
        return jsonify({"status": "opened"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# 1-Click Frame / Screenshot Grabber for Thumbnails
@app.route("/grab_frame", methods=["POST"])
def grab_frame():
    filename = request.json.get("filename", "").strip()
    timestamp = request.json.get("timestamp", "00:00:01").strip()

    input_path = os.path.join(NOT_UPLOADED_FOLDER, filename)
    if not filename or not os.path.exists(input_path):
        return jsonify({"error": "Please select a valid video first!"}), 400

    base_name = os.path.splitext(filename)[0]
    safe_ts = timestamp.replace(":", "-")
    out_name = f"{base_name}_frame_{safe_ts}_{int(time.time())}.png"
    output_path = os.path.join(THUMBNAILS_FOLDER, out_name)

    cmd = [
        "ffmpeg", "-y", "-ss", timestamp, "-i", input_path,
        "-frames:v", "1", "-q:v", "2", output_path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not os.path.exists(output_path):
        return jsonify({"error": "Failed to capture frame. Check timestamp!"}), 500

    return jsonify({"status": "ok", "image_url": f"/thumbnail/{out_name}", "filename": out_name})


def run_ffmpeg_with_progress(cmd, expected_duration):
    global edit_status
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
    for line in process.stdout:
        if "out_time_us=" in line:
            try:
                us = int(line.split("out_time_us=")[1].strip())
                sec = us / 1_000_000.0
                if expected_duration > 0:
                    pct = min(99.0, (sec / expected_duration) * 100)
                    edit_status["percent"] = round(pct, 1)
                    edit_status["message"] = f"Processing video... {pct:.1f}%"
            except ValueError:
                pass
    process.wait()
    return process.returncode


def run_edit_thread(input_path, start_time, end_time, speed):
    global edit_status
    edit_status = {"state": "processing", "percent": 0, "message": "Preparing video editor...", "filename": ""}

    try:
        total_dur = get_video_duration(input_path)
        if total_dur <= 0:
            edit_status = {"state": "error", "percent": 0, "message": "❌ Could not read video duration.", "filename": ""}
            return

        base_name = os.path.splitext(os.path.basename(input_path))[0]
        out_name = f"{base_name}_speed_{speed}x_{int(time.time())}.mp4"
        output_path = os.path.join(NOT_UPLOADED_FOLDER, out_name)

        atempo_str = build_atempo_filter(speed)

        if start_time and end_time:
            s = time_to_seconds(start_time)
            e = min(time_to_seconds(end_time), total_dur)
            if e <= s:
                edit_status = {"state": "error", "percent": 0, "message": "❌ End time must be greater than Start time!", "filename": ""}
                return

            parts_v, parts_a, filter_lines = [], [], []
            idx = 0

            if s > 0:
                filter_lines.append(f"[0:v]trim=start=0:end={s},setpts=PTS-STARTPTS[v{idx}]")
                filter_lines.append(f"[0:a]atrim=start=0:end={s},asetpts=PTS-STARTPTS[a{idx}]")
                parts_v.append(f"[v{idx}]")
                parts_a.append(f"[a{idx}]")
                idx += 1

            filter_lines.append(f"[0:v]trim=start={s}:end={e},setpts=(PTS-STARTPTS)/{speed}[v{idx}]")
            filter_lines.append(f"[0:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS,{atempo_str}[a{idx}]")
            parts_v.append(f"[v{idx}]")
            parts_a.append(f"[a{idx}]")
            idx += 1

            if e < total_dur:
                filter_lines.append(f"[0:v]trim=start={e},setpts=PTS-STARTPTS[v{idx}]")
                filter_lines.append(f"[0:a]atrim=start={e},asetpts=PTS-STARTPTS[a{idx}]")
                parts_v.append(f"[v{idx}]")
                parts_a.append(f"[a{idx}]")
                idx += 1

            concat_inputs = "".join(v + a for v, a in zip(parts_v, parts_a))
            filter_lines.append(f"{concat_inputs}concat=n={idx}:v=1:a=1[vout][aout]")
            filter_complex = ";".join(filter_lines)
            expected_dur = s + ((e - s) / speed) + max(0, total_dur - e)
        else:
            filter_complex = f"[0:v]setpts=PTS/{speed}[vout];[0:a]{atempo_str}[aout]"
            expected_dur = total_dur / speed

        edit_status["message"] = "Encoding with Intel GPU (Fast Mode)..."
        cmd_gpu = [
            "ffmpeg", "-y", "-i", input_path,
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-progress", "pipe:1", "-nostats", output_path
        ]
        rc = run_ffmpeg_with_progress(cmd_gpu, expected_dur)

        if rc != 0:
            edit_status["percent"] = 0
            edit_status["message"] = "GPU unavailable, switching to Fast CPU Mode..."
            cmd_cpu = [
                "ffmpeg", "-y", "-i", input_path,
                "-filter_complex", filter_complex,
                "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-c:a", "aac", "-b:a", "192k",
                "-progress", "pipe:1", "-nostats", output_path
            ]
            rc = run_ffmpeg_with_progress(cmd_cpu, expected_dur)

        if rc == 0:
            edit_status = {
                "state": "done",
                "percent": 100,
                "message": f"✅ Speed Edit Done! 'not_uploaded' mein save ho gaya ({out_name})",
                "filename": out_name
            }
        else:
            edit_status = {"state": "error", "percent": 0, "message": "❌ FFmpeg failed to process the video.", "filename": ""}
    except Exception as e:
        edit_status = {"state": "error", "percent": 0, "message": f"❌ Error: {str(e)}", "filename": ""}


# 1-Click YouTube Shorts Converter (9:16 Vertical)
def run_shorts_thread(input_path, style):
    global edit_status
    edit_status = {"state": "processing", "percent": 0, "message": "Converting to 9:16 YouTube Shorts...", "filename": ""}

    try:
        total_dur = get_video_duration(input_path)
        base_name = os.path.splitext(os.path.basename(input_path))[0]
        out_name = f"{base_name}_SHORTS_{style}_{int(time.time())}.mp4"
        output_path = os.path.join(NOT_UPLOADED_FOLDER, out_name)

        if style == "blur":
            # Blurred vertical background + crisp centered landscape video
            vf = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bg];[0:v]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[vout]"
            map_args = ["-filter_complex", vf, "-map", "[vout]", "-map", "0:a?"]
        else:
            # Center crop 9:16
            vf = "crop=ih*9/16:ih,scale=1080:1920"
            map_args = ["-vf", vf]

        edit_status["message"] = "Encoding Shorts with Intel GPU..."
        cmd_gpu = [
            "ffmpeg", "-y", "-i", input_path, *map_args,
            "-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-progress", "pipe:1", "-nostats", output_path
        ]
        rc = run_ffmpeg_with_progress(cmd_gpu, total_dur)

        if rc != 0:
            edit_status["percent"] = 0
            edit_status["message"] = "Switching to Fast CPU Mode for Shorts..."
            cmd_cpu = [
                "ffmpeg", "-y", "-i", input_path, *map_args,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-c:a", "aac", "-b:a", "192k",
                "-progress", "pipe:1", "-nostats", output_path
            ]
            rc = run_ffmpeg_with_progress(cmd_cpu, total_dur)

        if rc == 0:
            edit_status = {
                "state": "done",
                "percent": 100,
                "message": f"✅ Shorts Ready! 'not_uploaded' mein save ho gaya ({out_name})",
                "filename": out_name
            }
        else:
            edit_status = {"state": "error", "percent": 0, "message": "❌ Shorts conversion failed.", "filename": ""}
    except Exception as e:
        edit_status = {"state": "error", "percent": 0, "message": f"❌ Error: {str(e)}", "filename": ""}


@app.route("/start_edit", methods=["POST"])
def start_edit():
    existing_file = request.form.get("existing_file", "").strip()
    start_time = request.form.get("start_time", "").strip()
    end_time = request.form.get("end_time", "").strip()
    speed = float(request.form.get("speed", "1.5"))

    if "video_file" in request.files and request.files["video_file"].filename != "":
        f = request.files["video_file"]
        input_path = os.path.join(TEMP_UPLOAD_FOLDER, f.filename)
        f.save(input_path)
    elif existing_file:
        input_path = os.path.join(NOT_UPLOADED_FOLDER, existing_file)
    else:
        return jsonify({"error": "Please upload a video or select one from not_uploaded!"}), 400

    if not os.path.exists(input_path):
        return jsonify({"error": "Selected video file not found!"}), 404

    t = threading.Thread(target=run_edit_thread, args=(input_path, start_time, end_time, speed))
    t.start()
    return jsonify({"status": "started"})


@app.route("/start_shorts", methods=["POST"])
def start_shorts():
    filename = request.json.get("filename", "").strip()
    style = request.json.get("style", "blur")
    input_path = os.path.join(NOT_UPLOADED_FOLDER, filename)
    if not filename or not os.path.exists(input_path):
        return jsonify({"error": "Please select a video from 'not_uploaded' first!"}), 400

    t = threading.Thread(target=run_shorts_thread, args=(input_path, style))
    t.start()
    return jsonify({"status": "started"})


@app.route("/edit_status")
def get_edit_status():
    return jsonify(edit_status)


if __name__ == "__main__":
    app.run(debug=True)