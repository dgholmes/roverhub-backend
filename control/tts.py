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

Engines, tried in order:

* **Windows SAPI** via PowerShell's ``System.Speech``. Present on every Windows
  install with no pip dependency -- verified on the dev host (2 voices).
* **espeak-ng / espeak** on Linux, for when the backend moves off a dev laptop.
  Neither edge device has it and nor does the current host, so this path is
  written but unexercised; it is here so the Linux move is a package install
  rather than a code change.

Deliberately not a pip dependency (pyttsx3 and friends wrap exactly these two
engines anyway) and deliberately not audioop-based: this backend runs Python
3.13, where audioop was removed.
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import tempfile

logger = logging.getLogger("backend.control.tts")

# Long enough for a site announcement, short enough to bound synthesis time and
# the resulting clip. The audio-clip upload cap (10MB) is the real backstop.
MAX_TTS_CHARS = 1_000

# Synthesis is a subprocess; if an engine wedges we must not hold a request open.
SYNTH_TIMEOUT_S = 30


class TtsError(RuntimeError):
    """Synthesis failed. Surfaced to the operator rather than returning silence."""


class TtsUnavailable(TtsError):
    """No usable engine on this host."""


def available_engine() -> str | None:
    """Which engine this host can use, or None. Exposed so /tts can report
    capability without attempting a synthesis."""
    if platform.system() == "Windows":
        return "sapi" if shutil.which("powershell") else None
    for binary in ("espeak-ng", "espeak"):
        if shutil.which(binary):
            return binary
    return None


def synthesize_wav(text: str) -> bytes:
    """Synthesises `text` and returns WAV bytes."""
    text = (text or "").strip()
    if not text:
        raise TtsError("no text to speak")
    if len(text) > MAX_TTS_CHARS:
        raise TtsError(f"text exceeds {MAX_TTS_CHARS} characters")

    engine = available_engine()
    if engine is None:
        raise TtsUnavailable(
            "no text-to-speech engine on this host. On Linux install espeak-ng "
            "(apt install espeak-ng); on Windows SAPI is built in."
        )

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
