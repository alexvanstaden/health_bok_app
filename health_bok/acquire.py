"""Transcribe-if-needed: acquire a video's Transcript, captions preferred.

Free YouTube captions are used whenever they exist (PRD #1, user story 9); only
their genuine absence triggers downloading the audio and transcribing it via the
paid Whisper path (user story 10). Whichever path runs is recorded as the
Transcript's `source` so reliability can be judged later (user story 32).

Shared by the two places that acquire a Transcript: the daily pipeline, for a
*new* upload, and backfill approval, for a back-catalogue **Candidate** that was
stored metadata-only and has no archived Transcript yet (issue #15). Backfill is
exactly where Whisper genuinely fires on the owner's command, since a backfill
Candidate never fetched captions at list time (user story 29). Keeping the rule
in one place means both paths agree on captions-vs-Whisper.
"""

from __future__ import annotations

from .models import FetchedTranscript
from .ports import ContentSource, Transcriber


def acquire_transcript(
    video_id: str, *, content_source: ContentSource, transcriber: Transcriber
) -> FetchedTranscript:
    """Get the video's Transcript, preferring free captions over paid Whisper.

    Returns the caption Transcript when YouTube has captions; otherwise downloads
    the audio and transcribes it via Whisper, tagging the result `source="whisper"`
    and carrying the provenance the absent-caption path could not supply.
    """
    captioned = content_source.fetch_transcript(video_id)
    if captioned is not None:
        return captioned
    audio = content_source.fetch_audio(video_id)
    segments = transcriber.transcribe(audio)
    return FetchedTranscript(
        provenance=audio.provenance, segments=segments, source="whisper"
    )
