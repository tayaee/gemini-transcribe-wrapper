"""Test failure paths: mp3 preserved on failure, checkpoint resume, metadata off."""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import api
from gemini_transcribe_wrapper.stt import TranscriptionResult, Word


class BoomClient:
    """Fails on the first chunk transcription."""

    def __init__(self, *args, **kwargs):
        pass

    def transcribe_chunk(self, chunk_mp3, chunk_index=0):
        raise RuntimeError("simulated API failure")


def test_failure_keeps_mp3():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "input.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ar", "16000", "-ac", "1", str(src)],
            capture_output=True, check=True,
        )
        orig = api.TranscribeClient
        api.TranscribeClient = BoomClient
        try:
            result = api.gemini_transcribe(str(src), force=True, gemini_api_key="fake")
        finally:
            api.TranscribeClient = orig

        print("status:", result.results[0].status)
        print("error:", result.results[0].error)
        workdir = td / "temp" / "input.gemini-work"
        print("workdir exists after failure:", workdir.exists())
        print("temp_audio.mp3 kept:", (workdir / "temp_audio.mp3").exists())
        print("chunks kept:", list((workdir / "chunks").glob("*.mp3")) if (workdir / "chunks").exists() else [])
        print("lck leftover:", list(td.glob("*.lck")))
        print("leftover files:", result.results[0].leftover_files())


def test_checkpoint_resume():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "input.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ar", "16000", "-ac", "1", str(src)],
            capture_output=True, check=True,
        )
        workdir = td / "temp" / "input.gemini-work"
        chunk_dir = workdir / "chunks"
        chunk_dir.mkdir(parents=True)
        chunk = chunk_dir / "chunk_000.mp3"
        chunk.write_bytes(b"fake-mp3")

        from gemini_transcribe_wrapper.stt import load_checkpoint, save_checkpoint
        res = TranscriptionResult(
            text="체크포인트 텍스트",
            words=[Word("체크포인트", 0.0, 1.0, "spk_1"), Word("텍스트", 1.1, 2.0, "spk_1")],
        )
        save_checkpoint(chunk.with_suffix(".metadata.json"), res)
        loaded = load_checkpoint(chunk.with_suffix(".metadata.json"))
        if loaded is None:
            raise AssertionError("checkpoint failed to load")
        print("loaded checkpoint:", loaded.text, loaded.words)


def test_temp_path():
    """Temp files must go under temp_path when specified (cleaned on success)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "input.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ar", "16000", "-ac", "1", str(src)],
            capture_output=True, check=True,
        )
        temp_path = td / "mytemp"

        class FakeTranscribeClient:
            def __init__(self, *args, **kwargs):
                pass

            def transcribe_chunk(self, chunk_mp3, chunk_index=0):
                return TranscriptionResult(
                    text="테스트",
                    words=[Word("테스트", 0.0, 1.0, "spk_1")],
                )

        orig = api.TranscribeClient
        api.TranscribeClient = FakeTranscribeClient
        try:
            result = api.gemini_transcribe(
                str(src), force=True, gemini_api_key="fake", temp_path=str(temp_path)
            )
        finally:
            api.TranscribeClient = orig

        # Work dir must have been created under temp_path and cleaned up on success.
        print("temp_path exists:", temp_path.exists())
        print("no workdir left in temp_path:", not (temp_path / "input.gemini-work").exists())
        print("no workdir next to input:", not (td / ".input.gemini-work").exists())
        print("output:", result.output_files())
        print("leftover:", result.results[0].leftover_files())

        # Failure case: workdir should remain under temp_path and be reported.
        class BoomClient:
            def __init__(self, *args, **kwargs):
                pass

            def transcribe_chunk(self, chunk_mp3, chunk_index=0):
                raise RuntimeError("boom")

        api.TranscribeClient = BoomClient
        try:
            failed = api.gemini_transcribe(
                str(src), force=True, gemini_api_key="fake", temp_path=str(temp_path)
            )
        finally:
            api.TranscribeClient = orig

        print("failed workdir in temp_path:", (temp_path / "input.gemini-work").exists())
        print("failed leftover work_dir:", failed.results[0].leftover.work_dir)


if __name__ == "__main__":
    test_failure_keeps_mp3()
    test_checkpoint_resume()
    test_temp_path()
