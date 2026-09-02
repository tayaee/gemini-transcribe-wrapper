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


def test_speakers_mapping_api(tmp_path):
    # 1) parse_speakers
    m = parse_speakers("spk:0=궤도; spk:1=가람;")
    assert m == {"spk:0": "궤도", "spk:1": "가람"}
    try:
        parse_speakers("bad-format")
        raise AssertionError("should have raised")
    except ValueError:
        pass

    src = tmp_path / "input.mp4"
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
            diarized_srt_file=True,
            speakers={"spk:0": "궤도", "spk:1": "가람"},
        )
    finally:
        api.TranscribeClient = orig

    spk_text = (tmp_path / "input.diarized.srt").read_text(encoding="utf-8")
    assert "[궤도]" in spk_text and "[가람]" in spk_text
    assert "[spk:2]" in spk_text, "unmapped speaker should keep raw id"

    # 3) Full mapping covers everything.
    orig = api.TranscribeClient
    api.TranscribeClient = FakeClient
    try:
        api.gemini_transcribe(
            str(src), force=True, gemini_api_key="fake",
            diarized_srt_file=True,
            speakers={"spk:0": "궤도", "spk:1": "가람", "spk:2": "황가람"},
        )
    finally:
        api.TranscribeClient = orig

    spk_text2 = (tmp_path / "input.diarized.srt").read_text(encoding="utf-8")
    assert "[황가람]" in spk_text2 and "[spk:2]" not in spk_text2


def test_speakers_txt_file_auto_detection(tmp_path):
    src = tmp_path / "lecture.mp4"
    spk_file = tmp_path / "lecture.speakers.txt"
    spk_file.write_text("spk:0=강사\nspk:1=학생\n", encoding="utf-8")

    from gemini_transcribe_wrapper.api import _load_speakers_file
    loaded = _load_speakers_file("auto", input_file=src)
    assert loaded == {"spk:0": "강사", "spk:1": "학생"}

    # Off disables loading
    assert _load_speakers_file("off", input_file=src) == {}


def test_speakers_txt_file_single_line_multiple(tmp_path):
    src = tmp_path / "lecture.mp4"
    spk_file = tmp_path / ".speakers.txt"
    spk_file.write_text("spk:0=John Doe ; spk:1=Jane Doe", encoding="utf-8")

    from gemini_transcribe_wrapper.api import _load_speakers_file
    loaded = _load_speakers_file("auto", input_file=src)
    assert loaded == {"spk:0": "John Doe", "spk:1": "Jane Doe"}


def test_format_diarized_srt_string_replacement():
    from gemini_transcribe_wrapper.format import Cue, format_diarized_srt

    cues = [
        Cue(start=0.0, end=1.0, text="대사", speaker="spk:0"),
    ]

    # 1) Standard spk:0=홍길동 -> [홍길동] 대사
    srt1 = format_diarized_srt(cues, speaker_map={"spk:0": "홍길동"})
    assert "[홍길동] 대사" in srt1

    # 2) spk:0]=홍길동: -> [홍길동: 대사
    srt2 = format_diarized_srt(cues, speaker_map={"spk:0]": "홍길동:"})
    assert "[홍길동: 대사" in srt2

    # 3) [spk:0]=홍길동: -> 홍길동: 대사
    srt3 = format_diarized_srt(cues, speaker_map={"[spk:0]": "홍길동:"})
    assert "홍길동: 대사" in srt3

    # 4) [spk:0]=홍길동 -> 홍길동 대사
    srt4 = format_diarized_srt(cues, speaker_map={"[spk:0]": "홍길동"})
    assert "홍길동 대사" in srt4

    # 5) Exact space matching: [spk:0] =홍길동: -> 홍길동:대사
    parsed5 = parse_speakers("[spk:0] =홍길동:")
    assert parsed5 == {"[spk:0] ": "홍길동:"}
    srt5 = format_diarized_srt(cues, speaker_map=parsed5)
    assert "홍길동:대사" in srt5


def run():
    with tempfile.TemporaryDirectory() as td:
        test_speakers_mapping_api(Path(td))


if __name__ == "__main__":
    run()
