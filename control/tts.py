"""Text-to-speech for spoken announcements, synthesised locally.

Returns WAV. The browser then decodes it to the robot's 24kHz/16-bit/mono PCM
through exactly the same path an uploaded file takes (see
roverhub-frontend/src/lib/audioPcm.ts), so everything downstream -- the clip
store, the edge-side cache, volume, and DDS streaming -- is unchanged. This
module's only job is text in, WAV out.

Local rather than a cloud API: no key to manage (this backend has no auth or
secret handling at all), no per-character cost, and announcements keep working
when the site's uplink does not. The voice is robotic, which is acceptable for
"attention, inspection in progress" and is the trade that was chosen knowingly.

Engines, in order of preference:

* **MeloTTS (Korean) and Kokoro-82M (English)**, each in its own container,
  reached over HTTP at ``TTS_KO_URL`` / ``TTS_EN_URL``. Chosen by ear from the
  bake-off in ``tools/tts_bakeoff/`` -- both permissively licensed, both CPU-only,
  no GPU anywhere in the deployment.

  Two services rather than one because their dependency pins cannot coexist in a
  single Python environment: MeloTTS pins ``transformers==4.27.4`` on numpy 1.26
  while Kokoro runs transformers 5.x on numpy 2.x. Over HTTP rather than imported
  because this backend is deliberately thin, and neither torch nor CUDA belongs
  in its venv.

  Sample rate is not this module's problem. MeloTTS emits 44.1kHz and Kokoro
  24kHz, and the browser resamples both to the robot's 24kHz mono through
  ``OfflineAudioContext`` (and peak-normalises them) on the way to PCM, exactly
  as it does for an uploaded MP3.

* **Windows SAPI** via PowerShell's ``System.Speech``. Present on every Windows
  install with no pip dependency -- verified on the dev host (2 voices).
* **espeak-ng / espeak** on Linux, for when the backend moves off a dev laptop.

The last two remain because a dev host has no containers running, and because an
announcement in a robotic voice beats no announcement. They are a fallback, not
a tier: with no service configured, behaviour is exactly what it was before.
They are deliberately not a pip dependency (pyttsx3 and friends wrap exactly
these two engines anyway) and deliberately not audioop-based: this backend runs
Python 3.13, where audioop was removed.

⚠️ The local fallback never calls ``SelectVoice``, so it speaks every language in
the host's default voice. On this Korean-locale dev machine that means English
read by ``Microsoft Heami``; on a Linux/espeak host it would be Korean that
breaks. That is a property of the fallback, and the reason the services exist.
"""
from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

from control.tts_cache import TtsCache, cache_key

logger = logging.getLogger("backend.control.tts")

# Long enough for a site announcement, short enough to bound synthesis time and
# the resulting clip. The audio-clip upload cap (10MB) is the real backstop.
MAX_TTS_CHARS = 1_000

# Synthesis is a subprocess or an HTTP call; if an engine wedges we must not hold
# a request open. /tts is synchronous, so this is what an operator waits for.
SYNTH_TIMEOUT_S = 30

# Which container serves which language, and the engine each one runs. The names
# are what /tts/capability reports and what the cache is keyed on, so changing
# one deliberately invalidates every cached clip from that voice.
SERVICE_ENV = {"ko": "TTS_KO_URL", "en": "TTS_EN_URL"}
SERVICE_ENGINE = {"ko": "melotts", "en": "kokoro"}
LANGUAGE_NAME = {"ko": "korean", "en": "english"}

# Hangul: the syllable block, conjoining jamo, compatibility jamo, and the two
# extended-jamo blocks. Matching any of these means the text is Korean.
_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏ꥠ-꥿ힰ-퟿]")

# Beside data/audio-clips, for the same reason: /tmp is wiped on reboot and the
# first announcement of the morning should not pay for synthesis again.
DEFAULT_TTS_CACHE_DIR = str(Path(__file__).resolve().parents[1] / "data" / "tts-cache")


class TtsError(RuntimeError):
    """Synthesis failed. Surfaced to the operator rather than returning silence."""


