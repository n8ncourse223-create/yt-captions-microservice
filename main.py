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
    with open("/app/cookies.txt", "w", encoding="utf-8") as f:
    f.write(os.environ.get("YOUTUBE_COOKIES", ""))
    """
    Fetch auto-generated subtitles for a YouTube video and return as plain text.
    Will try the requested `lang` first, then fall back to any available language.
    """
    with tempfile.TemporaryDirectory() as tmp:
        url = f"https://www.youtube.com/watch?v={videoId}"

        # Step 1: probe available subtitles
        probe_cmd = ["yt-dlp", "-J", url]
        try:
            result = subprocess.run(probe_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            info = result.stdout
        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=500, detail=f"Failed to probe video: {e.stderr}")

        # Look for available subtitle languages
        available_langs = []
        for line in info.splitlines():
            if '"language":' in line:
                lang_code = line.strip().split(":")[-1].strip().strip('"').strip(",")
                available_langs.append(lang_code)

        # Step 2: pick a language to use
        chosen_lang = lang
        if lang not in available_langs and available_langs:
            chosen_lang = available_langs[0]  # fallback to first available

        # Step 3: try to download subtitles
        cmd = [
    "yt-dlp",
    "--cookies", "/app/cookies.txt",
    "--write-auto-subs",
    f"--sub-lang={chosen_lang}",
    "--skip-download",
    "--sub-format", "vtt",
    "-o", os.path.join(tmp, "%(id)s.%(ext)s"),
    url,
]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except subprocess.CalledProcessError:
            raise HTTPException(status_code=404, detail=f"No subtitles found for video {videoId} in {chosen_lang}.")

        # Step 4: find the .vtt file and parse it
        vtts = glob.glob(os.path.join(tmp, f"{videoId}.*.vtt")) or glob.glob(os.path.join(tmp, "*.vtt"))
        if not vtts:
            raise HTTPException(status_code=404, detail="Subtitles not found after download.")
        text = vtt_to_text(vtts[0])
        if not text:
            raise HTTPException(status_code=422, detail="Subtitles parsed but empty.")

        return JSONResponse({
            "video_id": videoId,
            "requested_lang": lang,
            "used_lang": chosen_lang,
            "chars": len(text),
            "text": text,
        })
# requirements.txt
# fastapi
# uvicorn
# yt-dlp
