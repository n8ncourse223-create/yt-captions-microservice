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

# Required for YouTube JS challenge solving (EJS)
YTDLP_FLAGS = [
    "--js-runtimes", "deno",
    "--remote-components", "ejs:github",
]

def run_cmd(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

def ytdlp_version():
    res = run_cmd(["yt-dlp", "--version"])
    return (res.stdout or res.stderr or "").strip()

def vtt_to_text(vtt_path: str) -> str:
    DROP_PREFIXES = ("WEBVTT", "Kind:", "Language:")
    NOISE_BRACKETS = re.compile(r"^\[[^\]]+\]$")  # e.g., [Muzyka]

    def too_similar(a: str, b: str) -> bool:
        a_words = [w for w in re.findall(r"\w+", a.lower()) if len(w) > 1]
        b_words = [w for w in re.findall(r"\w+", b.lower()) if len(w) > 1]
        if not a_words or not b_words:
            return False
        aset, bset = set(a_words), set(b_words)
        overlap = len(aset & bset) / min(len(aset), len(bset))
        return overlap >= 0.8

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

            if prev and too_similar(s, prev):
                continue

            lines.append(s)
            prev = s

    text = " ".join(lines)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\b(\w{2,})\s+\1\b", r"\1", text, flags=re.IGNORECASE)
    return text

def write_cookies_file() -> int:
    """Write cookies from env to /app/cookies.txt. Prefer base64 to avoid formatting issues."""
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
        raise HTTPException(
            status_code=500,
            detail="No cookies provided. Set YOUTUBE_COOKIES_B64 (preferred) or YOUTUBE_COOKIES."
        )

    with open("/app/cookies.txt", "w", encoding="utf-8") as cf:
        cf.write(cookie_text)
    return len(cookie_text.encode("utf-8"))

def probe_video(url: str):
    probe_cmd = [
        "yt-dlp",
        "--cookies", "/app/cookies.txt",
        *YTDLP_FLAGS,
        "-J",
        url,
    ]
    res = run_cmd(probe_cmd)
    if res.returncode != 0:
        return {
            "ok": False,
            "stderr": (res.stderr or "")[-1200:],
            "cmd": probe_cmd,
            "info": None,
        }

    try:
        info = json.loads(res.stdout)
    except json.JSONDecodeError as e:
        return {
            "ok": False,
            "stderr": f"Failed to parse probe JSON: {e}",
            "cmd": probe_cmd,
            "info": None,
        }

    return {
        "ok": True,
        "stderr": (res.stderr or "")[-1200:],
        "cmd": probe_cmd,
        "info": info,
    }

def choose_lang(advertised_langs, requested_lang):
    if not advertised_langs:
        return None
    if requested_lang in advertised_langs:
        return requested_lang
    for code in advertised_langs:
        if code and code.lower().startswith(requested_lang.lower()):
            return code
    return advertised_langs[0]

def list_vtts(tmp_dir: str, video_id: str):
    return glob.glob(os.path.join(tmp_dir, f"{video_id}.*.vtt")) or glob.glob(os.path.join(tmp_dir, "*.vtt"))

def pick_best_vtt(vtts, requested_lang):
    if not vtts:
        return None
    requested_lang = (requested_lang or "").lower()

    # Prefer a file containing the requested lang code in its filename
    for p in vtts:
        name = os.path.basename(p).lower()
        if f".{requested_lang}." in name or name.endswith(f".{requested_lang}.vtt"):
            return p

    # Then prefer any prefix match like pl-PL
    for p in vtts:
        name = os.path.basename(p).lower()
        if f".{requested_lang}-" in name or f".{requested_lang}_" in name:
            return p

    return vtts[0]

@app.get("/debug")
def debug(videoId: str = Query(..., alias="videoId")):
    cookie_bytes = write_cookies_file()
    url = f"https://www.youtube.com/watch?v={videoId}"

    probe = probe_video(url)
    info = probe["info"] or {}

    auto_caps = list((info.get("automatic_captions") or {}).keys()) if info else []
    manual_subs = list((info.get("subtitles") or {}).keys()) if info else []

    return JSONResponse({
        "cookie_bytes": cookie_bytes,
        "probe_ok": probe["ok"],
        "stderr_tail": probe["stderr"],
        "probe_cmd": probe["cmd"],
        "yt_dlp_version": ytdlp_version(),
        "title": info.get("title") if info else None,
        "available_auto": auto_caps,
        "available_manual": manual_subs,
    })

@app.get("/subs")
async def get_subs(videoId: str = Query(..., alias="videoId"), lang: str = "pl"):
    """
    Fetch subtitles for a YouTube video and return as plain text.
    Uses cookies for both probe (-J) and download to avoid 429/consent.
    Tries advertised subtitles first. If probe advertises none, falls back
    to direct subtitle download attempts instead of failing immediately.
    """
    cookie_bytes = write_cookies_file()

    with tempfile.TemporaryDirectory() as tmp:
        url = f"https://www.youtube.com/watch?v={videoId}"

        # --- Probe with cookies ---
        probe = probe_video(url)
        if not probe["ok"]:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to probe video (likely 429/consent): {probe['stderr']}"
            )

        info = probe["info"] or {}
        auto_caps = list((info.get("automatic_captions") or {}).keys())
        manual_subs = list((info.get("subtitles") or {}).keys())

        chosen_lang = None
        used_type = None

        if auto_caps:
            chosen_lang = choose_lang(auto_caps, lang)
            used_type = "auto"
        elif manual_subs:
            chosen_lang = choose_lang(manual_subs, lang)
            used_type = "manual"

        # --- Preferred path: advertised subtitles ---
        if chosen_lang and used_type:
            write_flag = "--write-auto-subs" if used_type == "auto" else "--write-subs"
            dl_cmd = [
                "yt-dlp",
                write_flag,
                "--cookies", "/app/cookies.txt",
                *YTDLP_FLAGS,
                f"--sub-lang={chosen_lang}",
                "--skip-download",
                "--sub-format", "vtt",
                "-o", os.path.join(tmp, "%(id)s.%(ext)s"),
                url,
            ]

            res = run_cmd(dl_cmd)
            if res.returncode == 0:
                vtts = list_vtts(tmp, videoId)
                vtt_path = pick_best_vtt(vtts, chosen_lang)
                if vtt_path:
                    text = vtt_to_text(vtt_path)
                    if text:
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

        # --- Fallback path: direct subtitle download attempt ---
        # Some videos do not advertise captions cleanly in probe JSON.
        fallback_cmd = [
            "yt-dlp",
            "--write-auto-subs",
            "--write-subs",
            "--cookies", "/app/cookies.txt",
            *YTDLP_FLAGS,
            "--sub-lang", "all",
            "--skip-download",
            "--sub-format", "vtt",
            "-o", os.path.join(tmp, "%(id)s.%(ext)s"),
            url,
        ]

        fallback_res = run_cmd(fallback_cmd)
        vtts = list_vtts(tmp, videoId)
        vtt_path = pick_best_vtt(vtts, lang)

        if vtt_path:
            text = vtt_to_text(vtt_path)
            if text:
                fname = os.path.basename(vtt_path).lower()
                inferred_type = "unknown"
                if ".vtt" in fname:
                    if ".orig." in fname or ".live_chat." in fname:
                        inferred_type = "manual"
                    else:
                        inferred_type = "auto_or_manual"

                return JSONResponse({
                    "video_id": videoId,
                    "requested_lang": lang,
                    "used_lang": lang,
                    "used_type": inferred_type,
                    "available_auto": auto_caps,
                    "available_manual": manual_subs,
                    "cookie_bytes": cookie_bytes,
                    "chars": len(text),
                    "text": text,
                })

        # If we still have nothing, return a richer error than before
        raise HTTPException(
            status_code=404,
            detail=(
                "No subtitles could be downloaded for this video. "
                f"advertised_auto={auto_caps}, advertised_manual={manual_subs}, "
                f"fallback_stderr={(fallback_res.stderr or '')[-800:]}"
            ),
        )