class TtsUnavailable(TtsError):
    """No usable engine on this host."""


def detect_language(text: str) -> str:
    """"ko" if the announcement contains any Hangul, otherwise "en".

    Deliberately asymmetric: a Korean announcement routinely contains Latin
    loanwords, units and robot names ("로봇 battery 15%"), so the presence of
    Hangul decides and a stray English word cannot drag the sentence to the
    English voice. The reverse mistake -- English read by the Korean voice -- is
    exactly the production bug this replaces, so it is the one worth preventing.
    """
    return "ko" if _HANGUL.search(text or "") else "en"


def _service_url(language: str) -> str | None:
    """The configured synthesiser for a language, or None.

    Read from the environment on every call rather than captured at import, so a
    service can be pointed somewhere else without restarting the backend -- and
    so tests can configure one without reloading the module.
    """
    return (os.environ.get(SERVICE_ENV[language]) or "").strip() or None


def _local_engine() -> str | None:
    """The host's own fallback engine, or None."""
    if platform.system() == "Windows":
        return "sapi" if shutil.which("powershell") else None
    for binary in ("espeak-ng", "espeak"):
        if shutil.which(binary):
            return binary
    return None


def available_engine() -> str | None:
    """What this host would speak with, or None if nothing can.

    A summary across both languages, because /tts/capability is asked once and
    has no text to route on: "melotts+kokoro" when both services are configured,
    "melotts+sapi" when only Korean is and the host can fall back, "sapi" alone
    when neither service is.
    """
    names = [SERVICE_ENGINE[lang] for lang in ("ko", "en") if _service_url(lang)]
    local = _local_engine()
    if local and len(names) < len(SERVICE_ENGINE):
        names.append(local)
    return "+".join(names) or None


def _cache() -> TtsCache:
    directory = (os.environ.get("TTS_CACHE_DIR") or "").strip() or DEFAULT_TTS_CACHE_DIR
    try:
        max_entries = int(os.environ.get("TTS_CACHE_MAX_ENTRIES") or 0)
    except ValueError:
        max_entries = 0
    return TtsCache(directory, max_entries) if max_entries > 0 else TtsCache(directory)


def synthesize_wav(text: str, *, use_cache: bool = True) -> bytes:
    """Synthesises `text` and returns WAV bytes.

    Routes on the text's own script -- Korean to MeloTTS, English to Kokoro --
    and falls back to the host's local engine for a language with no service
    configured. Validation happens first, so overlong or empty text never
    reaches a synthesiser.

    `use_cache` exists for tests that need to observe a synthesiser actually
    being called; the live path always wants the cache.
    """
    text = (text or "").strip()
    if not text:
        raise TtsError("no text to speak")
    if len(text) > MAX_TTS_CHARS:
        raise TtsError(f"text exceeds {MAX_TTS_CHARS} characters")

    language = detect_language(text)
    url = _service_url(language)

    if url is not None:
        engine = SERVICE_ENGINE[language]

        def produce() -> bytes:
            return _synthesize_remote(url, text, language)
    else:
        local = _local_engine()
        if local is None:
            raise TtsUnavailable(
                f"nothing can speak {LANGUAGE_NAME[language]} on this host. Set "
                f"{SERVICE_ENV[language]} to a voice container, or install a local "
                "fallback (apt install espeak-ng on Linux; on Windows SAPI is "
                "built in)."
            )
        engine = local

        def produce() -> bytes:
            return _synthesize_local(engine, text)

    if not use_cache:
        return produce()
    return _cache().get_or_create(cache_key(text, engine), produce)


