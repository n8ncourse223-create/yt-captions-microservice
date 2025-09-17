# main.py
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
import tempfile
import subprocess
import os
import glob
import re
import json
import base64

app = FastAPI(title="YouTube Auto-Captions → Text API")

VTT_TAG_RE = re.compile(r"<[^>]+>")
TIMESTAMP_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3} --> ")

def vtt_to_text(vtt_path: str) -> str:
    VTT_TAG_RE = re.compile(r"<[^>]+>")
    TIMESTAMP_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3} --> ")
    DROP_PREFIXES = ("WEBVTT", "Kind:", "Language:")
    NOISE_BRACKETS = re.compile(r"^\[[^\]]+\]$")  # e.g., [Muzyka]

    def too_similar(a: str, b: str) -> bool:
        a_words = [w for w in re.findall(r"\w+", a.lower()) if len(w) > 1]
        b_words = [w for w in re.findall(r"\w+", b.lower()) if len(w) > 1]
        if not a_words or not b_words:
            return False
        aset, bset = set(a_words), set(b_words)
        overlap = len(aset & bset) / min(len(aset), len(bset))
        return overlap >= 0.8  # only drop near-duplicates

    lines = []
    prev = ""

    with open(vtt_path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            s = raw.strip()
            if not s:
                continue
            if s.startswith(DROP_PREFIXES):
                continue
            if s.isdigit():
                continue
            if TIMESTAMP_RE.search(s) or "-->" in s:
                continue
            if NOISE_BRACKETS.match(s):
                continue

            s = VTT_TAG_RE.sub("", s)

            # Skip only if nearly the same as previous cue
            if prev and too_similar(s, prev):
                continue

            lines.append(s)
            prev = s

    text = " ".join(lines)
    text = re.sub(r"\s+", " ", text).strip()

    # Light pass to collapse immediate word repeats (keeps meaning)
    text = re.sub(r"\b(\w{2,})\s+\1\b", r"\1", text, flags=re.IGNORECASE)

    return text

def write_cookies_file() -> int:
    """Write cookies from env to /app/cookies.txt. Prefer base64 to avoid formatting issues.
       Returns number of bytes written."""
    cookies_b64 = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()
    cookies_raw = os.environ.get("YOUTUBE_COOKIES", "").strip()

    cookie_text = ""
    if cookies_b64:
        try:
            cookie_text = base64.b64decode(cookies_b64).decode("utf-8", "ignore")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to decode YOUTUBE_COOKIES_B64: {e}")
    elif cookies_raw:
        cookie_text = cookies_raw

    if not cookie_text:
        raise HTTPException(status_code=500, detail="No cookies provided. Set YOUTUBE_COOKIES_B64 (preferred) or YOUTUBE_COOKIES.")

    with open("/app/cookies.txt", "w", encoding="utf-8") as cf:
        cf.write(cookie_text)
    return len(cookie_text.encode("utf-8"))

@app.get("/debug")
def debug(videoId: str = Query(..., alias="videoId")):
    # Write cookies and probe with cookies
    cookie_bytes = write_cookies_file()
    url = f"https://www.youtube.com/watch?v={videoId}"
    probe_cmd = ["yt-dlp", "--cookies", "/app/cookies.txt", "-J", url]
    try:
        res = subprocess.run(probe_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ok = True
        stderr = ""
    except subprocess.CalledProcessError as e:
        ok = False
        stderr = (e.stderr or "")[-800:]  # tail for readability

    return JSONResponse({
        "cookie_bytes": cookie_bytes,
        "probe_ok": ok,
        "stderr_tail": stderr,
    })

@app.get("/subs")
async def get_subs(videoId: str = Query(..., alias="videoId"), lang: str = "pl"):
    """
    Fetch subtitles for a YouTube video and return as plain text.
    Uses cookies for both probe (-J) and download to avoid 429.
    Tries requested lang, then dialect match, then any available auto; falls back to manual if no auto.
    """
    cookie_bytes = write_cookies_file()

    with tempfile.TemporaryDirectory() as tmp:
        url = f"https://www.youtube.com/watch?v={videoId}"

        # --- Probe with cookies ---
        probe_cmd = ["yt-dlp", "--cookies", "/app/cookies.txt", "-J", url]
        try:
            res = subprocess.run(probe_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            info = json.loads(res.stdout)
        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=500, detail=f"Failed to probe video (likely 429/consent): {e.stderr[-500:]}")
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=500, detail=f"Failed to parse probe JSON: {e}")

        auto_caps = list((info.get("automatic_captions") or {}).keys())
        manual_subs = list((info.get("subtitles") or {}).keys())

        def find_prefix(langs, prefix):
            for code in langs:
                if code and code.lower().startswith(prefix.lower()):
                    return code
            return None

        chosen_lang = None
        used_type = None  # 'auto' or 'manual'

        if auto_caps:
            chosen_lang = lang if lang in auto_caps else (find_prefix(auto_caps, lang) or auto_caps[0])
            used_type = "auto"
        elif manual_subs:
            chosen_lang = lang if lang in manual_subs else (find_prefix(manual_subs, lang) or manual_subs[0])
            used_type = "manual"
        else:
            raise HTTPException(status_code=404, detail="No subtitles (auto or manual) advertised for this video.")

        # --- Download with cookies ---
        base_cmd = [
            "yt-dlp",
            "--cookies", "/app/cookies.txt",
            f"--sub-lang={chosen_lang}",
            "--skip-download",
            "--sub-format", "vtt",
            "-o", os.path.join(tmp, "%(id)s.%(ext)s"),
            url,
        ]
        base_cmd.insert(1, "--write-auto-subs" if used_type == "auto" else "--write-subs")

        try:
            subprocess.run(base_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=404, detail=f"Failed to download subtitles for {videoId} in {chosen_lang}: {e.stderr[-500:]}")

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
            "cookie_bytes": cookie_bytes,
            "chars": len(text),
            "text": text,
        })
