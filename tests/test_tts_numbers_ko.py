"""Tests for control.tts_numbers_ko.normalize_ko.

MeloTTS reads Korean digits through g2pkk, which (measured 2026-11-23) gets
common announcement numbers wrong: "15 퍼센트" came out 일오 (digit by digit),
"3명" came out 삼명 instead of 세명, "0.5" lost its point (영.오), "3m 앞에"
came out as garbage (사메 마페), "1.2 m/s" mixed English letters into Korean
(일.이 엠/에스), and "경로 12번" came out 열두번 ("twelve times"). The backend
therefore spells every number and unit out in Hangul before the text reaches
MeloTTS, so g2pkk has nothing numeric left to misread.
"""

import pytest

from control.tts_numbers_ko import normalize_ko

CASES = [
    # Sino-Korean, the default.
    ("15", "십오"),
    ("배터리 잔량은 15 퍼센트입니다.", "배터리 잔량은 십오 퍼센트입니다."),
    ("배터리 15%", "배터리 십오 퍼센트"),
    ("병동 3층 통로", "병동 삼층 통로"),
    ("2026년 11월 23일", "이천이십육년 십일월 이십삼일"),
    ("6월 10일", "유월 십일"),
    ("10월", "시월"),
    ("1,500원", "천오백원"),
    ("10,000원", "만원"),
    ("15000", "만오천"),
    ("110000", "십일만"),
    ("100000000", "일억"),
    ("경로 12번", "경로 십이번"),
    ("30분", "삼십분"),
    ("10", "십"),
    ("100", "백"),
    ("1000", "천"),
    ("1001", "천일"),
    ("2020", "이천이십"),
    ("0", "영"),
    # Native Korean before counters that take it, 1-99.
    ("로봇 2대가 대기 중입니다.", "로봇 두대가 대기 중입니다."),
    ("사람 3명이 감지되었습니다.", "사람 세명이 감지되었습니다."),
    ("1명", "한명"),
    ("4개", "네개"),
    ("11개", "열한개"),
    ("12개", "열두개"),
    ("20명", "스무명"),
    ("21명", "스물한명"),
    ("99개", "아흔아홉개"),
    ("100명", "백명"),
    ("3 명", "세 명"),
    ("3개월", "삼개월"),
    ("3시간", "세시간"),
    ("24시간", "스물네시간"),
    ("3번째", "세번째"),
    ("1번째", "첫번째"),
    ("3번 출구", "삼번 출구"),
    # Hours before 시: native 1-12, sino 0 and 13-24.
    ("3시에 출발합니다.", "세시에 출발합니다."),
    ("12시", "열두시"),
    ("15시", "십오시"),
    ("0시", "영시"),
    ("3시 30분", "세시 삼십분"),
    # Clock times.
    ("3:30", "세 시 삼십 분"),
    ("3:05", "세 시 오 분"),
    ("3:00", "세 시"),
    ("15:30", "십오 시 삼십 분"),
    # Decimals.
    ("0.5 미터 앞", "영 점 오 미터 앞"),
    ("3.14", "삼 점 일사"),
    ("0.05", "영 점 영오"),
    ("1.2", "일 점 이"),
    # Latin units after a number.
    ("3m 앞에 장애물", "삼 미터 앞에 장애물"),
    ("3 m", "삼 미터"),
    ("50cm", "오십 센티미터"),
    ("2km", "이 킬로미터"),
    ("속도 1.2 m/s", "속도 초속 일 점 이 미터"),
    ("30km/h", "시속 삼십 킬로미터"),
    ("20kg", "이십 킬로그램"),
    ("25°C", "이십오 도"),
    ("24V", "이십사 볼트"),
    ("15%", "십오 퍼센트"),
    # Codes and phone numbers, digit by digit with 공.
    ("007", "공공칠"),
    ("010-1234-5678", "공일공 일이삼사 오육칠팔"),
    # Negative numbers.
    ("-3", "마이너스 삼"),
    # A counter must be a whole word apart from particles and endings;
    # a word that merely starts with a counter syllable reads in sino.
    ("배터리 15 잔량", "배터리 십오 잔량"),
    ("로봇 2 대기 중", "로봇 이 대기 중"),
    ("3명령", "삼명령"),
    ("3 장애물", "삼 장애물"),
    ("3명입니다", "세명입니다"),
    ("3명씩", "세명씩"),
    ("3명에서는", "세명에서는"),
    ("3시경", "세시경"),
    ("3시반", "세시반"),
    ("3시부터", "세시부터"),
    ("3개를", "세개를"),
    ("5살이에요", "다섯살이에요"),
    # Leading zeros before a counter are padding, not a code.
    ("05시", "다섯시"),
    ("05월", "오월"),
    ("08일", "팔일"),
    # ISO dates read as dates; out-of-range ones stay a code.
    ("2026-11-23", "이천이십육년 십일월 이십삼일"),
    ("2026-06-10", "이천이십육년 유월 십일"),
    ("2026-13-40", "이공이육 일삼 사공"),
    # Text around numbers is preserved.
    ("LiDAR 2개", "LiDAR 두개"),
    ("순찰을 시작합니다.", "순찰을 시작합니다."),
    ("", ""),
]


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_normalize_ko(text: str, expected: str) -> None:
    assert normalize_ko(text) == expected


@pytest.mark.parametrize("text", [text for text, _ in CASES])
def test_normalize_ko_is_idempotent(text: str) -> None:
    once = normalize_ko(text)
    assert normalize_ko(once) == once


@pytest.mark.parametrize(
    "text",
    [
        "",
        "순찰을 시작합니다.",
        "LiDAR 센서 점검, 경로 확인 - 완료!",
        "  공백  과\t탭\n줄바꿈  ",
        "-",
    ],
)
def test_text_without_digits_is_unchanged(text: str) -> None:
    assert normalize_ko(text) is text
