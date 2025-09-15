# main.py
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
import tempfile
import subprocess
import os
import glob
import re
import json

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
    text = re.sub(r"\s+", " ", text).strip()
    return text

@app.get("/subs")
async def get_subs(videoId: str = Query(..., alias="videoId"), lang: str = "pl"):
    """
    Fetch subtitles for a YouTube video and return as plain text.
    Strategy:
      1) Probe available caption languages via yt-dlp -J
      2) Prefer auto-captions in requested `lang`; else try dialect match (e.g., pl-PL)
      3) Else fall back to any available auto-caption language
      4) If no auto-captions, try manual subtitles
      5) Use YouTube cookies (if provided) to avoid 429
    """
    # Write cookies (if provided via env) so yt-dlp can authenticate
    with open("/app/cookies.txt", "w", encoding="utf-8") as cf:
        cf.write(os.environ.get("YOUTUBE_COOKIES", ""))

    with tempfile.TemporaryDirectory() as tmp:
        url = f"https://www.youtube.com/watch?v={videoId}"

        # --- Probe metadata for available captions ---
        probe_cmd = ["yt-dlp", "-J", url]
        try:
            res = subprocess.run(probe_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            info = json.loads(res.stdout)
        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=500, detail=f"Failed to probe video: {e.stderr}")
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=500, detail=f"Failed to parse probe JSON: {e}")

        auto_caps = list((info.get("automatic_captions") or {}).keys())
        manual_subs = list((info.get("subtitles") or {}).keys())

        # Helper: find first code in `langs` that matches prefix like 'pl' or 'en'
        def find_prefix(langs, prefix):
            for code in langs:
                if code.lower().startswith(prefix.lower()):
                    return code
            return None

        chosen_lang = None
        used_type = None  # 'auto' or 'manual'

        # Prefer auto-captions in requested lang or dialect
        if auto_caps:
            if lang in auto_caps:
                chosen_lang = lang
            else:
                # try dialect match (e.g., 'pl' matches 'pl-PL')
                pref = find_prefix(auto_caps, lang)
                if pref:
                    chosen_lang = pref
            if not chosen_lang:
                # fall back to any available auto-caption language
                chosen_lang = auto_caps[0]
            used_type = 'auto'
        elif manual_subs:
            # no auto-captions; try manual subs
            if lang in manual_subs:
                chosen_lang = lang
            else:
                pref = find_prefix(manual_subs, lang)
                if pref:
                    chosen_lang = pref
            if not chosen_lang:
                chosen_lang = manual_subs[0]
            used_type = 'manual'
        else:
            raise HTTPException(status_code=404, detail="No subtitles (auto or manual) advertised for this video.")

        # --- Download captions with yt-dlp ---
        base_cmd = [
            "yt-dlp",
            "--cookies", "/app/cookies.txt",
            f"--sub-lang={chosen_lang}",
            "--skip-download",
            "--sub-format", "vtt",
            "-o", os.path.join(tmp, "%(id)s.%(ext)s"),
            url,
        ]
        if used_type == 'auto':
            base_cmd.insert(1, "--write-auto-subs")
        else:
            base_cmd.insert(1, "--write-subs")

        try:
            subprocess.run(base_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=404, detail=f"Failed to download subtitles for {videoId} in {chosen_lang}: {e.stderr}")

        # --- Find and parse the VTT file ---
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
            "used_type": used_type,
            "available_auto": auto_caps,
            "available_manual": manual_subs,
            "chars": len(text),
            "text": text,
        })
