"""Shared test fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_tts_cache(tmp_path, monkeypatch):
    """Points the synthesis cache at a throwaway directory for every test.

    Without this, any test that synthesises writes a real WAV into
    `data/tts-cache/` in the working tree, and a later test asserting that a
    synthesiser was called would silently get a hit from a previous run instead.
    """
    monkeypatch.setenv("TTS_CACHE_DIR", str(tmp_path / "tts-cache"))
