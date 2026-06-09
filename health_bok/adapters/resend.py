"""Resend adapter for the DigestSender port.

Renders the Digest as a simple HTML email and sends it via Resend. The Digest is
only a notification (ADR-0007): each item's primary call-to-action is a deep-link
into the Web App, where the owner actually reviews and approves the Candidate; the
source-video link is secondary. Nothing essential depends on this email.
"""

from __future__ import annotations

import html

from ..models import Digest

_SUBJECT = "Your health & longevity digest"


class ResendDigestSender:
    """Sends the daily Digest email via Resend."""

    def __init__(self, api_key: str, sender: str, recipient: str):
        # Imported lazily so the package imports without the SDK installed.
        import resend

        self._resend = resend
        self._resend.api_key = api_key
        self._sender = sender
        self._recipient = recipient

    def send(self, digest: Digest) -> None:
        self._resend.Emails.send(
            {
                "from": self._sender,
                "to": [self._recipient],
                "subject": _SUBJECT,
                "html": render_html(digest),
            }
        )


def render_html(digest: Digest) -> str:
    """Render the Digest to HTML.

    The Web App is the primary call-to-action per item (ADR-0007): "Review in the
    Web App" deep-links into the review queue, where approval happens. The source
    video stays available as a secondary link. When no Web App URL is configured
    the item degrades gracefully to the source link alone.
    """
    sections = []
    for item in digest.items:
        title = html.escape(item.title)
        url = html.escape(item.url, quote=True)
        body = html.escape(item.summary).replace("\n", "<br>")
        cta = ""
        if item.webapp_url:
            review = html.escape(item.webapp_url, quote=True)
            cta = f'<p><a href="{review}"><strong>Review in the Web App →</strong></a></p>\n'
        sections.append(
            f"<h2>{title}</h2>\n{cta}<p>{body}</p>\n"
            f'<p><a href="{url}">Watch the source video</a></p>'
        )
    return (
        "<h1>New content to review</h1>\n"
        "<p>Review and approve these in the Web App.</p>\n"
        + "\n<hr>\n".join(sections)
    )
