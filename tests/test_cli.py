# pylint: disable=protected-access
from __future__ import annotations

import pytest

from pogo_storage_mapper import cli
from pogo_storage_mapper.cli import build_parser, main, parse_workers
from pogo_storage_mapper.scan_frames import ScanSettings


def test_parse_workers_accepts_auto_and_positive_numbers() -> None:
    assert parse_workers("auto") is None
    assert parse_workers("3") == 3
    assert parse_workers(2) == 2


def test_parse_workers_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        parse_workers("0")
    with pytest.raises(ValueError):
        parse_workers("fast")


def test_parser_accepts_sync_metadata_command() -> None:
    args = build_parser().parse_args(["sync-metadata", "--output", "catalog.json"])

    assert args.command == "sync-metadata"
    assert str(args.output) == "catalog.json"


def test_parser_accepts_export_command() -> None:
    args = build_parser().parse_args(["export", "--input", "frames", "--output", "out"])

    assert args.command == "export"
    assert str(args.input) == "frames"
    assert str(args.output) == "out"
    assert args.ocr_mode == "balanced"
    assert args.workers == "auto"
    assert args.visible_crop is False
    assert args.max_export_frame_files == 0


def test_parser_accepts_export_max_frame_files() -> None:
    args = build_parser().parse_args(
        [
            "export",
            "--input",
            "frames",
            "--output",
            "out",
            "--max-export-frame-files",
            "25",
        ]
    )

    assert args.command == "export"
    assert args.max_export_frame_files == 25
    assert cli._scan_settings(args).max_export_frame_files == 25


def test_parser_leaves_non_positive_export_max_frame_files_unlimited() -> None:
    args = build_parser().parse_args(
        [
            "export",
            "--input",
            "frames",
            "--output",
            "out",
            "--max-export-frame-files",
            "-1",
        ]
    )

    assert cli._scan_settings(args).max_export_frame_files == -1


def test_parser_accepts_classify_command() -> None:
    args = build_parser().parse_args(
        ["classify", "--input", "inventory.xlsx", "--output", "classified"]
    )

    assert args.command == "classify"
    assert str(args.input) == "inventory.xlsx"
    assert str(args.output) == "classified"


def test_parser_accepts_validate_command() -> None:
    args = build_parser().parse_args(["validate", "--input", "inventory.xlsx"])

    assert args.command == "validate"
    assert str(args.input) == "inventory.xlsx"


def test_parser_accepts_scan_visible_crop() -> None:
    args = build_parser().parse_args(
        [
            "scan-frames",
            "--input",
            "frames",
            "--output",
            "out",
            "--visible-crop",
        ]
    )

    assert args.command == "scan-frames"
    assert args.visible_crop is True
    assert cli._scan_settings(args).visible_crop is True


def test_parser_accepts_export_visible_crop() -> None:
    args = build_parser().parse_args(
        ["export", "--input", "frames", "--output", "out", "--visible-crop"]
    )

    assert args.command == "export"
    assert args.visible_crop is True
    assert cli._scan_settings(args).visible_crop is True


def test_export_main_uses_default_ocr_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Report:
        failed_files: list[str] = []

        def summary_line(self) -> str:
            return "ok"

    captured_settings: list[ScanSettings] = []

    def fake_export(settings: ScanSettings) -> Report:
        captured_settings.append(settings)
        return Report()

    monkeypatch.setattr(cli, "run_production_export", fake_export)

    exit_code = main(["export", "--input", "frames", "--output", "out"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "ok\n"
    assert captured_settings[0].ocr_mode == "balanced"


def test_export_rejects_invalid_max_frame_attempts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "export",
            "--input",
            ".",
            "--output",
            "out",
            "--max-frame-attempts",
            "0",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Maximum frame attempts" in captured.err
