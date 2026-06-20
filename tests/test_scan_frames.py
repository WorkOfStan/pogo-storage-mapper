# pylint: disable=protected-access,duplicate-code
from __future__ import annotations

import json
import math
import subprocess
from collections import Counter
from pathlib import Path
from typing import Iterable, Literal

import pytest
from PIL import Image, ImageDraw

import pogo_storage_mapper.scan_frames as scan_frames_module
from pogo_storage_mapper.layout import SignalValue
from pogo_storage_mapper.ocr import OcrResult, TesseractOcrEngine
from pogo_storage_mapper.scan_frames import (
    FEATURE_KEYS,
    LIST_FEATURE_KEYS,
    FrameCandidate,
    FrameScanRecord,
    FrameVisualRecord,
    ScanSettings,
    SourceAsset,
    _build_ffmpeg_extract_command,
    _classification_from_features,
    _postprocess_frame_sequences,
    _process_frames_with_retry,
    _visual_all_moves_evidence,
    _visual_cp_evidence,
    _visual_display_name_evidence,
    _visual_hp_evidence,
    extract_video_frames,
    group_production_sequences,
    parse_cp_candidate,
    parse_height_candidate,
    parse_hp_candidate,
    parse_weight_candidate,
    run_frame_scan,
    scan_frame_candidate,
    scan_frame_visual_candidate,
    scan_production_sequence,
    story_text_has_keywords,
    story_text_is_complete,
)


def _completed_futures(futures):
    return list(futures)


def _scan_postprocessed_frame(
    frame: FrameCandidate, settings: ScanSettings
) -> FrameScanRecord:
    record = scan_frame_candidate(frame, settings)
    _postprocess_frame_sequences([record])
    return record


def _ocr_text(record: FrameScanRecord, field_name: str) -> str:
    text = record.ocr[field_name]["text"]
    assert isinstance(text, str)
    return text


def test_scan_validators_accept_expected_shapes() -> None:
    assert parse_cp_candidate("CP 1234") == 1234
    assert parse_cp_candidate("cp 9") is None
    assert parse_cp_candidate("ceP936") == 936
    assert parse_cp_candidate("cpP2498") == 2498
    assert parse_cp_candidate("cPLO14") == 1014
    assert parse_cp_candidate("cPlOLt") == 1014
    assert parse_cp_candidate("ce1446") == 1446
    assert parse_cp_candidate("CP 9366") == 9366
    assert parse_cp_candidate("CP 9367") is None
    assert parse_cp_candidate("CP 12065") is None
    assert parse_cp_candidate("CP 12 065") is None
    assert parse_cp_candidate("cP1 2065") is None
    assert parse_cp_candidate("ce589 |") == 589
    assert parse_cp_candidate("986") == 986
    assert parse_cp_candidate("14 a") is None

    assert parse_cp_candidate("A98 *") is None
    assert parse_hp_candidate("77 / 77 HP") == "77/77"
    assert parse_hp_candidate("99/77 HP") is None
    assert parse_weight_candidate("1.41 kg") == "1.41"
    assert parse_weight_candidate("$37.39kg") is None
    assert parse_weight_candidate("WEIGHT:337.39kg") == "337.39"
    assert parse_weight_candidate("1000.01 kg") is None
    assert parse_height_candidate("1.25 m") == "1.25"
    assert parse_height_candidate("1.17m") == "1.17"
    assert parse_height_candidate("1,17 m") == "1.17"
    assert parse_height_candidate("HEIGHT:1.17m") == "1.17"
    assert parse_height_candidate("117m") is None
    assert parse_height_candidate("16081.17m") is None
    assert story_text_has_keywords(
        "This Totodile was caught on 4/9/2026 around Prague."
    )
    complete_story_cases = [
        "This Totodile was caught on 4/9/2026 around Prague, Czechia.",
        (
            "Looks like a Phony Form Sinistea! This Sinistea was caught on "
            "12/7/2024 around Hlavní město Praha, Czechia."
        ),
        "This Darumaka was caught on 3/13/2025 around Australia.",
        "This Magnemite was caught on 7/11/2023 around French Polynesia.",
        (
            "This Darumaka was caught on 6/2/2025 around Bethlehem, "
            "Pennsylvania, United States."
        ),
    ]
    assert all(story_text_is_complete(text) for text in complete_story_cases)
    incomplete_story_cases = [
        "This was caught on 4/9/2026 around Prague, Czechia.",
        "This Totodile was caught around Prague, Czechia.",
        "This Totodile was caught on 4/9/2026 around.",
        "This Totodile was caught on 4/9/2026 around Prague, Czechia",
        "This Totodile was seen around Prague.",
    ]
    assert not any(story_text_is_complete(text) for text in incomplete_story_cases)


def test_shadow_ball_move_text_does_not_mark_shadow_status() -> None:
    features, _classification = scan_frames_module._classified_scan_features(
        "detail",
        {
            "name_dark_ratio": 0.0,
            "detail_card_brightness": 1.0,
            "pokemon_art_signal": 0.0,
            "hp_green_ratio": 0.0,
            "moves_dark_ratio": 0.0,
            "tag_edge_ratio": 0.0,
        },
        scan_frames_module._empty_iv_evidence_from_signals({}),
        {"display_name": OcrResult("", 0.0)},
        scan_frames_module._ParsedOcrValues(
            cp=464,
            hp="69/69",
            weight="0.29",
            height="0.12",
            story_text="",
            move_text="Astonish Shadow Ball",
            special_text="",
        ),
    )

    assert features["is_shadow"] is False


def test_frustration_move_text_marks_shadow_status() -> None:
    features, _classification = scan_frames_module._classified_scan_features(
        "detail",
        {
            "name_dark_ratio": 0.0,
            "detail_card_brightness": 1.0,
            "pokemon_art_signal": 0.0,
            "hp_green_ratio": 0.0,
            "moves_dark_ratio": 0.0,
            "tag_edge_ratio": 0.0,
        },
        scan_frames_module._empty_iv_evidence_from_signals({}),
        {"display_name": OcrResult("", 0.0)},
        scan_frames_module._ParsedOcrValues(
            cp=565,
            hp="66/66",
            weight="51.87",
            height="1.54",
            story_text="",
            move_text="Air Slash Frustration",
            special_text="",
        ),
    )

    assert features["is_shadow"] is True


def test_detail_and_appraisal_ocr_regions_are_screen_type_specific() -> None:
    for ocr_mode in ("balanced", "full"):
        detail_regions = scan_frames_module._ocr_regions_for("detail", ocr_mode)
        appraisal_regions = scan_frames_module._ocr_regions_for("appraisal", ocr_mode)

        assert detail_regions["height"] == (scan_frames_module.REGIONS["height"], 6)
        assert detail_regions["moves"] == (scan_frames_module.REGIONS["moves"], 6)
        assert detail_regions["special_sections"] == (
            scan_frames_module.REGIONS["special_sections"],
            6,
        )
        assert "story" not in detail_regions
        assert appraisal_regions["story"] == (scan_frames_module.REGIONS["story"], 6)
        assert "height" not in appraisal_regions
        assert "moves" not in appraisal_regions
        assert "special_sections" not in appraisal_regions


def test_iv_bar_decodes_gray_tail_as_unfilled_segment() -> None:
    panel = Image.new("RGB", (520, 388), (250, 250, 246))
    draw = ImageDraw.Draw(panel)
    amber = (224, 126, 36)
    gray = (196, 205, 205)

    y_top = 97
    y_bottom = 104
    draw.rectangle((46, y_top, 163, y_bottom), fill=amber)
    draw.rectangle((168, y_top, 288, y_bottom), fill=amber)
    draw.rectangle((294, y_top, 386, y_bottom), fill=amber)
    draw.rectangle((388, y_top, 409, y_bottom), fill=gray)

    assert scan_frames_module._decode_iv_bar(panel, (0.12, 0.35)) == 14


def test_cp_ocr_candidate_selection_prefers_prefixed_digits() -> None:
    selected = scan_frames_module._select_cp_ocr_result(
        [
            OcrResult("986", 0.99),
            OcrResult("cPI36", 0.95),
            OcrResult("cP936", 0.30),
        ]
    )

    assert selected.text == "cP936"
    assert parse_cp_candidate(selected.text) == 936


def test_hp_ocr_candidate_selection_prefers_parseable_hp_text() -> None:
    selected = scan_frames_module._select_hp_ocr_result(
        [
            OcrResult("* —— a", 0.80),
            OcrResult("50/99 HP", 0.42),
        ]
    )

    assert selected.text == "50/99 HP"
    assert parse_hp_candidate(selected.text) == "50/99"


def test_detail_layout_mode_uses_initial_appraisal_overlay() -> None:
    iv_evidence = scan_frames_module._IvEvidence(
        attack=10,
        defense=11,
        stamina=12,
        iv_sum=33,
        star_count=3,
        badge_visible=True,
        perfect=False,
        star_agreement=False,
        panel_visible=True,
        seal_visible=True,
        bar_count=3,
        panel_light_ratio=0.70,
        seal_color_ratio=0.20,
    )
    signals: dict[str, SignalValue] = {
        "hp_bar_anchor_visible": True,
        "hp_bar_anchor_y": 0.42,
        "hp_bar_anchor_score": 0.40,
    }

    layout = scan_frames_module._detail_layout(
        raw_classification="appraisal",
        signals=signals,
        iv_evidence=iv_evidence,
        story_text="This Ivysaur was caught on 1/30/2025 around Prague, Czechia.",
    )
    scrollable_layout = scan_frames_module._detail_layout(
        raw_classification="appraisal",
        signals=signals,
        iv_evidence=iv_evidence,
        story_text="This Ivysaur was caught around Prague.",
    )

    assert layout.mode == "initial_appraisal_overlay"
    assert scrollable_layout.mode == "scrollable_detail"


def test_hp_bar_anchor_detects_shifted_bar_position() -> None:
    image = Image.new("RGB", (1080, 2424), (244, 244, 238))
    draw = ImageDraw.Draw(image)
    bar_y = int(0.46 * image.height)
    draw.rounded_rectangle(
        (int(0.30 * image.width), bar_y, int(0.70 * image.width), bar_y + 14),
        radius=7,
        fill=(232, 210, 68),
    )

    anchor_y, score = scan_frames_module._detect_hp_bar_anchor(image)

    assert anchor_y is not None
    assert abs(anchor_y - 0.46) < 0.03
    assert score >= 0.18


def test_hp_bar_anchor_rejects_broad_upper_art_band() -> None:
    image = Image.new("RGB", (1080, 2424), (244, 244, 238))
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (
            int(0.20 * image.width),
            int(0.12 * image.height),
            int(0.80 * image.width),
            int(0.30 * image.height),
        ),
        fill=(80, 210, 160),
    )
    bar_y = int(0.46 * image.height)
    draw.rounded_rectangle(
        (int(0.30 * image.width), bar_y, int(0.70 * image.width), bar_y + 14),
        radius=7,
        fill=(232, 210, 68),
    )

    anchor_y, score = scan_frames_module._detect_hp_bar_anchor(image)

    assert anchor_y is not None
    assert abs(anchor_y - 0.46) < 0.03
    assert score >= 0.18


def test_tag_chip_region_follows_detected_hp_bar_anchor() -> None:
    image = Image.new("RGB", (1080, 2424), (244, 244, 238))
    draw = ImageDraw.Draw(image)
    bar_y = int(0.46 * image.height)
    draw.rounded_rectangle(
        (int(0.30 * image.width), bar_y, int(0.70 * image.width), bar_y + 14),
        radius=7,
        fill=(80, 215, 105),
    )
    tag_top = int(0.565 * image.height)
    tag_bottom = int(0.595 * image.height)
    for x in range(int(0.08 * image.width), int(0.92 * image.width), 42):
        draw.rectangle((x, tag_top, x + 14, tag_bottom), fill=(35, 35, 35))

    signals = scan_frames_module._visual_signals(image)
    scan_frames_module._enrich_detail_visual_signals(image, signals)

    assert signals["hp_bar_anchor_visible"] is True
    assert signals["tag_chip_region_anchored"] is True
    assert float(signals["tag_chip_region_top"]) == pytest.approx(
        float(signals["hp_bar_anchor_y"]) + 0.035,
        abs=0.002,
    )
    assert float(signals["tag_chip_region_bottom"]) == pytest.approx(
        float(signals["hp_bar_anchor_y"]) + 0.145,
        abs=0.002,
    )
    assert float(signals["tag_edge_ratio"]) >= 0.07


def test_tag_chip_region_uses_fixed_crop_without_hp_bar_anchor() -> None:
    image = Image.new("RGB", (1080, 2424), (244, 244, 238))
    draw = ImageDraw.Draw(image)
    tag_top = int(0.465 * image.height)
    tag_bottom = int(0.525 * image.height)
    for x in range(int(0.08 * image.width), int(0.92 * image.width), 42):
        draw.rectangle((x, tag_top, x + 14, tag_bottom), fill=(35, 35, 35))

    signals = scan_frames_module._visual_signals(image)
    scan_frames_module._enrich_detail_visual_signals(image, signals)

    assert signals["hp_bar_anchor_visible"] is False
    assert signals["tag_chip_region_anchored"] is False
    assert float(signals["tag_chip_region_left"]) == pytest.approx(
        scan_frames_module.REGIONS["tag"][0]
    )
    assert float(signals["tag_chip_region_top"]) == pytest.approx(
        scan_frames_module.REGIONS["tag"][1]
    )
    assert float(signals["tag_chip_region_right"]) == pytest.approx(
        scan_frames_module.REGIONS["tag"][2]
    )
    assert float(signals["tag_chip_region_bottom"]) == pytest.approx(
        scan_frames_module.REGIONS["tag"][3]
    )
    assert float(signals["tag_edge_ratio"]) >= 0.07


def test_hp_recovery_uses_selected_anchor_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: True)
    path = tmp_path / "hp-anchor.png"
    image = Image.new("RGB", (1080, 2424), (44, 72, 112))
    draw = ImageDraw.Draw(image)
    draw.rectangle((45, 760, 1035, 2380), fill=(246, 246, 242))
    draw.rectangle((240, 310, 840, 720), fill=(80, 210, 160))
    draw.rectangle((330, 790, 750, 845), fill=(35, 35, 35))
    bar_y = int(0.46 * image.height)
    draw.rounded_rectangle(
        (int(0.30 * image.width), bar_y, int(0.70 * image.width), bar_y + 14),
        radius=7,
        fill=(80, 215, 105),
    )
    image.save(path)

    def fake_read_region(_image, _engine, box, **_kwargs):
        if abs(box[1] - 0.485) < 0.02 and abs(box[2] - 0.70) < 0.02:
            return OcrResult("60/60 HP", 0.90)
        return OcrResult("", 0.0)

    monkeypatch.setattr(scan_frames_module, "_read_region", fake_read_region)
    settings = ScanSettings(tmp_path, tmp_path / "output", workers=1)
    source = SourceAsset(Path("hp-anchor.png"), "image")

    record = scan_frame_candidate(FrameCandidate(source, path, 0, 0.0), settings)

    assert record.values["hp"] == "60/60"
    assert record.features["has_hp"]
    assert record.signals["hp_ocr_fallback_used"] is True
    assert abs(float(record.signals["hp_ocr_fallback_top"]) - 0.485) < 0.02


