"""Test output freshness: skip only when targets exist AND are newer than source."""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import api
from gemini_transcribe_wrapper.models import TranscribeStatus
from gemini_transcribe_wrapper.stt import TranscriptionResult, Word

CALLS = {"n": 0}


class FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def transcribe_chunk(self, chunk_mp3, chunk_index=0):
        CALLS["n"] += 1
        return TranscriptionResult(
            text="테스트",
            words=[Word("테스트", 0.0, 1.0, "spk:0")],
        )


def make_input(td: Path) -> Path:
    src = td / "input.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ar", "16000", "-ac", "1", str(src)],
        capture_output=True, check=True,
    )
    return src


def set_mtime(path: Path, ts: float) -> None:
    os.utime(path, (ts, ts))


def run():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = make_input(td)
        orig = api.TranscribeClient
        api.TranscribeClient = FakeClient

        # 1) No targets -> generate
        r = api.gemini_transcribe(str(src), gemini_api_key="fake")
        assert r.results[0].status == TranscribeStatus.SUCCESS, "should generate when no targets"
        print("1) no targets -> SUCCESS, calls:", CALLS["n"])

        # 2) Targets newer than source -> skip
        base_ts = time.time()
        set_mtime(src, base_ts - 100)  # source old
        for p in (td / "input.spk", td / "input.srt", td / "input.txt"):
            set_mtime(p, base_ts)  # targets new
        calls_before = CALLS["n"]
        r = api.gemini_transcribe(str(src), gemini_api_key="fake")
        assert r.results[0].status == TranscribeStatus.SKIPPED, "should skip when targets are newer"
        assert CALLS["n"] == calls_before, "no API call on skip"
        print("2) targets newer -> SKIPPED")

        # 3) Source newer than targets -> regenerate (must not be SKIPPED).
        set_mtime(src, base_ts + 200)  # source now newer
        r = api.gemini_transcribe(str(src), gemini_api_key="fake")
        assert r.results[0].status == TranscribeStatus.SUCCESS, "should regenerate when source is newer"
        print("3) source newer -> SUCCESS, regenerated")

        # 4) --force always regenerates (may re-render from transcript).
        r = api.gemini_transcribe(str(src), force=True, gemini_api_key="fake")
        assert r.results[0].status == TranscribeStatus.SUCCESS
        print("4) --force -> SUCCESS, regenerated")

        api.TranscribeClient = orig
        print("PASS: freshness rules work")


if __name__ == "__main__":
    run()
