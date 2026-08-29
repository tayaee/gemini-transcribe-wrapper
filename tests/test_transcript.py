"""Test transcript.json: creation, re-render without API, and --no-transcript."""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import api
from gemini_transcribe_wrapper.stt import TranscriptionResult, Word

API_CALLS = {"n": 0}


class CountingClient:
    def __init__(self, *args, **kwargs):
        pass

    def transcribe_chunk(self, chunk_mp3, chunk_index=0):
        API_CALLS["n"] += 1
        return TranscriptionResult(
            text="안녕하세요 여러분 반갑습니다",
            words=[
                Word("안녕하세요", 0.0, 1.0, "spk:0"),
                Word("여러분", 1.2, 2.0, "spk:0"),
                Word("반갑습니다", 2.2, 3.0, "spk:0"),
            ],
        )


def make_input(td: Path) -> Path:
    src = td / "input.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ar", "16000", "-ac", "1", str(src)],
        capture_output=True, check=True,
    )
    return src


def run():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = make_input(td)

        orig = api.TranscribeClient
        api.TranscribeClient = CountingClient
        try:
            # 1) First run: transcript.json is created by default.
            api.gemini_transcribe(str(src), force=True, gemini_api_key="fake")
        finally:
            api.TranscribeClient = orig

        transcript = td / "input.transcript.json"
        print("transcript exists:", transcript.exists())
        print("calls after first run:", API_CALLS["n"])
        assert transcript.exists(), "transcript.json should be created by default"

        # 2) Delete outputs, keep transcript -> re-run must NOT call the API.
        for p in (td / "input.speakers.srt", td / "input.srt", td / "input.txt"):
            p.unlink()

        api.TranscribeClient = CountingClient
        try:
            r2 = api.gemini_transcribe(str(src), gemini_api_key="fake")
        finally:
            api.TranscribeClient = orig

        print("calls after re-render run:", API_CALLS["n"])
        print("outputs regenerated:", sorted(p.name for p in td.glob("input.speakers.srt")) + sorted(p.name for p in td.glob("input.srt")) + sorted(p.name for p in td.glob("input.txt")))
        print("re-render status:", r2.results[0].status)
        assert API_CALLS["n"] == 1, f"re-render should not call API, calls={API_CALLS['n']}"
        assert (td / "input.speakers.srt").exists() and (td / "input.srt").exists() and (td / "input.txt").exists()

        # 3) --no-transcript-json: transcript.json removed after processing.
        api.TranscribeClient = CountingClient
        try:
            api.gemini_transcribe(
                str(src), force=True, gemini_api_key="fake", create_transcript_json=False
            )
        finally:
            api.TranscribeClient = orig

        print("transcript exists after no-transcript:", transcript.exists())
        assert not transcript.exists(), "transcript.json should be removed with create_transcript_json=False"

        # 4) transcript.json content contains word data needed for rendering.
        import json
        data = json.loads(transcript.read_text(encoding="utf-8")) if transcript.exists() else None
        # recreate to inspect
        api.TranscribeClient = CountingClient
        try:
            api.gemini_transcribe(str(src), force=True, gemini_api_key="fake")
        finally:
            api.TranscribeClient = orig
        data = json.loads(transcript.read_text(encoding="utf-8"))
        print("transcript keys:", sorted(data.keys()))
        print("chunks:", len(data["chunks"]))
        print("first chunk words:", len(data["chunks"][0]["words"]))
        print("PASS: transcript flow works")


if __name__ == "__main__":
    run()
