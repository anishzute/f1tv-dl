"""Decode escaped human-readable API text without altering URLs or file paths."""

import html
import re

UNICODE_ESCAPE = re.compile(
    r"(?<!\\)(?:\\u[dD][89aAbB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}"
    r"|\\u[0-9a-fA-F]{4}|\\U[0-9a-fA-F]{8})"
)


def display_text(value):
    def decode(match):
        escaped = match[0]
        if len(escaped) == 12:
            high, low = int(escaped[2:6], 16), int(escaped[8:12], 16)
            codepoint = 0x10000 + ((high - 0xD800) << 10) + low - 0xDC00
        else:
            codepoint = int(escaped[2:], 16)
        if codepoint < 32 or 0xD800 <= codepoint <= 0xDFFF or codepoint > 0x10FFFF:
            return escaped
        return chr(codepoint)

    return html.unescape(UNICODE_ESCAPE.sub(decode, str(value)))
