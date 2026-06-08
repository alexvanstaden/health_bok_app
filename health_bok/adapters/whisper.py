"""OpenAI Whisper adapter for the Transcriber port.

Transcribes a caption-less video's downloaded audio into timestamped segments,
so the daily job can still archive a Transcript when YouTube offers no captions
(PRD #1, user story 10). The audio itself is downloaded by the YouTube
`ContentSource`; this adapter only crosses the OpenAI boundary. `verbose_json`
gives per-segment timestamps, preserving the deep-link capability captions have
(user story 12).
"""

from __future__ import annotations

import io

from ..models import FetchedAudio, TranscriptSegment

_MODEL = "whisper-1"


class WhisperTranscriber:
    """Transcribes audio into timestamped segments via the OpenAI Whisper API."""

    def __init__(self, api_key: str, model: str = _MODEL):
        # Imported lazily so the package imports without the SDK installed.
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self._model = model

    def transcribe(self, audio: FetchedAudio) -> list[TranscriptSegment]:
        # Whisper keys the format off the upload filename's extension, so carry
        # the suffix yt-dlp produced (e.g. ".m4a") through on the in-memory file.
        upload = io.BytesIO(audio.data)
        upload.name = f"{audio.provenance.video_id}{audio.suffix}"
        result = self._client.audio.transcriptions.create(
            model=self._model,
            file=upload,
            response_format="verbose_json",
        )
        return [
            TranscriptSegment(
                text=segment.text.strip(),
                start=float(segment.start),
                duration=float(segment.end) - float(segment.start),
            )
            for segment in (result.segments or [])
        ]
