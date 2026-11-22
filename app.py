import asyncio
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import edge_tts
import httpx
import imageio_ffmpeg
import yt_dlp
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="AI Movie Recap Mobile Studio Pro")

WORK_DIR = Path("/tmp/recap_mobile_studio")
WORK_DIR.mkdir(parents=True, exist_ok=True)
FONT_DIR = WORK_DIR / "fonts"
FONT_DIR.mkdir(parents=True, exist_ok=True)
MYANMAR_FONT_PATH = FONT_DIR / "NotoSansMyanmar-Bold.ttf"

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

ASPECT_DIMENSIONS = {
    "9:16": (720, 1280),
    "16:9": (1280, 720),
    "4:5": (1080, 1350),
    "1:1": (1080, 1080),
}


def cleanup_old_files(max_age_seconds: int = 3600):
    now = time.time()
    for f in WORK_DIR.iterdir():
        try:
            if f.is_file() and (now - f.stat().st_mtime) > max_age_seconds:
                f.unlink()
        except Exception:
            pass


async def ensure_myanmar_font() -> bool:
    if MYANMAR_FONT_PATH.exists() and MYANMAR_FONT_PATH.stat().st_size > 10000:
        return True
    font_urls = [
        "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansMyanmar/NotoSansMyanmar-Bold.ttf",
        "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansmyanmar/NotoSansMyanmar%5Bwdth%2Cwght%5D.ttf",
    ]
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for url in font_urls:
            try:
                resp = await client.get(url)
                if resp.status_code == 200 and len(resp.content) > 10000:
                    MYANMAR_FONT_PATH.write_bytes(resp.content)
                    return True
            except Exception:
                continue
    return False


def supports_subtitles_filter() -> bool:
    res = subprocess.run([FFMPEG_EXE, "-filters"], capture_output=True, text=True)
    return " subtitles " in res.stdout


def get_media_info(file_path: str) -> tuple[bool, float]:
    res = subprocess.run(
        [FFMPEG_EXE, "-i", file_path],
        capture_output=True,
        text=True,
    )
    stderr = res.stderr
    has_audio = "Audio:" in stderr
    duration = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if m:
        h, mins, secs = float(m.group(1)), float(m.group(2)), float(m.group(3))
        duration = h * 3600 + mins * 60 + secs
    return has_audio, duration


def escape_ffmpeg_path(p: Path) -> str:
    s = str(p).replace("\\", "/")
    s = s.replace(":", "\\:")
    s = s.replace("'", "\\'")
    return s


def clean_subtitle_raw(raw_text: str) -> str:
    lines = raw_text.splitlines()
    cleaned = []
    seen = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("Kind:") or line.startswith("Language:"):
            continue
        if re.match(r"^\d+$", line):
            continue
        if "-->" in line:
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line and line not in seen:
            seen.add(line)
            cleaned.append(line)
    return "\n".join(cleaned)


class UrlRequest(BaseModel):
    url: str
    cookies_txt: str = ""


class ScriptRequest(BaseModel):
    api_key: str
    source_text: str
    tone: str = "စိတ်လှုပ်ရှားဖွယ် (Exciting & Engaging)"


class TTSRequest(BaseModel):
    text: str
    voice: str = "my-MM-NilarNeural"
    rate: str = "+5%"
    pitch: str = "+0Hz"


@app.post("/api/yt-subs")
async def extract_subtitles_from_url(req: UrlRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="YouTube သို့မဟုတ် Video Link ထည့်ပေးပါ။")

    cleanup_old_files()
    job_id = uuid.uuid4().hex[:10]
    sub_prefix = WORK_DIR / f"yt_sub_{job_id}"
    cookie_file = None

    if req.cookies_txt.strip():
        cookie_file = WORK_DIR / f"cookies_{job_id}.txt"
        cookie_file.write_text(req.cookies_txt.strip(), encoding="utf-8")

    def _fetch_subs():
        ydl_opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en", "en-US", "zh-Hans", "zh", "th", "ja", "ko", "my"],
            "subtitlesformat": "srt/vtt/best",
            "outtmpl": str(sub_prefix),
            "quiet": True,
            "no_warnings": True,
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        }
        if cookie_file:
            ydl_opts["cookiefile"] = str(cookie_file)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "Video")
        return title

    try:
        title = await asyncio.to_thread(_fetch_subs)
    except Exception as e:
        if cookie_file and cookie_file.exists():
            cookie_file.unlink()
        raise HTTPException(status_code=400, detail=f"စာတန်းဆွဲယူ၍ မရပါ: {str(e)[:250]}")

    if cookie_file and cookie_file.exists():
        cookie_file.unlink()

    sub_files = list(WORK_DIR.glob(f"yt_sub_{job_id}*"))
    if not sub_files:
        raise HTTPException(status_code=404, detail="ဒီ Video မှာ Auto Subtitle / CC မပါဝင်ပါ။")

    raw_content = sub_files[0].read_text(encoding="utf-8", errors="ignore")
    for sf in sub_files:
        sf.unlink(missing_ok=True)

    clean_text = clean_subtitle_raw(raw_content)
    return {"title": title, "text": clean_text}


