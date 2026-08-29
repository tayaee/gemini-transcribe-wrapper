"""End-to-end pipeline test with a mocked Gemini client (no network)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import api
from gemini_transcribe_wrapper.stt import TranscriptionResult, Word


class FakeFile:
    uri = "files/fake-uri"
    name = "files/fake-name"


def make_fake_client():
    return None


def run():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # Create a tiny valid mp3 via ffmpeg
        import subprocess

        src = td / "input.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ar", "16000", "-ac", "1", str(src)],
            capture_output=True,
            check=True,
        )

        class FakeTranscribeClient:
            def __init__(self, *args, **kwargs):
                pass

            def transcribe_chunk(self, chunk_mp3, chunk_index=0):
                return TranscriptionResult(
                    text="안녕하세요 여러분 반갑습니다",
                    words=[
                        Word("안녕하세요", 0.0, 1.0, "spk_1"),
                        Word("여러분", 1.2, 2.0, "spk_1"),
                        Word("반갑습니다", 2.2, 3.0, "spk_2"),
                    ],
                )

        api.TranscribeClient = FakeTranscribeClient

        result = api.gemini_transcribe(str(src), force=True, gemini_api_key="fake-key")
        print("status:", result.results[0].status)
        print("output:", result.output_files())
        for p in result.output_files():
            path = Path(p)
            print(f"--- {path.name} ---")
            print(path.read_text(encoding="utf-8")[:150])

        # Verify no .lck, no .tmp, no chunk mp3 leftovers
        leftovers = [p.name for p in td.iterdir() if p.suffix in (".lck", ".tmp")]
        workdir = td / ".input.gemini-work"
        print("leftovers in outdir:", leftovers)
        print("workdir exists:", workdir.exists())
        print("leftover list:", result.results[0].leftover_files())


if __name__ == "__main__":
    run()
