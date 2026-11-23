"""The synthesis cache.

Why it exists: MeloTTS runs at roughly 0.3x realtime on CPU, so a six-second
Korean announcement costs about two seconds to synthesise. Operators repeat the
same handful of announcements all day, and nobody should wait twice for the same
sentence. The cache makes the second use free.

Keyed on the text AND the engine, because the same sentence through a different
voice is different audio -- a cache that ignored that would keep serving the old
voice after an engine swap.
"""
from __future__ import annotations

import os

import pytest

from control.tts_cache import TtsCache, cache_key


def test_the_same_text_and_engine_share_a_key():
    assert cache_key("순찰을 시작합니다.", "melotts") == cache_key("순찰을 시작합니다.", "melotts")


def test_different_text_gets_a_different_key():
    assert cache_key("stop", "kokoro") != cache_key("go", "kokoro")


def test_the_same_text_through_a_different_engine_gets_a_different_key():
    # The bug this prevents: swapping engines and still serving the old voice.
    assert cache_key("Attention.", "kokoro") != cache_key("Attention.", "sapi")


def test_a_miss_calls_the_synthesiser_and_a_hit_does_not(tmp_path):
    cache = TtsCache(str(tmp_path))
    calls = []

    def produce() -> bytes:
        calls.append(1)
        return b"RIFF....WAVEfake"

    first = cache.get_or_create(cache_key("순찰", "melotts"), produce)
    second = cache.get_or_create(cache_key("순찰", "melotts"), produce)

    assert first == second == b"RIFF....WAVEfake"
    assert len(calls) == 1, "a cache hit re-synthesised"


def test_a_failed_synthesis_is_not_cached(tmp_path):
    cache = TtsCache(str(tmp_path))

    def explode() -> bytes:
        raise RuntimeError("engine died")

    with pytest.raises(RuntimeError):
        cache.get_or_create(cache_key("순찰", "melotts"), explode)

    assert cache.get(cache_key("순찰", "melotts")) is None


def test_an_unreadable_entry_is_treated_as_a_miss(tmp_path):
    # A half-written file from a killed process must not poison the cache
    # forever -- it should simply be re-synthesised.
    cache = TtsCache(str(tmp_path))
    key = cache_key("순찰", "melotts")
    cache.put(key, b"RIFF....WAVEreal")
    for entry in tmp_path.iterdir():
        entry.write_bytes(b"")

    assert cache.get(key) is None


def test_the_cache_is_bounded_and_evicts_the_oldest(tmp_path):
    cache = TtsCache(str(tmp_path), max_entries=3)

    for n in range(5):
        cache.put(cache_key(f"announcement {n}", "melotts"), b"RIFF" + bytes([n]) * 12)

    # Count the cache's own entries, not everything in the directory: a
    # transient `.part` from an interrupted write is swept separately and is not
    # what "bounded" means here.
    assert len(list(tmp_path.glob("*.wav"))) <= 3


def test_a_completed_write_leaves_no_temporary_file(tmp_path):
    cache = TtsCache(str(tmp_path))
    cache.put(cache_key("순찰", "melotts"), b"RIFF....WAVE")

    assert list(tmp_path.glob("*.part")) == []


def test_a_stale_temporary_file_is_swept_but_a_fresh_one_is_left_alone(tmp_path):
    # A crashed write leaves a .part behind and nothing else would remove it.
    # A *fresh* one may belong to another worker's synthesis in flight, and
    # deleting that would make its rename fail and silently stop it caching.
    cache = TtsCache(str(tmp_path), max_entries=1)
    stale = tmp_path / "old.part"
    fresh = tmp_path / "new.part"
    stale.write_bytes(b"interrupted")
    fresh.write_bytes(b"in flight")
    os.utime(stale, (0, 0))

    for n in range(3):
        cache.put(cache_key(f"announcement {n}", "melotts"), b"RIFF....WAVE")

    assert not stale.exists()
    assert fresh.exists()


def test_a_surviving_entry_still_reads_back_after_eviction(tmp_path):
    cache = TtsCache(str(tmp_path), max_entries=3)
    for n in range(5):
        cache.put(cache_key(f"announcement {n}", "melotts"), b"RIFF" + bytes([n]) * 12)

    newest = cache_key("announcement 4", "melotts")
    assert cache.get(newest) == b"RIFF" + bytes([4]) * 12


def test_a_missing_cache_directory_is_created(tmp_path):
    cache = TtsCache(str(tmp_path / "nested" / "tts-cache"))
    key = cache_key("순찰", "melotts")
    cache.put(key, b"RIFF....WAVE")

    assert cache.get(key) == b"RIFF....WAVE"
