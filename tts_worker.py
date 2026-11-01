#!/usr/bin/env python3
"""
Persistent VieNeu-TTS Worker Daemon (voice='Minh Quân' default, alias of preset 'Hải Đăng').
- Giữ nguyên giao thức JSON qua stdin/stdout của bản ZeroTTS trước đây.
- Làm sạch văn bản tiếng Việt: lọc ký tự lạ, emote, ký tự lặp; chuẩn hóa NFKC -> NFC.
- Peak normalization (<= 0.90) + cosine fade-in/out để chống rè/xước.
"""
import hashlib
import json
import os
import re
import sys
import time
import traceback
import unicodedata
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "3")
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
os.environ.setdefault("MKL_NUM_THREADS", "3")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "3")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import numpy as np
import soundfile as sf
from vieneu import Vieneu

DEFAULT_VOICE = "Minh Quân"
# Tên giọng cũ (ZeroTTS) và tên không phân biệt hoa thường đều map về giọng VieNeu hợp lệ.
LEGACY_VOICE_MAP = {"maichi": DEFAULT_VOICE, "mai chi": DEFAULT_VOICE}
CACHE_DIR = Path(__file__).resolve().parent / "data" / "tts-cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Chỉ giữ lại chữ cái tiếng Việt (Latin + dấu tiếng Việt), chữ số và dấu câu cơ bản
VIETNAMESE_ALLOWED_RE = re.compile(
    r"[^0-9a-zA-ZÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝàáâãèéêìíòóôõùúýĂăĐđĨĩŨũƠơƯưẠ-ỹ\s.,!?]"
)
BRACKET_EMOTE_RE = re.compile(r"\[[^\]]{1,24}\]")
REPEATED_CHARS_RE = re.compile(r"(.)\1{2,}", re.IGNORECASE)
MULTI_PUNCT_RE = re.compile(r"([.,!?])\1+")
MULTI_SPACE_RE = re.compile(r"\s+")


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def clean_and_normalize_text(raw_text: str) -> str:
    if not raw_text:
        return ""
    text = unicodedata.normalize("NFC", unicodedata.normalize("NFKC", str(raw_text)))
    text = BRACKET_EMOTE_RE.sub(" ", text)
    text = VIETNAMESE_ALLOWED_RE.sub(" ", text)
    text = MULTI_PUNCT_RE.sub(r"\1", text)
    text = REPEATED_CHARS_RE.sub(r"\1\1", text)
    text = MULTI_SPACE_RE.sub(" ", text).strip(" .,!?-")

    letters = re.findall(r"[a-zA-ZÀ-ỹĂăĐđĨĩŨũƠơƯư]", text)
    if len(letters) < 2:
        return ""

    if len(text) > 160:
        text = text[:160].rsplit(" ", 1)[0]

    normalized = VIETNAMESE_ALLOWED_RE.sub(" ", text)
    normalized = MULTI_SPACE_RE.sub(" ", normalized).strip()
    if normalized and normalized[-1] not in ".!?":
        normalized += "."
    return normalized


def polish_waveform(wav: np.ndarray, sample_rate: int) -> np.ndarray:
    """De-click ends and prevent digital clipping ('xước/rè')."""
    arr = np.asarray(wav, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return arr

    arr = arr - np.mean(arr)

    peak = float(np.max(np.abs(arr)))
    if peak > 0.90:
        arr = arr * (0.90 / peak)

    fade_in = min(int(sample_rate * 0.012), arr.size // 4)
    fade_out = min(int(sample_rate * 0.020), arr.size // 4)
    if fade_in > 1:
        ramp_in = 0.5 * (1.0 - np.cos(np.linspace(0, np.pi, fade_in, dtype=np.float32)))
        arr[:fade_in] *= ramp_in
    if fade_out > 1:
        ramp_out = 0.5 * (1.0 + np.cos(np.linspace(0, np.pi, fade_out, dtype=np.float32)))
        arr[-fade_out:] *= ramp_out

    return arr


def main() -> None:
    t0 = time.time()
    emit({"type": "status", "status": "loading", "model": "vieneu-v3-turbo"})

    try:
        tts = Vieneu()
        # list_preset_voices trả về (nhãn hiển thị, id giọng); alias như 'Minh Quân' không nằm trong danh sách này
        # nhưng vẫn được infer() resolve (đã kiểm tra khi tổng hợp thử).
        preset_ids = [vid for _label, vid in tts.list_preset_voices()]
        voice_ids = list(dict.fromkeys([DEFAULT_VOICE] + preset_ids))
        case_map = {v.lower(): v for v in voice_ids}
        case_map.update(LEGACY_VOICE_MAP)
        sample_rate = int(getattr(tts, "sample_rate", 24000))
        load_sec = round(time.time() - t0, 2)
        emit({
            "type": "ready",
            "model": "vieneu-v3-turbo",
            "voice": DEFAULT_VOICE,
            "voices": voice_ids,
            "sampleRate": sample_rate,
            "loadSec": load_sec,
        })
    except Exception as exc:
        emit({
            "type": "error",
            "fatal": True,
            "error": str(exc),
            "trace": traceback.format_exc(),
        })
        sys.exit(1)

    def resolve_voice(requested: str) -> str:
        key = (requested or "").strip().lower()
        if not key:
            return DEFAULT_VOICE
        return case_map.get(key, DEFAULT_VOICE)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            text = (req.get("text") or "").strip()
            voice = resolve_voice(req.get("voice") or DEFAULT_VOICE)

            norm_text = clean_and_normalize_text(text)
            if not norm_text:
                emit({"type": "result", "id": req_id, "ok": False, "error": "Filtered empty/spam text"})
                continue

            cache_key = hashlib.sha1(f"vieneu-v1::{voice}::{norm_text}".encode("utf-8")).hexdigest()[:20]
            out_name = f"vieneu_{cache_key}.wav"
            out_path = CACHE_DIR / out_name

            if out_path.is_file() and out_path.stat().st_size > 200:
                emit({
                    "type": "result",
                    "id": req_id,
                    "ok": True,
                    "cached": True,
                    "voice": voice,
                    "file": out_name,
                    "url": f"/tts-cache/{out_name}",
                    "synthSec": 0.0,
                })
                continue

            t_synth = time.time()
            raw_wav = tts.infer(norm_text, voice=voice)
            raw_wav = np.asarray(raw_wav)
            if raw_wav is None or raw_wav.size == 0:
                emit({"type": "result", "id": req_id, "ok": False, "error": "No audio generated"})
                continue

            clean_audio = polish_waveform(raw_wav, sample_rate)
            sf.write(str(out_path), clean_audio, sample_rate, subtype="PCM_16")
            synth_sec = round(time.time() - t_synth, 2)
            duration_sec = round(clean_audio.shape[-1] / float(sample_rate), 2)

            emit({
                "type": "result",
                "id": req_id,
                "ok": True,
                "cached": False,
                "voice": voice,
                "file": out_name,
                "url": f"/tts-cache/{out_name}",
                "synthSec": synth_sec,
                "durationSec": duration_sec,
            })
        except Exception as exc:
            emit({
                "type": "result",
                "id": req_id,
                "ok": False,
                "error": str(exc),
            })


if __name__ == "__main__":
    main()
