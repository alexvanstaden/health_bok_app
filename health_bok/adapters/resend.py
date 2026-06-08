"""Resend adapter for the DigestSender port.

Renders the Digest as a simple HTML email — one section per item, each linking
to its source video — and sends it via Resend.
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
    """Render the Digest to HTML. Each item links to its source video."""
    sections = []
    for item in digest.items:
        title = html.escape(item.title)
        url = html.escape(item.url, quote=True)
        body = html.escape(item.summary).replace("\n", "<br>")
        sections.append(
            f'<h2><a href="{url}">{title}</a></h2>\n<p>{body}</p>'
        )
    return (
        "<h1>Today's new videos</h1>\n" + "\n<hr>\n".join(sections)
    )