@app.post("/api/download-video")
async def download_video_from_url(req: UrlRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="YouTube, TikTok သို့မဟုတ် Facebook Link ထည့်ပေးပါ။")

    cleanup_old_files()
    job_id = uuid.uuid4().hex[:10]
    out_template = str(WORK_DIR / f"dl_{job_id}.%(ext)s")
    cookie_file = None

    if req.cookies_txt.strip():
        cookie_file = WORK_DIR / f"cookies_{job_id}.txt"
        cookie_file.write_text(req.cookies_txt.strip(), encoding="utf-8")

    def _download():
        ydl_opts = {
            "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[ext=mp4]/best",
            "outtmpl": out_template,
            "merge_output_format": "mp4",
            "ffmpeg_location": FFMPEG_EXE,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            },
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        }
        if cookie_file:
            ydl_opts["cookiefile"] = str(cookie_file)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "Downloaded Video")
        return title

    try:
        title = await asyncio.to_thread(_download)
    except Exception as e:
        if cookie_file and cookie_file.exists():
            cookie_file.unlink()
        raise HTTPException(
            status_code=400,
            detail=f"Video ဒေါင်းလုဒ်မရပါ (Link မှန်မမှန် စစ်ပါ သို့မဟုတ် Private ဖြစ်နေနိုင်ပါသည်): {str(e)[:250]}",
        )

    if cookie_file and cookie_file.exists():
        cookie_file.unlink()

    downloaded_files = list(WORK_DIR.glob(f"dl_{job_id}.*"))
    if not downloaded_files:
        raise HTTPException(status_code=500, detail="Video ဖိုင် သိမ်းဆည်း၍ မရပါ။")

    final_file = downloaded_files[0]
    return {
        "video_file": final_file.name,
        "video_url": f"/files/{final_file.name}",
        "title": title,
    }


