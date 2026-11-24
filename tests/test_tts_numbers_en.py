"""Tests for control.tts_numbers_en.normalize_en.

Kokoro's misaki G2P (measured 2026-11-23) reads plain numbers well but spells
unit abbreviations as letters -- "3m" came out "three em", "1.2 m/s" came out
"one point two em ess" -- and has no reading for the colon in "3:30". The
backend therefore spells units and clock times out in English before the text
reaches Kokoro, and leaves plain numbers as digits.
"""

import pytest

from control.tts_numbers_en import normalize_en

CASES = [
    # Units after a number.
    ("Obstacle 3m ahead.", "Obstacle 3 meters ahead."),
    ("3 m", "3 meters"),
    ("1 m", "1 meter"),
    ("1.5 m", "1.5 meters"),
    ("50cm", "50 centimeters"),
    ("2km", "2 kilometers"),
    ("Speed 1.2 m/s", "Speed 1.2 meters per second"),
    ("30km/h", "30 kilometers per hour"),
    ("20kg", "20 kilograms"),
    ("25°C", "25 degrees Celsius"),
    ("1°C", "1 degree Celsius"),
    ("24V", "24 volts"),
    ("3h", "3 hours"),
    ("1h", "1 hour"),
    ("15min", "15 minutes"),
    ("30sec", "30 seconds"),
    ("200ms", "200 milliseconds"),
    # Clock times.
    ("Leaving at 3:30.", "Leaving at three thirty."),
    ("3:05", "three oh five"),
    ("3:00", "three o'clock"),
    ("15:30", "fifteen thirty"),
    ("12:00", "twelve o'clock"),
    ("9:45", "nine forty-five"),
    # Left alone: misaki already reads these, or they are not units.
    ("Battery 15%", "Battery 15%"),
    ("Route 12", "Route 12"),
    ("the 1990s", "the 1990s"),
    ("5G network", "5G network"),
    ("Room 3A", "Room 3A"),
    ("3 meters", "3 meters"),
    ("3 miles", "3 miles"),
    ("I am here", "I am here"),
    ("", ""),
]


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_normalize_en(text, expected):
    assert normalize_en(text) == expected


@pytest.mark.parametrize("text", [text for text, _ in CASES])
def test_normalize_en_is_idempotent(text):
    once = normalize_en(text)
    assert normalize_en(once) == once


@pytest.mark.parametrize(
    "text",
    [
        "Battery 15%",
        "Route 12",
        "the 1990s",
        "5G network",
        "Room 3A",
        "3 meters",
        "3 miles",
        "I am here",
        "",
        "  Mixed   spacing,\tpunctuation!  ",
        "12:30:45",
        "25:30",
        "3:7",
    ],
)
def test_text_with_nothing_to_rewrite_is_returned_unchanged(text):
    assert normalize_en(text) == text
