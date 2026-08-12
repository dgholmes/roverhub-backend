import os
import tempfile

import pytest

from control.tts import (
    MAX_TTS_CHARS,
    TtsError,
    available_engine,
    synthesize_wav,
)

HAS_ENGINE = available_engine() is not None
needs_engine = pytest.mark.skipif(not HAS_ENGINE, reason="no local TTS engine on this host")


def test_capability_reports_a_concrete_answer():
    engine = available_engine()
    assert engine is None or isinstance(engine, str)


# --- input validation ------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
def test_empty_text_is_rejected_before_any_engine_runs(bad):
    with pytest.raises(TtsError, match="no text"):
        synthesize_wav(bad)


def test_overlong_text_is_rejected():
    with pytest.raises(TtsError, match="exceeds"):
        synthesize_wav("a" * (MAX_TTS_CHARS + 1))


def test_none_is_treated_as_empty_rather_than_crashing():
    with pytest.raises(TtsError):
        synthesize_wav(None)


# --- synthesis -------------------------------------------------------------


@needs_engine
def test_produces_a_real_wav():
    wav = synthesize_wav("Attention. Site inspection in progress.")

    # RIFF/WAVE header, so the browser can actually decode it.
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    assert len(wav) > 1000


@needs_engine
def test_longer_text_produces_longer_audio():
    short = synthesize_wav("Stop.")
    long = synthesize_wav("Stop. Please clear the area immediately and wait for instructions.")

    assert len(long) > len(short)


@needs_engine
def test_unicode_text_is_handled():
    # Text reaches the engine as UTF-8 via a file/stdin, not a command line, so
    # non-ASCII must survive intact.
    wav = synthesize_wav("Attention — zone 3 · 안전 점검")

    assert wav[:4] == b"RIFF"


# --- injection -------------------------------------------------------------
#
# The Windows path drives PowerShell. Operator text is passed through a UTF-8 file
# referenced by an environment variable, never interpolated into the command
# string -- this backend has no auth, so that text is fully untrusted.


@needs_engine
def test_shell_metacharacters_are_spoken_not_executed():
    marker = os.path.join(tempfile.gettempdir(), "roverhub-tts-injection-canary.txt")
    if os.path.exists(marker):
        os.remove(marker)

    # If the text were interpolated into the PowerShell command, this would create
    # the canary file. It must instead be treated as words to say.
    hostile = f"'; New-Item -ItemType File -Path '{marker}' -Force; '"
    wav = synthesize_wav(hostile)

    assert not os.path.exists(marker), "operator text was executed as a shell command"
    assert wav[:4] == b"RIFF"


@needs_engine
def test_quotes_and_backticks_do_not_break_synthesis():
    # PowerShell escaping is subtle enough that these are exactly what a
    # hand-quoted implementation gets wrong.
    for text in ['He said "stop"', "it's fine", "back`tick", "$env:PATH", "a & b | c"]:
        assert synthesize_wav(text)[:4] == b"RIFF"
