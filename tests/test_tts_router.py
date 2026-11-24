"""The language router: which synthesiser serves a given announcement.

Korean goes to MeloTTS and English to Kokoro, each in its own container, because
their dependency pins cannot coexist in one Python environment -- MeloTTS pins
`transformers==4.27.4` on numpy 1.26, Kokoro runs transformers 5.x on numpy 2.x.
So the backend talks to both over HTTP and stays free of torch, which is the
whole point: this service is deliberately thin.

These tests run a REAL HTTP server rather than mocking httpx. The failure modes
that matter here -- a service that is down, one that returns a non-200, one that
returns something that is not audio -- are exactly the ones a mock papers over.
"""
from __future__ import annotations

import io
import json
import socket
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from control.tts import (
    MAX_TTS_CHARS,
    TtsError,
    available_engine,
    detect_language,
    synthesize_wav,
)


def make_wav(seconds: float = 0.25, rate: int = 24_000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class _Service:
    """A real synthesiser over real HTTP, on an ephemeral port."""

    def __init__(self, *, status: int = 200, body: bytes | None = None):
        self.status = status
        self.body = make_wav() if body is None else body
        self.requests: list[dict] = []
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    service.requests.append(json.loads(raw))
                except ValueError:
                    service.requests.append({"_raw": raw.decode("utf-8", "replace")})
                payload = (
                    service.body
                    if service.status == 200
                    else b'{"detail": "engine exploded"}'
                )
                self.send_response(service.status)
                self.send_header(
                    "Content-Type",
                    "audio/wav" if service.status == 200 else "application/json",
                )
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # keep the test run quiet
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/synthesize"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def service():
    made: list[_Service] = []

    def make(**kwargs) -> _Service:
        made.append(_Service(**kwargs))
        return made[-1]

    yield make
    for one in made:
        one.close()


def dead_url() -> str:
    """A port nothing is listening on, for the service-is-down case."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/synthesize"


# --- language detection ----------------------------------------------------


def test_hangul_syllables_are_korean():
    assert detect_language("순찰을 시작합니다.") == "ko"


def test_plain_ascii_is_english():
    assert detect_language("Attention. Inspection in progress.") == "en"


def test_mixed_script_is_korean():
    # A Korean announcement containing a loanword or a unit is still Korean, and
    # must not be routed to the English voice on the strength of a stray word.
    assert detect_language("로봇 battery 15% 남았습니다") == "ko"


def test_jamo_alone_is_korean():
    # Compatibility jamo, outside the AC00-D7A3 syllable block.
    assert detect_language("ㄱㄴㄷ") == "ko"


@pytest.mark.parametrize("text", ["15%", "3 - 4", "...", "Zone 3"])
def test_latin_or_textless_input_is_english(text):
    assert detect_language(text) == "en"


# --- routing ---------------------------------------------------------------


def test_korean_text_goes_to_the_korean_service(monkeypatch, service):
    korean, english = service(), service()
    monkeypatch.setenv("TTS_KO_URL", korean.url)
    monkeypatch.setenv("TTS_EN_URL", english.url)

    wav = synthesize_wav("순찰을 시작합니다.", use_cache=False)

    assert wav[:4] == b"RIFF"
    assert len(korean.requests) == 1
    assert korean.requests[0]["text"] == "순찰을 시작합니다."
    assert english.requests == []


def test_english_text_goes_to_the_english_service(monkeypatch, service):
    korean, english = service(), service()
    monkeypatch.setenv("TTS_KO_URL", korean.url)
    monkeypatch.setenv("TTS_EN_URL", english.url)

    synthesize_wav("Attention. Inspection in progress.", use_cache=False)

    assert len(english.requests) == 1
    assert korean.requests == []


def test_no_service_configured_falls_back_to_the_local_engine(monkeypatch):
    monkeypatch.delenv("TTS_KO_URL", raising=False)
    monkeypatch.delenv("TTS_EN_URL", raising=False)
    if available_engine() is None:
        pytest.skip("no local TTS engine on this host")

    assert synthesize_wav("Attention.", use_cache=False)[:4] == b"RIFF"


def test_english_falls_back_locally_when_only_korean_is_configured(monkeypatch, service):
    korean = service()
    monkeypatch.setenv("TTS_KO_URL", korean.url)
    monkeypatch.delenv("TTS_EN_URL", raising=False)
    if available_engine() is None:
        pytest.skip("no local TTS engine on this host")

    assert synthesize_wav("Attention.", use_cache=False)[:4] == b"RIFF"
    assert korean.requests == [], "English must not be sent to the Korean voice"


# --- failure modes ---------------------------------------------------------


def test_a_service_that_is_down_names_itself_in_the_error(monkeypatch):
    monkeypatch.setenv("TTS_KO_URL", dead_url())

    with pytest.raises(TtsError, match="korean"):
        synthesize_wav("순찰을 시작합니다.", use_cache=False)


def test_a_service_error_response_is_surfaced_not_swallowed(monkeypatch, service):
    broken = service(status=500)
    monkeypatch.setenv("TTS_KO_URL", broken.url)

    with pytest.raises(TtsError, match="500"):
        synthesize_wav("순찰을 시작합니다.", use_cache=False)


def test_a_service_returning_something_that_is_not_audio_is_rejected(monkeypatch, service):
    liar = service(body=b"<html>not audio</html>")
    monkeypatch.setenv("TTS_KO_URL", liar.url)

    with pytest.raises(TtsError, match="not audio|WAV"):
        synthesize_wav("순찰을 시작합니다.", use_cache=False)


def test_validation_runs_before_the_service_is_called(monkeypatch, service):
    korean = service()
    monkeypatch.setenv("TTS_KO_URL", korean.url)

    with pytest.raises(TtsError, match="exceeds"):
        synthesize_wav("가" * (MAX_TTS_CHARS + 1), use_cache=False)

    assert korean.requests == [], "overlong text reached the synthesiser"


# --- capability ------------------------------------------------------------


def test_capability_names_the_configured_services(monkeypatch, service):
    korean, english = service(), service()
    monkeypatch.setenv("TTS_KO_URL", korean.url)
    monkeypatch.setenv("TTS_EN_URL", english.url)

    engine = available_engine()

    assert engine is not None
    assert "melotts" in engine and "kokoro" in engine


# --- numbers stay in the announcement's language ----------------------------
#
# The rule: once an announcement is routed to a language, its numbers and units
# are spoken in that language too, never handed to the other voice. Measured on
# 2026-11-23, neither engine manages this alone: MeloTTS's g2pkk reads
# "15 퍼센트" digit by digit (일오) and "3m" as English letters inside Korean,
# and Kokoro's misaki reads "3m" as "three em". So the router spells numbers
# and units out in the routed language before either engine sees them.


def _both(monkeypatch, service):
    korean, english = service(), service()
    monkeypatch.setenv("TTS_KO_URL", korean.url)
    monkeypatch.setenv("TTS_EN_URL", english.url)
    return korean, english


def test_a_korean_number_reaches_the_korean_voice_already_in_korean(monkeypatch, service):
    korean, english = _both(monkeypatch, service)

    synthesize_wav("배터리 잔량은 15 퍼센트입니다.", use_cache=False)

    assert korean.requests[0]["text"] == "배터리 잔량은 십오 퍼센트입니다."
    assert english.requests == []


def test_a_unit_inside_a_korean_sentence_is_spoken_in_korean(monkeypatch, service):
    korean, _ = _both(monkeypatch, service)

    synthesize_wav("3m 앞에 장애물이 있습니다.", use_cache=False)

    assert korean.requests[0]["text"] == "삼 미터 앞에 장애물이 있습니다."


def test_an_english_unit_reaches_the_english_voice_as_a_word(monkeypatch, service):
    korean, english = _both(monkeypatch, service)

    synthesize_wav("Obstacle 3m ahead.", use_cache=False)

    assert english.requests[0]["text"] == "Obstacle 3 meters ahead."
    assert korean.requests == []


def test_digits_never_decide_the_language_the_words_around_them_do(monkeypatch, service):
    korean, english = _both(monkeypatch, service)

    synthesize_wav("Battery at 15%.", use_cache=False)
    synthesize_wav("배터리 15%", use_cache=False)

    assert [r["text"] for r in english.requests] == ["Battery at 15%."]
    assert [r["text"] for r in korean.requests] == ["배터리 십오 퍼센트"]


def test_one_announcement_written_two_ways_is_synthesised_once(monkeypatch, service):
    # The cache keys on what is actually spoken, so "15" and "십오" share an
    # entry -- and a later fix to the normaliser misses the old, wrong audio
    # instead of replaying it from the cache.
    korean, _ = _both(monkeypatch, service)

    first = synthesize_wav("배터리 15%")
    second = synthesize_wav("배터리 십오 퍼센트")

    assert len(korean.requests) == 1
    assert first == second