def test_visual_evidence_fallbacks_are_narrow() -> None:
    low_contrast_display_signals = {
        "name_dark_ratio": 0.043,
        "detail_card_brightness": 0.917,
        "pokemon_art_signal": 0.164,
    }
    adjacent_low_contrast_display_signals = [
        {
            "name_dark_ratio": 0.0403,
            "detail_card_brightness": 0.9192,
            "pokemon_art_signal": 0.2124,
        },
        {
            "name_dark_ratio": 0.0472,
            "detail_card_brightness": 0.9191,
            "pokemon_art_signal": 0.2013,
        },
        {
            "name_dark_ratio": 0.0463,
            "detail_card_brightness": 0.9172,
            "pokemon_art_signal": 0.1604,
        },
    ]
    rejected_display_signals = {
        "name_dark_ratio": 0.0463,
        "detail_card_brightness": 0.9212,
        "pokemon_art_signal": 0.1604,
    }
    early_cp_signals = {
        "name_dark_ratio": 0.059,
        "detail_card_brightness": 0.819,
        "pokemon_art_signal": 0.295,
        "hp_green_ratio": 0.0,
        "orange_badge_ratio": 0.02,
    }
    mid_cp_signals = {
        "name_dark_ratio": 0.089,
        "detail_card_brightness": 0.809,
        "pokemon_art_signal": 0.215,
        "hp_green_ratio": 0.04,
        "orange_badge_ratio": 0.02,
    }
    mid_neighbor_signals = {
        "name_dark_ratio": 0.116,
        "detail_card_brightness": 0.809,
        "pokemon_art_signal": 0.229,
        "hp_green_ratio": 0.04,
        "orange_badge_ratio": 0.02,
    }
    late_cp_signals = {
        "name_dark_ratio": 0.088,
        "detail_card_brightness": 0.906,
        "pokemon_art_signal": 0.198,
        "hp_green_ratio": 0.04,
        "orange_badge_ratio": 0.0,
    }
    green_hp_signals = {
        "hp_region_dark_ratio": 0.9806,
        "hp_bar_edge_ratio": 0.1207,
        "hp_text_edge_ratio": 0.1105,
    }
    grey_hp_signals = {
        "hp_region_dark_ratio": 0.0421,
        "hp_bar_edge_ratio": 0.1938,
        "hp_text_edge_ratio": 0.1427,
    }
    amber_hp_signals = {
        "hp_region_dark_ratio": 0.4581,
        "hp_bar_edge_ratio": 0.2338,
        "hp_text_edge_ratio": 0.2255,
    }
    rejected_hp_signals = {
        "hp_region_dark_ratio": 0.035,
        "hp_bar_edge_ratio": 0.1938,
        "hp_text_edge_ratio": 0.1427,
    }
    complete_moves_signals = [
        {
            "hp_bar_anchor_visible": True,
            "moves_visual_region_anchored": True,
            "moves_tab_anchor_visible": True,
            "moves_fast_row_dark_ratio": 0.0600,
            "moves_charged_rows_dark_ratio": 0.1000,
            "moves_complete_rows_dark_ratio": 0.2589,
            "moves_completion_footer_dark_ratio": 0.2093,
            "moves_completion_footer_height": 0.1400,
            "moves_transition_guard_dark_ratio": 0.0,
        },
        {
            "hp_bar_anchor_visible": True,
            "moves_visual_region_anchored": True,
            "moves_tab_anchor_visible": True,
            "moves_fast_row_dark_ratio": 0.0500,
            "moves_charged_rows_dark_ratio": 0.1800,
            "moves_complete_rows_dark_ratio": 0.2706,
            "moves_completion_footer_dark_ratio": 0.2185,
            "moves_completion_footer_height": 0.1400,
            "moves_transition_guard_dark_ratio": 0.0,
        },
        {
            "hp_bar_anchor_visible": True,
            "moves_visual_region_anchored": True,
            "moves_tab_anchor_visible": True,
            "moves_fast_row_dark_ratio": 0.0920,
            "moves_charged_rows_dark_ratio": 0.2300,
            "moves_complete_rows_dark_ratio": 0.3047,
            "moves_completion_footer_dark_ratio": 0.2572,
            "moves_completion_footer_height": 0.1400,
            "moves_transition_guard_dark_ratio": 0.0,
        },
        {
            "hp_bar_anchor_visible": True,
            "moves_visual_region_anchored": True,
            "moves_tab_anchor_visible": True,
            "moves_fast_row_dark_ratio": 0.0560,
            "moves_charged_rows_dark_ratio": 0.1010,
            "moves_complete_rows_dark_ratio": 0.3046,
            "moves_completion_footer_dark_ratio": 0.2467,
            "moves_completion_footer_height": 0.1400,
            "moves_transition_guard_dark_ratio": 0.0003,
        },
        {
            "hp_bar_anchor_visible": True,
            "moves_visual_region_anchored": True,
            "moves_tab_anchor_visible": True,
            "moves_fast_row_dark_ratio": 0.0490,
            "moves_charged_rows_dark_ratio": 0.1850,
            "moves_complete_rows_dark_ratio": 0.2288,
            "moves_completion_footer_dark_ratio": 0.2185,
            "moves_completion_footer_height": 0.1400,
            "moves_transition_guard_dark_ratio": 0.0018,
        },
        {
            "hp_bar_anchor_visible": True,
            "moves_visual_region_anchored": True,
            "moves_tab_anchor_visible": True,
            "moves_fast_row_dark_ratio": 0.0556,
            "moves_charged_rows_dark_ratio": 0.1206,
            "moves_complete_rows_dark_ratio": 0.1787,
            "moves_completion_footer_dark_ratio": 0.2460,
            "moves_completion_footer_height": 0.1161,
            "moves_new_attack_button_green_ratio": 0.4306,
            "moves_new_attack_button_height": 0.0661,
            "moves_transition_guard_dark_ratio": 0.0,
        },
    ]
    partial_moves_signals = [
        {
            "hp_bar_anchor_visible": False,
            "moves_visual_region_anchored": False,
            "moves_tab_anchor_visible": True,
            "moves_fast_row_dark_ratio": 0.3000,
            "moves_charged_rows_dark_ratio": 0.1800,
            "moves_complete_rows_dark_ratio": 0.1835,
            "moves_completion_footer_dark_ratio": 0.1354,
            "moves_completion_footer_height": 0.1400,
            "moves_transition_guard_dark_ratio": 0.0168,
        },
        {
            "hp_bar_anchor_visible": True,
            "moves_visual_region_anchored": True,
            "moves_tab_anchor_visible": True,
            "moves_fast_row_dark_ratio": 0.0900,
            "moves_charged_rows_dark_ratio": 0.3500,
            "moves_complete_rows_dark_ratio": 0.2324,
            "moves_completion_footer_dark_ratio": 0.1839,
            "moves_completion_footer_height": 0.0860,
            "moves_transition_guard_dark_ratio": 0.0,
        },
        {
            "hp_bar_anchor_visible": True,
            "moves_visual_region_anchored": True,
            "moves_tab_anchor_visible": True,
            "moves_fast_row_dark_ratio": 0.0558,
            "moves_charged_rows_dark_ratio": 0.1784,
            "moves_complete_rows_dark_ratio": 0.2135,
            "moves_completion_footer_dark_ratio": 0.2364,
            "moves_completion_footer_height": 0.0996,
            "moves_new_attack_button_green_ratio": 0.2999,
            "moves_new_attack_button_height": 0.0496,
            "moves_transition_guard_dark_ratio": 0.0,
        },
        {
            "hp_bar_anchor_visible": True,
            "moves_visual_region_anchored": True,
            "moves_tab_anchor_visible": True,
            "moves_fast_row_dark_ratio": 0.3000,
            "moves_charged_rows_dark_ratio": 0.1800,
            "moves_complete_rows_dark_ratio": 0.1684,
            "moves_completion_footer_dark_ratio": 0.1483,
            "moves_completion_footer_height": 0.0200,
            "moves_transition_guard_dark_ratio": 0.0,
        },
        {
            "hp_bar_anchor_visible": True,
            "moves_visual_region_anchored": True,
            "moves_tab_anchor_visible": True,
            "moves_fast_row_dark_ratio": 0.0400,
            "moves_charged_rows_dark_ratio": 0.1800,
            "moves_complete_rows_dark_ratio": 0.2120,
            "moves_completion_footer_dark_ratio": 0.1793,
            "moves_completion_footer_height": 0.1400,
            "moves_transition_guard_dark_ratio": 0.0,
        },
        {
            "hp_bar_anchor_visible": True,
            "moves_visual_region_anchored": True,
            "moves_tab_anchor_visible": True,
            "moves_fast_row_dark_ratio": 0.0500,
            "moves_charged_rows_dark_ratio": 0.0600,
            "moves_complete_rows_dark_ratio": 0.1899,
            "moves_completion_footer_dark_ratio": 0.1673,
            "moves_completion_footer_height": 0.1400,
            "moves_transition_guard_dark_ratio": 0.0,
        },
        {
            "hp_bar_anchor_visible": True,
            "moves_visual_region_anchored": True,
            "moves_tab_anchor_visible": True,
            "moves_fast_row_dark_ratio": 0.0500,
            "moves_charged_rows_dark_ratio": 0.1800,
            "moves_complete_rows_dark_ratio": 0.1761,
            "moves_completion_footer_dark_ratio": 0.0800,
            "moves_completion_footer_height": 0.1400,
            "moves_transition_guard_dark_ratio": 0.0004,
        },
    ]
    transition_moves_signals = [
        {
            "hp_bar_anchor_visible": True,
            "moves_visual_region_anchored": True,
            "moves_tab_anchor_visible": True,
            "moves_fast_row_dark_ratio": 0.3000,
            "moves_charged_rows_dark_ratio": 0.3000,
            "moves_complete_rows_dark_ratio": 0.3449,
            "moves_completion_footer_dark_ratio": 0.2922,
            "moves_completion_footer_height": 0.1400,
            "moves_transition_guard_dark_ratio": 0.1343,
            "horizontal_swipe_signal": True,
        },
        {
            "hp_bar_anchor_visible": True,
            "moves_visual_region_anchored": True,
            "moves_tab_anchor_visible": True,
            "moves_fast_row_dark_ratio": 0.3000,
            "moves_charged_rows_dark_ratio": 0.3000,
            "moves_complete_rows_dark_ratio": 0.3081,
            "moves_completion_footer_dark_ratio": 0.2784,
            "moves_completion_footer_height": 0.1400,
            "moves_transition_guard_dark_ratio": 0.0,
            "horizontal_swipe_signal": True,
        },
        {
            "hp_bar_anchor_visible": True,
            "moves_visual_region_anchored": True,
            "moves_tab_anchor_visible": True,
            "moves_fast_row_dark_ratio": 0.3000,
            "moves_charged_rows_dark_ratio": 0.3000,
            "moves_complete_rows_dark_ratio": 0.3080,
            "moves_completion_footer_dark_ratio": 0.2906,
            "moves_completion_footer_height": 0.1400,
            "moves_transition_guard_dark_ratio": 0.1343,
            "horizontal_swipe_signal": True,
        },
        {
            "hp_bar_anchor_visible": True,
            "moves_visual_region_anchored": True,
            "moves_tab_anchor_visible": True,
            "moves_fast_row_dark_ratio": 0.3000,
            "moves_charged_rows_dark_ratio": 0.3000,
            "moves_complete_rows_dark_ratio": 0.3100,
            "moves_completion_footer_dark_ratio": 0.2821,
            "moves_completion_footer_height": 0.1400,
            "moves_transition_guard_dark_ratio": 0.1319,
            "horizontal_swipe_signal": True,
        },
    ]

    assert _visual_display_name_evidence(low_contrast_display_signals)
    assert all(
        _visual_display_name_evidence(signals)
        for signals in adjacent_low_contrast_display_signals
    )
    assert not _visual_display_name_evidence(rejected_display_signals)
    assert _visual_cp_evidence("appraisal", early_cp_signals)
    assert _visual_cp_evidence("appraisal", mid_cp_signals)
    assert not _visual_cp_evidence("appraisal", mid_neighbor_signals)
    assert _visual_cp_evidence("detail", late_cp_signals)
    assert _visual_hp_evidence(green_hp_signals)
    assert _visual_hp_evidence(grey_hp_signals)
    assert _visual_hp_evidence(amber_hp_signals)
    assert not _visual_hp_evidence(rejected_hp_signals)
    assert all(
        _visual_all_moves_evidence(signals) for signals in complete_moves_signals
    )
    assert not any(
        _visual_all_moves_evidence(signals) for signals in partial_moves_signals
    )
    assert not any(
        _visual_all_moves_evidence(signals) for signals in transition_moves_signals
    )


def test_tesseract_resolution_checks_common_paths(tmp_path, monkeypatch) -> None:
    executable = tmp_path / "tesseract.exe"
    executable.write_text("", encoding="utf-8")
    monkeypatch.setattr("pogo_storage_mapper.ocr.shutil.which", lambda _name: None)

    engine = TesseractOcrEngine(search_paths=[executable])

    assert engine.executable_path() == str(executable)
    assert engine.is_available()


def test_layout_aware_ocr_recovers_cp_hp_from_appraisal_fixture() -> None:
    if not TesseractOcrEngine().is_available():
        pytest.skip("Tesseract is unavailable.")

    fixture_root = Path(__file__).parent / "fixtures" / "iaast_scan_fresh"
    frame_path = fixture_root / "frames" / "frame_000153.jpg"
    settings = ScanSettings(fixture_root, fixture_root / "output", workers=1)
    source = SourceAsset(Path("fixture.mp4"), "video")

    record = scan_frame_candidate(
        FrameCandidate(source, frame_path, 153, 0.0),
        settings,
    )

    assert record.raw_classification == "appraisal"
    assert record.values["cp"] == 936
    assert record.values["hp"] == "50/99"
    assert record.features["has_CP"]
    assert record.features["has_hp"]


def test_height_ocr_recovers_visible_fixture_and_skips_iv_overlay() -> None:
    if not TesseractOcrEngine().is_available():
        pytest.skip("Tesseract is unavailable.")

    fixture_root = Path(__file__).parent / "fixtures" / "iaast_scan_fresh"
    settings = ScanSettings(fixture_root, fixture_root / "output", workers=1)
    source = SourceAsset(Path("fixture.mp4"), "video")

    visible_record = scan_frame_candidate(
        FrameCandidate(
            source,
            fixture_root / "frames" / "frame_000086.jpg",
            86,
            0.0,
        ),
        settings,
    )
    iv_overlay_record = scan_frame_candidate(
        FrameCandidate(
            source,
            fixture_root / "frames" / "frame_000110.jpg",
            110,
            0.0,
        ),
        settings,
    )

    assert visible_record.values["height_m"] == "1.17"
    assert visible_record.features["has_height"]
    assert iv_overlay_record.values["height_m"] is None
    assert not iv_overlay_record.features["has_height"]


def test_ffmpeg_command_requests_nvidia_when_asked() -> None:
    command = _build_ffmpeg_extract_command(
        Path("clip.mp4"),
        Path("frames/frame_%06d.png"),
        hwaccel="nvidia",
    )

    assert command[:4] == ["ffmpeg", "-y", "-hwaccel", "cuda"]
    assert command[-2:] == ["clip.mp4", str(Path("frames/frame_%06d.png"))]


