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


def test_extract_error_description_length():
    from gemini_transcribe_wrapper.stt import _extract_error_description

    assert _extract_error_description(None) == "OK"
    short_exc = RuntimeError("short error")
    assert _extract_error_description(short_exc) == "short error"
    long_msg = "x" * 800
    long_exc = RuntimeError(long_msg)
    desc = _extract_error_description(long_exc)
    assert len(desc) == 500
    assert desc.endswith("...")
    assert desc.startswith("x" * 497)


def test_summarize_error_for_log_extracts_urls():
    """Short 429-style summary must include all http(s) URLs found in the message."""
    from gemini_transcribe_wrapper.stt import _summarize_error_for_log

    quota_msg = (
        "You exceeded your current quota, please check your plan and billing details. "
        "For more information on this error, head to: "
        "https://ai.google.dev/gemini-api/docs/rate-limits. "
        "To monitor your current usage, head to: https://ai.dev/rate-limit. "
        "\n* Quota exceeded for metric: "
        "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
        "limit: 25, model: gemini-3.5-transcribe\nPlease retry in 52.639038235s."
    )

    class FakeQuotaExc(Exception):
        code = 429
        message = quota_msg

    summary = _summarize_error_for_log(FakeQuotaExc())

    # Status code surfaced, message body stripped of the multi-line JSON.
    assert summary.startswith("Caught error 429 from API call. Head to ")
    # Both URLs extracted; bare-domain metric name is NOT treated as a URL.
    assert "https://ai.google.dev/gemini-api/docs/rate-limits" in summary
    assert "https://ai.dev/rate-limit" in summary
    assert "generativelanguage.googleapis.com" not in summary
    # The trailing 'Head to' clause is comma-separated and in original order.
    head_idx = summary.index("Head to ")
    tail = summary[head_idx + len("Head to "):]
    parts = [p.strip() for p in tail.split(",")]
    assert parts == [
        "https://ai.google.dev/gemini-api/docs/rate-limits",
        "https://ai.dev/rate-limit",
    ]


def test_summarize_error_for_log_no_urls():
    """When the message has no http(s) URL, omit the trailing 'Head to' clause."""
    from gemini_transcribe_wrapper.stt import _summarize_error_for_log

    err = RuntimeError("plain text error without any link")
    assert _summarize_error_for_log(err) == "Caught error 500 from API call."


def test_summarize_error_for_log_strips_trailing_punct():
    """Trailing '.' or ',' glued to a URL by the upstream formatter must be stripped."""
    from gemini_transcribe_wrapper.stt import _summarize_error_for_log

    err = RuntimeError("see https://example.com/docs, and also https://example.com/faq).")
    summary = _summarize_error_for_log(err)
    assert summary == (
        "Caught error 500 from API call. "
        "Head to https://example.com/docs, https://example.com/faq"
    )


def test_summarize_error_for_log_dedupes_urls():
    """Repeated URLs in the message must not be duplicated in the summary."""
    from gemini_transcribe_wrapper.stt import _summarize_error_for_log

    err = RuntimeError("see https://example.com/docs again https://example.com/docs.")
    summary = _summarize_error_for_log(err)
    assert summary.count("https://example.com/docs") == 1


if __name__ == "__main__":
    test_failure_keeps_mp3()
    test_checkpoint_resume()
    test_temp_path()
