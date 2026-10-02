#!/usr/bin/env python3
"""Docker's health check. Exit 0 = the page answers, anything else = it does not.

🔴 WHY THIS IS A FILE AND NOT A ONE-LINER

It used to be a one-liner in the Dockerfile, and it had `\n` written INSIDE a
double-quoted shell string:

    CMD python3 -c "import sys\ntry:\n sys.exit(...)"

Docker passes that to `sh -c`, and a double-quoted shell string does not turn
`\n` into a newline — Python received the two characters backslash and n and
answered `SyntaxError: unexpected character after line continuation character`.
Every container since 3.0.0 reported „unhealthy" from its very first check, for
six months, and the check never looked at anything. Der Besitzer saw it in his
Container Manager with a failing streak of 392.

A `try:` block cannot live on one line, so the one-liner needed newlines it
could not have. A file can have as many as it likes — and it can be read, and
tested, which a string in a Dockerfile cannot.

🔑 The port is asked, not assumed: `POSTWACHE_WEB_PORT` is what `post_web.py`
itself reads, so a changed port moves the check with it.
"""
import os
import sys
import urllib.error
import urllib.request

PORT = os.environ.get("POSTWACHE_WEB_PORT", "8110")
ZIEL = "http://127.0.0.1:%s/api/gesundheit" % PORT


def gesund() -> bool:
    try:
        with urllib.request.urlopen(ZIEL, timeout=4) as antwort:
            return antwort.status == 200
    except urllib.error.HTTPError as e:
        # 🔑 Belt and braces: this door is deliberately NOT behind the page
        #    lock, so 401 should never happen. If a later version ever puts it
        #    there, „please sign in" is the server working as configured — not
        #    a reason to restart a healthy container.
        return e.code == 401
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(0 if gesund() else 1)
