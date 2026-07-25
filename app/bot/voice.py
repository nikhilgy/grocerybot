"""Voice note download + speech-to-text transcription via Groq Whisper."""
from __future__ import annotations

import logging

from groq import AsyncGroq
from telegram import File

from app import config

logger = logging.getLogger(__name__)

_groq_client = AsyncGroq(api_key=config.GROQ_API_KEY) if config.GROQ_API_KEY else None


async def download_voice(telegram_file: File) -> bytes:
    """Downloads a Telegram voice note (OGG/Opus) as raw bytes."""
    byte_array = await telegram_file.download_as_bytearray()
    return bytes(byte_array)


async def transcribe(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    """Transcribes an OGG/Opus voice note to text via Groq's Whisper endpoint.

    Telegram voice notes are OGG, which Groq's transcription API accepts
    natively — no conversion needed.
    """
    if _groq_client is None:
        raise RuntimeError(
            "No speech-to-text backend configured. Set GROQ_API_KEY to enable "
            "voice note transcription."
        )

    response = await _groq_client.audio.transcriptions.create(
        model="whisper-large-v3-turbo",
        file=(filename, audio_bytes),
    )
    return response.text.strip()
