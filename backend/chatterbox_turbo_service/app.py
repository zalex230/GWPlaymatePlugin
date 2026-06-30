from __future__ import annotations

import inspect
import io
import os
import time
import traceback
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field


DEFAULT_MODEL_ID = os.getenv("CHATTERBOX_TURBO_MODEL_ID", "ResembleAI/chatterbox-turbo")
DEFAULT_DEVICE = os.getenv("CHATTERBOX_TURBO_DEVICE", "auto")
DEFAULT_VOICE_SAMPLE = os.getenv("CHATTERBOX_TTS_VOICE_SAMPLE", "")
PRELOAD_MODEL = os.getenv("CHATTERBOX_TTS_PRELOAD", "true").strip().lower() not in {"0", "false", "no", "off"}

EXPRESSION_TAGS = {
    "happy": "[happy] [chuckle]",
    "teasing": "[teasing] [chuckle]",
    "flirty": "[softly] [chuckle]",
    "confident": "[confident]",
    "annoyed": "[annoyed]",
    "angry": "[angry]",
    "sad": "[sad] [sigh]",
    "worried": "[worried] [gasp]",
    "embarrassed": "[embarrassed] [sigh]",
}


class SpeechRequest(BaseModel):
    input: str = Field(min_length=1)
    voice_sample_path: str = ""
    response_format: str = "wav"
    expression: str = "neutral"
    exaggeration: float | None = None
    temperature: float | None = None
    paralinguistic_tags: list[str] = Field(default_factory=list)


app = FastAPI(title="GWPlaymate Chatterbox Turbo TTS", version="0.1.0")


def _device() -> str:
    if DEFAULT_DEVICE != "auto":
        return DEFAULT_DEVICE
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


@lru_cache(maxsize=1)
def _load_model() -> Any:
    try:
        import chatterbox.tts_turbo as tts_turbo
    except Exception as exc:
        raise RuntimeError("chatterbox-tts with Turbo support is not installed") from exc

    class _NoopWatermarker:
        def apply_watermark(self, wav: Any, sample_rate: int) -> Any:
            return wav

    if getattr(tts_turbo.perth, "PerthImplicitWatermarker", None) is None:
        tts_turbo.perth.PerthImplicitWatermarker = _NoopWatermarker

    ChatterboxTurboTTS = tts_turbo.ChatterboxTurboTTS
    device = _device()
    try:
        return ChatterboxTurboTTS.from_pretrained(device=device, model_id=DEFAULT_MODEL_ID)
    except TypeError:
        return ChatterboxTurboTTS.from_pretrained(device=device)


@app.on_event("startup")
def preload_model() -> None:
    if not PRELOAD_MODEL:
        return
    started = time.perf_counter()
    try:
        _load_model()
    except Exception:
        traceback.print_exc()
        return
    elapsed = time.perf_counter() - started
    print(f"Chatterbox Turbo model preloaded in {elapsed:.2f}s on {_device()}.", flush=True)


def _render_text(request: SpeechRequest) -> str:
    tags = [tag for tag in request.paralinguistic_tags if tag.strip()]
    if not tags and request.expression in EXPRESSION_TAGS:
        tags = [EXPRESSION_TAGS[request.expression]]
    if not tags:
        return request.input.strip()
    return f"{' '.join(tags)} {request.input.strip()}"


def _generate_wave(request: SpeechRequest) -> tuple[Any, int]:
    model = _load_model()
    text = _render_text(request)
    audio_prompt_path = request.voice_sample_path or DEFAULT_VOICE_SAMPLE
    kwargs: dict[str, Any] = {}
    signature = inspect.signature(model.generate)
    if "audio_prompt_path" in signature.parameters and audio_prompt_path:
        kwargs["audio_prompt_path"] = audio_prompt_path
    if "temperature" in signature.parameters and request.temperature is not None:
        kwargs["temperature"] = request.temperature
    wav = model.generate(text, **kwargs)
    sample_rate = int(getattr(model, "sr", 24000))
    return wav, sample_rate


def _encode_wav(wav: Any, sample_rate: int) -> bytes:
    try:
        import torchaudio
    except Exception as exc:
        raise RuntimeError("torchaudio is required to encode Chatterbox output") from exc

    if hasattr(wav, "detach"):
        wav = wav.detach().cpu()
    if getattr(wav, "ndim", 0) == 1:
        wav = wav.unsqueeze(0)
    buffer = io.BytesIO()
    torchaudio.save(buffer, wav, sample_rate, format="wav", encoding="PCM_S", bits_per_sample=16)
    return buffer.getvalue()


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "gwplaymate-chatterbox-turbo",
        "device": _device(),
        "model_id": DEFAULT_MODEL_ID,
        "model_loaded": _load_model.cache_info().currsize > 0,
        "preload_enabled": PRELOAD_MODEL,
    }


@app.post("/v1/audio/speech")
def speech(request: SpeechRequest) -> Response:
    if request.response_format.strip().lower() not in {"wav", "wave"}:
        raise HTTPException(status_code=400, detail="Only wav response_format is supported by the local Turbo service")
    try:
        wav, sample_rate = _generate_wave(request)
        audio = _encode_wav(wav, sample_rate)
    except RuntimeError as exc:
        traceback.print_exc()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Chatterbox Turbo generation failed: {type(exc).__name__}") from exc
    if not audio:
        raise HTTPException(status_code=502, detail="Chatterbox Turbo returned empty audio")
    return Response(content=audio, media_type="audio/wav")


def main() -> None:
    import uvicorn

    host = os.getenv("CHATTERBOX_TTS_HOST", "127.0.0.1")
    port = int(os.getenv("CHATTERBOX_TTS_PORT", "4123"))
    uvicorn.run("backend.chatterbox_turbo_service.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