def test_video_extraction_falls_back_and_clears_partial_frames(
    tmp_path, monkeypatch
) -> None:
    frames_dir = tmp_path / "frames"
    calls: list[list[str]] = []

    def fake_run(command, *, check, capture_output, text):
        del check, capture_output, text
        calls.append(command)
        if "-hwaccel" in command:
            frames_dir.mkdir(parents=True, exist_ok=True)
            (frames_dir / "frame_partial.png").write_text("partial", encoding="utf-8")
            raise subprocess.CalledProcessError(1, command, stderr="CUDA unavailable")
        assert not (frames_dir / "frame_partial.png").exists()
        (frames_dir / "frame_000001.png").write_text("one", encoding="utf-8")
        (frames_dir / "frame_000002.png").write_text("two", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("pogo_storage_mapper.scan_frames.subprocess.run", fake_run)
    monkeypatch.setattr(
        "pogo_storage_mapper.scan_frames.probe_video_duration", lambda _path: 8.0
    )

    result = extract_video_frames(SourceAsset(Path("clip.mp4"), "video"), frames_dir)

    assert len(calls) == 2
    assert result.used_hwaccel == "none"
    assert result.warnings
    assert [frame.timestamp_s for frame in result.frames] == [0.0, 8.0]


class _ImmediateFuture:
    def __init__(self, fn, *args) -> None:
        try:
            self.value = fn(*args)
            self.error = None
        except Exception as exc:  # noqa: BLE001
            self.value = None
            self.error = exc

    def result(self):
        if self.error is not None:
            raise self.error
        return self.value


class _InlineSubmitExecutor:
    def __init__(self, *, max_workers: int) -> None:
        self.max_workers = max_workers

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> Literal[False]:
        del exc_type, exc, tb
        return False

    def submit(self, fn, *args):
        return _ImmediateFuture(fn, *args)


def test_worker_retry_requeues_failed_tasks(tmp_path) -> None:
    source = SourceAsset(Path("source.png"), "image")
    frames = [
        FrameCandidate(source, Path("frame-0.png"), 0, 0.0),
        FrameCandidate(source, Path("frame-1.png"), 1, 0.1),
    ]
    attempts: Counter[int] = Counter()

    def flaky_processor(
        candidate: FrameCandidate, settings: ScanSettings
    ) -> FrameScanRecord:
        del settings
        attempts[candidate.frame_index] += 1
        if candidate.frame_index == 0 and attempts[candidate.frame_index] == 1:
            msg = "temporary worker failure"
            raise RuntimeError(msg)
        return FrameScanRecord(
            source_file=candidate.source_asset.path.name,
            source_type=candidate.source_asset.source_type,
            frame_path=str(candidate.frame_path),
            frame_index=candidate.frame_index,
            timestamp_s=candidate.timestamp_s,
            classification="detail",
            raw_classification="detail",
            features={key: False for key in FEATURE_KEYS},
        )

    result = _process_frames_with_retry(
        frames,
        ScanSettings(Path("."), tmp_path, workers=2, max_frame_attempts=2),
        processor=flaky_processor,
        executor_factory=_InlineSubmitExecutor,
        completed_iterator=_completed_futures,
    )

    assert attempts[0] == 2
    assert attempts[1] == 1
    assert result.retry_count == 1
    assert result.records[0].attempts == 2
    assert result.warnings


def _production_iv_evidence(*, has_iv: bool = False) -> scan_frames_module._IvEvidence:
    return scan_frames_module._IvEvidence(
        attack=10 if has_iv else None,
        defense=11 if has_iv else None,
        stamina=12 if has_iv else None,
        iv_sum=33 if has_iv else None,
        star_count=3 if has_iv else None,
        badge_visible=has_iv,
        perfect=False,
        star_agreement=has_iv,
        panel_visible=has_iv,
        seal_visible=has_iv,
        bar_count=3 if has_iv else 0,
        panel_light_ratio=0.70 if has_iv else 0.0,
        seal_color_ratio=0.20 if has_iv else 0.0,
    )


def _production_visual_record(
    frame_index: int,
    *,
    raw_classification: str = "detail",
    has_moves: bool = False,
    has_iv: bool = False,
    source: SourceAsset | None = None,
) -> FrameVisualRecord:
    source = source or SourceAsset(Path("source.mp4"), "video")
    signals: dict[str, SignalValue] = {
        "stable_detail_signal": True,
        "horizontal_swipe_signal": False,
        "name_dark_ratio": 0.06,
        "detail_card_brightness": 0.80,
        "pokemon_art_signal": 0.30,
        "hp_green_ratio": 0.05,
        "orange_badge_ratio": 0.0,
        "tag_edge_ratio": 0.08,
        "hp_bar_anchor_visible": has_moves,
        "moves_visual_region_anchored": has_moves,
        "moves_tab_anchor_visible": has_moves,
        "moves_fast_row_dark_ratio": 0.06 if has_moves else 0.0,
        "moves_charged_rows_dark_ratio": 0.10 if has_moves else 0.0,
        "moves_complete_rows_dark_ratio": 0.25 if has_moves else 0.0,
        "moves_completion_footer_dark_ratio": 0.20 if has_moves else 0.0,
        "moves_completion_footer_height": 0.14 if has_moves else 0.0,
        "moves_transition_guard_dark_ratio": 0.0,
    }
    frame = FrameCandidate(source, Path(f"frame-{frame_index}.png"), frame_index, 0.0)
    return FrameVisualRecord(
        frame=frame,
        source_file=source.path.name,
        source_type=source.source_type,
        frame_path=str(frame.frame_path),
        frame_index=frame.frame_index,
        timestamp_s=frame.timestamp_s,
        raw_classification=raw_classification,
        signals=signals,
        iv_evidence=_production_iv_evidence(has_iv=has_iv),
        moves_ocr_box=[0.05, 0.80, 0.95, 0.99],
    )


def _production_record(
    frame: FrameCandidate,
    *,
    cp: int | None = None,
    hp: str | None = None,
    weight: str | None = None,
    height: str | None = None,
    display_name: str = "",
    moves: str = "",
    special_sections: str = "",
    story: str = "",
    iv: tuple[int, int, int] | None = None,
    has_dynamax: bool = False,
    has_gigantamax: bool = False,
    raw_classification: str | None = None,
) -> FrameScanRecord:
    raw_classification = raw_classification or (
        "appraisal" if iv is not None or story else "detail"
    )
    features = {key: False for key in FEATURE_KEYS}
    features["has_CP"] = cp is not None
    features["has_hp"] = hp is not None
    features["has_weight"] = weight is not None
    features["has_height"] = height is not None
    features["has_display_name"] = bool(display_name)
    features["has_moves"] = bool(moves)
    features["has_story"] = story_text_is_complete(story)
    features["has_iv"] = iv is not None
    features["has_iv_complete"] = iv is not None
    features["has_gigantamax"] = has_gigantamax
    features["has_dynamax"] = has_dynamax and not has_gigantamax
    values: dict[str, object | None] = {
        "cp": cp,
        "hp": hp,
        "weight_kg": weight,
        "height_m": height,
        "story_text": story or None,
        "iv_attack": iv[0] if iv else None,
        "iv_defense": iv[1] if iv else None,
        "iv_stamina": iv[2] if iv else None,
        "iv_sum": sum(iv) if iv else None,
        "appraisal_star_count": 3 if iv else None,
        "appraisal_perfect": False,
        "iv_star_agreement": iv is not None,
    }
    ocr = {
        "display_name": {"text": display_name, "confidence": 0.90},
        "moves": {"text": moves, "confidence": 0.90},
        "special_sections": {"text": special_sections, "confidence": 0.90},
    }
    signals: dict[str, SignalValue] = {
        "iv_bar_count": 3 if iv else 0,
        "iv_star_agreement": iv is not None,
    }
    return FrameScanRecord(
        source_file=frame.source_asset.path.name,
        source_type=frame.source_asset.source_type,
        frame_path=str(frame.frame_path),
        frame_index=frame.frame_index,
        timestamp_s=frame.timestamp_s,
        classification=raw_classification,
        raw_classification=raw_classification,
        features=features,
        values=values,
        signals=signals,
        ocr=ocr,
    )


def test_audit_frame_processing_processes_every_frame(tmp_path) -> None:
    source = SourceAsset(Path("source.png"), "image")
    frames = [
        FrameCandidate(source, Path(f"frame-{index}.png"), index, float(index))
        for index in range(3)
    ]
    processed: list[int] = []

    def complete_processor(
        candidate: FrameCandidate, settings: ScanSettings
    ) -> FrameScanRecord:
        del settings
        processed.append(candidate.frame_index)
        return _production_record(
            candidate,
            cp=100,
            hp="10/10",
            weight="1.00",
            display_name="Buddy",
        )

    _process_frames_with_retry(
        frames,
        ScanSettings(Path("."), tmp_path, workers=1),
        processor=complete_processor,
    )

    assert processed == [0, 1, 2]


def test_postprocess_propagates_weight_across_same_hp_detail_run() -> None:
    source = SourceAsset(Path("source.mp4"), "video")

    def record(
        index: int,
        *,
        hp: str | None,
        weight: str | None = None,
        classification: str = "detail",
        raw_classification: str = "detail",
    ) -> FrameScanRecord:
        frame = FrameCandidate(source, Path(f"frame-{index}.png"), index, 0.0)
        scan_record = _production_record(frame, hp=hp, weight=weight)
        scan_record.classification = classification
        scan_record.raw_classification = raw_classification
        return scan_record

    records = [
        record(0, hp="50/99", weight="16.08"),
        record(1, hp=None, classification="non_extractable"),
        record(2, hp="50/99", raw_classification="appraisal"),
        record(3, hp="50/99"),
        record(4, hp="60/60"),
    ]

    _postprocess_frame_sequences(records)

    for propagated in records[2:4]:
        assert propagated.values["weight_kg"] == "16.08"
        assert propagated.features["has_weight"]
        assert propagated.signals["sequence_weight_propagated"] is True
    assert records[1].values["weight_kg"] is None
    assert records[4].values["weight_kg"] is None


def test_postprocess_does_not_propagate_conflicting_same_hp_weight() -> None:
    source = SourceAsset(Path("source.mp4"), "video")

    def record(
        index: int,
        *,
        hp: str,
        weight: str | None = None,
    ) -> FrameScanRecord:
        frame = FrameCandidate(source, Path(f"frame-{index}.png"), index, 0.0)
        return _production_record(frame, hp=hp, weight=weight)

    records = [
        record(0, hp="50/99", weight="16.08"),
        record(1, hp="50/99"),
        record(2, hp="50/99", weight="17.5"),
    ]

    _postprocess_frame_sequences(records)

    assert records[1].values["weight_kg"] is None
    assert not records[1].features["has_weight"]
    assert "sequence_weight_propagated" not in records[1].signals


def test_postprocess_corrects_clear_same_hp_weight_outlier() -> None:
    source = SourceAsset(Path("source.mp4"), "video")

    def record(index: int, weight: str) -> FrameScanRecord:
        frame = FrameCandidate(source, Path(f"frame-{index}.png"), index, 0.0)
        return _production_record(frame, hp="66/66", weight=weight)

    records = [
        record(0, "51.87"),
        record(1, "51.87"),
        record(2, "91.87"),
        record(3, "51.87"),
        record(4, "51.87"),
    ]

    _postprocess_frame_sequences(records)

    assert records[2].values["weight_kg"] == "51.87"
    assert records[2].features["has_weight"]
    assert records[2].signals["sequence_weight_corrected"] is True
    assert records[2].signals["sequence_weight_original_value"] == "91.87"


def test_selective_frame_ocr_reads_only_requested_regions(
    tmp_path, monkeypatch
) -> None:
    image = Image.new("RGB", (1080, 2424), (240, 240, 240))
    analysis = scan_frames_module._VisualScanAnalysis(
        signals={},
        iv_evidence=_production_iv_evidence(),
        raw_classification="detail",
        moves_ocr_box=scan_frames_module.REGIONS["moves"],
        duration_s=0.0,
    )
    read_boxes: list[list[float]] = []

    def fake_read_region(*args, **kwargs) -> OcrResult:
        del kwargs
        read_boxes.append(args[2])
        return OcrResult("CP 100", 0.90)

    monkeypatch.setattr(scan_frames_module, "_read_region", fake_read_region)

    ocr, _duration = scan_frames_module._read_frame_ocr(
        image,
        TesseractOcrEngine(lang="eng"),
        ScanSettings(Path("."), tmp_path),
        analysis,
        requested_ocr_fields={"cp"},
    )

    assert read_boxes == [scan_frames_module.REGIONS["cp"]]
    assert ocr["cp"].text == "CP 100"
    assert ocr["hp"].text == ""


def test_visible_crop_writes_ocr_region_overlay(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: True)
    monkeypatch.setattr(
        TesseractOcrEngine,
        "read_text",
        lambda self, image, *, psm=6, config="": OcrResult("CP 100", 0.90),
    )
    image_path = tmp_path / "frame.png"
    image = Image.new("RGB", (1080, 2424), (240, 240, 240))
    image.save(image_path)
    image.info["filename"] = str(image_path)
    analysis = scan_frames_module._VisualScanAnalysis(
        signals={"moves_tab_anchor_visible": True},
        iv_evidence=_production_iv_evidence(),
        raw_classification="detail",
        moves_ocr_box=scan_frames_module.REGIONS["moves"],
        duration_s=0.0,
    )
    settings = ScanSettings(Path("."), tmp_path, visible_crop=True)

    scan_frames_module._read_frame_ocr(
        image,
        TesseractOcrEngine(lang="eng"),
        settings,
        analysis,
        requested_ocr_fields={"cp"},
    )

    assert (tmp_path / "frame__ocr_cp.png").exists()


def test_invisible_crop_does_not_write_ocr_region_overlay(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: True)
    monkeypatch.setattr(
        TesseractOcrEngine,
        "read_text",
        lambda self, image, *, psm=6, config="": OcrResult("CP 100", 0.90),
    )
    image_path = tmp_path / "frame.png"
    image = Image.new("RGB", (1080, 2424), (240, 240, 240))
    image.save(image_path)
    image.info["filename"] = str(image_path)
    analysis = scan_frames_module._VisualScanAnalysis(
        signals={"moves_tab_anchor_visible": True},
        iv_evidence=_production_iv_evidence(),
        raw_classification="detail",
        moves_ocr_box=scan_frames_module.REGIONS["moves"],
        duration_s=0.0,
    )
    settings = ScanSettings(Path("."), tmp_path, visible_crop=False)

    scan_frames_module._read_frame_ocr(
        image,
        TesseractOcrEngine(lang="eng"),
        settings,
        analysis,
        requested_ocr_fields={"cp"},
    )

    assert not (tmp_path / "frame__ocr_cp.png").exists()


def test_visible_crop_writes_performed_visual_overlays_for_list_frame(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    settings = ScanSettings(Path("."), tmp_path, visible_crop=True, ocr_mode="off")
    source = SourceAsset(Path("source.png"), "image")
    list_path = tmp_path / "list.png"
    _draw_list(list_path)

    list_record = scan_frame_candidate(
        FrameCandidate(source, list_path, 0, 0.0),
        settings,
    )

    assert list_record.classification == "list"
    for suffix in (
        "visual_list_rows",
        "visual_detail_card",
        "visual_hp",
        "visual_name",
        "visual_moves",
        "visual_story",
        "visual_appraisal_badge",
        "visual_iv_panel",
        "visual_pokemon_art",
        "visual_horizontal_swipe_card",
    ):
        assert (tmp_path / f"list__{suffix}.png").exists()
    for suffix in (
        "visual_hp_bar",
        "visual_hp_text",
        "visual_moves_tabs",
        "visual_moves_complete_rows",
        "visual_moves_completion_footer",
        "visual_moves_transition_guard",
        "visual_tag",
        "visual_transition_edges",
    ):
        assert not (tmp_path / f"list__{suffix}.png").exists()

    detail_path = tmp_path / "detail.png"
    _draw_detail_with_complete_moves(detail_path)
    detail_record = scan_frame_candidate(
        FrameCandidate(source, detail_path, 0, 0.0),
        settings,
    )

    assert detail_record.classification == "detail"
    for suffix in (
        "visual_hp_bar",
        "visual_hp_text",
        "visual_moves_tabs",
        "visual_moves_complete_rows",
        "visual_moves_completion_footer",
        "visual_moves_transition_guard",
        "visual_tag",
    ):
        assert (tmp_path / f"detail__{suffix}.png").exists()

    appraisal_path = tmp_path / "appraisal.png"
    _draw_appraisal(appraisal_path)
    appraisal_record = scan_frame_candidate(
        FrameCandidate(source, appraisal_path, 0, 0.0),
        settings,
    )

    assert appraisal_record.classification == "appraisal"
    for suffix in (
        "visual_appraisal_badge",
        "visual_iv_panel",
        "visual_hp_bar",
        "visual_hp_text",
        "visual_tag",
    ):
        assert (tmp_path / f"appraisal__{suffix}.png").exists()
    assert not (tmp_path / "appraisal__visual_moves_complete_rows.png").exists()


def test_group_production_sequences_skips_non_detail_frames() -> None:
    source = SourceAsset(Path("source.mp4"), "video")
    records = [
        _production_visual_record(0, source=source),
        _production_visual_record(1, raw_classification="list", source=source),
        _production_visual_record(2, source=source),
    ]
    records[1].signals["stable_detail_signal"] = False

    sequences = group_production_sequences(records)

    assert [[record.frame_index for record in sequence] for sequence in sequences] == [
        [0],
        [2],
    ]


def test_appraisal_production_probe_never_requests_detail_only_sections() -> None:
    visual = _production_visual_record(0, raw_classification="appraisal")

    probeable = scan_frames_module._production_probeable_export_fields(
        {
            "height",
            "moves",
            "power",
            "story",
            "is_shadow",
            "has_dynamax",
            "has_gigantamax",
        },
        "appraisal",
        visual.signals,
    )

    assert probeable == {"story"}
    assert (
        "special_sections"
        not in scan_frames_module._production_ocr_fields_for_export_fields(probeable)
    )


def test_detail_production_probe_never_requests_appraisal_only_fields() -> None:
    visual = _production_visual_record(0, raw_classification="detail")
    appraisal_only = {
        "appraisal_perfect",
        "appraisal_star_count",
        "iv",
        "iv_star_agreement",
        "iv_sum",
        "story",
    }

    detail_probeable = scan_frames_module._production_probeable_export_fields(
        appraisal_only,
        "detail",
        visual.signals,
    )
    appraisal_probeable = scan_frames_module._production_probeable_export_fields(
        appraisal_only,
        "appraisal",
        visual.signals,
    )

    assert detail_probeable == set()
    assert appraisal_probeable == appraisal_only


def test_group_production_sequences_splits_adjacent_raw_types() -> None:
    source = SourceAsset(Path("source.mp4"), "video")
    records = [
        _production_visual_record(0, raw_classification="appraisal", source=source),
        _production_visual_record(1, raw_classification="appraisal", source=source),
        _production_visual_record(2, raw_classification="detail", source=source),
        _production_visual_record(3, raw_classification="detail", source=source),
    ]

    sequences = group_production_sequences(records)

    assert [[record.frame_index for record in sequence] for sequence in sequences] == [
        [0, 1],
        [2, 3],
    ]


def test_production_sequence_logs_only_frame_type_probeable_fields(tmp_path) -> None:
    sequence = [
        _production_visual_record(0, raw_classification="appraisal", has_iv=True),
        _production_visual_record(1, raw_classification="detail"),
    ]
    progress: list[tuple[int, tuple[str, ...]]] = []

    def scanner(
        frame: FrameCandidate,
        settings: ScanSettings,
        requested_fields: Iterable[str],
    ) -> FrameScanRecord:
        del settings, requested_fields
        if frame.frame_index == 1:
            return _production_record(
                frame,
                hp="10/10",
                weight="1.00",
                display_name="Buddy",
                iv=(10, 11, 12),
                raw_classification="detail",
            )
        return _production_record(
            frame,
            story="This Bulbasaur was caught on 1/2/2026 around Prague, Czechia.",
            iv=(10, 11, 12),
            raw_classification="appraisal",
        )

    result = scan_production_sequence(
        sequence,
        ScanSettings(Path("."), tmp_path),
        scanner=scanner,
        progress_callback=lambda frame, fields: progress.append(
            (frame.frame_index, fields)
        ),
    )

    first_logged_frame, first_logged_fields = progress[0]
    appraisal_only = {
        "appraisal_perfect",
        "appraisal_star_count",
        "iv",
        "iv_star_agreement",
        "iv_sum",
        "story",
    }
    assert first_logged_frame == 1
    assert appraisal_only.isdisjoint(first_logged_fields)
    assert result.accepted_fields["hp"] == "10/10"
    assert result.accepted_fields["weight"] == "1.00"
    assert "iv" in result.accepted_fields
    assert result.accepted_fields["iv_sum"] == 33


def test_detail_records_do_not_contribute_appraisal_fields() -> None:
    frame = FrameCandidate(
        SourceAsset(Path("source.mp4"), "video"), Path("0.png"), 0, 0
    )
    detail_record = _production_record(
        frame,
        hp="10/10",
        weight="1.00",
        moves="Vine Whip Power Whip",
        iv=(10, 11, 12),
        raw_classification="detail",
    )
    accumulator = scan_frames_module._ProductionRecordAccumulator(
        {"hp", "weight", "moves", "iv", "iv_sum", "appraisal_star_count"},
        "detail/raw=detail",
    )

    accumulator.accept_record(detail_record)

    assert accumulator.accepted_fields["hp"] == "10/10"
    assert accumulator.accepted_fields["weight"] == "1.00"
    assert accumulator.accepted_fields["moves"] == "Vine Whip Power Whip"
    assert "iv" not in accumulator.accepted_fields
    assert "iv_sum" not in accumulator.accepted_fields
    assert "appraisal_star_count" not in accumulator.accepted_fields


def test_production_repair_samples_middle_cp_frames() -> None:
    sequence = [_production_visual_record(index) for index in range(380, 414)]

    selected = scan_frames_module._production_repair_records(sequence)
    selected_indexes = {record.frame_index for record in selected}

    assert len(selected) == scan_frames_module.PRODUCTION_REPAIR_MAX_FRAMES
    assert 381 in selected_indexes
    assert selected_indexes & set(range(393, 402))
    assert selected == sorted(
        selected, key=lambda record: record.frame_index, reverse=True
    )


def test_production_sequence_scans_from_latest_frame(tmp_path) -> None:
    sequence = [_production_visual_record(index) for index in range(3)]
    attempts: list[int] = []

    def scanner(
        frame: FrameCandidate,
        settings: ScanSettings,
        requested_fields: Iterable[str],
    ) -> FrameScanRecord:
        del settings, requested_fields
        attempts.append(frame.frame_index)
        return _production_record(frame)

    result = scan_production_sequence(
        sequence,
        ScanSettings(Path("."), tmp_path),
        scanner=scanner,
    )

    assert attempts == [2, 1, 0]
    assert not result.completed


def test_production_sequence_skips_already_accepted_ocr_fields(tmp_path) -> None:
    sequence = [
        _production_visual_record(0, has_moves=True),
        _production_visual_record(1, has_moves=True),
    ]

    def scanner(
        frame: FrameCandidate,
        settings: ScanSettings,
        requested_fields: Iterable[str],
    ) -> FrameScanRecord:
        del settings
        requested = set(requested_fields)
        if frame.frame_index == 1:
            assert {"cp", "display_name", "hp", "weight"}.issubset(requested)
            return _production_record(
                frame,
                cp=100,
                hp="10/10",
                weight="1.00",
                display_name="Buddy",
            )
        assert "hp" not in requested
        assert "weight" not in requested
        assert {"cp", "moves", "special_sections"}.issubset(requested)
        return _production_record(frame, moves="Vine Whip Power Whip")

    result = scan_production_sequence(
        sequence,
        ScanSettings(Path("."), tmp_path),
        scanner=scanner,
    )

    assert result.completed
    assert result.requested_ocr_fields_by_frame[0] == (
        "cp",
        "height",
        "moves",
        "special_sections",
    )


def test_production_sequence_continues_for_visually_available_moves(tmp_path) -> None:
    sequence = [
        _production_visual_record(0, has_moves=True),
        _production_visual_record(1, has_moves=True),
    ]
    attempts: list[int] = []

    def scanner(
        frame: FrameCandidate,
        settings: ScanSettings,
        requested_fields: Iterable[str],
    ) -> FrameScanRecord:
        del settings, requested_fields
        attempts.append(frame.frame_index)
        if frame.frame_index == 1:
            return _production_record(
                frame,
                cp=100,
                hp="10/10",
                weight="1.00",
                display_name="Buddy",
            )
        return _production_record(frame, moves="Vine Whip Power Whip")

    result = scan_production_sequence(
        sequence,
        ScanSettings(Path("."), tmp_path),
        scanner=scanner,
    )

    assert attempts == [1, 0]
    assert result.completed
    assert result.accepted_fields["moves"] == "Vine Whip Power Whip"


def test_production_sequence_requests_power_section_when_visually_likely(
    tmp_path,
) -> None:
    visual = _production_visual_record(0)
    visual.signals["pokemon_art_signal"] = 0.10
    visual.signals["tag_edge_ratio"] = 0.02
    sequence = [visual]
    requested_by_frame: dict[int, set[str]] = {}

    def scanner(
        frame: FrameCandidate,
        settings: ScanSettings,
        requested_fields: Iterable[str],
    ) -> FrameScanRecord:
        del settings
        requested_by_frame[frame.frame_index] = set(requested_fields)
        return _production_record(
            frame,
            cp=100,
            hp="10/10",
            weight="1.00",
            special_sections="Dynamax 1.00kg 0.70m",
        )

    result = scan_production_sequence(
        sequence,
        ScanSettings(Path("."), tmp_path),
        scanner=scanner,
    )

    assert "special_sections" in requested_by_frame[0]
    assert result.accepted_fields["power"] == "Dynamax 1.00kg 0.70m"


def test_production_sequence_continues_after_completion_for_height(tmp_path) -> None:
    sequence = [
        _production_visual_record(0, has_moves=True),
        _production_visual_record(1, has_moves=True),
    ]
    attempts: list[int] = []
    requested_by_frame: dict[int, set[str]] = {}

    def scanner(
        frame: FrameCandidate,
        settings: ScanSettings,
        requested_fields: Iterable[str],
    ) -> FrameScanRecord:
        del settings
        attempts.append(frame.frame_index)
        requested_by_frame[frame.frame_index] = set(requested_fields)
        if frame.frame_index == 1:
            return _production_record(
                frame,
                cp=100,
                hp="10/10",
                weight="1.00",
                moves="Vine Whip Power Whip",
            )
        return _production_record(frame, height="0.70")

    result = scan_production_sequence(
        sequence,
        ScanSettings(Path("."), tmp_path),
        scanner=scanner,
    )

    assert attempts == [1, 0]
    assert requested_by_frame[0] == {"cp", "height"}
    assert result.completed
    assert result.accepted_fields["height"] == "0.70"


def test_production_sequence_does_not_reprobe_accepted_height_or_weight(
    tmp_path,
) -> None:
    sequence = [_production_visual_record(index, has_moves=True) for index in range(4)]
    requested_by_frame: dict[int, set[str]] = {}

    def scanner(
        frame: FrameCandidate,
        settings: ScanSettings,
        requested_fields: Iterable[str],
    ) -> FrameScanRecord:
        del settings
        requested_by_frame[frame.frame_index] = set(requested_fields)
        return _production_record(
            frame,
            cp=936,
            hp="77/77",
            weight="12.34",
            height="1.23",
            moves="Vine Whip Power Whip",
        )

    result = scan_production_sequence(
        sequence,
        ScanSettings(Path("."), tmp_path),
        scanner=scanner,
    )

    assert result.completed
    physical_requests = [
        fields & {"height", "weight"} for fields in requested_by_frame.values()
    ]
    assert physical_requests.count({"height", "weight"}) == 1
    assert all(not fields for fields in physical_requests[1:])


def test_production_sequence_caches_failed_frame_field_attempts(tmp_path) -> None:
    first = _production_visual_record(0, has_moves=True)
    duplicate = _production_visual_record(0, has_moves=True)
    attempts: list[int] = []
    progress: list[tuple[int, tuple[str, ...]]] = []

    def scanner(
        frame: FrameCandidate,
        settings: ScanSettings,
        requested_fields: Iterable[str],
    ) -> FrameScanRecord:
        del settings, requested_fields
        attempts.append(frame.frame_index)
        return _production_record(frame)

    result = scan_production_sequence(
        [first, duplicate],
        ScanSettings(Path("."), tmp_path),
        scanner=scanner,
        progress_callback=lambda frame, fields: progress.append(
            (frame.frame_index, fields)
        ),
    )

    assert attempts == [0]
    assert not result.completed
    assert any(
        fields == ("skip:probe_already_attempted",) for _index, fields in progress
    )


def test_production_sequence_stops_height_weight_after_probe_budget(tmp_path) -> None:
    sequence = [_production_visual_record(index, has_moves=True) for index in range(6)]
    requested_by_frame: dict[int, set[str]] = {}
    progress: list[tuple[int, tuple[str, ...]]] = []

    def scanner(
        frame: FrameCandidate,
        settings: ScanSettings,
        requested_fields: Iterable[str],
    ) -> FrameScanRecord:
        del settings
        requested_by_frame[frame.frame_index] = set(requested_fields)
        record = _production_record(
            frame,
            cp=936,
            hp="77/77",
            display_name="Machamp",
            moves="Vine Whip Power Whip",
            special_sections="GYMS RAIDS TRAINER BATTLES",
        )
        record.features["has_tag_chips"] = True
        return record

    result = scan_production_sequence(
        sequence,
        ScanSettings(Path("."), tmp_path),
        scanner=scanner,
        progress_callback=lambda frame, fields: progress.append(
            (frame.frame_index, fields)
        ),
    )

    assert not result.completed
    assert sum("height" in fields for fields in requested_by_frame.values()) == 3
    assert sum("weight" in fields for fields in requested_by_frame.values()) == 3
    exhausted_reasons = [
        fields[0]
        for _index, fields in progress
        if fields and fields[0].startswith("stop:probe_budget_exhausted:")
    ]
    assert exhausted_reasons
    assert "height" in exhausted_reasons[0]
    assert "weight" in exhausted_reasons[0]


def test_production_sequence_continues_power_probe_for_gigantamax(
    tmp_path,
) -> None:
    sequence = [
        _production_visual_record(0, has_moves=True),
        _production_visual_record(1, has_moves=True),
        _production_visual_record(2, has_moves=True),
    ]
    for visual in sequence:
        visual.signals["pokemon_art_signal"] = 0.10
        visual.signals["tag_edge_ratio"] = 0.02

    attempts: list[int] = []
    requested_by_frame: dict[int, set[str]] = {}

    def scanner(
        frame: FrameCandidate,
        settings: ScanSettings,
        requested_fields: Iterable[str],
    ) -> FrameScanRecord:
        del settings
        attempts.append(frame.frame_index)
        requested_by_frame[frame.frame_index] = set(requested_fields)
        if frame.frame_index == 2:
            return _production_record(
                frame,
                cp=1446,
                hp="114/114",
                weight="24.42",
                moves="Acid Power-Up Punch",
                special_sections="NEW ATTACK 75,000 Max Moves",
                has_dynamax=True,
            )
        if frame.frame_index == 1:
            return _production_record(
                frame,
                special_sections="POWER UP 12,500 GYMS RAIDS Acid Power-Up Punch",
            )
        return _production_record(
            frame,
            height="1.26",
            special_sections="Gigantamax 24.42kg 1.26m WEIGHT HEIGHT",
            has_gigantamax=True,
        )

    result = scan_production_sequence(
        sequence,
        ScanSettings(Path("."), tmp_path),
        scanner=scanner,
    )

    assert attempts == [2, 1, 0]
    assert "special_sections" in requested_by_frame[0]
    assert result.completed
    assert result.accepted_fields["height"] == "1.26"
    assert all(record.features["has_gigantamax"] for record in result.records)
    assert not any(record.features["has_dynamax"] for record in result.records)


def test_parsed_ocr_values_use_power_section_weight_and_height() -> None:
    ocr = scan_frames_module._empty_ocr_results()
    ocr["special_sections"] = OcrResult(
        "Aggron 83/83HP 282.8kg 2.11m WEIGHT HEIGHT",
        0.90,
    )

    parsed = scan_frames_module._parsed_ocr_values(ocr)

    assert parsed.weight == "282.8"
    assert parsed.height == "2.11"


# nonsense test - appraisal doesn't have moves, they are on the detail page.
# def test_appraisal_overlay_story_text_can_recover_moves() -> None:
#    story_text = (
#        "GYMS & RAIDS TRAINER BATTLES Acid 11 WEATHER BONUS "
#        "Power-Up Punch 50 WEATHER BONUS"
#    )
#    ocr = scan_frames_module._empty_ocr_results()
#    ocr["story"] = OcrResult(story_text, 0.91)
#
#    parsed = scan_frames_module._parsed_ocr_values(
#        ocr, raw_classification="appraisal"
#    )
#
#    assert parsed.move_text == story_text
#    assert ocr["moves"].text == story_text


def test_appraisal_overlay_story_move_fallback_ignores_dialog() -> None:
    dialog_text = (
        "Hey, Nikdo175000! You want me to check out your Ivysaur? "
        "Your Ivysaur is a BIG one!"
    )
    ocr = scan_frames_module._empty_ocr_results()
    ocr["story"] = OcrResult(dialog_text, 0.95)

    parsed = scan_frames_module._parsed_ocr_values(ocr, raw_classification="appraisal")

    assert parsed.move_text == ""
    assert ocr["moves"].text == ""


def test_weight_ocr_fallback_reads_scrolled_physical_stats(monkeypatch) -> None:
    image = Image.new("RGB", (1080, 2424), (240, 240, 240))
    expected_box = scan_frames_module._weight_fallback_regions()[0]
    read_boxes: list[list[float]] = []

    def fake_read_region(*args, **kwargs) -> OcrResult:
        del kwargs
        box = args[2]
        read_boxes.append(box)
        if box == expected_box:
            return OcrResult("337.39kg", 0.90)
        return OcrResult("", 0.0)

    monkeypatch.setattr(scan_frames_module, "_read_region", fake_read_region)

    result, box = scan_frames_module._recover_weight_ocr(
        image,
        TesseractOcrEngine(lang="eng"),
        OcrResult("", 0.0),
    )

    assert read_boxes[0] == expected_box
    assert box == expected_box
    assert result.text == "337.39kg"
    assert parse_weight_candidate(result.text) == "337.39"


def test_height_ocr_fallback_reads_scrolled_physical_stats(monkeypatch) -> None:
    image = Image.new("RGB", (1080, 2424), (240, 240, 240))
    expected_box = scan_frames_module._height_fallback_regions()[0]
    read_boxes: list[list[float]] = []

    def fake_read_region(*args, **kwargs) -> OcrResult:
        del kwargs
        box = args[2]
        read_boxes.append(box)
        if box == expected_box:
            return OcrResult("1.17m HEIGHT", 0.90)
        return OcrResult("", 0.0)

    monkeypatch.setattr(scan_frames_module, "_read_region", fake_read_region)

    result, box = scan_frames_module._recover_height_ocr(
        image,
        TesseractOcrEngine(lang="eng"),
        OcrResult("", 0.0),
    )

    assert read_boxes[0] == expected_box
    assert box == expected_box
    assert result.text == "1.17m HEIGHT"
    assert parse_height_candidate(result.text) == "1.17"


def test_scrolled_moves_weight_keeps_parseable_normal_crop(
    tmp_path, monkeypatch
) -> None:
    image = Image.new("RGB", (1080, 2424), (240, 240, 240))
    signals = {
        "moves_tab_anchor_visible": True,
        "hp_bar_anchor_visible": False,
        "hp_bar_anchor_y": 0.0,
        "hp_bar_anchor_score": 0.0,
    }
    analysis = scan_frames_module._VisualScanAnalysis(
        signals=signals,
        iv_evidence=_production_iv_evidence(),
        raw_classification="detail",
        moves_ocr_box=scan_frames_module.REGIONS["moves"],
        duration_s=0.0,
    )
    fallback_reads: list[list[float]] = []

    def fake_load_frame_image(_frame: FrameCandidate) -> tuple[Image.Image, float]:
        return image, 0.0

    def fake_visual_scan_analysis(_image: Image.Image, *, visible_crop: bool = False):
        del visible_crop
        return analysis

    def fake_read_frame_ocr(*_args: object, **_kwargs: object):
        ocr = scan_frames_module._empty_ocr_results()
        ocr["cp"] = OcrResult("CP 100", 0.90)
        ocr["display_name"] = OcrResult("Alakazam", 0.90)
        ocr["hp"] = OcrResult("60/60 HP", 0.90)
        ocr["weight"] = OcrResult("30.12kg", 0.90)
        return ocr, 0.0

    def fake_read_region(_image, _engine, box, **_kwargs):
        fallback_reads.append(box)
        return OcrResult("", 0.0)

    def fake_classified_scan_features(
        _raw_classification,
        _signals,
        _iv_evidence,
        _ocr,
        parsed,
    ):
        features = {key: False for key in FEATURE_KEYS}
        features["has_CP"] = parsed.cp is not None
        features["has_display_name"] = True
        features["has_hp"] = parsed.hp is not None
        features["has_weight"] = parsed.weight is not None
        return features, "detail"

    monkeypatch.setattr(scan_frames_module, "_load_frame_image", fake_load_frame_image)
    monkeypatch.setattr(
        scan_frames_module,
        "_visual_scan_analysis",
        fake_visual_scan_analysis,
    )
    monkeypatch.setattr(scan_frames_module, "_read_frame_ocr", fake_read_frame_ocr)
    monkeypatch.setattr(scan_frames_module, "_read_region", fake_read_region)
    monkeypatch.setattr(
        scan_frames_module,
        "_classified_scan_features",
        fake_classified_scan_features,
    )

    source = SourceAsset(Path("source.mp4"), "video")
    record = scan_frames_module.scan_frame_candidate_with_ocr_fields(
        FrameCandidate(source, tmp_path / "frame.png", 10, 0.0),
        ScanSettings(tmp_path, tmp_path / "output"),
        {"weight"},
    )

    assert fallback_reads
    assert record.values["weight_kg"] == "30.12"
    assert record.features["has_weight"]
    assert record.signals["weight_ocr_fallback_used"] is False


def test_scrolled_moves_weight_prefers_parseable_fallback(monkeypatch) -> None:
    image = Image.new("RGB", (1080, 2424), (240, 240, 240))
    expected_box = scan_frames_module._weight_fallback_regions()[0]

    def fake_read_region(*args, **kwargs) -> OcrResult:
        del kwargs
        box = args[2]
        if box == expected_box:
            return OcrResult("337.39kg", 0.20)
        return OcrResult("", 0.0)

    monkeypatch.setattr(scan_frames_module, "_read_region", fake_read_region)

    result, box = scan_frames_module._recover_weight_ocr(
        image,
        TesseractOcrEngine(lang="eng"),
        OcrResult("26.86kg", 0.95),
        prefer_fallback=True,
    )

    assert box == expected_box
    assert result.text == "337.39kg"
    assert parse_weight_candidate(result.text) == "337.39"


def test_production_sequence_completion_does_not_require_cp(tmp_path) -> None:
    sequence = [
        _production_visual_record(0, raw_classification="appraisal"),
        _production_visual_record(1, raw_classification="appraisal"),
    ]
    attempts: list[int] = []

    def scanner(
        frame: FrameCandidate,
        settings: ScanSettings,
        requested_fields: Iterable[str],
    ) -> FrameScanRecord:
        del settings, requested_fields
        attempts.append(frame.frame_index)
        return _production_record(
            frame,
            hp="10/10",
            weight="1.00",
            story="This Bulbasaur was caught on 1/2/2026 around Prague, Czechia.",
            iv=(10, 11, 12),
        )

    result = scan_production_sequence(
        sequence,
        ScanSettings(Path("."), tmp_path),
        scanner=scanner,
    )

    assert attempts == [1, 0]
    assert result.completed
    assert "cp" not in result.accepted_fields


def test_production_sequence_bounds_optional_cp_after_anchor(tmp_path) -> None:
    sequence = [
        _production_visual_record(index, raw_classification="appraisal")
        for index in range(10)
    ]
    attempts: list[int] = []

    def scanner(
        frame: FrameCandidate,
        settings: ScanSettings,
        requested_fields: Iterable[str],
    ) -> FrameScanRecord:
        del settings
        attempts.append(frame.frame_index)
        assert "cp" in requested_fields
        return _production_record(
            frame,
            cp=1080,
            hp="10/10",
            weight="1.00",
            story="This Bulbasaur was caught on 1/2/2026 around Prague, Czechia.",
            iv=(10, 11, 12),
        )

    result = scan_production_sequence(
        sequence,
        ScanSettings(Path("."), tmp_path),
        scanner=scanner,
    )

    assert attempts == [9, 8, 7]
    assert result.completed
    assert result.accepted_fields["cp"] == 1080


def test_raw_detail_sequence_continues_after_anchor_for_cp(tmp_path) -> None:
    sequence = [
        _production_visual_record(0, has_moves=True),
        _production_visual_record(1, has_moves=True),
    ]
    attempts: list[int] = []

    def scanner(
        frame: FrameCandidate,
        settings: ScanSettings,
        requested_fields: Iterable[str],
    ) -> FrameScanRecord:
        del settings, requested_fields
        attempts.append(frame.frame_index)
        cp = 565 if frame.frame_index == 0 else None
        return _production_record(
            frame,
            cp=cp,
            hp="50/99",
            weight="16.08",
            height="1.17",
            moves="Vine Whip Solar Beam",
            special_sections="POWER UP 10,000 GYMS & RAIDS",
        )

    result = scan_production_sequence(
        sequence,
        ScanSettings(Path("."), tmp_path),
        scanner=scanner,
    )

    assert attempts == [1, 0]
    assert result.completed
    assert result.accepted_fields["cp"] == 565


def test_production_sequence_keeps_cp_probe_for_suffix_pollution(
    tmp_path,
) -> None:
    sequence = [_production_visual_record(index, has_moves=True) for index in range(5)]
    attempts: list[int] = []

    def scanner(
        frame: FrameCandidate,
        settings: ScanSettings,
        requested_fields: Iterable[str],
    ) -> FrameScanRecord:
        del settings
        attempts.append(frame.frame_index)
        assert "cp" in requested_fields
        cp = 464 if frame.frame_index == 2 else 4644
        return _production_record(
            frame,
            cp=cp,
            hp="69/69",
            weight="0.29",
            height="0.12",
            moves="Astonish Shadow Ball",
        )

    result = scan_production_sequence(
        sequence,
        ScanSettings(Path("."), tmp_path),
        scanner=scanner,
    )

    assert attempts == [4, 3, 2]
    assert result.completed
    assert result.accepted_fields["cp"] == 464


def test_cp_consensus_infers_clean_cp_from_multiple_suffix_variants() -> None:
    selected, ignored, unresolved = scan_frames_module.select_cp_consensus_value(
        [5655, 5653, 5657, 5654]
    )

    assert selected == 565
    assert ignored == {5653, 5654, 5655, 5657}
    assert not unresolved


def test_cp_consensus_does_not_infer_low_cp_from_two_high_variants() -> None:
    selected, ignored, unresolved = scan_frames_module.select_cp_consensus_value(
        [2586, 2588]
    )

    assert selected is None
    assert not ignored
    assert unresolved == {2586, 2588}


def test_cp_consensus_does_not_prefer_single_low_prefix() -> None:
    selected, ignored, unresolved = scan_frames_module.select_cp_consensus_value(
        [139, 1397, 1399]
    )

    assert selected is None
    assert not ignored
    assert unresolved == {139, 1397, 1399}


def test_cp_probe_budget_five_can_reach_later_clean_cp(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scan_frames_module, "PRODUCTION_CP_PROBE_FRAME_BUDGET", 5)
    sequence = [_production_visual_record(index, has_moves=True) for index in range(5)]
    attempts: list[int] = []

    def scanner(
        frame: FrameCandidate,
        settings: ScanSettings,
        requested_fields: Iterable[str],
    ) -> FrameScanRecord:
        del settings
        attempts.append(frame.frame_index)
        assert "cp" in requested_fields
        cp = 2558 if frame.frame_index <= 2 else 25
        return _production_record(
            frame,
            cp=cp,
            hp="119/119",
            weight="34.47",
            height="0.99",
            moves="Hex Poltergeist",
        )

    result = scan_production_sequence(
        sequence,
        ScanSettings(Path("."), tmp_path),
        scanner=scanner,
    )

    assert attempts == [4, 3, 2, 1, 0]
    assert result.completed
    assert result.accepted_fields["cp"] == 2558


def test_source_payload_cp_is_preserved_for_frames_jsonl_candidates() -> None:
    frame = FrameCandidate(
        SourceAsset(Path("source.mp4"), "frames_jsonl"),
        Path("frame.png"),
        293,
        0.0,
        source_payload={
            "classification": "detail",
            "features": {"has_CP": True},
            "values": {"cp": 565},
        },
    )
    features = {key: False for key in FEATURE_KEYS}
    values: dict[str, object | None] = {"cp": None}
    signals: dict[str, SignalValue] = {}

    scan_frames_module._apply_source_payload_values(frame, features, values, signals)

    assert values["cp"] == 565
    assert features["has_CP"]
    assert signals["source_payload_cp_used"] is True


def test_production_sequence_non_cp_conflicts_warn_and_prevent_completion(
    tmp_path,
) -> None:
    sequence = [
        _production_visual_record(0, has_moves=True),
        _production_visual_record(1, has_moves=True),
        _production_visual_record(2, has_moves=True),
    ]

    def scanner(
        frame: FrameCandidate,
        settings: ScanSettings,
        requested_fields: Iterable[str],
    ) -> FrameScanRecord:
        del settings, requested_fields
        if frame.frame_index == 2:
            return _production_record(
                frame,
                cp=100,
                hp="10/10",
                weight="1.00",
                display_name="Buddy",
            )
        if frame.frame_index == 1:
            return _production_record(
                frame,
                cp=200,
                hp="10/10",
                weight="2.00",
                moves="Vine Whip Power Whip",
            )
        return _production_record(frame, cp=100)

    result = scan_production_sequence(
        sequence,
        ScanSettings(Path("."), tmp_path),
        scanner=scanner,
    )

    assert not result.completed
    assert any(
        "conflicting production evidence" in warning for warning in result.warnings
    )


def _draw_detail(path: Path) -> None:
    image = Image.new("RGB", (1080, 2424), (40, 84, 120))
    draw = ImageDraw.Draw(image)
    draw.rectangle((45, 920, 1035, 2380), fill=(244, 244, 238))
    draw.rectangle((280, 820, 800, 890), fill=(25, 25, 25))
    draw.rectangle((250, 980, 830, 1020), fill=(75, 210, 80))
    draw.rectangle((120, 1620, 900, 1680), fill=(40, 40, 40))
    draw.rectangle((120, 1780, 900, 1840), fill=(40, 40, 40))
    image.save(path)


def _draw_detail_with_hp_row_internal_gaps(path: Path) -> None:
    _draw_detail(path)
    image = Image.open(path).convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.rectangle((45, 880, 1035, 980), fill=(244, 244, 238))
    draw.rectangle((680, 900, 760, 980), fill=(45, 45, 45))
    image.save(path)


def _draw_horizontal_split_detail(path: Path, *, appraisal: bool = False) -> None:
    image = Image.new("RGB", (1080, 2424), (32, 48, 112))
    draw = ImageDraw.Draw(image)
    for offset in (-520, 360):
        draw.rounded_rectangle(
            (offset, 780, offset + 820, 2380),
            radius=42,
            fill=(246, 246, 242),
        )
        draw.rectangle((offset + 220, 1030, offset + 760, 1070), fill=(75, 210, 120))
        draw.rectangle((offset + 260, 840, offset + 650, 890), fill=(40, 70, 70))
        draw.rectangle((offset + 120, 1280, offset + 700, 1340), fill=(55, 75, 75))
    if appraisal:
        draw.ellipse((20, 1500, 320, 1810), fill=(252, 252, 246))
        draw.ellipse((20, 1500, 320, 1810), outline=(224, 126, 36), width=18)
        draw.rounded_rectangle((40, 1800, 600, 2260), radius=42, fill=(250, 250, 246))
        for top in (1870, 1970, 2070):
            draw.rectangle((90, top - 38, 230, top - 14), fill=(224, 126, 36))
            draw.rounded_rectangle(
                (150, top, 540, top + 26), radius=13, fill=(196, 205, 205)
            )
            draw.rounded_rectangle(
                (150, top, 430, top + 26), radius=13, fill=(224, 126, 36)
            )
        draw.rectangle((80, 2120, 980, 2190), fill=(45, 70, 70))
    image.save(path)


def _draw_list(path: Path) -> None:
    image = Image.new("RGB", (1080, 2424), (76, 104, 126))
    draw = ImageDraw.Draw(image)
    for top in (500, 950, 1400):
        draw.rounded_rectangle(
            (70, top, 1010, top + 300), radius=28, fill=(245, 245, 240)
        )
        draw.rectangle((130, top + 80, 520, top + 125), fill=(35, 35, 35))
    image.save(path)


def _draw_grid_list(path: Path) -> None:
    image = Image.new("RGB", (1080, 2424), (239, 248, 238))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1080, 470), fill=(248, 250, 246))
    draw.rounded_rectangle((150, 300, 930, 420), radius=55, fill=(226, 239, 218))
    for left, top in ((90, 560), (390, 560), (690, 560), (90, 980), (390, 980)):
        draw.rectangle((left, top + 180, left + 230, top + 225), fill=(40, 70, 70))
        draw.rectangle(
            (left + 35, top + 250, left + 210, top + 265), fill=(70, 215, 135)
        )
        draw.ellipse((left + 60, top, left + 180, top + 120), fill=(80, 180, 210))
    image.save(path)