@app.post("/api/script")
async def generate_script(req: ScriptRequest):
    if not req.api_key.strip():
        raise HTTPException(status_code=400, detail="Gemini API Key ထည့်ပေးပါ။")
    if not req.source_text.strip():
        raise HTTPException(status_code=400, detail="ဘာသာပြန်မည့် စာသား သို့မဟုတ် SRT ထည့်ပေးပါ။")

    prompt = f"""You are a professional Burmese Movie Recap scriptwriter for TikTok, Facebook, and YouTube.
Translate and rewrite the following movie plot/subtitles into an engaging, natural spoken Burmese narration script.

Rules:
1. Tone: {req.tone}
2. Use natural spoken Burmese (စကားပြောဟန်) that sounds smooth when read by Burmese Text-to-Speech.
3. Break sentences into short, punchy lines so subtitles and pacing flow well.
4. Do NOT include scene numbers, timestamps, markdown bold symbols (**), or stage directions—output ONLY the pure Burmese narration text ready for voiceover.

Source text:
{req.source_text[:25000]}
"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={req.api_key.strip()}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Gemini API Error: {resp.text[:300]}")
        data = resp.json()
        try:
            script_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError):
            raise HTTPException(status_code=500, detail="AI ထံမှ စာသားပြန်မရပါ။")

    return {"script": script_text}


@app.post("/api/tts")
async def generate_tts(req: TTSRequest):
    cleanup_old_files()
    clean_text = req.text.strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="အသံထုတ်ရန် မြန်မာစာသား ထည့်ပေးပါ။")

    file_id = uuid.uuid4().hex[:10]
    mp3_name = f"tts_{file_id}.mp3"
    srt_name = f"sub_{file_id}.srt"
    mp3_path = WORK_DIR / mp3_name
    srt_path = WORK_DIR / srt_name

    communicate = edge_tts.Communicate(
        clean_text,
        req.voice,
        rate=req.rate,
        pitch=req.pitch,
    )
    submaker = edge_tts.SubMaker()

    with open(mp3_path, "wb") as audio_f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_f.write(chunk["data"])
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                if hasattr(submaker, "feed"):
                    submaker.feed(chunk)
                else:
                    submaker.create_sub((chunk["offset"], chunk["duration"]), chunk["text"])

    srt_content = submaker.get_srt() if hasattr(submaker, "get_srt") else submaker.generate_subs()
    with open(srt_path, "w", encoding="utf-8") as srt_f:
        srt_f.write(srt_content)

    return {
        "mp3_file": mp3_name,
        "srt_file": srt_name,
        "mp3_url": f"/files/{mp3_name}",
        "srt_url": f"/files/{srt_name}",
        "srt_text": srt_content,
    }


@app.post("/api/render")
async def render_video(
    video: UploadFile | None = File(None),
    server_video_file: str = Form(""),
    tts_file: str = Form(""),
    srt_file: str = Form(""),
    bgm: UploadFile | None = File(None),
    aspect: str = Form("9:16"),
    speed: float = Form(1.15),
    blur: int = Form(20),
    zoom: float = Form(1.05),
    mirror: bool = Form(False),
    color_boost: bool = Form(True),
    burn_subs: bool = Form(False),
    orig_vol: float = Form(0.0),
    bgm_vol: float = Form(0.15),
    match_audio: bool = Form(True),
):
    cleanup_old_files()
    job_id = uuid.uuid4().hex[:10]
    out_video = WORK_DIR / f"recap_{job_id}.mp4"
    temp_files_to_remove = []

    if video is not None and video.filename:
        ext = Path(video.filename).suffix or ".mp4"
        in_video = WORK_DIR / f"in_{job_id}{ext}"
        in_video.write_bytes(await video.read())
        temp_files_to_remove.append(in_video)
    elif server_video_file:
        candidate = WORK_DIR / os.path.basename(server_video_file)
        if not candidate.exists():
            raise HTTPException(status_code=400, detail="ဒေါင်းလုဒ်ဆွဲထားသော Video ဖိုင် မတွေ့ပါ။ ပြန်ဒေါင်းပေးပါ။")
        in_video = candidate
    else:
        raise HTTPException(status_code=400, detail="Video Link ထည့်ဒေါင်းပါ သို့မဟုတ် Gallery မှ Video ရွေးပါ။")

    bgm_path = None
    if bgm is not None and bgm.filename:
        bgm_ext = Path(bgm.filename).suffix or ".mp3"
        bgm_path = WORK_DIR / f"bgm_{job_id}{bgm_ext}"
        bgm_path.write_bytes(await bgm.read())
        temp_files_to_remove.append(bgm_path)

    tts_path = (WORK_DIR / os.path.basename(tts_file)) if tts_file else None
    has_tts = tts_path is not None and tts_path.exists()

    srt_path = (WORK_DIR / os.path.basename(srt_file)) if srt_file else None
    has_srt = srt_path is not None and srt_path.exists()

    video_has_audio, raw_vid_duration = get_media_info(str(in_video))
    tts_duration = get_media_info(str(tts_path))[1] if has_tts else 0.0

    speed = max(0.5, min(2.0, float(speed)))
    zoom = max(1.0, min(1.3, float(zoom)))
    blur = max(0, min(40, int(blur)))
    orig_vol = max(0.0, min(1.0, float(orig_vol)))
    bgm_vol = max(0.0, min(1.0, float(bgm_vol)))

    effective_vid_duration = (raw_vid_duration / speed) if raw_vid_duration > 0 else 0.0

    W, H = ASPECT_DIMENSIONS.get(aspect, (720, 1280))

    # Build Video Filter Chain (Universal Blur Background + Fit Foreground for 9:16, 16:9, 4:5, 1:1)
    v_ops = [f"setpts=PTS/{speed}"]
    if mirror:
        v_ops.append("hflip")
    if color_boost:
        v_ops.append("eq=contrast=1.06:saturation=1.12:brightness=0.01")

    base_chain = f"[0:v]{','.join(v_ops)}[vbase]"
    blur_part = f",boxblur={blur}:2" if blur > 0 else ""

    compose_chain = (
        f"{base_chain};"
        f"[vbase]split=2[vbg][vfg];"
        f"[vbg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}{blur_part}[bg];"
        f"[vfg]scale={W}:{H}:force_original_aspect_ratio=decrease,"
        f"scale=trunc(iw*{zoom}/2)*2:trunc(ih*{zoom}/2)*2,"
        f"crop=min(iw\\,{W}):min(ih\\,{H}):(iw-min(iw\\,{W}))/2:(ih-min(ih\\,{H}))/2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[vcomp]"
    )

    can_burn = False
    if burn_subs and has_srt and supports_subtitles_filter():
        if await ensure_myanmar_font():
            can_burn = True

    if can_burn:
        esc_srt = escape_ffmpeg_path(srt_path)
        esc_fdir = escape_ffmpeg_path(FONT_DIR)
        sub_layout = {
            "9:16": (85, 14),
            "16:9": (35, 17),
            "4:5": (70, 16),
            "1:1": (55, 16),
        }
        margin_v, font_sz = sub_layout.get(aspect, (65, 15))
        sub_filter = (
            f"subtitles='{esc_srt}':fontsdir='{esc_fdir}':"
            f"force_style='FontName=Noto Sans Myanmar,FontSize={font_sz},"
            f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H80000000,"
            f"BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV={margin_v}'"
        )
        v_filter = f"{compose_chain};[vcomp]{sub_filter},format=yuv420p[outv]"
    else:
        v_filter = f"{compose_chain};[vcomp]format=yuv420p[outv]"

    # Build Inputs & Audio Filter Chain
    cmd = [FFMPEG_EXE, "-y"]
    if has_tts and match_audio:
        cmd += ["-stream_loop", "-1"]
    cmd += ["-i", str(in_video)]

    next_idx = 1
    tts_idx = None
    bgm_idx = None

    if has_tts:
        cmd += ["-i", str(tts_path)]
        tts_idx = next_idx
        next_idx += 1

    if bgm_path is not None:
        cmd += ["-stream_loop", "-1", "-i", str(bgm_path)]
        bgm_idx = next_idx
        next_idx += 1

    audio_filters = []
    audio_labels = []

    if video_has_audio and orig_vol > 0:
        audio_filters.append(f"[0:a]atempo={speed},volume={orig_vol}[a_orig]")
        audio_labels.append("[a_orig]")

    if tts_idx is not None:
        audio_filters.append(f"[{tts_idx}:a]volume=1.15[a_tts]")
        audio_labels.append("[a_tts]")

    if bgm_idx is not None and bgm_vol > 0:
        audio_filters.append(f"[{bgm_idx}:a]volume={bgm_vol}[a_bgm]")
        audio_labels.append("[a_bgm]")

    full_filter = v_filter
    if audio_filters:
        full_filter += ";" + ";".join(audio_filters)

    if len(audio_labels) == 0:
        cmd += ["-filter_complex", full_filter, "-map", "[outv]", "-an"]
    elif len(audio_labels) == 1:
        cmd += ["-filter_complex", full_filter, "-map", "[outv]", "-map", audio_labels[0]]
    else:
        mix_inputs = "".join(audio_labels)
        full_filter += f";{mix_inputs}amix=inputs={len(audio_labels)}:duration=longest:normalize=0[outa]"
        cmd += ["-filter_complex", full_filter, "-map", "[outv]", "-map", "[outa]"]

    # Duration Control
    if has_tts and match_audio and tts_duration > 0:
        cmd += ["-t", f"{tts_duration + 0.3:.2f}"]
    elif not match_audio and effective_vid_duration > 0:
        cmd += ["-t", f"{effective_vid_duration:.2f}"]
    elif has_tts and match_audio:
        cmd += ["-shortest"]

    cmd += [
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "24",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_video),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    for tf in temp_files_to_remove:
        tf.unlink(missing_ok=True)

    if proc.returncode != 0 or not out_video.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Render Error: {stderr.decode(errors='ignore')[-500:]}",
        )

    return {
        "video_url": f"/files/{out_video.name}",
        "filename": out_video.name,
    }


@app.get("/files/{filename}")
async def serve_file(filename: str):
    safe_name = os.path.basename(filename)
    file_path = WORK_DIR / safe_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=file_path, filename=safe_name)

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="my">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
<title>AI Movie Recap Studio Pro</title>
<style>
  :root {
    --bg: #0b0f19;
    --card: #141b2d;
    --border: #25304a;
    --primary: #6366f1;
    --accent: #10b981;
    --pink: #ec4899;
    --text: #f1f5f9;
    --muted: #94a3b8;
  }
  * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Pyidaungsu", "Noto Sans Myanmar", sans-serif; }
  body { margin: 0; background: var(--bg); color: var(--text); padding-bottom: 90px; }
  header {
    background: linear-gradient(135deg, #1e1b4b, #0f172a);
    padding: 14px 16px;
    text-align: center;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    z-index: 50;
  }
  header h1 { margin: 0; font-size: 17px; }
  header p { margin: 3px 0 0; font-size: 11px; color: var(--muted); }
  .tabs {
    display: flex;
    background: var(--card);
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 58px;
    z-index: 40;
  }
  .tab-btn {
    flex: 1;
    padding: 12px 4px;
    background: none;
    border: none;
    color: var(--muted);
    font-size: 13px;
    font-weight: 700;
    cursor: pointer;
    border-bottom: 2px solid transparent;
  }
  .tab-btn.active { color: #fff; border-bottom-color: var(--primary); background: rgba(99,102,241,0.12); }
  .container { max-width: 600px; margin: 0 auto; padding: 14px; }
  .section { display: none; }
  .section.active { display: block; }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 14px;
    margin-bottom: 14px;
  }
  label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; color: #cbd5e1; }
  input[type="text"], input[type="url"], input[type="password"], select, textarea {
    width: 100%;
    background: #090d16;
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 10px;
    padding: 10px 12px;
    font-size: 14px;
    margin-bottom: 10px;
  }
  textarea { min-height: 125px; resize: vertical; line-height: 1.6; }
  .row { display: flex; gap: 8px; }
  .row > div { flex: 1; }
  .preset-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    margin-bottom: 12px;
  }
  .preset-btn {
    background: #090d16;
    border: 1px solid var(--border);
    color: var(--text);
    padding: 9px 8px;
    border-radius: 10px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    text-align: center;
  }
  .preset-btn.active {
    border-color: var(--primary);
    background: rgba(99,102,241,0.18);
    color: #fff;
  }
  .slider-wrap { margin-bottom: 12px; }
  .slider-head { display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 4px; }
  input[type="range"] { width: 100%; accent-color: var(--primary); }
  .btn {
    width: 100%;
    padding: 12px;
    border: none;
    border-radius: 11px;
    font-size: 14px;
    font-weight: 700;
    cursor: pointer;
    color: #fff;
    background: var(--primary);
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    text-decoration: none;
    margin-top: 6px;
  }
  .btn:disabled { opacity: 0.55; cursor: not-allowed; }
  .btn-green { background: var(--accent); color: #042f2e; }
  .btn-pink { background: var(--pink); color: #fff; }
  .btn-outline { background: transparent; border: 1px solid var(--border); color: var(--text); }
  audio, video { width: 100%; border-radius: 10px; margin-top: 10px; background: #000; }
  .badge {
    display: inline-block;
    background: rgba(16,185,129,0.15);
    color: #34d399;
    padding: 4px 10px;
    border-radius: 99px;
    font-size: 12px;
    margin-bottom: 8px;
  }
  .status { font-size: 13px; color: #fbbf24; margin-top: 8px; text-align: center; line-height: 1.5; }
  .file-box {
    border: 2px dashed var(--border);
    border-radius: 12px;
    padding: 16px;
    text-align: center;
    cursor: pointer;
    margin-bottom: 10px;
    font-size: 13px;
  }
  .source-toggle {
    display: flex;
    gap: 8px;
    margin-bottom: 12px;
  }
  .source-toggle button {
    flex: 1;
    padding: 9px;
    border-radius: 9px;
    border: 1px solid var(--border);
    background: #090d16;
    color: var(--muted);
    font-weight: 600;
    font-size: 13px;
    cursor: pointer;
  }
  .source-toggle button.active {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
  }
  .check-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 0;
    font-size: 13px;
    cursor: pointer;
  }
  .check-row input { width: 18px; height: 18px; accent-color: var(--primary); }
  details { margin-top: 8px; font-size: 12px; color: var(--muted); }
  summary { cursor: pointer; padding: 6px 0; }
</style>
</head>
<body>

<header>
  <h1>🎬 AI Movie Recap Studio Pro</h1>
  <p>TikTok • YouTube • Facebook All-in-One Studio</p>
</header>

<div class="tabs">
  <button class="tab-btn active" onclick="switchTab(0)">၁။ AI Script</button>
  <button class="tab-btn" onclick="switchTab(1)">၂။ မြန်မာအသံ</button>
  <button class="tab-btn" onclick="switchTab(2)">၃။ Video Render</button>
</div>

<div class="container">

  <!-- TAB 1: SCRIPT -->
  <div class="section active" id="tab-0">
    <div class="card">
      <label>Google Gemini API Key (Free Key ထည့်ပါ)</label>
      <input type="password" id="apiKey" placeholder="AIzaSy..." oninput="localStorage.setItem('gemini_key', this.value)" />

      <label>🔗 YouTube Link မှ စာတန်း (Subtitles) တိုက်ရိုက်ဆွဲယူရန် (Optional)</label>
      <div class="row" style="margin-bottom: 8px;">
        <div style="flex: 2;">
          <input type="url" id="ytSubUrl" placeholder="https://youtu.be/..." style="margin-bottom:0;" />
        </div>
        <div style="flex: 1;">
          <button class="btn btn-outline" id="btnFetchSubs" onclick="fetchYtSubs()" style="margin-top:0; padding:10px;">📥 စာတန်းဆွဲမည်</button>
        </div>
      </div>

      <label>ဇာတ်ကြောင်းဟန် (Narration Tone)</label>
      <select id="tone">
        <option value="စိတ်လှုပ်ရှားဖွယ် (Exciting & Engaging)">🔥 စိတ်လှုပ်ရှားဖွယ် (Action/Thriller)</option>
        <option value="ဝမ်းနည်းဒရာမာ (Emotional Drama)">💧 ဝမ်းနည်းဒရာမာ (Drama/Romance)</option>
        <option value="ထိတ်လန့်သည်းဖို (Horror & Mystery)">👻 ထိတ်လန့်သည်းဖို (Horror/Mystery)</option>
        <option value="ဟာသနှောပြီး ပြောပြဟန် (Funny & Casual)">😄 ဟာသနှောပြီး ပြောပြဟန်</option>
      </select>

      <label>သို့မဟုတ် ဖုန်းထဲမှ .SRT / .TXT ဖိုင်ရွေးပါ / အောက်တွင် စာကူးထည့်ပါ</label>
      <input type="file" accept=".srt,.txt" onchange="loadSrtFile(event)" style="margin-bottom:10px; font-size:13px;" />
      <textarea id="sourceText" placeholder="English / Chinese movie plot သို့မဟုတ် SRT စာသားများကို ဒီမှာထည့်ပါ..."></textarea>

      <button class="btn" id="btnScript" onclick="generateScript()">✨ မြန်မာ Recap Script ရေးမည်</button>
      <div class="status" id="statusScript"></div>
    </div>
  </div>

  <!-- TAB 2: TTS -->
  <div class="section" id="tab-1">
    <div class="card">
      <label>မြန်မာ Recap စာသား (တိုက်ရိုက်ပြင်ဆင်နိုင်သည်)</label>
      <textarea id="burmeseScript" placeholder="ဒီနေရာမှာ မြန်မာစာသား ထည့်ပြီး အသံထုတ်ပါ..."></textarea>

      <div class="row">
        <div>
          <label>အသံရွေးပါ (100% Free)</label>
          <select id="voice">
            <option value="my-MM-NilarNeural">👩 Nilar (မိန်းကလေးအသံ)</option>
            <option value="my-MM-ThihaNeural">👨 Thiha (ယောက်ျားလေးအသံ)</option>
          </select>
        </div>
        <div>
          <label>အသံအနိမ့်အမြင့် (Pitch)</label>
          <select id="pitch">
            <option value="+0Hz">ပုံမှန် (0Hz)</option>
            <option value="+4Hz">အနည်းငယ်မြင့် (+4Hz)</option>
            <option value="-4Hz">အနည်းငယ်နိမ့် (-4Hz)</option>
          </select>
        </div>
      </div>

      <div class="slider-wrap">
        <div class="slider-head">
          <span>စကားပြောနှုန်း (TTS Speed)</span>
          <span id="ttsRateVal">+5%</span>
        </div>
        <input type="range" id="ttsRate" min="-20" max="35" value="5" oninput="document.getElementById('ttsRateVal').innerText = (this.value>=0?'+':'')+this.value+'%'" />
      </div>

      <button class="btn" id="btnTTS" onclick="generateTTS()">🎙️ မြန်မာအသံ ထုတ်မည် (Free Unlimited)</button>
      <div class="status" id="statusTTS"></div>
    </div>

    <div class="card" id="ttsResultCard" style="display:none;">
      <span class="badge">✅ အသံဖိုင် အဆင်သင့်ဖြစ်ပါပြီ</span>
      <audio id="audioPlayer" controls></audio>
      <div class="row" style="margin-top:8px;">
        <a id="dlMp3" class="btn btn-outline" download>⬇️ MP3 သိမ်းမည်</a>
        <a id="dlSrt" class="btn btn-outline" download>⬇️ SRT သိမ်းမည်</a>
      </div>
      <button class="btn btn-green" style="margin-top:10px;" onclick="switchTab(2)">➡️ ဒီအသံနဲ့ Video Render လုပ်မည်</button>
    </div>
  </div>

  <!-- TAB 3: VIDEO RENDER -->
  <div class="section" id="tab-2">
    <div class="card">
      <label>၁။ ဗီဒီယို ထည့်သွင်းမည့် နည်းလမ်းရွေးပါ</label>
      <div class="source-toggle">
        <button id="modeUrlBtn" class="active" onclick="setVideoMode('url')">🔗 YouTube / TikTok / FB Link</button>
        <button id="modeFileBtn" onclick="setVideoMode('file')">📁 ဖုန်း Gallery မှရွေးမည်</button>
      </div>

      <!-- URL DOWNLOAD MODE -->
      <div id="urlSourceBox">
        <input type="url" id="videoUrlInput" placeholder="YouTube, TikTok, Facebook Video Link ထည့်ပါ..." />
        <button class="btn btn-pink" id="btnDownloadVid" onclick="downloadVideoFromLink()">⬇️ Link မှ Video ဒေါင်းလုဒ်ဆွဲမည်</button>
        <div class="status" id="statusDownload"></div>

        <div id="downloadedPreviewBox" style="display:none; margin-top:10px;">
          <span class="badge" id="downloadedTitleBadge">✅ Video ဒေါင်းပြီးပါပြီ</span>
          <video id="rawVideoPreview" controls playsinline></video>
          <a id="dlRawVideoBtn" class="btn btn-outline" download style="margin-top:8px;">📥 မူရင်း Video ကို Gallery ထဲ တိုက်ရိုက်သိမ်းမည်</a>
        </div>

        <details>
          <summary>⚙️ Advanced: YouTube Bot Check ကျော်ရန် (Cookies ထည့်ရန် - မလိုအပ်ပါက ကျော်ပါ)</summary>
          <textarea id="cookiesTxt" style="min-height:70px; font-size:12px;" placeholder="Netscape format cookies.txt စာသား (လိုအပ်မှသာ ထည့်ပါ)..."></textarea>
        </details>
      </div>

      <!-- GALLERY FILE MODE -->
      <div id="fileSourceBox" style="display:none;">
        <div class="file-box" onclick="document.getElementById('videoInput').click()">
          <div id="videoFileName">📁 ဖုန်း Gallery ထဲမှ ဗီဒီယိုဖိုင် ရွေးရန် နှိပ်ပါ (MP4 / MOV)</div>
          <input type="file" id="videoInput" accept="video/*" style="display:none" onchange="onVideoSelected(event)" />
        </div>
      </div>
    </div>

    <div class="card">
      <label>၂။ တင်မည့် Platform အလိုက် အမြန်ရွေးရန် (One-Tap Presets)</label>
      <div class="preset-grid">
        <button type="button" class="preset-btn active" id="pre-916" onclick="applyPlatformPreset('9:16', 1.15, 20, this)">📱 TikTok / Shorts / Reels (9:16)</button>
        <button type="button" class="preset-btn" id="pre-169" onclick="applyPlatformPreset('16:9', 1.10, 20, this)">🖥️ YouTube Video ရှည် (16:9)</button>
        <button type="button" class="preset-btn" id="pre-45" onclick="applyPlatformPreset('4:5', 1.15, 20, this)">📘 Facebook Feed Post (4:5)</button>
        <button type="button" class="preset-btn" id="pre-11" onclick="applyPlatformPreset('1:1', 1.15, 20, this)">🟦 Facebook Square (1:1)</button>
      </div>

      <div class="row">
        <div>
          <label>Screen အချိုး (Aspect Ratio)</label>
          <select id="aspect">
            <option value="9:16">📱 9:16 (TikTok / YT Shorts / FB Reels)</option>
            <option value="16:9">🖥️ 16:9 (YouTube Landscape / FB Video)</option>
            <option value="4:5">📘 4:5 (Facebook Feed Portrait)</option>
            <option value="1:1">🟦 1:1 (Facebook Square)</option>
          </select>
        </div>
        <div>
          <label>ဗီဒီယို အရှည်</label>
          <select id="matchAudio">
            <option value="true">✅ TTS အသံအရှည်အတိုင်း</option>
            <option value="false">🎬 မူရင်း Video အရှည်အတိုင်း</option>
          </select>
        </div>
      </div>

      <div class="slider-wrap">
        <div class="slider-head">
          <span>⚡ Video Speed (Copyright ရှောင်ရန်)</span>
          <span id="vidSpeedVal">1.15x</span>
        </div>
        <input type="range" id="vidSpeed" min="0.8" max="1.6" step="0.05" value="1.15" oninput="document.getElementById('vidSpeedVal').innerText = parseFloat(this.value).toFixed(2)+'x'" />
      </div>

      <div class="slider-wrap">
        <div class="slider-head">
          <span>🌫️ Blur Background အဝါးနှုန်း (ဘေး/အပေါ်အောက် အတွက်)</span>
          <span id="blurVal">20</span>
        </div>
        <input type="range" id="blur" min="0" max="40" step="2" value="20" oninput="document.getElementById('blurVal').innerText = this.value" />
      </div>

      <div class="slider-wrap">
        <div class="slider-head">
          <span>🔍 Anti-Copyright Zoom (ဘေးသားဖြတ်ရန်)</span>
          <span id="zoomVal">105%</span>
        </div>
        <input type="range" id="zoom" min="1.0" max="1.25" step="0.01" value="1.05" oninput="document.getElementById('zoomVal').innerText = Math.round(this.value*100)+'%'" />
      </div>

      <label class="check-row">
        <input type="checkbox" id="mirrorCheck" />
        <span>🔄 Mirror / ဘယ်ညာလှန်မည် (Copyright ရှောင်ရန် အကောင်းဆုံး)</span>
      </label>

      <label class="check-row">
        <input type="checkbox" id="colorBoostCheck" checked />
        <span>🎨 Color & Contrast Boost (အရောင်အနည်းငယ် ပြောင်းမည်)</span>
      </label>

      <label class="check-row">
        <input type="checkbox" id="burnSubsCheck" />
        <span>📝 မြန်မာစာတန်းထိုး (Subtitles) ဗီဒီယိုပေါ် တစ်ခါတည်းထည့်မည်</span>
      </label>

      <hr style="border:0; border-top:1px solid var(--border); margin:10px 0;" />

      <div class="slider-wrap">
        <div class="slider-head">
          <span>🔊 မူရင်းဇာတ်ကား အသံအတိုးအကျယ်</span>
          <span id="origVolVal">0% (Mute)</span>
        </div>
        <input type="range" id="origVol" min="0" max="0.5" step="0.05" value="0" oninput="document.getElementById('origVolVal').innerText = this.value==0?'0% (Mute)':Math.round(this.value*100)+'%'" />
      </div>

      <label>🎵 နောက်ခံတေးဂီတ (BGM MP3 - Optional)</label>
      <input type="file" id="bgmInput" accept="audio/*" style="font-size:13px; margin-bottom:8px;" />

      <div class="slider-wrap">
        <div class="slider-head">
          <span>🎶 BGM အသံအတိုးအကျယ်</span>
          <span id="bgmVolVal">15%</span>
        </div>
        <input type="range" id="bgmVol" min="0" max="0.5" step="0.05" value="0.15" oninput="document.getElementById('bgmVolVal').innerText = Math.round(this.value*100)+'%'" />
      </div>

      <button class="btn" id="btnRender" onclick="renderVideo()">🎬 Video Render လုပ်မည်</button>
      <div class="status" id="statusRender"></div>
    </div>

    <div class="card" id="renderResultCard" style="display:none;">
      <span class="badge">🎉 Video Render ပြီးပါပြီ!</span>
      <video id="videoPreview" controls playsinline></video>
      <a id="dlVideo" class="btn btn-green" download="movie_recap.mp4" style="margin-top:12px;">📥 ဖုန်း Gallery / Downloads ထဲ သိမ်းမည်</a>
      <button class="btn btn-outline" onclick="shareToGallery()">📤 Gallery / Share Menu ဖြင့် တိုက်ရိုက်သိမ်းမည်</button>
    </div>
  </div>

</div>

<script>
  let videoMode = "url";
  let serverVideoFile = "";
  let currentMp3File = "";
  let currentSrtFile = "";
  let currentVideoUrl = "";
  let currentVideoName = "movie_recap.mp4";

  window.onload = () => {
    const savedKey = localStorage.getItem("gemini_key");
    if (savedKey) document.getElementById("apiKey").value = savedKey;
  };

  function switchTab(idx) {
    document.querySelectorAll(".tab-btn").forEach((b, i) => b.classList.toggle("active", i === idx));
    document.querySelectorAll(".section").forEach((s, i) => s.classList.toggle("active", i === idx));
  }

  function applyPlatformPreset(aspectVal, speedVal, blurVal, btnEl) {
    document.querySelectorAll(".preset-btn").forEach(b => b.classList.remove("active"));
    if (btnEl) btnEl.classList.add("active");
    document.getElementById("aspect").value = aspectVal;
    document.getElementById("vidSpeed").value = speedVal;
    document.getElementById("vidSpeedVal").innerText = parseFloat(speedVal).toFixed(2) + "x";
    document.getElementById("blur").value = blurVal;
    document.getElementById("blurVal").innerText = blurVal;
  }

  function setVideoMode(mode) {
    videoMode = mode;
    document.getElementById("modeUrlBtn").classList.toggle("active", mode === "url");
    document.getElementById("modeFileBtn").classList.toggle("active", mode === "file");
    document.getElementById("urlSourceBox").style.display = mode === "url" ? "block" : "none";
    document.getElementById("fileSourceBox").style.display = mode === "file" ? "block" : "none";
  }

  function loadSrtFile(e) {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      document.getElementById("sourceText").value = ev.target.result;
    };
    reader.readAsText(file);
  }

  function onVideoSelected(e) {
    const file = e.target.files[0];
    if (file) {
      document.getElementById("videoFileName").innerText = "✅ ရွေးထားသောဖိုင်: " + file.name;
    }
  }

  async function fetchYtSubs() {
    const url = document.getElementById("ytSubUrl").value;
    const cookies_txt = document.getElementById("cookiesTxt").value;
    const btn = document.getElementById("btnFetchSubs");
    const status = document.getElementById("statusScript");

    btn.disabled = true;
    status.innerText = "⏳ YouTube Link မှ စာတန်း (Subtitles) ဆွဲယူနေပါသည်...";

    try {
      const res = await fetch("/api/yt-subs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, cookies_txt })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Subtitle error");

      document.getElementById("sourceText").value = data.text;
      status.innerText = "✅ စာတန်းရပါပြီ (" + data.title + ")။ အောက်က ခလုတ်ကိုနှိပ်ပြီး ဘာသာပြန်ပါ။";
    } catch (err) {
      status.innerText = "❌ " + err.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function generateScript() {
    const apiKey = document.getElementById("apiKey").value;
    const sourceText = document.getElementById("sourceText").value;
    const tone = document.getElementById("tone").value;
    const btn = document.getElementById("btnScript");
    const status = document.getElementById("statusScript");

    btn.disabled = true;
    status.innerText = "⏳ AI မှ မြန်မာလို ဘာသာပြန်ပြီး Script ရေးနေပါသည်...";

    try {
      const res = await fetch("/api/script", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: apiKey, source_text: sourceText, tone })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Error");

      document.getElementById("burmeseScript").value = data.script;
      status.innerText = "✅ Script ရေးပြီးပါပြီ! အသံထုတ်ရန် Tab (၂) သို့ ပြောင်းနေပါသည်...";
      setTimeout(() => switchTab(1), 800);
    } catch (err) {
      status.innerText = "❌ " + err.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function generateTTS() {
    const text = document.getElementById("burmeseScript").value;
    const voice = document.getElementById("voice").value;
    const pitch = document.getElementById("pitch").value;
    const rateVal = document.getElementById("ttsRate").value;
    const rate = (rateVal >= 0 ? "+" : "") + rateVal + "%";
    const btn = document.getElementById("btnTTS");
    const status = document.getElementById("statusTTS");

    btn.disabled = true;
    status.innerText = "🎙️ မြန်မာအသံ ထုတ်နေပါသည်...";

    try {
      const res = await fetch("/api/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, voice, rate, pitch })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "TTS Error");

      currentMp3File = data.mp3_file;
      currentSrtFile = data.srt_file;
      document.getElementById("audioPlayer").src = data.mp3_url;
      document.getElementById("dlMp3").href = data.mp3_url;
      document.getElementById("dlSrt").href = data.srt_url;
      document.getElementById("ttsResultCard").style.display = "block";
      status.innerText = "";
    } catch (err) {
      status.innerText = "❌ " + err.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function downloadVideoFromLink() {
    const url = document.getElementById("videoUrlInput").value;
    const cookies_txt = document.getElementById("cookiesTxt").value;
    const btn = document.getElementById("btnDownloadVid");
    const status = document.getElementById("statusDownload");

    btn.disabled = true;
    status.innerText = "⏳ Link မှ Video ဒေါင်းလုဒ်ဆွဲနေပါသည် (ခဏစောင့်ပါ)...";

    try {
      const res = await fetch("/api/download-video", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, cookies_txt })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Download failed");

      serverVideoFile = data.video_file;
      document.getElementById("downloadedTitleBadge").innerText = "✅ ဒေါင်းပြီး: " + data.title;
      document.getElementById("rawVideoPreview").src = data.video_url;
      document.getElementById("dlRawVideoBtn").href = data.video_url;
      document.getElementById("downloadedPreviewBox").style.display = "block";
      status.innerText = "";
    } catch (err) {
      status.innerText = "❌ " + err.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function renderVideo() {
    const fileInput = document.getElementById("videoInput");
    if (videoMode === "url" && !serverVideoFile) {
      alert("ကျေးဇူးပြု၍ Link ထည့်ပြီး Video အရင်ဒေါင်းလုဒ်ဆွဲပါ။");
      return;
    }
    if (videoMode === "file" && !fileInput.files.length) {
      alert("ကျေးဇူးပြု၍ ဖုန်း Gallery ထဲမှ Video ဖိုင် အရင်ရွေးပါ။");
      return;
    }

    const btn = document.getElementById("btnRender");
    const status = document.getElementById("statusRender");
    btn.disabled = true;
    status.innerText = "⏳ ဗီဒီယို Render လုပ်နေပါသည် (ခဏစောင့်ပေးပါ)...";

    const formData = new FormData();
    if (videoMode === "file") {
      formData.append("video", fileInput.files[0]);
    } else {
      formData.append("server_video_file", serverVideoFile);
    }

    const bgmInput = document.getElementById("bgmInput");
    if (bgmInput.files.length) {
      formData.append("bgm", bgmInput.files[0]);
    }

    formData.append("tts_file", currentMp3File);
    formData.append("srt_file", currentSrtFile);
    formData.append("aspect", document.getElementById("aspect").value);
    formData.append("speed", document.getElementById("vidSpeed").value);
    formData.append("blur", document.getElementById("blur").value);
    formData.append("zoom", document.getElementById("zoom").value);
    formData.append("mirror", document.getElementById("mirrorCheck").checked);
    formData.append("color_boost", document.getElementById("colorBoostCheck").checked);
    formData.append("burn_subs", document.getElementById("burnSubsCheck").checked);
    formData.append("orig_vol", document.getElementById("origVol").value);
    formData.append("bgm_vol", document.getElementById("bgmVol").value);
    formData.append("match_audio", document.getElementById("matchAudio").value);

    try {
      const res = await fetch("/api/render", { method: "POST", body: formData });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Render failed");

      currentVideoUrl = data.video_url;
      currentVideoName = data.filename;
      document.getElementById("videoPreview").src = data.video_url;
      const dlBtn = document.getElementById("dlVideo");
      dlBtn.href = data.video_url;
      dlBtn.download = data.filename;
      document.getElementById("renderResultCard").style.display = "block";
      status.innerText = "";
    } catch (err) {
      status.innerText = "❌ " + err.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function shareToGallery() {
    if (!currentVideoUrl) return;
    try {
      const resp = await fetch(currentVideoUrl);
      const blob = await resp.blob();
      const file = new File([blob], currentVideoName, { type: "video/mp4" });
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        await navigator.share({ files: [file], title: "Movie Recap Video" });
      } else {
        document.getElementById("dlVideo").click();
      }
    } catch (e) {
      document.getElementById("dlVideo").click();
    }
  }
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
