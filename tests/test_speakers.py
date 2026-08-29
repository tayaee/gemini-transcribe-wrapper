"""Test --speakers mapping: speaker names, coverage warning, and re-render."""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import api
from gemini_transcribe_wrapper.cli import parse_speakers
from gemini_transcribe_wrapper.stt import TranscriptionResult, Word


class FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def transcribe_chunk(self, chunk_mp3, chunk_index=0):
        return TranscriptionResult(
            text="안녕하세요 반갑습니다",
            words=[
                Word("안녕하세요", 0.0, 1.0, "spk:0"),
                Word("반갑습니다", 1.2, 2.0, "spk:1"),
                Word("네", 2.2, 3.0, "spk:2"),
            ],
        )


def run():
    # 1) parse_speakers
    m = parse_speakers("spk:0=궤도; spk:1=가람;")
    print("parsed:", m)
    assert m == {"spk:0": "궤도", "spk:1": "가람"}
    try:
        parse_speakers("bad-format")
        raise AssertionError("should have raised")
    except ValueError:
        pass

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "input.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ar", "16000", "-ac", "1", str(src)],
            capture_output=True, check=True,
        )
        orig = api.TranscribeClient
        api.TranscribeClient = FakeClient
        try:
            # 2) Partial mapping: unmapped speaker keeps raw id.
            api.gemini_transcribe(
                str(src), force=True, gemini_api_key="fake",
                speakers={"spk:0": "궤도", "spk:1": "가람"},
            )
        finally:
            api.TranscribeClient = orig

        spk_text = (td / "input.speakers.srt").read_text(encoding="utf-8")
        print("speaker tags:", sorted({l[1:-1] for l in spk_text.splitlines() if l.startswith("[") and l.endswith("]")}))
        assert "[궤도]" in spk_text and "[가람]" in spk_text
        assert "[spk:2]" in spk_text, "unmapped speaker should keep raw id"

        # 3) Full mapping covers everything.
        orig = api.TranscribeClient
        api.TranscribeClient = FakeClient
        try:
            api.gemini_transcribe(
                str(src), force=True, gemini_api_key="fake",
                speakers={"spk:0": "궤도", "spk:1": "가람", "spk:2": "황가람"},
            )
        finally:
            api.TranscribeClient = orig

        spk_text2 = (td / "input.speakers.srt").read_text(encoding="utf-8")
        assert "[황가람]" in spk_text2 and "[spk:2]" not in spk_text2
        print("PASS: speaker mapping works")


if __name__ == "__main__":
    run()