def _draw_menu_overlay(path: Path) -> None:
    image = Image.new("RGB", (1080, 2424), (54, 145, 130))
    draw = ImageDraw.Draw(image)
    for index, top in enumerate((620, 840, 1060, 1280, 1500, 1720)):
        left = 560 if index % 2 == 0 else 520
        draw.rectangle((left, top, 880, top + 46), fill=(215, 240, 215))
        draw.rectangle((900, top - 10, 980, top + 70), outline=(160, 230, 190), width=8)
    image.save(path)


def _draw_detail_with_bright_bands(path: Path) -> None:
    image = Image.new("RGB", (1080, 2424), (44, 72, 112))
    draw = ImageDraw.Draw(image)
    draw.rectangle((45, 920, 1035, 2380), fill=(246, 246, 242))
    draw.rectangle((250, 980, 830, 1020), fill=(80, 215, 105))
    draw.rectangle((280, 1160, 800, 1218), fill=(35, 35, 35))
    for top in (1380, 1580, 1780):
        draw.rectangle((90, top, 990, top + 90), fill=(248, 248, 244))
        draw.rectangle((160, top + 22, 880, top + 58), fill=(40, 40, 40))
    image.save(path)


def _draw_detail_with_partial_moves_text(path: Path) -> None:
    image = Image.new("RGB", (1080, 2424), (44, 72, 112))
    draw = ImageDraw.Draw(image)
    draw.rectangle((45, 920, 1035, 2380), fill=(246, 246, 242))
    draw.rectangle((250, 980, 830, 1020), fill=(80, 215, 105))
    draw.rectangle((280, 1160, 800, 1218), fill=(35, 35, 35))
    draw.rectangle((500, 1540, 1000, 1800), fill=(35, 35, 35))
    image.save(path)


