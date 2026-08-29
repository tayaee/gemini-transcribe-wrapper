"""Test multi-chunk checkpoint resume: chunk 0 done (checkpoint exists) -> API called once for chunk 1 only."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import api
from gemini_transcribe_wrapper.stt import TranscriptionResult, Word

API_CALLS = {"n": 0}


def run():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "input.mp4"
        import subprocess

        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1600", "-ar", "16000", "-ac", "1", str(src)],
            capture_output=True, check=True,
        )
        # First run with a failing client: chunk 0 completes, chunk 1 fails.
        # The failure keeps workdir + checkpoint for chunk 0.

        class FailSecondClient:
            def __init__(self, *args, **kwargs):
                pass

            def transcribe_chunk(self, chunk_mp3, chunk_index=0):
                API_CALLS["n"] += 1
                if chunk_mp3.name == "chunk_001.mp3":
                    raise RuntimeError("simulated failure on second chunk")
                return TranscriptionResult(
                    text="첫번째 청크",
                    words=[Word("첫번째", 0.0, 1.0, "spk:0"), Word("청크", 1.1, 2.0, "spk:0")],
                )

        orig = api.TranscribeClient
        api.TranscribeClient = FailSecondClient
        try:
            failed = api.gemini_transcribe(
                str(src), force=True, gemini_api_key="fake", request_interval_secs=0
            )
        finally:
            api.TranscribeClient = orig

        calls_after_fail = API_CALLS["n"]
        print("API calls after failed run:", calls_after_fail)
        workdir = td / ".input.gemini-work"
        print("workdir kept:", workdir.exists())
        leftover = failed.results[0].leftover
        print("leftover work_dir:", leftover.work_dir)
        print("leftover files:", leftover.all_files())

        # Resume with a working client: chunk 0 must be skipped (checkpoint), only chunk 1 called.
        class OkClient:
            def __init__(self, *args, **kwargs):
                pass

            def transcribe_chunk(self, chunk_mp3, chunk_index=0):
                API_CALLS["n"] += 1
                return TranscriptionResult(
                    text="두번째 청크",
                    words=[Word("두번째", 0.0, 1.0, "spk:1"), Word("청크", 1.2, 2.0, "spk:1")],
                )

        api.TranscribeClient = OkClient
        try:
            resumed = api.gemini_transcribe(
                str(src), force=True, gemini_api_key="fake", request_interval_secs=0
            )
        finally:
            api.TranscribeClient = orig

        calls_after_resume = API_CALLS["n"]
        print("API calls after resume run:", calls_after_resume)
        print("produced:", resumed.output_files())
        # Failed run: chunk 0 (1 call) then chunk 1 (1 call) = 2 total.
        # Resume run must skip chunk 0 (checkpoint) and call only chunk 1 = +1.
        assert calls_after_resume == calls_after_fail + 1, (
            f"resume should add exactly 1 API call, got {calls_after_resume} vs {calls_after_fail}"
        )
        print("PASS: checkpoint resume works")


if __name__ == "__main__":
    run()
