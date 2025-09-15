main.py
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
import tempfile
import subprocess
import os
import glob
import re

app = FastAPI(title="YouTube Auto-Captions → Text API")

VTT_TAG_RE = re.compile(r"<[^>]+>")
TIMESTAMP_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3} --> ")


def vtt_to_text(vtt_path: str) -> str:
    lines = []
    with open(vtt_path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            s = raw.strip()
            if not s:
                continue
            if s.startswith("WEBVTT"):
                continue
            # skip cue number lines like '12'
            if s.isdigit():
                continue
            # skip timestamp lines
            if TIMESTAMP_RE.search(s) or "-->" in s:
                continue
            # drop styling tags
            s = VTT_TAG_RE.sub("", s)
            lines.append(s)
    # merge consecutive lines, collapse duplicates
    text = " ".join(lines)
    # collapse repeated spaces
    text = re.sub(r"\s+", " ", text).strip()
    return text


@app.get("/subs")
async def get_subs(videoId: str = Query(..., alias="videoId"), lang: str = "pl"):
    """
    Fetch auto-generated subtitles for a YouTube video and return as plain text.
    Query params:
      - videoId: YouTube video ID
      - lang: subtitle language code (e.g., 'pl', 'en')
    """
    with tempfile.TemporaryDirectory() as tmp:
        url = f"https://www.youtube.com/watch?v={videoId}"
        # Use yt-dlp to download auto-subs only
        cmd = [
            "yt-dlp",
            "--write-auto-subs",
            f"--sub-lang={lang}",
            "--skip-download",
            "--sub-format", "vtt",
            "-o", os.path.join(tmp, "%(id)s.%(ext)s"),
            url,
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except subprocess.CalledProcessError as e:
            # Try fallback language if primary failed
            alt = "en" if lang != "en" else "pl"
            try:
                cmd[2] = f"--sub-lang={alt}"
                subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                lang = alt
            except subprocess.CalledProcessError:
                raise HTTPException(status_code=404, detail=f"No subtitles found for video {videoId} in {lang} or fallback.")

        # find the downloaded .vtt file
        vtts = glob.glob(os.path.join(tmp, f"{videoId}.*.vtt")) or glob.glob(os.path.join(tmp, f"*.vtt"))
        if not vtts:
            raise HTTPException(status_code=404, detail="Subtitles not found after download.")
        text = vtt_to_text(vtts[0])
        if not text:
            raise HTTPException(status_code=422, detail="Subtitles parsed but empty.")
        return JSONResponse({
            "video_id": videoId,
            "lang": lang,
            "chars": len(text),
            "text": text,
        })

# requirements.txt
# fastapi
# uvicorn
# yt-dlp