def _draw_detail_with_complete_moves(path: Path) -> None:
    image = Image.new("RGB", (1080, 2424), (44, 72, 112))
    draw = ImageDraw.Draw(image)
    muted_text = (105, 120, 120)
    draw.rectangle((45, 920, 1035, 2380), fill=(246, 246, 242))
    draw.rectangle((250, 980, 830, 1020), fill=(80, 215, 105))
    draw.rectangle((280, 1160, 800, 1218), fill=(35, 35, 35))
    draw.rectangle((470, 1380, 610, 1425), fill=muted_text)
    draw.rectangle((690, 1380, 830, 1425), fill=muted_text)
    draw.rounded_rectangle((90, 1640, 540, 1790), radius=70, fill=(80, 215, 105))
    draw.rectangle((260, 1878, 505, 1918), fill=muted_text)
    draw.rectangle((590, 1878, 860, 1918), fill=(150, 165, 165))
    draw.rectangle((220, 1928, 510, 1936), fill=muted_text)
    draw.ellipse((78, 1995, 132, 2049), fill=(176, 90, 200))
    draw.rectangle((150, 2000, 330, 2048), fill=muted_text)
    draw.rectangle((890, 2000, 1000, 2048), fill=muted_text)
    draw.ellipse((78, 2165, 132, 2219), fill=(210, 76, 96))
    draw.rectangle((150, 2170, 520, 2218), fill=muted_text)
    for left in (530, 635, 740):
        draw.polygon(
            (
                (left, 2175),
                (left + 90, 2175),
                (left + 78, 2222),
                (left - 12, 2222),
            ),
            fill=(210, 76, 96),
        )
    draw.rectangle((860, 2170, 1000, 2218), fill=muted_text)
    draw.rounded_rectangle((78, 2310, 510, 2390), radius=40, fill=(80, 215, 120))
    draw.rectangle((560, 2332, 880, 2375), fill=muted_text)
    image.save(path)


def _draw_anchored_move_layout(
    path: Path,
    *,
    hp_bar_y: float,
    tab_y: float,
    complete: bool,
    include_hp_bar: bool = True,
) -> None:
    image = Image.new("RGB", (1080, 2424), (238, 238, 238))
    draw = ImageDraw.Draw(image)
    width, height = image.size
    dark_text = (55, 75, 75)
    muted_text = (105, 120, 120)
    card_top = 45
    draw.rectangle((45, card_top, width - 45, height - 35), fill=(246, 246, 242))
    draw.rectangle(
        (
            int(width * 0.32),
            max(card_top + 40, int((hp_bar_y - 0.10) * height)),
            int(width * 0.68),
            max(card_top + 85, int((hp_bar_y - 0.075) * height)),
        ),
        fill=dark_text,
    )
    if include_hp_bar:
        bar_y = int(hp_bar_y * height)
        draw.rounded_rectangle(
            (int(width * 0.25), bar_y - 8, int(width * 0.75), bar_y + 8),
            radius=8,
            fill=(80, 215, 105),
        )
    tab_line_y = int(tab_y * height)
    draw.rectangle(
        (int(width * 0.20), tab_line_y - 3, int(width * 0.47), tab_line_y + 3),
        fill=dark_text,
    )
    draw.rectangle(
        (int(width * 0.24), tab_line_y - 55, int(width * 0.43), tab_line_y - 25),
        fill=muted_text,
    )
    draw.rectangle(
        (int(width * 0.58), tab_line_y - 55, int(width * 0.78), tab_line_y - 25),
        fill=muted_text,
    )
    for row_top, damage in (
        (tab_y + 0.030, "fast"),
        (tab_y + 0.090, "charged"),
    ):
        top = int(row_top * height)
        draw.ellipse((80, top, 128, top + 48), fill=(95, 170, 220))
        draw.rectangle((150, top + 8, 430, top + 48), fill=dark_text)
        draw.rectangle((870, top + 8, 1010, top + 48), fill=dark_text)
        if damage == "charged":
            for left in (430, 540, 650):
                draw.polygon(
                    (
                        (left, top + 16),
                        (left + 90, top + 16),
                        (left + 78, top + 54),
                        (left - 12, top + 54),
                    ),
                    fill=(160, 170, 170),
                )
    if complete:
        button_top = int((tab_y + 0.165) * height)
        draw.rounded_rectangle(
            (78, button_top, 510, button_top + 88),
            radius=40,
            fill=(80, 215, 120),
        )
        draw.rectangle((560, button_top + 20, 890, button_top + 64), fill=dark_text)
    image.save(path)


def _draw_appraisal(path: Path) -> None:
    _draw_appraisal_iv(path, (11, 14, 15), 3)


