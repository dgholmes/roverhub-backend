"""Spell out numbers and units in Hangul before Korean text goes to MeloTTS.

MeloTTS turns digits into Korean with the g2pkk library, and measured on
2026-11-23 it misreads the numbers announcements use most:

- "15 퍼센트" came out 일오 퍼센트 (digit by digit) instead of 십오 퍼센트;
- "사람 3명이" came out 삼명 instead of 세명 (counters take native numbers);
- "0.5 미터" came out 영.오 -- the decimal point was never spoken;
- "3m 앞에" came out as garbage (사메 마페), and "1.2 m/s" mixed English
  letters into Korean speech (일.이 엠/에스);
- "경로 12번" came out 열두번 ("twelve times") instead of 십이번 (a label).

Announcements must be spoken entirely in the language they were routed to,
numbers and units included, so for Korean text we rewrite every number --
and any unit written after it -- into Hangul words here, before synthesis.
That leaves g2pkk nothing numeric to misread.

Sino-Korean (일 이 삼 ...) is the default. Native Korean (한 두 세 ...) is used
for 1-99 before counters that take it (명, 개, 대, 시간 ...), and for hours 1-12
before 시 -- but only when the counter is a whole word apart from particles
(3명이, 3시경), never when it is merely the first syllable of another word
(15 잔량, 2 대기). Decimals, clock times, ISO dates, phone numbers, negatives
and Latin units (m, km/h, %, ...) each have their own reading below.

The rewrite is idempotent: its output contains no ASCII digits, so a second
pass finds nothing to change.
"""

from __future__ import annotations

import re

_SINO_DIGITS = "영일이삼사오육칠팔구"
# Digit-by-digit readings (phone numbers, codes) say 공 for zero.
_CODE_DIGITS = "공일이삼사오육칠팔구"

_PLACES = ("", "십", "백", "천")  # within a four-digit group
_GROUPS = ("", "만", "억", "조", "경")  # one per four-digit group

