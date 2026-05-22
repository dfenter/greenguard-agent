"""
Voicemail transcription pipeline for Greenguard USA.

iPhone voicemails → iCloud Drive → this script → transcribed text → Gmail agent

SETUP (one-time on iPhone):
  1. Open Shortcuts app → New Shortcut
  2. Add action: "Get Voicemails" → limit 1 (newest)
  3. Add action: "Save to Files" → iCloud Drive/Voicemails/
  4. Add action: "Run Shortcut" → set as automation trigger on new voicemail
     (Shortcuts → Automation → + → App → Phone → opens → run shortcut)

This script watches ~/Library/Mobile Documents/com~apple~CloudDocs/Voicemails/
transcribes any new audio files, and injects them into the Gmail agent pipeline
as if they were email leads.

Run automatically via launchd — see voicemail.plist
"""

import os
import time
import logging
import hashlib
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

ICLOUD_VOICEMAIL_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Voicemails"
PROCESSED_LOG = Path(__file__).parent / "logs/voicemails_processed.txt"
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base.en")  # tiny.en | base.en | small.en


def _load_processed() -> set:
    if PROCESSED_LOG.exists():
        return set(PROCESSED_LOG.read_text().splitlines())
    return set()


def _mark_processed(file_hash: str):
    PROCESSED_LOG.parent.mkdir(exist_ok=True)
    with open(PROCESSED_LOG, "a") as f:
        f.write(file_hash + "\n")


def _file_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def transcribe(audio_path: Path) -> str:
    """Transcribe audio file using faster-whisper. Returns transcript text."""
    from faster_whisper import WhisperModel
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(audio_path), language="en")
    return " ".join(seg.text.strip() for seg in segments).strip()


def process_voicemail(audio_path: Path) -> None:
    """Transcribe and inject into agent pipeline as a lead."""
    log.info("Transcribing: %s", audio_path.name)
    text = transcribe(audio_path)
    if not text:
        log.warning("Empty transcription for %s", audio_path.name)
        return

    log.info("Transcript: %s", text)

    # Inject into Gmail agent as a synthetic email lead
    _inject_as_lead(audio_path.stem, text)


def _inject_as_lead(filename: str, transcript: str) -> None:
    """Write transcript to a leads inbox file for the agent to pick up."""
    leads_dir = Path(__file__).parent / "leads"
    leads_dir.mkdir(exist_ok=True)
    out = leads_dir / f"{filename}.txt"
    out.write_text(
        f"SOURCE: Voicemail\n"
        f"FILE: {filename}\n\n"
        f"TRANSCRIPT:\n{transcript}\n"
    )
    log.info("Lead saved → %s", out)


def watch():
    """Watch iCloud voicemail folder for new audio files."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )

    if not ICLOUD_VOICEMAIL_DIR.exists():
        log.error("Voicemail folder not found: %s", ICLOUD_VOICEMAIL_DIR)
        log.error("Create it and set up the iPhone Shortcut per the docstring above.")
        return

    log.info("Watching %s", ICLOUD_VOICEMAIL_DIR)
    processed = _load_processed()

    while True:
        for ext in ("*.m4a", "*.mp3", "*.wav", "*.amr", "*.caf"):
            for audio in ICLOUD_VOICEMAIL_DIR.glob(ext):
                fhash = _file_hash(audio)
                if fhash in processed:
                    continue
                try:
                    process_voicemail(audio)
                    _mark_processed(fhash)
                except Exception as exc:
                    log.error("Failed %s: %s", audio.name, exc)
        time.sleep(30)


if __name__ == "__main__":
    watch()
