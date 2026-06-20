from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageEnhance, ImageOps


@dataclass(slots=True)
class OcrResult:
    text: str
    confidence: float


class TesseractOcrEngine:
    def __init__(
        self,
        lang: str = "eng",
        *,
        executable: str | Path | None = None,
        search_paths: Sequence[str | Path] | None = None,
    ) -> None:
        self.lang = lang
        self._explicit_executable = Path(executable) if executable is not None else None
        self._search_paths = tuple(
            Path(path) for path in search_paths or self._default_paths()
        )
        self._resolved_executable: str | None = None
        self._did_resolve_executable = False

    @staticmethod
    def _default_paths() -> tuple[str, ...]:
        return (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        )

    def executable_path(self) -> str | None:
        if self._did_resolve_executable:
            return self._resolved_executable

        candidates: list[Path] = []
        if self._explicit_executable is not None:
            candidates.append(self._explicit_executable)
        if path_from_env := shutil.which("tesseract"):
            candidates.append(Path(path_from_env))
        candidates.extend(self._search_paths)

        for candidate in candidates:
            if candidate.exists():
                self._resolved_executable = str(candidate)
                self._did_resolve_executable = True
                return self._resolved_executable

        self._resolved_executable = None
        self._did_resolve_executable = True
        return None

    def is_available(self) -> bool:
        return self.executable_path() is not None

    def read_text(
        self, image: Image.Image, *, psm: int = 6, config: str = ""
    ) -> OcrResult:
        executable = self.executable_path()
        if executable is None:
            msg = "Tesseract is not installed."
            raise RuntimeError(msg)

        import pytesseract  # type: ignore[import-untyped]

        pytesseract.pytesseract.tesseract_cmd = executable
        tess_config = f"--psm {psm} {config}".strip()

        # Preprocess the image to improve Tesseract accuracy
        processed_image = ImageOps.grayscale(image)
        processed_image = ImageEnhance.Contrast(processed_image).enhance(2.0)

        data = pytesseract.image_to_data(
            processed_image,
            config=tess_config,
            lang=self.lang,
            output_type=pytesseract.Output.DICT,
        )
        tokens = [token.strip() for token in data["text"] if token.strip()]
        confidences = [
            float(confidence)
            for confidence, token in zip(data["conf"], data["text"], strict=True)
            if token.strip() and confidence not in {"-1", -1}
        ]
        mean_confidence = (
            sum(confidences) / len(confidences) / 100 if confidences else 0.0
        )
        return OcrResult(" ".join(tokens).strip(), mean_confidence)