# Attributive native forms, as used before a counter.
_NATIVE_ONES = ("", "한", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉")
_NATIVE_TENS = ("", "열", "스물", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔")

# Counters that take native numbers (1-99).
_NATIVE_COUNTERS = frozenset({
    "명", "개", "대", "마리", "살", "권", "잔", "곳", "가지", "바퀴",
    "시간", "사람", "장", "군데", "달", "번째",
})
# Words that start with a native counter but read in sino. Listing them lets
# the longest-match rule pick them first: 3개월 is 삼개월, not 세개월.
_SINO_OVERRIDES = frozenset({"개월", "개국", "달러"})
# Handled specially: 시 (hours) and 월 (months).
_COUNTERS = _NATIVE_COUNTERS | _SINO_OVERRIDES | {"시", "월"}
# Sino counters. They change nothing about how a number is read, but a
# zero-padded number before one is padding, not a code: 08일 is 팔일.
_SINO_COUNTERS = frozenset({
    "년", "일", "분", "초", "층", "번", "호", "회", "차", "주", "원", "도",
})


def _counter_regex(counters: frozenset[str]) -> re.Pattern[str]:
    # Longest first, so 개월 wins over 개 and 시간 over 시.
    return re.compile(
        "[ ]?(" + "|".join(sorted(counters, key=len, reverse=True)) + ")"
    )


_COUNTER_RE = _counter_regex(_COUNTERS)
_PADDED_COUNTER_RE = _counter_regex(_COUNTERS | _SINO_COUNTERS)

# Particles and endings that may follow a counter in the same word. A counter
# is only a counter if the rest of its word is made of these: 3명이, 3명에서는
# and 3시경 are counted, but 15 잔량, 2 대기 and 3명령 are other words that
# merely start with a counter syllable, and read in sino.
_SUFFIXES = tuple(sorted(
    (
        "이", "가", "은", "는", "을", "를", "의", "에", "에서", "에게", "께",
        "한테", "으로", "로", "와", "과", "도", "만", "까지", "부터", "씩",
        "째", "쯤", "경", "반", "정도", "가량", "이나", "나", "마다", "뿐",
        "밖에", "이상", "이하", "미만", "내외", "동안", "만큼", "입니다",
        "이다", "이며", "이고", "이에요", "예요", "이야", "야", "인", "이라",
        "이라고", "이지만", "이면",
    ),
    key=len,
    reverse=True,
))
_HANGUL_RUN_RE = re.compile("[가-힣]*")

# Standard irregular month readings (6월 is 유월, 10월 is 시월). Only the number
# is replaced -- the 월 stays in the text. Every other month is plain sino.
_MONTHS = {6: "유", 10: "시"}

# Latin unit -> (word placed before the number, word placed after it).
# Speed units move in front as 초속 / 시속: "1.2 m/s" -> "초속 일 점 이 미터".
_UNITS: dict[str, tuple[str, str]] = {
    "m/s": ("초속 ", "미터"),
    "km/h": ("시속 ", "킬로미터"),
    "cm": ("", "센티미터"),
    "mm": ("", "밀리미터"),
    "km": ("", "킬로미터"),
    "kg": ("", "킬로그램"),
    "°C": ("", "도"),
    "℃": ("", "도"),
    "m": ("", "미터"),
    "V": ("", "볼트"),
    "%": ("", "퍼센트"),
}
_UNIT_ALTERNATION = "|".join(
    re.escape(unit) for unit in sorted(_UNITS, key=len, reverse=True)
)

# One regex, four kinds of number token, tried in this order at each place:
#   clock  -- H:MM or HH:MM;
#   date   -- an ISO date YYYY-MM-DD (range-checked in the replacement);
#   code   -- hyphen-joined digit groups (phone numbers), read digit by digit;
#   number -- an optional minus sign (only at the start or after whitespace),
#             an integer with optional thousands separators, an optional
#             decimal part, then an optional Latin unit that ends at a word
#             boundary (so "3 meters" keeps its "m").
# [0-9] rather than \d: \d also matches non-ASCII digits, which we leave alone.
_TOKEN_RE = re.compile(
    rf"""
    (?P<clock>(?<![0-9])(?P<hour>[0-9]{{1,2}}):(?P<minute>[0-9]{{2}})(?![0-9]))
    | (?P<date>(?<![0-9])(?P<year>[0-9]{{4}})-(?P<month>[0-9]{{2}})-(?P<day>[0-9]{{2}})
        (?![0-9]|-[0-9]))
    | (?P<code>(?<![0-9])[0-9]+(?:-[0-9]+)+)
    | (?P<number>
        (?P<minus>(?:(?<=\s)|^)-)?
        (?P<int>[0-9]{{1,3}}(?:,[0-9]{{3}})+|[0-9]+)
        (?:\.(?P<frac>[0-9]+))?
        (?:[ ]?(?P<unit>{_UNIT_ALTERNATION})(?![A-Za-z]))?
      )
    """,
    re.VERBOSE,
)

_HAS_DIGIT_RE = re.compile("[0-9]")


def _sino_below_10000(n: int) -> str:
    """1-9999 in sino-Korean, without 일 before 십/백/천 (1000 -> 천)."""
    words = []
    for place in range(3, -1, -1):
        digit = n // 10**place % 10
        if digit == 0:
            continue
        if digit == 1 and place > 0:
            words.append(_PLACES[place])
        else:
            words.append(_SINO_DIGITS[digit] + _PLACES[place])
    return "".join(words)


def sino(n: int) -> str:
    """A non-negative integer in sino-Korean: 15 -> 십오, 110000 -> 십일만."""
    if n == 0:
        return "영"
    if n >= 10 ** (4 * len(_GROUPS)):
        # Beyond 경: no announcement says this; read it digit by digit.
        return "".join(_SINO_DIGITS[int(d)] for d in str(n))
    parts = []
    for index in range(len(_GROUPS)):
        group = n // 10 ** (4 * index) % 10000
        if group == 0:
            continue
        # 10000 is 만, not 일만; 일억 and 일조 keep their 일.
        words = "" if (group == 1 and index == 1) else _sino_below_10000(group)
        parts.append(words + _GROUPS[index])
    return "".join(reversed(parts))


def native(n: int) -> str:
    """1-99 in attributive native Korean: 3 -> 세, 20 -> 스무, 21 -> 스물한."""
    if n == 20:
        return "스무"
    tens, ones = divmod(n, 10)
    return _NATIVE_TENS[tens] + _NATIVE_ONES[ones]


def _hour(n: int) -> str:
    """An hour before 시: native for 1-12, sino for 0 and 13-24."""
    return native(n) if 1 <= n <= 12 else sino(n)


def _decimal(integer: str, fraction: str) -> str:
    """0.05 -> 영 점 영오: sino integer part, then each fractional digit."""
    return sino(int(integer)) + " 점 " + "".join(_SINO_DIGITS[int(d)] for d in fraction)


def _code(digits: str) -> str:
    """Digit by digit with 공 for zero: 007 -> 공공칠."""
    return "".join(_CODE_DIGITS[int(d)] for d in digits)


def _clock(hour: int, minute: int) -> str:
    """3:30 -> 세 시 삼십 분; 3:00 -> 세 시."""
    if hour > 24 or minute > 59:
        # Not a time of day (a ratio, a score); read both sides as numbers.
        return f"{sino(hour)}:{sino(minute)}"
    words = f"{_hour(hour)} 시"
    if minute:
        words += f" {sino(minute)} 분"
    return words


def _date(year: int, month: int, day: int) -> str | None:
    """2026-06-10 -> 이천이십육년 유월 십일; None when it is not a real date."""
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{sino(year)}년 {_MONTHS.get(month, sino(month))}월 {sino(day)}일"


def _only_suffixes(rest: str) -> bool:
    """True if `rest` is empty or made entirely of particles and endings.

    Strips the longest matching suffix from the front until nothing is left,
    so combinations work: 에서는 is 에서 + 는, 까지만 is 까지 + 만.
    """
    while rest:
        for suffix in _SUFFIXES:
            if rest.startswith(suffix):
                rest = rest[len(suffix):]
                break
        else:
            return False
    return True


def _counter_after(text: str, end: int, counter_re: re.Pattern[str]) -> str | None:
    """The counter written right after a number, if it is a whole counter.

    The longest matching counter is taken, then the rest of its Hangul word
    must be particles or endings (see _SUFFIXES); otherwise the number is
    followed by some other word that merely starts like a counter.
    """
    match = counter_re.match(text, end)
    if not match:
        return None
    rest = _HANGUL_RUN_RE.match(text, match.end()).group()
    return match.group(1) if _only_suffixes(rest) else None


def _counted(n: int, text: str, end: int) -> str:
    """An integer read according to the counter, if any, that follows it."""
    counter = _counter_after(text, end, _COUNTER_RE)
    if counter == "시":
        return _hour(n)
    if counter == "월":
        return _MONTHS.get(n, sino(n))
    if counter in _NATIVE_COUNTERS:
        if counter == "번째" and n == 1:
            return "첫"
        if 1 <= n <= 99:
            return native(n)
    return sino(n)


def _replace(match: re.Match[str]) -> str:
    if match.group("clock"):
        return _clock(int(match.group("hour")), int(match.group("minute")))
    if match.group("date"):
        date = _date(
            int(match.group("year")), int(match.group("month")), int(match.group("day"))
        )
        if date is not None:
            return date
        return " ".join(_code(group) for group in match.group("date").split("-"))
    if match.group("code"):
        return " ".join(_code(group) for group in match.group("code").split("-"))

    text, end = match.string, match.end()
    raw_int = match.group("int")
    digits = raw_int.replace(",", "")
    fraction = match.group("frac")
    minus = match.group("minus")
    unit = match.group("unit")
    # A leading zero makes a code (007) -- unless a unit or counter follows,
    # in which case the zeros are padding (05시, 08일) and the number reads
    # normally.
    is_code = (
        len(digits) > 1
        and digits[0] == "0"
        and "," not in raw_int
        and not unit
        and _counter_after(text, end, _PADDED_COUNTER_RE) is None
    )

    if fraction is not None:
        words = _decimal(digits, fraction)
    elif is_code:
        words = _code(digits)
    elif minus or unit:
        words = sino(int(digits))
    else:
        words = _counted(int(digits), text, end)

    if minus:
        words = "마이너스 " + words
    if unit:
        before, after = _UNITS[unit]
        return f"{before}{words} {after}"
    return words


def normalize_ko(text: str) -> str:
    """Return `text` with every number, and any unit written after it, spelled out in Hangul."""
    if not _HAS_DIGIT_RE.search(text):
        return text
    return _TOKEN_RE.sub(_replace, text)