def _synthesize_remote(url: str, text: str, language: str) -> bytes:
    """Asks a voice container for this announcement.

    Every failure is turned into a TtsError naming the language, because what
    reaches the operator otherwise is a bare connection error that says nothing
    about which of the two voices is down.
    """
    engine = SERVICE_ENGINE[language]
    name = LANGUAGE_NAME[language]
    try:
        response = httpx.post(
            url,
            json={"text": text, "language": language},
            timeout=SYNTH_TIMEOUT_S,
        )
    except httpx.RequestError as exc:
        logger.error("tts service %s unreachable: %s", url, exc)
        raise TtsError(
            f"the {name} voice service ({engine}) is not reachable at {url}: {exc}"
        ) from exc

    if response.status_code != 200:
        detail = response.text.strip()[:200]
        logger.error("tts service %s returned %s: %s", url, response.status_code, detail)
        raise TtsError(
            f"the {name} voice service ({engine}) returned HTTP "
            f"{response.status_code}: {detail or 'no detail'}"
        )

    wav = response.content
    # A 200 carrying an error page would otherwise reach the browser's decoder
    # and fail there, far from the cause -- and get cached on the way.
    if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise TtsError(f"the {name} voice service ({engine}) did not return WAV audio")
    return wav


def _synthesize_local(engine: str, text: str) -> bytes:
    """The host's own engine, for a language with no service configured."""
    # A real temp directory rather than a single NamedTemporaryFile: the engines
    # want to open the output path themselves, and on Windows an open handle from
    # this process blocks that.
    with tempfile.TemporaryDirectory(prefix="roverhub-tts-") as work:
        wav_path = os.path.join(work, "speech.wav")
        if engine == "sapi":
            _synthesize_sapi(text, wav_path, work)
        else:
            _synthesize_espeak(engine, text, wav_path)

        if not os.path.isfile(wav_path) or os.path.getsize(wav_path) == 0:
            raise TtsError("the speech engine produced no audio")
        with open(wav_path, "rb") as handle:
            return handle.read()


def _synthesize_sapi(text: str, wav_path: str, work_dir: str) -> None:
    """Windows SAPI through PowerShell's System.Speech.

    The text is passed via a UTF-8 file, never interpolated into the PowerShell
    command. Interpolating operator-supplied text into a shell command is command
    injection -- a clip labelled `'; Remove-Item -Recurse $HOME; '` would execute
    -- and PowerShell quoting has enough escapes (backtick, $, single vs double
    quotes) that getting it right by hand is a bad bet. A file sidesteps quoting
    entirely, and this backend has no auth, so the input is fully untrusted.
    """
    text_path = os.path.join(work_dir, "text.txt")
    with open(text_path, "w", encoding="utf-8") as handle:
        handle.write(text)

    script = (
        "$ErrorActionPreference = 'Stop'; "
        "Add-Type -AssemblyName System.Speech; "
        "$text = [System.IO.File]::ReadAllText($env:ROVERHUB_TTS_TEXT, "
        "[System.Text.Encoding]::UTF8); "
        "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$synth.SetOutputToWaveFile($env:ROVERHUB_TTS_WAV); "
        "$synth.Speak($text); "
        "$synth.Dispose()"
    )
    # Paths go through the environment too, so nothing operator-influenced is ever
    # part of the command string.
    env = dict(os.environ, ROVERHUB_TTS_TEXT=text_path, ROVERHUB_TTS_WAV=wav_path)
    _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env)


def _synthesize_espeak(binary: str, text: str, wav_path: str) -> None:
    """espeak-ng/espeak on Linux.

    Text is passed on stdin rather than as an argument: it avoids any argv length
    limit for longer announcements, and keeps the text out of the process list
    where it would otherwise be visible to every user on the host.
    """
    _run([binary, "-w", wav_path, "--stdin"], stdin_text=text)


def _run(argv: list, env: dict | None = None, stdin_text: str | None = None) -> None:
    """Runs a synthesis command. Never shell=True -- argv form means no shell is
    involved and argument quoting cannot be subverted."""
    try:
        result = subprocess.run(
            argv,
            input=stdin_text.encode("utf-8") if stdin_text is not None else None,
            capture_output=True,
            timeout=SYNTH_TIMEOUT_S,
            env=env,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TtsError(f"speech synthesis timed out after {SYNTH_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise TtsError(f"could not run the speech engine: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        logger.error("tts engine %s failed (rc=%s): %s", argv[0], result.returncode, detail)
        raise TtsError(f"speech synthesis failed: {detail or f'exit code {result.returncode}'}")
