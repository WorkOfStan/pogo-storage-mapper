from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from pogo_storage_mapper.ocr import TesseractOcrEngine


@dataclass(slots=True)
class DependencyStatus:
    name: str
    ok: bool
    path: str | None
    note: str
    required: bool = True


@dataclass(slots=True)
class DoctorReport:
    dependencies: list[DependencyStatus]


def _ffmpeg_cuda_status(ffmpeg_path: str | None) -> DependencyStatus:
    note = "Optional CUDA decode support for faster MP4 frame extraction."
    if ffmpeg_path is None:
        return DependencyStatus("ffmpeg_cuda", False, None, note, required=False)

    try:
        completed = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-hwaccels"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return DependencyStatus("ffmpeg_cuda", False, None, note, required=False)

    output = f"{completed.stdout}\n{completed.stderr}".casefold()
    return DependencyStatus(
        "ffmpeg_cuda",
        "cuda" in output,
        "cuda" if "cuda" in output else None,
        note,
        required=False,
    )


def collect_doctor_report() -> DoctorReport:
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    tesseract_path = TesseractOcrEngine().executable_path()
    return DoctorReport(
        dependencies=[
            DependencyStatus(
                "ffmpeg",
                ffmpeg_path is not None,
                ffmpeg_path,
                "Required for MP4 frame extraction.",
            ),
            DependencyStatus(
                "ffprobe",
                ffprobe_path is not None,
                ffprobe_path,
                "Required for MP4 metadata and timestamp generation.",
            ),
            DependencyStatus(
                "tesseract",
                tesseract_path is not None,
                tesseract_path,
                (
                    "Optional during scanning, but required for OCR-backed "
                    "feature evidence."
                ),
                required=False,
            ),
            _ffmpeg_cuda_status(ffmpeg_path),
        ]
    )


def render_doctor_report(report: DoctorReport) -> str:
    lines = ["Dependency status:"]
    for dependency in report.dependencies:
        if dependency.required:
            status = "OK" if dependency.ok else "MISSING"
        else:
            status = "AVAILABLE" if dependency.ok else "UNAVAILABLE"
        lines.append(f"- {dependency.name}: {status} ({dependency.path or '-'})")
        lines.append(f"  {dependency.note}")
    return "\n".join(lines)
