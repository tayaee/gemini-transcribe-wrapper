"""Pydantic result models for the gemini_transcribe() API."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TranscribeStatus(str, Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    LOCKED = "locked"
    FAILED = "failed"
    NOT_FOUND = "not_found"


class TranscribeInput(BaseModel):
    """Echo of the input file and the options used for processing."""

    input_file: str = Field(..., description="Input media file path.")
    output_dir: str | None = Field(None, description="Directory for final outputs.")
    output_base: str | None = Field(None, description="Output base name.")
    language: str = Field("ko-KR", description="BCP-47 language code.")
    create_diarized_srt: bool = Field(True, description="Whether .diarized.srt output was requested.")
    create_srt: bool = Field(True, description="Whether .srt output was requested.")
    create_txt: bool = Field(True, description="Whether .txt output was requested.")
    create_metadata_json: bool = Field(False, description="Whether .metadata.json output was requested.")
    create_transcript_json: bool = Field(True, description="Whether .transcript.json was kept.")
    ffsubsync_srt: bool = Field(False, description="Whether .ffsubsync.srt was written.")
    force: bool = Field(False, description="Whether re-processing was forced.")
    temp_dir: str | None = Field(None, description="Temp dir used for intermediate files.")
    line_interval_secs: float = Field(1.0, description="TXT newline break gap (s).")
    paragraph_interval_secs: float = Field(2.5, description="TXT paragraph break gap (s).")
    request_interval_secs: float = Field(30.0, description="Delay between API calls (s).")


class TranscribeOutput(BaseModel):
    """Requested final output files that were generated."""

    diarized_srt: str | None = Field(None, description="Speaker-diarized subtitle file (.diarized.srt).")
    srt: str | None = Field(None, description="Subtitle file (.srt).")
    txt: str | None = Field(None, description="Editor-formatted text file (.txt).")
    metadata_json: str | None = Field(None, description="Metadata file (.metadata.json).")

    def as_list(self) -> list[str]:
        return [p for p in (self.diarized_srt, self.srt, self.txt, self.metadata_json) if p]


class TranscribeLeftover(BaseModel):
    """Files left behind that the caller may clean up.

    Includes informational files (e.g. per-chunk metadata.json when the
    metadata output was not requested) and intermediate artifacts kept after
    a failure (chunk mp3s, temp audio, workdir) for resume/cleanup purposes.
    """

    metadata_files: list[str] = Field(
        default_factory=list,
        description="Chunk checkpoint files (*.metadata.json).",
    )
    intermediate_files: list[str] = Field(
        default_factory=list,
        description="Intermediate mp3/chunk files and other temp artifacts.",
    )
    work_dir: str | None = Field(
        None, description="Work directory left behind (e.g. on failure)."
    )

    def all_files(self) -> list[str]:
        return list(self.metadata_files) + list(self.intermediate_files)


class TranscribeResult(BaseModel):
    """Result of transcribing a single input file."""

    input: TranscribeInput
    output: TranscribeOutput = Field(default_factory=lambda: TranscribeOutput())
    leftover: TranscribeLeftover = Field(default_factory=lambda: TranscribeLeftover())
    status: TranscribeStatus = TranscribeStatus.SUCCESS
    error: str | None = Field(None, description="Error message on failure.")

    def output_files(self) -> list[str]:
        return self.output.as_list()

    def leftover_files(self) -> list[str]:
        return self.leftover.all_files()


class BatchTranscribeResult(BaseModel):
    """Aggregate result for glob/path inputs that match multiple files."""

    results: list[TranscribeResult] = Field(default_factory=list)

    def output_files(self) -> list[str]:
        out: list[str] = []
        for r in self.results:
            out.extend(r.output_files())
        return out

    def leftover_files(self) -> list[str]:
        leftover: list[str] = []
        for r in self.results:
            leftover.extend(r.leftover_files())
        return leftover

    def all_files(self) -> list[str]:
        return self.output_files() + self.leftover_files()

    @property
    def succeeded(self) -> bool:
        return all(r.status in (TranscribeStatus.SUCCESS, TranscribeStatus.SKIPPED) for r in self.results)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)