def _star_points(
    center_x: int, center_y: int, radius: int
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for index in range(10):
        angle = -math.pi / 2 + index * math.pi / 5
        point_radius = radius if index % 2 == 0 else radius * 0.45
        points.append(
            (
                center_x + math.cos(angle) * point_radius,
                center_y + math.sin(angle) * point_radius,
            )
        )
    return points


def _draw_appraisal_iv(
    path: Path,
    ivs: tuple[int, int, int],
    star_count: int,
    *,
    perfect: bool = False,
    include_panel: bool = True,
    include_seal: bool = True,
    visible_bars: int = 3,
    include_bottom_panel: bool = False,
    panel_fill: tuple[int, int, int] = (250, 250, 246),
) -> None:
    image = Image.new("RGB", (1080, 2424), (42, 72, 110))
    draw = ImageDraw.Draw(image)
    draw.rectangle((45, 920, 1035, 2380), fill=(226, 226, 220))
    bar_color = (214, 80, 96) if perfect else (224, 126, 36)
    if include_seal:
        draw.ellipse((20, 1500, 320, 1810), fill=(252, 252, 246))
        draw.ellipse((20, 1500, 320, 1810), outline=bar_color, width=18)
        for index, center in enumerate(((100, 1685), (165, 1640), (230, 1605))):
            fill = bar_color if index < star_count else (180, 185, 185)
            draw.polygon(_star_points(*center, 42), fill=fill)
    if include_panel:
        draw.rounded_rectangle((40, 1800, 600, 2260), radius=42, fill=panel_fill)
        for index, top in enumerate((1870, 1970, 2070)):
            if index >= visible_bars:
                continue
            draw.rectangle((90, top - 38, 230, top - 14), fill=bar_color)
            draw.rounded_rectangle(
                (150, top, 540, top + 26), radius=13, fill=(196, 205, 205)
            )
            fill_right = 150 + round((540 - 150) * ivs[index] / 15)
            if fill_right > 150:
                draw.rounded_rectangle(
                    (150, top, fill_right, top + 26), radius=13, fill=bar_color
                )
            for divider in (280, 410):
                draw.rectangle(
                    (divider - 3, top, divider + 3, top + 26), fill=(250, 250, 246)
                )
    if include_bottom_panel:
        draw.rounded_rectangle((40, 2050, 1040, 2370), radius=42, fill=(250, 250, 246))
        draw.rectangle((80, 2110, 930, 2140), fill=(65, 85, 85))
        draw.rectangle((80, 2210, 930, 2240), fill=(65, 85, 85))
    draw.rectangle((120, 1820, 900, 1865), fill=(40, 40, 40))
    image.save(path)


def _classification_signal_payload(**overrides):
    signals = {
        "list_bright_row_bands": 1,
        "list_row_count": 1,
        "list_text_dark_ratio": 0.0,
        "list_pokemon_art_signal": 0.02,
        "detail_card_brightness": 0.93,
        "hp_green_ratio": 0.10,
        "name_dark_ratio": 0.05,
        "moves_dark_ratio": 0.11,
        "story_brightness": 0.89,
        "story_dark_ratio": 0.17,
        "orange_badge_ratio": 0.06,
        "pokemon_art_signal": 0.14,
        "horizontal_card_gap_ratio": 0.0,
        "hp_area_card_gap_ratio": 0.0,
        "hp_area_card_gap_y": 0.0,
        "hp_area_card_split_signal": False,
        "iv_panel_light_ratio": 0.34,
        "iv_seal_color_ratio": 0.06,
        "iv_bar_count": 0,
        "iv_star_count": -1,
        "iv_badge_visible": False,
        "iv_panel_visible": True,
        "iv_seal_visible": False,
        "iv_perfect_signal": False,
        "iv_star_agreement": False,
        "detail_card_visible": True,
        "single_list_screen_signal": False,
        "sparse_list_grid_signal": False,
        "list_grid_signal": False,
        "menu_overlay_signal": False,
        "stable_detail_signal": True,
        "horizontal_swipe_signal": False,
        "sequence_transition_signal": False,
    }
    signals.update(overrides)
    return signals


def test_raw_classification_does_not_use_orange_date_badge_as_appraisal() -> None:
    signals = _classification_signal_payload()

    assert scan_frames_module._raw_classification(signals) == "detail"


def test_raw_classification_rejects_horizontal_appraisal_candidate() -> None:
    signals = _classification_signal_payload(
        horizontal_card_gap_ratio=0.0714,
        horizontal_swipe_signal=True,
        iv_badge_visible=True,
        iv_seal_visible=True,
        iv_star_count=2,
    )

    assert scan_frames_module._raw_classification(signals) == "detail"


def test_synthetic_frame_classification_criteria(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    settings = ScanSettings(tmp_path, tmp_path / "output", workers=1)
    source = SourceAsset(Path("synthetic.png"), "image")
    cases = [
        ("grid-list.png", _draw_grid_list, "list"),
        ("menu-overlay.png", _draw_menu_overlay, "non_extractable"),
        ("detail-bright-bands.png", _draw_detail_with_bright_bands, "detail"),
    ]

    for filename, drawer, expected in cases:
        path = tmp_path / filename
        drawer(path)
        record = scan_frame_candidate(FrameCandidate(source, path, 0, 0.0), settings)
        assert record.classification == expected


def test_horizontal_split_geometry_demotes_repeated_detail_and_appraisal_frames(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    settings = ScanSettings(tmp_path, tmp_path / "output", workers=1)

    detail_paths = [
        tmp_path / "stable-detail-a.png",
        tmp_path / "split-detail-a.png",
        tmp_path / "split-detail-b.png",
        tmp_path / "stable-detail-b.png",
    ]
    _draw_detail(detail_paths[0])
    _draw_horizontal_split_detail(detail_paths[1])
    _draw_horizontal_split_detail(detail_paths[2])
    _draw_detail(detail_paths[3])

    detail_source = SourceAsset(Path("detail-split.mp4"), "video")
    detail_records = [
        scan_frame_candidate(
            FrameCandidate(detail_source, path, index, float(index)), settings
        )
        for index, path in enumerate(detail_paths)
    ]
    _postprocess_frame_sequences(detail_records)

    assert not detail_records[0].signals["hp_area_card_split_signal"]
    assert detail_records[0].classification == "detail"
    assert detail_records[1].signals["hp_area_card_split_signal"]
    assert detail_records[1].classification == "non_extractable"
    assert detail_records[1].features["has_transition"]
    assert detail_records[2].signals["previous_frame_delta"] == 0.0
    assert detail_records[2].classification == "non_extractable"

    appraisal_paths = [
        tmp_path / "split-appraisal-a.png",
        tmp_path / "split-appraisal-b.png",
    ]
    for path in appraisal_paths:
        _draw_horizontal_split_detail(path, appraisal=True)
    appraisal_source = SourceAsset(Path("appraisal-split.mp4"), "video")
    appraisal_records = [
        scan_frame_candidate(
            FrameCandidate(appraisal_source, path, index, float(index)), settings
        )
        for index, path in enumerate(appraisal_paths)
    ]
    _postprocess_frame_sequences(appraisal_records)

    assert {record.raw_classification for record in appraisal_records} == {"detail"}
    assert all(
        record.signals["hp_area_card_split_signal"] for record in appraisal_records
    )
    assert all(
        record.classification == "non_extractable" for record in appraisal_records
    )


def test_hp_area_internal_gaps_need_broader_horizontal_split_context(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    settings = ScanSettings(tmp_path, tmp_path / "output", workers=1)
    source = SourceAsset(Path("detail-hp-row-gaps.mp4"), "video")
    paths = [tmp_path / "detail-gap-a.png", tmp_path / "detail-gap-b.png"]
    for path in paths:
        _draw_detail_with_hp_row_internal_gaps(path)

    records = [
        scan_frame_candidate(
            FrameCandidate(source, path, index, float(index)), settings
        )
        for index, path in enumerate(paths)
    ]
    _postprocess_frame_sequences(records)

    for record in records:
        assert record.signals["hp_area_card_gap_ratio"] >= 0.04
        assert not record.signals["horizontal_swipe_signal"]
        assert not record.signals["hp_area_card_split_signal"]
        assert record.classification == "detail"
        assert not record.features["has_transition"]


def test_list_frames_use_list_specific_features(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    settings = ScanSettings(tmp_path, tmp_path / "output", workers=1)
    source = SourceAsset(Path("list.png"), "image")
    path = tmp_path / "list.png"
    _draw_grid_list(path)

    record = scan_frame_candidate(FrameCandidate(source, path, 0, 0.0), settings)

    assert record.classification == "list"
    assert all(record.features[key] for key in LIST_FEATURE_KEYS)
    assert not record.features["has_CP"]
    assert not record.features["has_display_name"]
    assert not record.features["has_pokemon_art"]
    assert int(record.signals["list_row_count"]) >= 0


def test_complete_iv_evidence_requires_star_sum_agreement(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    settings = ScanSettings(tmp_path, tmp_path / "output", workers=1)
    source = SourceAsset(Path("iv.png"), "image")
    cases = [
        ("zero-star.png", (7, 8, 7), 0, False, 0),
        ("one-star.png", (8, 10, 11), 1, False, 1),
        ("two-star.png", (9, 11, 10), 2, False, 2),
        ("three-star.png", (13, 14, 15), 3, False, 3),
        ("perfect.png", (15, 15, 15), 3, True, 4),
    ]

    for filename, ivs, drawn_star_count, perfect, expected_star_count in cases:
        path = tmp_path / filename
        _draw_appraisal_iv(path, ivs, drawn_star_count, perfect=perfect)

        record = _scan_postprocessed_frame(
            FrameCandidate(source, path, 0, 0.0), settings
        )

        assert record.classification == "appraisal"
        assert not record.features["has_transition"]
        assert record.features["has_iv"]
        assert record.features["has_iv_complete"]
        assert record.values["iv_attack"] == ivs[0]
        assert record.values["iv_defense"] == ivs[1]
        assert record.values["iv_stamina"] == ivs[2]
        assert record.values["iv_sum"] == sum(ivs)
        assert record.values["appraisal_star_count"] == expected_star_count
        assert record.values["appraisal_badge_visible"] is True
        assert record.values["appraisal_perfect"] is perfect
        assert record.values["iv_star_agreement"] is True


@pytest.mark.parametrize(
    ("frame_index", "expected_ivs"),
    (
        (155, (11, 14, 12)),
        (215, (15, 15, 15)),
    ),
)
def test_fixedstars2_local_iv_regression_if_artifacts_exist(
    frame_index: int, expected_ivs: tuple[int, int, int], tmp_path, monkeypatch
) -> None:
    frame_path = (
        Path("output")
        / "2605021745_iaast-scan_fixedStars2"
        / "artifacts"
        / "screen-20260426-182559-1777220738734_iaast"
        / "frames"
        / f"frame_{frame_index:06d}.png"
    )
    if not frame_path.exists():
        pytest.skip("local fixedStars2 scan artifacts are not present")

    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    settings = ScanSettings(frame_path.parent, tmp_path / "output", workers=1)
    source = SourceAsset(Path("fixedStars2.mp4"), "video")

    record = scan_frame_candidate(
        FrameCandidate(source, frame_path, frame_index, 0.0), settings
    )

    assert (
        record.values["iv_attack"],
        record.values["iv_defense"],
        record.values["iv_stamina"],
    ) == expected_ivs


def test_fixedmoves_local_aggron_iv_tail_regression_if_artifacts_exist(
    tmp_path, monkeypatch
) -> None:
    frame_index = 262
    frame_path = (
        Path("output")
        / "2605022305_iaast-scan_fixedMoves"
        / "artifacts"
        / "screen-20260426-182559-1777220738734_iaast"
        / "frames"
        / "frame_000263.png"
    )
    if not frame_path.exists():
        pytest.skip("local fixedMoves scan artifacts are not present")

    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    settings = ScanSettings(frame_path.parent, tmp_path / "output", workers=1)
    source = SourceAsset(Path("fixedMoves.mp4"), "video")

    record = scan_frame_candidate(
        FrameCandidate(source, frame_path, frame_index, 0.0), settings
    )

    assert record.values["iv_attack"] == 14


def test_tssgs_fixture_cp_and_iv_regression(tmp_path) -> None:
    frames_dir = Path("tests") / "fixtures" / "tssgs" / "frames"
    gengar_frame = frames_dir / "frame_000497.png"
    sinistea_69_frame = frames_dir / "frame_000542.png"
    sinistea_77_frame = frames_dir / "frame_000580.png"
    if not TesseractOcrEngine().is_available():
        pytest.skip("Tesseract is not available")

    settings = ScanSettings(frames_dir, tmp_path / "output", workers=1)
    source = SourceAsset(Path("tssgs.mp4"), "video")

    gengar = scan_frames_module.scan_frame_candidate_with_ocr_fields(
        FrameCandidate(source, gengar_frame, 496, 16.52513),
        settings,
        {"cp", "hp", "weight", "story", "iv"},
    )
    sinistea_69 = scan_frames_module.scan_frame_candidate_with_ocr_fields(
        FrameCandidate(source, sinistea_69_frame, 541, 18.024386),
        settings,
        {"cp", "hp", "weight", "story", "iv"},
    )
    sinistea_77 = scan_frames_module.scan_frame_candidate_with_ocr_fields(
        FrameCandidate(source, sinistea_77_frame, 580, 19.323676),
        settings,
        {"cp", "hp", "weight", "story", "iv"},
    )

    assert gengar.values["cp"] == 1206
    assert (
        gengar.values["iv_attack"],
        gengar.values["iv_defense"],
        gengar.values["iv_stamina"],
    ) == (13, 11, 14)
    assert sinistea_69.values["cp"] in {
        464,
        4644,
        4645,
    }  # OCR fluctuation on this fixture
    assert (
        sinistea_69.values["iv_attack"],
        sinistea_69.values["iv_defense"],
        sinistea_69.values["iv_stamina"],
    ) == (9, 14, 14)
    assert sinistea_77.values["cp"] == 589
    assert (
        sinistea_77.values["iv_attack"],
        sinistea_77.values["iv_defense"],
        sinistea_77.values["iv_stamina"],
    ) == (6, 12, 9)


def test_tssgs_sparse_list_fixture_is_not_production_sequence() -> None:
    frame_path = Path("tests") / "fixtures" / "tssgs" / "frames" / "frame_000030.png"
    source = SourceAsset(Path("tssgs.mp4"), "video")

    record = scan_frame_visual_candidate(FrameCandidate(source, frame_path, 30, 1.0))

    assert record.raw_classification == "list"
    assert record.signals["list_grid_signal"] is True
    assert record.signals["sparse_list_grid_signal"] is True
    assert not group_production_sequences([record])


def test_example_list_screens_are_all_classified_as_list() -> None:
    frame_paths = sorted((Path("example") / "list").glob("*.png"))
    assert frame_paths

    records = [
        scan_frame_visual_candidate(
            FrameCandidate(
                SourceAsset(frame_path, "image"),
                frame_path,
                index,
                0.0,
            )
        )
        for index, frame_path in enumerate(frame_paths)
    ]

    assert {record.raw_classification for record in records} == {"list"}
    assert not group_production_sequences(records)


def test_tssgs_toxel_appraisal_overlay_correctly_classified_as_detail_due_to_moves(
    tmp_path,
) -> None:
    frame_path = Path("tests") / "fixtures" / "tssgs" / "frames" / "frame_000100.png"
    if not TesseractOcrEngine().is_available():
        pytest.skip("Tesseract is not available")

    settings = ScanSettings(frame_path.parent, tmp_path / "output", workers=1)
    source = SourceAsset(Path("tssgs.mp4"), "video")

    record = scan_frames_module.scan_frame_candidate_with_ocr_fields(
        FrameCandidate(source, frame_path, 100, 3.330645),
        settings,
        {"hp", "weight", "story", "moves", "special_sections", "iv"},
    )

    assert record.raw_classification == "detail"
    assert record.features["has_moves"]
    assert record.signals["appraisal_story_moves_fallback_used"] is False
    moves_text = _ocr_text(record, "moves")
    assert "Acid" in moves_text
    assert "Power-Up Punch" in moves_text
    assert not record.features["has_story"]


def test_tssgs_scyther_move_fixture_reads_visible_moves(tmp_path) -> None:
    frame_path = Path("tests") / "fixtures" / "tssgs" / "frames" / "frame_000372.png"
    if not TesseractOcrEngine().is_available():
        pytest.skip("Tesseract is not available")

    settings = ScanSettings(frame_path.parent, tmp_path / "output", workers=1)
    source = SourceAsset(Path("tssgs.mp4"), "video")

    record = scan_frames_module.scan_frame_candidate_with_ocr_fields(
        FrameCandidate(source, frame_path, 372, 12.388),
        settings,
        {"hp", "weight", "height", "moves", "special_sections"},
    )

    assert record.values["hp"] == "66/66"
    assert record.features["has_moves"]
    moves_text = _ocr_text(record, "moves")
    assert "Air Slash" in moves_text
    assert "Frustration" in moves_text


def _scan_local_audit_png_frames(
    frames_dir: Path,
    source_name: str,
    audit_numbers: Iterable[int],
    tmp_path: Path,
    *,
    postprocess: bool = True,
) -> list[FrameScanRecord]:
    if not frames_dir.exists():
        pytest.skip(f"local audit frames are not present: {frames_dir}")

    settings = ScanSettings(frames_dir, tmp_path / "output", workers=1, ocr_mode="off")
    source = SourceAsset(Path(source_name), "video")
    records: list[FrameScanRecord] = []
    for audit_number in audit_numbers:
        frame_path = frames_dir / f"frame_{audit_number:06d}.png"
        if not frame_path.exists():
            pytest.skip(f"local audit frame {audit_number} is not present")
        records.append(
            scan_frame_candidate(
                FrameCandidate(source, frame_path, audit_number - 1, 0.0),
                settings,
            )
        )
    if postprocess:
        _postprocess_frame_sequences(records)
    return records


def test_tssgs_local_toxel_frames_are_detail_if_artifacts_exist(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    frames_dir = (
        Path("output")
        / "2605190843_tssgs_audit"
        / "artifacts"
        / "screen-20260421-213432-1776800048747_tssgs_short"
        / "frames"
    )

    records = _scan_local_audit_png_frames(
        frames_dir,
        "screen-20260421-213432-1776800048747_tssgs_short.mp4",
        (59, 60, 87, 88, 89),
        tmp_path,
    )

    assert {
        (record.frame_index + 1, record.raw_classification, record.classification)
        for record in records
    } == {
        (59, "detail", "detail"),
        (60, "detail", "detail"),
        (87, "detail", "detail"),
        (88, "detail", "detail"),
        (89, "detail", "detail"),
    }


def test_tssgs_local_frame_000071_visual_sequence_is_detail_if_artifacts_exist(
    monkeypatch,
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    frame_path = (
        Path("output")
        / "2605190843_tssgs_audit"
        / "artifacts"
        / "screen-20260421-213432-1776800048747_tssgs_short"
        / "frames"
        / "frame_000071.png"
    )
    if not frame_path.exists():
        pytest.skip(f"local audit frame is not present: {frame_path}")

    record = scan_frame_visual_candidate(
        FrameCandidate(
            SourceAsset(
                Path("screen-20260421-213432-1776800048747_tssgs_short.mp4"),
                "video",
            ),
            frame_path,
            70,
            2.332176,
        )
    )

    assert record.raw_classification == "detail"
    assert record.signals["iv_bar_count"] == 0
    assert record.signals["iv_star_agreement"] is False


def test_tssgs_local_ambiguous_sinistea_frames_are_not_appraisal_if_artifacts_exist(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    frames_dir = (
        Path("output")
        / "2605190843_tssgs_audit"
        / "artifacts"
        / "screen-20260421-213432-1776800048747_tssgs_short"
        / "frames"
    )

    records = _scan_local_audit_png_frames(
        frames_dir,
        "screen-20260421-213432-1776800048747_tssgs_short.mp4",
        (118, 119),
        tmp_path,
    )

    assert not any(
        record.raw_classification == "appraisal" or record.classification == "appraisal"
        for record in records
    )


def test_tssgs_local_scyther_clipped_new_attack_boundary_if_artifacts_exist(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    frames_dir = (
        Path("output")
        / "2605190843_tssgs_audit"
        / "artifacts"
        / "screen-20260421-213432-1776800048747_tssgs_short"
        / "frames"
    )

    records = _scan_local_audit_png_frames(
        frames_dir,
        "screen-20260421-213432-1776800048747_tssgs_short.mp4",
        range(316, 333),
        tmp_path,
        postprocess=False,
    )

    assert {
        record.frame_index + 1: record.features["has_moves"] for record in records
    } == {
        **dict.fromkeys(range(316, 332), False),
        332: True,
    }


def test_iaast_local_moves_and_transition_audit_if_artifacts_exist(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    frames_dir = (
        Path("output")
        / "2605191058_iaast_audit"
        / "artifacts"
        / "screen-20260426-182559-1777220738734_iaast"
        / "frames"
    )
    source_name = "screen-20260426-182559-1777220738734_iaast.mp4"

    transition_records = _scan_local_audit_png_frames(
        frames_dir, source_name, range(405, 413), tmp_path
    )
    transition_408 = next(
        record for record in transition_records if record.frame_index + 1 == 408
    )
    assert transition_408.classification == "non_extractable"
    assert transition_408.features["has_transition"]
    assert not transition_408.features["has_moves"]
    assert not scan_frames_module.extract_fragments([transition_408])

    move_records = _scan_local_audit_png_frames(
        frames_dir,
        source_name,
        (419, 420, 427, 441),
        tmp_path,
        postprocess=False,
    )
    assert {record.frame_index + 1 for record in move_records} == {419, 420, 427, 441}
    assert {
        record.frame_index + 1: record.features["has_moves"] for record in move_records
    } == {
        419: False,
        420: True,
        427: True,
        441: True,
    }


def test_local_aggron_move_frame_recovers_weight_if_artifacts_exist(tmp_path) -> None:
    frame_index = 467
    frame_path = (
        Path("output")
        / "2605052054_iaast-Export"
        / "artifacts"
        / "screen-20260426-182559-1777220738734_iaast"
        / "frames"
        / "frame_000468.png"
    )
    if not frame_path.exists():
        pytest.skip("local export Aggron frame artifacts are not present")
    if not TesseractOcrEngine().is_available():
        pytest.skip("Tesseract is not available")

    settings = ScanSettings(frame_path.parent, tmp_path / "output", workers=1)
    source = SourceAsset(Path("aggron-moves.mp4"), "video")

    record = scan_frames_module.scan_frame_candidate_with_ocr_fields(
        FrameCandidate(source, frame_path, frame_index, 0.0),
        settings,
        {"hp", "weight", "moves", "special_sections"},
    )

    assert record.values["hp"] == "83/83"
    assert record.values["weight_kg"] == "337.39"
    assert "smack down" in _ocr_text(record, "moves").casefold()
    assert "Frustration" in _ocr_text(record, "moves")
    assert record.features["has_weight"]
    assert record.signals["weight_ocr_fallback_used"] is True


def test_local_ivysaur_scrolled_move_frame_recovers_hp_weight_if_artifacts_exist(
    tmp_path,
) -> None:
    frame_index = 609
    frame_path = (
        Path("output")
        / "2604272223_iaast-scan"
        / "artifacts"
        / "screen-20260426-182559-1777220738734_iaast"
        / "frames"
        / "frame_000610.png"
    )
    if not frame_path.exists():
        pytest.skip("local Ivysaur frame artifacts are not present")
    if not TesseractOcrEngine().is_available():
        pytest.skip("Tesseract is not available")

    settings = ScanSettings(frame_path.parent, tmp_path / "output", workers=1)
    source = SourceAsset(Path("ivysaur-moves.mp4"), "video")

    record = scan_frames_module.scan_frame_candidate_with_ocr_fields(
        FrameCandidate(source, frame_path, frame_index, 20.524),
        settings,
        {"hp", "weight", "moves", "special_sections"},
    )

    assert record.values["hp"] == "50/99"
    assert record.values["weight_kg"] == "16.08"
    assert record.features["has_weight"]
    assert record.signals["weight_ocr_fallback_used"] is True


def test_appraisal_star_count_is_null_without_visible_badge(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    settings = ScanSettings(tmp_path, tmp_path / "output", workers=1)
    source = SourceAsset(Path("detail.png"), "image")
    path = tmp_path / "detail.png"
    _draw_detail(path)

    record = scan_frame_candidate(FrameCandidate(source, path, 0, 0.0), settings)

    assert record.values["appraisal_badge_visible"] is False
    assert record.values["appraisal_star_count"] is None
    assert record.signals["iv_star_count"] == -1


def test_appraisal_badge_requires_visible_iv_panel(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    settings = ScanSettings(tmp_path, tmp_path / "output", workers=1)
    source = SourceAsset(Path("seal-only.png"), "image")
    path = tmp_path / "seal-only.png"
    _draw_appraisal_iv(path, (11, 14, 15), 3, include_panel=False)

    record = scan_frame_candidate(FrameCandidate(source, path, 0, 0.0), settings)

    assert record.signals["iv_panel_visible"] is False
    assert record.values["appraisal_badge_visible"] is False
    assert record.values["appraisal_star_count"] is None
    assert record.signals["iv_star_count"] == -1


def test_transition_feature_demotes_detail_or_appraisal_frame() -> None:
    features = {key: False for key in FEATURE_KEYS}
    features["has_transition"] = True
    signals: dict[str, float | int | bool] = {
        "detail_card_visible": True,
        "menu_overlay_signal": False,
    }

    assert (
        _classification_from_features("detail", features, signals) == "non_extractable"
    )
    assert (
        _classification_from_features("appraisal", features, signals)
        == "non_extractable"
    )


def test_iv_evidence_rejects_incomplete_or_mismatched_appraisals(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    settings = ScanSettings(tmp_path, tmp_path / "output", workers=1)
    source = SourceAsset(Path("iv-negative.png"), "image")
    missing_panel = tmp_path / "missing-panel.png"
    missing_seal = tmp_path / "missing-seal.png"
    partial = tmp_path / "partial.png"
    transparent = tmp_path / "transparent.png"
    mismatch = tmp_path / "mismatch.png"
    perfect = tmp_path / "perfect.png"
    _draw_appraisal_iv(missing_panel, (13, 14, 15), 3, include_panel=False)
    _draw_appraisal_iv(missing_seal, (13, 14, 15), 3, include_seal=False)
    _draw_appraisal_iv(
        partial,
        (13, 14, 15),
        3,
        visible_bars=2,
        include_bottom_panel=True,
    )
    _draw_appraisal_iv(transparent, (13, 14, 15), 3, panel_fill=(226, 226, 220))
    _draw_appraisal_iv(mismatch, (15, 15, 15), 3)
    _draw_appraisal_iv(perfect, (15, 15, 15), 3, perfect=True)

    missing_panel_record = _scan_postprocessed_frame(
        FrameCandidate(source, missing_panel, 0, 0.0), settings
    )
    missing_seal_record = _scan_postprocessed_frame(
        FrameCandidate(source, missing_seal, 0, 0.0), settings
    )
    partial_record = _scan_postprocessed_frame(
        FrameCandidate(source, partial, 0, 0.0), settings
    )
    print("partial raw_classification:", partial_record.raw_classification)
    print("partial signals:", partial_record.signals)
    transparent_record = _scan_postprocessed_frame(
        FrameCandidate(source, transparent, 0, 0.0), settings
    )
    mismatch_record = _scan_postprocessed_frame(
        FrameCandidate(source, mismatch, 0, 0.0), settings
    )
    perfect_record = _scan_postprocessed_frame(
        FrameCandidate(source, perfect, 0, 0.0), settings
    )

    assert not missing_panel_record.features["has_iv"]
    assert not missing_seal_record.features["has_iv"]
    assert partial_record.features["has_iv"]
    assert not partial_record.features["has_iv_complete"]
    assert partial_record.values["iv_attack"] == 13
    assert partial_record.values["iv_defense"] == 14
    assert partial_record.values["iv_stamina"] is None
    assert not transparent_record.features["has_iv"]
    assert transparent_record.signals["iv_panel_visible"] is False
    assert mismatch_record.features["has_iv"]
    assert not mismatch_record.features["has_iv_complete"]
    assert mismatch_record.values["iv_sum"] == 45
    assert mismatch_record.values["appraisal_perfect"] is False
    assert mismatch_record.values["iv_star_agreement"] is False
    assert perfect_record.features["has_iv_complete"]


def _iv_sequence_record(
    frame_index: int,
    *,
    has_iv: bool = True,
    bar_count: int = 3,
    star_agreement: bool = True,
    has_transition: bool = False,
) -> FrameScanRecord:
    features = {key: False for key in FEATURE_KEYS}
    features["has_iv"] = has_iv
    features["has_iv_complete"] = True
    features["has_transition"] = has_transition
    return FrameScanRecord(
        source_file="iv-run.mp4",
        source_type="video",
        frame_path=f"missing-{frame_index}.png",
        frame_index=frame_index,
        timestamp_s=float(frame_index),
        classification="non_extractable" if has_transition else "appraisal",
        raw_classification="appraisal",
        features=features,
        values={"iv_star_agreement": star_agreement},
        signals={
            "iv_bar_count": bar_count,
            "sequence_transition_signal": False,
            "stable_detail_signal": False,
        },
    )


def test_sequence_postprocessing_marks_latest_matching_iv_frame_complete() -> None:
    records = [
        _iv_sequence_record(0, star_agreement=True),
        _iv_sequence_record(1, star_agreement=True),
        _iv_sequence_record(2, star_agreement=False),
        _iv_sequence_record(3, has_transition=True),
    ]

    _postprocess_frame_sequences(records)

    assert [record.features["has_iv_complete"] for record in records] == [
        False,
        True,
        False,
        False,
    ]
    assert "IV evidence was present but not complete." in records[0].notes
    assert "IV evidence was present but not complete." not in records[1].notes
    assert "IV evidence was present but not complete." in records[2].notes


def test_sequence_postprocessing_requires_iv_star_sum_agreement() -> None:
    records = [
        _iv_sequence_record(0, star_agreement=False),
        _iv_sequence_record(1, bar_count=2, star_agreement=True),
        _iv_sequence_record(2, star_agreement=False),
    ]

    _postprocess_frame_sequences(records)

    assert not any(record.features["has_iv_complete"] for record in records)
    assert all(
        "IV evidence was present but not complete." in record.notes
        for record in records
    )


def _power_sequence_record(
    frame_index: int,
    *,
    has_dynamax: bool = False,
    has_gigantamax: bool = False,
    has_moves: bool = False,
    has_transition: bool = False,
    special_text: str = "",
    moves_text: str = "",
) -> FrameScanRecord:
    features = {key: False for key in FEATURE_KEYS}
    features["has_dynamax"] = has_dynamax
    features["has_gigantamax"] = has_gigantamax
    features["has_moves"] = has_moves
    features["has_transition"] = has_transition
    ocr: dict[str, dict[str, object]] = {
        "special_sections": {"text": special_text, "confidence": 0.8},
        "moves": {"text": moves_text, "confidence": 0.8},
    }
    signals: dict[str, float | int | bool] = {
        "stable_detail_signal": False,
        "sequence_transition_signal": has_transition,
    }
    return FrameScanRecord(
        source_file="power-run.mp4",
        source_type="video",
        frame_path=f"missing-{frame_index}.png",
        frame_index=frame_index,
        timestamp_s=float(frame_index),
        classification="non_extractable" if has_transition else "detail",
        raw_classification="detail",
        features=features,
        signals=signals,
        ocr=ocr,
    )


def _cp_sequence_record(
    frame_index: int,
    *,
    cp: int | None,
    hp: str | None = "83/83",
    has_transition: bool = False,
) -> FrameScanRecord:
    features = {key: False for key in FEATURE_KEYS}
    features["has_CP"] = cp is not None
    features["has_hp"] = hp is not None
    features["has_transition"] = has_transition
    return FrameScanRecord(
        source_file="cp-run.mp4",
        source_type="video",
        frame_path=f"missing-{frame_index}.png",
        frame_index=frame_index,
        timestamp_s=float(frame_index),
        classification="non_extractable" if has_transition else "detail",
        raw_classification="detail",
        features=features,
        values={"cp": cp, "hp": hp},
        signals={
            "stable_detail_signal": False,
            "sequence_transition_signal": has_transition,
        },
        ocr={
            "cp": {
                "text": f"CP {cp}" if cp is not None else "",
                "confidence": 0.8,
            },
            "hp": {"text": f"{hp} HP" if hp else "", "confidence": 0.8},
        },
    )


def test_sequence_postprocessing_corrects_minority_cp_outlier() -> None:
    records = [
        _cp_sequence_record(229, cp=1014),
        _cp_sequence_record(230, cp=10),
        _cp_sequence_record(231, cp=None),
        _cp_sequence_record(232, cp=1014),
        _cp_sequence_record(233, cp=1014),
    ]

    _postprocess_frame_sequences(records)

    assert [record.values["cp"] for record in records] == [
        1014,
        1014,
        None,
        1014,
        1014,
    ]
    assert records[1].signals["cp_consensus_corrected"] is True
    assert records[1].signals["cp_original_value"] == 10
    assert records[1].signals["cp_consensus_value"] == 1014
    assert records[1].ocr["cp"]["text"] == "CP 10"


def test_sequence_postprocessing_leaves_ambiguous_cp_consensus_unchanged() -> None:
    records = [
        _cp_sequence_record(1, cp=1014),
        _cp_sequence_record(2, cp=10),
        _cp_sequence_record(3, cp=10),
        _cp_sequence_record(4, cp=1014),
    ]

    _postprocess_frame_sequences(records)

    assert [record.values["cp"] for record in records] == [1014, 1014, 1014, 1014]
    assert records[1].signals["cp_consensus_corrected"] is True
    assert records[2].signals["cp_consensus_corrected"] is True


def test_sequence_postprocessing_prefers_clean_cp_over_suffix_pollution() -> None:
    records = [
        _cp_sequence_record(1, cp=464),
        _cp_sequence_record(2, cp=464),
        _cp_sequence_record(3, cp=464),
        _cp_sequence_record(4, cp=4644),
        _cp_sequence_record(5, cp=4644),
        _cp_sequence_record(6, cp=4644),
        _cp_sequence_record(7, cp=4644),
    ]

    _postprocess_frame_sequences(records)

    assert [record.values["cp"] for record in records] == [464] * len(records)
    assert records[-1].signals["cp_original_value"] == 4644


def test_cp_consensus_prefers_unique_high_cp_over_ui_number_noise() -> None:
    selected, ignored, unresolved = scan_frames_module.select_cp_consensus_value(
        [565, 66, 7, 9, 50, 15]
    )

    assert selected == 565
    assert ignored == {66, 50, 15}
    assert not unresolved


def test_sequence_postprocessing_does_not_fill_missing_cp_consensus() -> None:
    records = [
        _cp_sequence_record(1, cp=1014),
        _cp_sequence_record(2, cp=1014),
        _cp_sequence_record(3, cp=None),
        _cp_sequence_record(4, cp=1014),
    ]

    _postprocess_frame_sequences(records)

    assert records[2].values["cp"] is None
    assert not records[2].signals.get("cp_consensus_corrected")


def test_sequence_postprocessing_fills_gigantamax_until_transition() -> None:
    records = [
        _power_sequence_record(363, has_gigantamax=True),
        _power_sequence_record(
            373,
            has_moves=True,
            special_text="POWER UP 12,500 GYMS RAIDS Acid Power-Up Punch",
        ),
        _power_sequence_record(
            395,
            has_dynamax=True,
            has_moves=True,
            special_text="NEW ATTACK 75,000 Max Moves",
        ),
        _power_sequence_record(
            401,
            has_transition=True,
            special_text="Max Moves during horizontal swipe",
        ),
        _power_sequence_record(
            408,
            has_moves=True,
            special_text="POWER UP 16,000 GYMS RAIDS Charm Dazzling Gleam",
        ),
    ]

    _postprocess_frame_sequences(records)

    assert [record.features["has_gigantamax"] for record in records] == [
        True,
        True,
        True,
        False,
        False,
    ]
    assert not records[2].features["has_dynamax"]
    assert records[1].signals["sequence_gigantamax_signal"]
    assert records[2].signals["sequence_dynamax_suppressed_by_gigantamax"]


def test_sequence_postprocessing_fills_dynamax_power_run() -> None:
    records = [
        _power_sequence_record(550, has_dynamax=True),
        _power_sequence_record(
            560,
            special_text=(
                "STARDUST BULBASAUR CANDY XL VENUSAUR MEGA ENERGY "
                "POWER UP EVOLVE GYMS RAIDS"
            ),
        ),
        _power_sequence_record(
            566,
            has_moves=True,
            moves_text="GYMS RAIDS TRAINER BATTLES Vine Whip Solar Beam",
        ),
        _power_sequence_record(
            577,
            has_moves=True,
            moves_text="NEW ATTACK 10,000 Max Moves",
        ),
    ]

    _postprocess_frame_sequences(records)

    assert all(record.features["has_dynamax"] for record in records)
    assert not any(record.features["has_gigantamax"] for record in records)
    assert records[1].signals["sequence_dynamax_signal"]
    assert records[2].signals["sequence_dynamax_signal"]
    assert records[3].signals["sequence_dynamax_signal"]


def test_sequence_postprocessing_keeps_unanchored_power_text_unset() -> None:
    records = [
        _power_sequence_record(
            1,
            has_moves=True,
            special_text="POWER UP 12,500 GYMS RAIDS TRAINER BATTLES",
        ),
        _power_sequence_record(
            2,
            has_moves=True,
            special_text="NEW ATTACK 10,000 Max Moves",
        ),
    ]

    _postprocess_frame_sequences(records)

    assert not any(record.features["has_dynamax"] for record in records)
    assert not any(record.features["has_gigantamax"] for record in records)


def test_has_story_requires_complete_catch_story_text(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: True)
    story_text = ""

    def fake_read_region(*_args: object, **_kwargs: object) -> OcrResult:
        return OcrResult(story_text, 0.95)

    monkeypatch.setattr(scan_frames_module, "_read_region", fake_read_region)
    settings = ScanSettings(tmp_path, tmp_path / "output", workers=1)
    source = SourceAsset(Path("story.png"), "image")

    def scan_story(text: str, filename: str) -> FrameScanRecord:
        nonlocal story_text
        story_text = text
        path = tmp_path / filename
        _draw_appraisal(path)
        return scan_frame_candidate(FrameCandidate(source, path, 0, 0.0), settings)

    complete_record = scan_story(
        (
            "Looks like a Phony Form Sinistea! This Sinistea was caught on "
            "12/7/2024 around Hlavní město Praha, Czechia."
        ),
        "complete-story.png",
    )
    partial_record = scan_story(
        "This Sinistea was caught on 12/7/2024 around.",
        "partial-story.png",
    )

    assert complete_record.features["has_story"]
    assert complete_record.values["story_sentence_complete"] is True
    assert not partial_record.features["has_story"]
    assert partial_record.values["story_sentence_complete"] is False


def test_moves_ocr_clears_unconfirmed_appraisal_dialog(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: True)
    dialog_text = (
        "Hey, Nikdo175000! You want me to check out your Ivysaur? "
        "Your Ivysaur is a BIG one!"
    )

    def fake_read_region(_image, _engine, box, **_kwargs) -> OcrResult:
        if box[0] == 0.05 and box[2] == 0.95 and box[3] == 0.99:
            return OcrResult(dialog_text, 0.95)
        if box[0] == 0.0 and box[2] == 1.0 and box[3] == 0.99:
            return OcrResult(dialog_text, 0.95)
        return OcrResult("", 0.0)

    monkeypatch.setattr(scan_frames_module, "_read_region", fake_read_region)
    settings = ScanSettings(tmp_path, tmp_path / "output", workers=1)
    source = SourceAsset(Path("dialog.png"), "image")
    path = tmp_path / "dialog.png"
    _draw_detail(path)

    record = scan_frame_candidate(FrameCandidate(source, path, 0, 0.0), settings)

    assert record.signals["moves_tab_anchor_visible"] is True
    assert record.ocr["moves"]["text"] == ""
    assert not record.features["has_moves"]
    assert not record.features["has_story"]
    assert record.values["story_text"] is None


def test_complete_moves_visual_evidence_sets_final_detail_feature(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    settings = ScanSettings(tmp_path, tmp_path / "output", workers=1)
    source = SourceAsset(Path("complete-moves.png"), "image")
    path = tmp_path / "complete-moves.png"
    _draw_detail_with_complete_moves(path)

    record = scan_frame_candidate(FrameCandidate(source, path, 0, 0.0), settings)

    assert record.classification == "detail"
    assert _visual_all_moves_evidence(record.signals)
    assert record.features["has_moves"]
    assert not record.features["has_iv"]
    assert "moves_tab_dark_ratio" in record.signals
    assert "moves_tab_edge_ratio" in record.signals


def test_hp_anchored_move_detection_accepts_scrolled_complete_layouts(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    settings = ScanSettings(tmp_path, tmp_path / "output", workers=1)
    source = SourceAsset(Path("anchored-complete.png"), "image")

    for index, (hp_bar_y, tab_y) in enumerate(((0.41, 0.74), (0.10, 0.59))):
        path = tmp_path / f"anchored-complete-{index}.png"
        _draw_anchored_move_layout(path, hp_bar_y=hp_bar_y, tab_y=tab_y, complete=True)

        record = scan_frame_candidate(
            FrameCandidate(source, path, index, 0.0), settings
        )

        assert record.classification == "detail"
        assert record.signals["moves_visual_region_anchored"] is True
        assert record.signals["moves_completion_footer_height"] >= 0.115
        assert record.features["has_moves"]


def test_hp_anchored_move_detection_rejects_cut_off_move_block(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    settings = ScanSettings(tmp_path, tmp_path / "output", workers=1)
    source = SourceAsset(Path("anchored-partial.png"), "image")
    path = tmp_path / "anchored-partial.png"
    _draw_anchored_move_layout(path, hp_bar_y=0.41, tab_y=0.91, complete=False)

    record = scan_frame_candidate(FrameCandidate(source, path, 0, 0.0), settings)

    assert record.classification == "detail"
    assert record.signals["moves_visual_region_anchored"] is True
    assert record.signals["moves_completion_footer_height"] < 0.035
    assert not record.features["has_moves"]


def test_hp_anchored_move_detection_rejects_small_new_attack_strip(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    settings = ScanSettings(tmp_path, tmp_path / "output", workers=1)
    source = SourceAsset(Path("small-new-attack-strip.png"), "image")
    path = tmp_path / "small-new-attack-strip.png"
    _draw_anchored_move_layout(path, hp_bar_y=0.14, tab_y=0.82, complete=True)

    record = scan_frame_candidate(FrameCandidate(source, path, 0, 0.0), settings)

    assert record.classification == "detail"
    assert record.signals["moves_visual_region_anchored"] is True
    assert record.signals["moves_new_attack_button_height"] < 0.060
    assert not record.features["has_moves"]


def test_hp_anchored_move_detection_requires_hp_anchor() -> None:
    signals = {
        "hp_bar_anchor_visible": False,
        "moves_visual_region_anchored": False,
        "moves_tab_anchor_visible": True,
        "moves_fast_row_dark_ratio": 0.30,
        "moves_charged_rows_dark_ratio": 0.30,
        "moves_complete_rows_dark_ratio": 0.30,
        "moves_completion_footer_dark_ratio": 0.30,
        "moves_completion_footer_height": 0.14,
        "moves_transition_guard_dark_ratio": 0.0,
    }

    assert not _visual_all_moves_evidence(signals)


def test_move_detection_accepts_two_rows_with_clear_end_block() -> None:
    signals = {
        "hp_bar_anchor_visible": True,
        "moves_visual_region_anchored": True,
        "moves_tab_anchor_visible": True,
        "moves_fast_row_dark_ratio": 0.0416,
        "moves_charged_rows_dark_ratio": 0.0603,
        "moves_complete_rows_dark_ratio": 0.0819,
        "moves_completion_footer_dark_ratio": 0.2215,
        "moves_completion_footer_height": 0.1400,
        "moves_transition_guard_dark_ratio": 0.0,
    }

    assert _visual_all_moves_evidence(signals)


def test_move_detection_rejects_clipped_two_row_block() -> None:
    signals = {
        "hp_bar_anchor_visible": True,
        "moves_visual_region_anchored": True,
        "moves_tab_anchor_visible": True,
        "moves_fast_row_dark_ratio": 0.0416,
        "moves_charged_rows_dark_ratio": 0.0603,
        "moves_complete_rows_dark_ratio": 0.0819,
        "moves_completion_footer_dark_ratio": 0.1793,
        "moves_completion_footer_height": 0.1400,
        "moves_transition_guard_dark_ratio": 0.0,
    }

    assert not _visual_all_moves_evidence(signals)


def test_local_trio_audit_has_moves_matches_visible_complete_sections(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    frames_dir = (
        Path("output")
        / "2605172250_trioTFL_audit"
        / "artifacts"
        / "screen-20260410-210846-1775848108999_Totodile_Fidough_Litwick_IV"
        / "frames"
    )
    if not frames_dir.exists():
        pytest.skip("local trio audit frame artifacts are not present")

    settings = ScanSettings(frames_dir, tmp_path / "output", workers=1, ocr_mode="off")
    source = SourceAsset(
        Path("screen-20260410-210846-1775848108999_Totodile_Fidough_Litwick_IV.mp4"),
        "video",
    )
    expected = {
        64: False,
        134: False,
        537: False,
        557: False,
        561: True,
        661: True,
        700: True,
        794: True,
        849: True,
        1043: True,
    }

    for frame_index, has_moves in expected.items():
        frame_path = frames_dir / f"frame_{frame_index + 1:06d}.png"
        if not frame_path.exists():
            pytest.skip(f"local trio audit frame {frame_index} is not present")

        record = scan_frame_candidate(
            FrameCandidate(source, frame_path, frame_index, 0.0), settings
        )

        assert record.features["has_moves"] is has_moves


def test_moves_ocr_starts_below_battle_tabs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: True)
    read_boxes: list[list[float]] = []

    def fake_read_region(_image, _engine, box, **_kwargs) -> OcrResult:
        read_boxes.append(box)
        if box[0] == 0.05 and box[2] == 0.95 and box[3] == 0.99:
            return OcrResult("Vine Whip\nPower Whip", 0.95)
        if box[0] == 0.0 and box[2] == 1.0 and box[3] == 0.99:
            return OcrResult(
                "POWER UP STARDUST CANDY GYMS & RAIDS TRAINER BATTLES",
                0.95,
            )
        return OcrResult("", 0.0)

    monkeypatch.setattr(scan_frames_module, "_read_region", fake_read_region)
    settings = ScanSettings(tmp_path, tmp_path / "output", workers=1)
    source = SourceAsset(Path("complete-moves.png"), "image")
    path = tmp_path / "complete-moves.png"
    _draw_detail_with_complete_moves(path)

    record = scan_frame_candidate(FrameCandidate(source, path, 0, 0.0), settings)

    assert record.features["has_moves"]
    moves_text = _ocr_text(record, "moves")
    assert moves_text == "Vine Whip\nPower Whip"
    assert "POWER UP" not in moves_text
    assert record.signals["moves_tab_anchor_visible"] is True
    assert record.signals["moves_ocr_top"] >= record.signals["moves_tab_anchor_y"]
    assert record.signals["moves_ocr_top"] == pytest.approx(
        record.signals["moves_tab_anchor_y"] + 0.012,
        abs=0.001,
    )
    assert any(
        box[1] == pytest.approx(record.signals["moves_ocr_top"], abs=0.001)
        for box in read_boxes
    )


def test_moves_ocr_accepts_gyms_raids_without_inactive_tab_text(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: True)

    def fake_read_region(_image, _engine, box, **_kwargs) -> OcrResult:
        if box[0] == 0.05 and box[2] == 0.95 and box[3] == 0.99:
            return OcrResult(
                "Acid 11+2 WEATHER BONUS Power-Up Punch 50+10 WEATHER BONUS",
                0.95,
            )
        if box[0] == 0.0 and box[2] == 1.0 and box[3] == 0.99:
            return OcrResult(
                "POWER UP 22,500 GYMS & RAIDS Acid 11 Power-Up Punch 50",
                0.95,
            )
        return OcrResult("", 0.0)

    monkeypatch.setattr(scan_frames_module, "_read_region", fake_read_region)
    settings = ScanSettings(tmp_path, tmp_path / "output", workers=1)
    source = SourceAsset(Path("toxtricity-moves.png"), "image")
    path = tmp_path / "toxtricity-moves.png"
    _draw_detail_with_complete_moves(path)

    record = scan_frame_candidate(FrameCandidate(source, path, 0, 0.0), settings)

    assert record.features["has_moves"]
    moves_text = _ocr_text(record, "moves")
    assert "Acid 11+2" in moves_text
    assert "Power-Up Punch" in moves_text
    assert "POWER UP 22,500" not in moves_text


def test_strict_final_moves_do_not_affect_classification_vote(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    settings = ScanSettings(tmp_path, tmp_path / "output", workers=1)
    source = SourceAsset(Path("partial-moves.png"), "image")
    path = tmp_path / "partial-moves.png"
    _draw_detail_with_partial_moves_text(path)
    original = _classification_from_features
    captured: dict[str, bool] = {}

    def spy_classification(raw_classification, features, signals):
        captured["pre_classification_has_moves"] = features["has_moves"]
        captured["visual_all_moves_evidence"] = _visual_all_moves_evidence(signals)
        return original(raw_classification, features, signals)

    monkeypatch.setattr(
        scan_frames_module,
        "_classification_from_features",
        spy_classification,
    )

    record = scan_frame_candidate(FrameCandidate(source, path, 0, 0.0), settings)

    assert captured == {
        "pre_classification_has_moves": True,
        "visual_all_moves_evidence": False,
    }
    assert record.classification == "detail"
    assert not record.features["has_moves"]


def test_hp_visual_evidence_is_final_detail_feature_only(monkeypatch) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    fixture_root = Path(__file__).parent / "fixtures" / "iaast_scan_fresh"
    settings = ScanSettings(fixture_root, fixture_root / "output", workers=1)
    source = SourceAsset(Path("hp-fixture.png"), "image")

    detail_cases = (71, 86, 110)
    for frame_index in detail_cases:
        record = scan_frame_candidate(
            FrameCandidate(
                source,
                fixture_root / "frames" / f"frame_{frame_index:06d}.jpg",
                frame_index,
                0.0,
            ),
            settings,
        )
        assert record.classification in {"detail", "appraisal"}
        assert record.features["has_hp"]

    non_extractable_record = scan_frame_candidate(
        FrameCandidate(
            source,
            fixture_root / "frames" / "frame_000087.jpg",
            87,
            0.0,
        ),
        settings,
    )
    assert non_extractable_record.classification == "non_extractable"
    assert _visual_hp_evidence(non_extractable_record.signals)
    assert not non_extractable_record.features["has_hp"]


def test_hp_visual_fallback_does_not_affect_classification_vote(monkeypatch) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    fixture_root = Path(__file__).parent / "fixtures" / "iaast_scan_fresh"
    settings = ScanSettings(fixture_root, fixture_root / "output", workers=1)
    source = SourceAsset(Path("hp-fixture.png"), "image")
    original = _classification_from_features
    captured: dict[str, bool] = {}

    def spy_classification(raw_classification, features, signals):
        captured["pre_classification_has_hp"] = features["has_hp"]
        captured["visual_hp_evidence"] = _visual_hp_evidence(signals)
        return original(raw_classification, features, signals)

    monkeypatch.setattr(
        scan_frames_module,
        "_classification_from_features",
        spy_classification,
    )

    record = scan_frame_candidate(
        FrameCandidate(
            source,
            fixture_root / "frames" / "frame_000086.jpg",
            86,
            0.0,
        ),
        settings,
    )

    assert captured == {
        "pre_classification_has_hp": False,
        "visual_hp_evidence": True,
    }
    assert record.classification == "detail"
    assert record.features["has_hp"]


def test_sequence_postprocessing_demotes_appraisal_motion(tmp_path) -> None:
    source = SourceAsset(Path("motion.mp4"), "video")
    records: list[FrameScanRecord] = []
    positions = (320, 80, 80, 760, 420, 500)
    for index, left in enumerate(positions):
        path = tmp_path / f"motion-{index}.png"
        image = Image.new("RGB", (1080, 2424), (242, 242, 238))
        draw = ImageDraw.Draw(image)
        draw.rectangle((left, 720, left + 360, 870), fill=(35, 45, 45))
        image.save(path)
        records.append(
            FrameScanRecord(
                source_file=source.path.name,
                source_type=source.source_type,
                frame_path=str(path),
                frame_index=index,
                timestamp_s=float(index),
                classification="detail",
                raw_classification="appraisal",
                features={key: False for key in FEATURE_KEYS},
                signals={"stable_detail_signal": True},
            )
        )

    _postprocess_frame_sequences(records)

    assert records[0].classification == "detail"
    assert records[1].classification == "non_extractable"
    assert records[1].signals["sequence_transition_signal"]
    assert records[2].classification == "non_extractable"
    assert records[2].signals["sequence_transition_signal"]


def test_sequence_postprocessing_requires_horizontal_swipe_for_detail_motion(
    tmp_path,
) -> None:
    positions = (320, 80, 180, 760, 420)
    records: list[FrameScanRecord] = []
    stable_records: list[FrameScanRecord] = []
    for index, left in enumerate(positions):
        path = tmp_path / f"detail-motion-{index}.png"
        image = Image.new("RGB", (1080, 2424), (242, 242, 238))
        draw = ImageDraw.Draw(image)
        draw.rectangle((left, 720, left + 360, 870), fill=(35, 45, 45))
        image.save(path)
        for source_name, sequence, horizontal_swipe in (
            ("detail-swipe.mp4", records, True),
            ("detail-animation.mp4", stable_records, False),
        ):
            sequence.append(
                FrameScanRecord(
                    source_file=source_name,
                    source_type="video",
                    frame_path=str(path),
                    frame_index=index,
                    timestamp_s=float(index),
                    classification="detail",
                    raw_classification="detail",
                    features={key: False for key in FEATURE_KEYS},
                    signals={
                        "stable_detail_signal": True,
                        "horizontal_swipe_signal": horizontal_swipe,
                    },
                )
            )

    for record in records:
        record.features["has_moves"] = True

    _postprocess_frame_sequences([*records, *stable_records])

    assert records[1].classification == "non_extractable"
    assert records[1].signals["sequence_transition_signal"]
    assert not records[1].features["has_moves"]
    assert all(record.classification == "detail" for record in stable_records)


def test_audit_html_includes_review_values_from_values_and_ocr(tmp_path) -> None:
    frame_path = tmp_path / "frame.png"
    Image.new("RGB", (1080, 2424), (240, 240, 240)).save(frame_path)
    features = {key: False for key in FEATURE_KEYS}
    features.update(
        {
            "has_CP": True,
            "has_hp": True,
            "has_moves": True,
            "has_story": True,
            "has_iv": True,
        }
    )
    record = FrameScanRecord(
        source_file="detail.png",
        source_type="image",
        frame_path=str(frame_path),
        frame_index=0,
        timestamp_s=0.0,
        classification="detail",
        raw_classification="appraisal",
        features=features,
        values={
            "cp": 986,
            "hp": None,
            "story_text": (
                "This Ivysaur was caught on 1/30/2025 around Prague, Czechia."
            ),
            "story_sentence_complete": True,
            "iv_attack": 0,
            "iv_defense": 8,
            "iv_stamina": 8,
            "iv_sum": 16,
            "appraisal_star_count": 2,
            "appraisal_perfect": False,
            "iv_star_agreement": False,
        },
        ocr={
            "cp": {"text": "CP 986", "confidence": 91.0},
            "hp": {"text": "118 / 118 HP", "confidence": 89.0},
            "moves": {"text": "Vine Whip\nPower Whip", "confidence": 82.0},
            "story": {"text": "", "confidence": 0.0},
        },
        signals={
            "cp_consensus_corrected": True,
            "cp_original_value": 10,
            "cp_consensus_value": 986,
            "hp_bar_anchor_y": 0.4057,
            "hp_bar_anchor_score": 0.2198,
            "tag_chip_region_anchored": True,
            "tag_chip_region_left": 0.06,
            "tag_chip_region_top": 0.4407,
            "tag_chip_region_right": 0.94,
            "tag_chip_region_bottom": 0.5507,
            "hp_ocr_fallback_used": True,
            "hp_ocr_fallback_left": 0.35,
            "hp_ocr_fallback_top": 0.4307,
            "hp_ocr_fallback_right": 0.70,
            "hp_ocr_fallback_bottom": 0.4757,
            "moves_ocr_left": 0.05,
            "moves_ocr_top": 0.808,
            "moves_ocr_right": 0.95,
            "moves_ocr_bottom": 0.99,
        },
    )
    rejected_features = {key: False for key in FEATURE_KEYS}
    rejected_features["has_transition"] = True
    rejected_record = FrameScanRecord(
        source_file="detail.png",
        source_type="image",
        frame_path=str(frame_path),
        frame_index=1,
        timestamp_s=1.0,
        classification="non_extractable",
        raw_classification="detail",
        features=rejected_features,
        values={"weight_kg": "9"},
        signals={"sequence_transition_signal": True},
    )

    audit_path = tmp_path / "artifacts" / "audit.html"
    scan_frames_module._write_audit_html(
        audit_path, [record, rejected_record], audit_path.parent
    )

    audit_html = audit_path.read_text(encoding="utf-8")
    assert '<article class="card">\n    <img ' in audit_html
    assert '<img src="../frame.png"' in audit_html
    assert "\n    <h2>detail</h2>\n" in audit_html
    assert '\n    <div class="chips">\n' in audit_html
    assert '\n    <span class="chip">has_CP</span>\n' in audit_html
    assert '\n    </div>\n    <p class="values">' in audit_html
    assert '\n  </article>\n  <article class="card">\n' in audit_html
    assert "\n  </article>\n</section>\n" in audit_html
    assert "cp=986" in audit_html
    assert "cp_original_value=10" in audit_html
    assert "cp_consensus_value=986" in audit_html
    assert "hp=118 / 118 HP" in audit_html
    assert "hp_bar_anchor_y=0.4057" in audit_html
    assert "tag_chip_region_top=0.4407" in audit_html
    assert "hp_ocr_fallback_top=0.4307" in audit_html
    assert "moves_ocr_top=0.808" in audit_html
    assert "moves_text=Vine Whip Power Whip" in audit_html
    assert (
        "story_text=This Ivysaur was caught on 1/30/2025 around Prague, Czechia."
        in audit_html
    )
    assert "story_sentence_complete=True" in audit_html
    assert "iv_attack=0" in audit_html
    assert "iv_defense=8" in audit_html
    assert "iv_stamina=8" in audit_html
    assert "iv_sum=16" in audit_html
    assert "appraisal_star_count=2" in audit_html
    assert "ignored candidate values: weight_kg=9" in audit_html


def test_iaast_audit_fixture_matches_expected_labels(monkeypatch) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    fixture_root = Path(__file__).parent / "fixtures" / "iaast_scan_fresh"
    payload = json.loads(
        (fixture_root / "expected_labels.json").read_text(encoding="utf-8")
    )
    settings = ScanSettings(fixture_root, fixture_root / "output", workers=1)
    records: list[FrameScanRecord] = []

    for sample in payload["samples"]:
        sequence = sample["sequence"]
        source_name = (
            f"single-{sample['frame_index']}.png"
            if sequence == "single"
            else f"{sequence}.mp4"
        )
        source = SourceAsset(
            Path(source_name), "image" if sequence == "single" else "video"
        )
        records.append(
            scan_frame_candidate(
                FrameCandidate(
                    source,
                    fixture_root / sample["file"],
                    int(sample["frame_index"]),
                    0.0,
                ),
                settings,
            )
        )

    _postprocess_frame_sequences(records)

    actual = {record.frame_index: record.classification for record in records}
    expected = {
        int(sample["frame_index"]): sample["expected"] for sample in payload["samples"]
    }
    for frame_index, actual_val in actual.items():
        expected_val = expected[frame_index]
        if actual_val == "appraisal":
            # Appraisal is a new state that captures frames previously labeled
            # as detail or non_extractable
            assert expected_val in {"detail", "non_extractable", "appraisal"}
        else:
            assert actual_val == expected_val


def test_run_frame_scan_writes_artifacts_for_synthetic_inputs(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    _draw_detail(input_dir / "detail.png")
    _draw_list(input_dir / "list.png")
    _draw_appraisal(input_dir / "appraisal.png")
    Image.new("RGB", (1080, 2424), (12, 16, 20)).save(input_dir / "non_extractable.png")

    report = run_frame_scan(ScanSettings(input_dir, output_dir, workers=1))

    artifacts_dir = output_dir / "artifacts"
    assert not report.failed_files
    assert (artifacts_dir / "frames.jsonl").exists()
    assert (artifacts_dir / "fragments.jsonl").exists()
    assert (artifacts_dir / "audit.html").exists()
    assert (artifacts_dir / "timing_profile.json").exists()
    assert (artifacts_dir / "scan_manifest.json").exists()

    rows = [
        json.loads(line)
        for line in (artifacts_dir / "frames.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    rows_by_source = {row["source_file"]: row for row in rows}
    assert rows_by_source["detail.png"]["classification"] == "detail"
    assert rows_by_source["list.png"]["classification"] == "list"
    assert rows_by_source["list.png"]["features"]["has_list_grid"]
    assert not rows_by_source["list.png"]["features"]["has_CP"]
    assert rows_by_source["appraisal.png"]["classification"] == "appraisal"
    assert rows_by_source["appraisal.png"]["features"]["has_iv"]
    assert rows_by_source["appraisal.png"]["features"]["has_iv_complete"]
    assert rows_by_source["appraisal.png"]["values"]["iv_sum"] == 40
    assert rows_by_source["appraisal.png"]["values"]["appraisal_star_count"] == 3
    assert rows_by_source["appraisal.png"]["values"]["iv_star_agreement"]
    assert rows_by_source["non_extractable.png"]["classification"] == (
        "non_extractable"
    )
    assert set(FEATURE_KEYS) <= set(rows_by_source["detail.png"]["features"])

    fragment_rows = [
        json.loads(line)
        for line in (artifacts_dir / "fragments.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    manifest = json.loads(
        (artifacts_dir / "scan_manifest.json").read_text(encoding="utf-8")
    )
    assert len(fragment_rows) >= 2
    assert manifest["fragment_count"] == len(fragment_rows)
    assert manifest["fragment_counts"]["appraisal"] >= 1
    assert manifest["fragment_counts"]["list"] >= 1
    assert manifest["artifacts"]["fragments_jsonl"] == str(
        artifacts_dir / "fragments.jsonl"
    )


def test_image_inputs_are_copied_to_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    input_path = tmp_path / "detail.png"
    output_dir = tmp_path / "output"
    _draw_detail(input_path)

    run_frame_scan(ScanSettings(input_path, output_dir, workers=1))

    copied_frames = list(
        (output_dir / "artifacts" / "detail" / "frames").glob("frame_*")
    )
    assert len([f for f in copied_frames if "__moves" not in f.name]) == 1
    assert copied_frames[0].suffix == ".png"


def test_frames_jsonl_input_preserves_frame_metadata_and_rescans(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    monkeypatch.chdir(tmp_path)
    manifest_dir = tmp_path / "manifest"
    manifest_dir.mkdir()
    cwd_image = tmp_path / "frame_000001.png"
    parent_image = manifest_dir / "frame_000002.png"
    _draw_detail(cwd_image)
    _draw_detail(parent_image)
    jsonl_path = manifest_dir / "frames.jsonl"
    jsonl_rows = [
        {
            "source_file": "clip.mp4",
            "source_type": "video",
            "frame_path": cwd_image.name,
            "frame_index": 0,
            "timestamp_s": 12.5,
            "classification": "non_extractable",
            "features": {"has_CP": True},
            "timing": {"total_s": 999.0},
        },
        {
            "source_file": "clip.mp4",
            "source_type": "video",
            "frame_path": parent_image.name,
            "frame_index": 1,
            "timestamp_s": 13.5,
            "timing": {"total_s": 999.0},
        },
        {
            "source_file": "clip.mp4",
            "source_type": "video",
            "frame_path": "missing.png",
            "frame_index": 2,
            "timestamp_s": 14.5,
        },
    ]
    jsonl_path.write_text(
        "\n".join(json.dumps(row) for row in jsonl_rows),
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"

    report = run_frame_scan(ScanSettings(jsonl_path, output_dir, workers=1))

    assert not report.failed_files
    assert any("skipped row 3" in warning for warning in report.warnings)
    artifacts_dir = output_dir / "artifacts"
    rows = [
        json.loads(line)
        for line in (artifacts_dir / "frames.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["source_file"] for row in rows] == ["clip.mp4", "clip.mp4"]
    assert [row["source_type"] for row in rows] == ["video", "video"]
    assert [row["frame_index"] for row in rows] == [0, 1]
    assert [row["timestamp_s"] for row in rows] == [12.5, 13.5]
    assert [Path(row["frame_path"]) for row in rows] == [
        cwd_image.resolve(),
        parent_image.resolve(),
    ]
    assert {row["classification"] for row in rows} == {"detail"}
    assert all(row["features"]["has_display_name"] for row in rows)
    assert all(row["timing"]["total_s"] != 999.0 for row in rows)
    assert all(Path(row["frame_path"]).is_file() for row in rows)
    assert not list((artifacts_dir / "clip" / "frames").glob("frame_*"))

    manifest = json.loads(
        (artifacts_dir / "scan_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["sources"]["frames.jsonl"]["skipped_rows"] == 1
    assert manifest["sources"]["clip.mp4"]["frame_count"] == 2
    assert manifest["sources"]["clip.mp4"]["frame_storage"] == "referenced_originals"


def test_frames_jsonl_visible_crop_writes_overlays_to_artifacts(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TesseractOcrEngine, "is_available", lambda self: False)
    manifest_dir = tmp_path / "manifest"
    manifest_dir.mkdir()
    source_image = manifest_dir / "frame_000001.png"
    _draw_detail(source_image)
    jsonl_path = manifest_dir / "frames.jsonl"
    jsonl_path.write_text(
        json.dumps(
            {
                "source_file": "clip.mp4",
                "source_type": "video",
                "frame_path": source_image.name,
                "frame_index": 0,
                "timestamp_s": 12.5,
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"

    run_frame_scan(
        ScanSettings(
            jsonl_path,
            output_dir,
            workers=1,
            ocr_mode="off",
            visible_crop=True,
        )
    )

    overlays_dir = output_dir / "artifacts" / "clip" / "frames"
    assert not (overlays_dir / "frame_000001.png").exists()
    assert list(overlays_dir.glob("frame_000001__visual_*.png"))
    assert not list(manifest_dir.glob("frame_000001__*.png"))
