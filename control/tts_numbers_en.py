"""Spell out units and clock times in English before text goes to Kokoro.

Kokoro's grapheme-to-phoneme step (misaki) reads plain numbers well -- "15%",
"3rd", "1,500" and "Route 12" all come out right -- but it spells unit
abbreviations as letters ("3m" becomes "three em", "1.2 m/s" becomes "one
point two em ess") and has no reading for the colon in a clock time ("3:30"
becomes "three <unknown> thirty").

Announcements must be spoken entirely in the language they were routed to,
so for English text we rewrite those two cases into English words here,
before synthesis. Plain numbers are deliberately left as digits: misaki
already reads them correctly, and rewriting them would only add risk.

The rewrite is idempotent -- its own output ("3 meters", "three thirty")
never matches again -- so running it twice is harmless.
"""

from __future__ import annotations

import re

# (singular, plural) for each unit abbreviation we rewrite.
#
# Deliberately absent: bare "s" (would turn "the 1990s" into seconds), "g"
# (grams versus "5G"), and "%" (misaki already says "percent").
_UNITS: dict[str, tuple[str, str]] = {
    "m/s": ("meter per second", "meters per second"),
    "km/h": ("kilometer per hour", "kilometers per hour"),
    "°C": ("degree Celsius", "degrees Celsius"),
    "cm": ("centimeter", "centimeters"),
    "mm": ("millimeter", "millimeters"),
    "km": ("kilometer", "kilometers"),
    "kg": ("kilogram", "kilograms"),
    "min": ("minute", "minutes"),
    "sec": ("second", "seconds"),
    "ms": ("millisecond", "milliseconds"),
    "m": ("meter", "meters"),
    "V": ("volt", "volts"),
    "h": ("hour", "hours"),
}

# Longest abbreviation first, so "m/s" wins over "m" and "km/h" over "km".
_UNIT_ALTERNATION = "|".join(
    re.escape(unit) for unit in sorted(_UNITS, key=len, reverse=True)
)

# A number (integer or decimal) not glued to a preceding word or decimal
# point, an optional single space, then a unit that ends at a word boundary.
# The trailing (?!\w) is what keeps "3 meters", "3 miles", "3A" and "5G" alone.
_UNIT_RE = re.compile(
    rf"(?<![\w.])(\d+(?:\.\d+)?) ?({_UNIT_ALTERNATION})(?!\w)"
)

# H:MM or HH:MM that is not part of a longer run of digits and colons
# (so "12:30:45" and "123:45" are left alone). The hour range is checked
# in the replacement, which returns the match untouched when out of range.
_CLOCK_RE = re.compile(r"(?<![\d:])(\d{1,2}):([0-5]\d)(?![\d:])")

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
)
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}


def _number_words(n: int) -> str:
    """English words for 0-59, lower-case, tens hyphenated ("forty-five")."""
    if not 0 <= n <= 59:
        raise ValueError(f"_number_words only covers 0-59, got {n}")
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] if ones == 0 else f"{_TENS[tens]}-{_ONES[ones]}"


def _spell_unit(match: re.Match[str]) -> str:
    number, unit = match.group(1), match.group(2)
    singular, plural = _UNITS[unit]
    return f"{number} {singular if number == '1' else plural}"


def _spell_clock(match: re.Match[str]) -> str:
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23:
        return match.group(0)
    hour_words = _number_words(hour)
    if minute == 0:
        words = f"{hour_words} o'clock"
    elif minute < 10:
        words = f"{hour_words} oh {_number_words(minute)}"
    else:
        words = f"{hour_words} {_number_words(minute)}"
    # Keep a glued suffix ("3:30pm") from fusing with the last word.
    following = match.string[match.end():match.end() + 1]
    return f"{words} " if following.isalpha() else words


def normalize_en(text: str) -> str:
    """Return `text` with units after numbers and H:MM clock times spelled out in English."""
    # Clock times first, so the minutes of "3:30h" are never read as "30h".
    text = _CLOCK_RE.sub(_spell_clock, text)
    return _UNIT_RE.sub(_spell_unit, text)
