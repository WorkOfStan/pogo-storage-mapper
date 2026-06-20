from __future__ import annotations

from conftest import appraisal_values, species, species_catalog

from pogo_storage_mapper.extract import (
    enrich_fragments_with_moves,
    enrich_fragments_with_species,
    extract_fragment,
    extract_fragments,
    parse_catch_story,
    story_text_is_complete,
    write_fragments_jsonl,
)
from pogo_storage_mapper.metadata import MetadataCatalog, MoveEntry
from pogo_storage_mapper.scan_frames import FEATURE_KEYS, FrameScanRecord


def _features(*enabled: str) -> dict[str, bool]:
    features = {key: False for key in FEATURE_KEYS}
    for key in enabled:
        features[key] = True
    return features


def _ocr(**texts: str) -> dict[str, dict[str, object]]:
    return {key: {"text": value, "confidence": 0.9} for key, value in texts.items()}


def _record(
    *,
    classification: str | None = None,
    raw_classification: str = "detail",
    features: dict[str, bool] | None = None,
    values: dict[str, object | None] | None = None,
    ocr: dict[str, dict[str, object]] | None = None,
) -> FrameScanRecord:
    return FrameScanRecord(
        source_file="source.mp4",
        source_type="video",
        frame_path="frame.png",
        frame_index=42,
        timestamp_s=12.5,
        classification=classification or raw_classification,
        raw_classification=raw_classification,
        features=features or _features(),
        values=values or {},
        ocr=ocr or {},
    )


def _move_catalog() -> MetadataCatalog:
    return MetadataCatalog(
        moves=(
            MoveEntry(move_key="acid", move_name="Acid"),
            MoveEntry(move_key="air-slash-fast", move_name="Air Slash"),
            MoveEntry(move_key="astonish-fast", move_name="Astonish"),
            MoveEntry(move_key="frustration", move_name="Frustration"),
            MoveEntry(move_key="max-strike", move_name="Max Strike"),
            MoveEntry(move_key="power-up-punch", move_name="Power-Up Punch"),
            MoveEntry(move_key="power-whip", move_name="Power Whip"),
            MoveEntry(move_key="shadow-ball", move_name="Shadow Ball"),
            MoveEntry(move_key="shadow-bone", move_name="Shadow Bone"),
            MoveEntry(move_key="solar-beam", move_name="Solar Beam"),
            MoveEntry(move_key="vine-whip", move_name="Vine Whip"),
        )
    )


def test_parse_catch_story_accepts_flexible_complete_sentences() -> None:
    cases = [
        (
            "This Bulbasaur was caught on 1/2/2026 around Prague, Czechia.",
            "Bulbasaur",
            "1/2/2026",
            "Prague, Czechia",
            "Czechia",
        ),
        (
            "Looks like a Phony Form Sinistea! This Sinistea was caught on "
            "12/7/2024 around Hlavni mesto Praha, Czechia.",
            "Sinistea",
            "12/7/2024",
            "Hlavni mesto Praha, Czechia",
            "Czechia",
        ),
        (
            "This Kangaskhan was caught on 5/1/2026 around Australia.",
            "Kangaskhan",
            "5/1/2026",
            "Australia",
            "Australia",
        ),
        (
            "This Oricorio was caught on 4/30/2026 around French Polynesia.",
            "Oricorio",
            "4/30/2026",
            "French Polynesia",
            "French Polynesia",
        ),
        (
            "This Litwick was caught on 10/31/2025 around Bethlehem, "
            "Pennsylvania, United States.",
            "Litwick",
            "10/31/2025",
            "Bethlehem, Pennsylvania, United States",
            "United States",
        ),
        (
            "This one is ready for battle. This Mr. Mime was caught on "
            "6/6/2025 around Paris, France.",
            "Mr. Mime",
            "6/6/2025",
            "Paris, France",
            "France",
        ),
    ]

    for text, name, date, location, country in cases:
        story = parse_catch_story(text)

        assert story is not None
        assert story.canonical_name_text == name
        assert story.catch_date_text == date
        assert story.location_text == location
        assert story.catch_country_text == country
        assert story_text_is_complete(text)


def test_parse_catch_story_rejects_incomplete_text() -> None:
    cases = [
        "This was caught on 1/2/2026 around Czechia.",
        "This Bulbasaur was caught around Czechia.",
        "This Bulbasaur was caught on 1/2/2026 around .",
        "This Bulbasaur caught around Czechia",
        "This Bulbasaur was caught on 1/2/2026 around Czechia",
    ]

    assert all(parse_catch_story(text) is None for text in cases)
    assert not any(story_text_is_complete(text) for text in cases)


def test_extract_detail_fragment_from_gated_frame() -> None:
    record = _record(
        features=_features(
            "has_CP",
            "has_display_name",
            "has_hp",
            "has_weight",
            "has_height",
            "has_moves",
            "has_story",
            "is_shadow",
            "has_gigantamax",
        ),
        values={
            "cp": 2498,
            "hp": "77/77",
            "weight_kg": "1.41",
            "height_m": "0.7",
            "story_text": (
                "This Bulbasaur was caught on 1/2/2026 around Prague, Czechia."
            ),
        },
        ocr=_ocr(display_name="Buddy", moves="Vine Whip Solar Beam"),
    )

    fragment = extract_fragment(record)

    assert fragment is not None
    assert fragment.fragment_type == "detail"
    fields = fragment.fields
    assert fields["cp"].value == 2498
    assert fields["hp_current"].value == 77
    assert fields["hp_max"].value == 77
    assert fields["weight_kg"].value == 1.41
    assert fields["height_m"].value == 0.7
    assert fields["display_name_text"].value == "Buddy"
    assert fields["moves_text"].value == "Vine Whip Solar Beam"
    assert fields["canonical_name_text"].value == "Bulbasaur"
    assert fields["catch_country_text"].value == "Czechia"
    assert fields["is_shadow"].source == "feature_gate"
    assert fields["has_gigantamax"].value is True


def test_enrich_fragments_with_species_metadata(tmp_path) -> None:
    record = _record(
        features=_features("has_story"),
        values={
            "story_text": (
                "This Bulbasaur was caught on 1/2/2026 around Prague, Czechia."
            ),
        },
    )
    catalog = species_catalog(
        species("bulbasaur", "Bulbasaur", 1, aliases=("BULBASAUR",))
    )

    fragments = extract_fragments([record])
    enrich_fragments_with_species(fragments, catalog)
    path = tmp_path / "fragments.jsonl"
    write_fragments_jsonl(path, fragments)

    assert len(fragments) == 1
    fields = fragments[0].fields
    assert fields["canonical_name_text"].value == "Bulbasaur"
    assert fields["species_key"].value == "bulbasaur"
    assert fields["species_name"].value == "Bulbasaur"
    assert fields["pokedex_id"].value == 1
    assert fields["species_key"].source == "metadata_catalog"
    assert '"species_key"' in path.read_text(encoding="utf-8")


def test_enrich_fragments_with_species_from_display_name() -> None:
    record = _record(
        features=_features("has_display_name"),
        ocr=_ocr(display_name="Aggron"),
    )
    catalog = species_catalog(species("aggron", "Aggron", 306))

    fragments = extract_fragments([record])
    enrich_fragments_with_species(fragments, catalog)

    fields = fragments[0].fields
    assert fields["canonical_name_text"].value == "Aggron"
    assert fields["species_key"].value == "aggron"
    assert fields["pokedex_id"].value == 306


def test_enrich_fragments_with_species_from_fuzzy_story_name() -> None:
    record = _record(
        features=_features("has_story"),
        values={
            "story_text": (
                "This lvysaur was caught on 1/2/2026 around Prague, Czechia."
            ),
        },
    )
    catalog = species_catalog(species("ivysaur", "Ivysaur", 2))

    fragments = extract_fragments([record])
    enrich_fragments_with_species(fragments, catalog)

    fields = fragments[0].fields
    assert fields["canonical_name_text"].value == "Ivysaur"
    assert fields["species_key"].value == "ivysaur"


def test_extract_detail_fragment_rejects_noisy_display_name() -> None:
    record = _record(
        features=_features("has_display_name"),
        ocr=_ocr(display_name="4.42kg - 1.26m"),
    )

    fragment = extract_fragment(record)

    assert fragment is None


def test_unknown_species_text_remains_unresolved() -> None:
    record = _record(
        features=_features("has_story"),
        values={
            "story_text": (
                "This Missingno was caught on 1/2/2026 around Prague, Czechia."
            ),
        },
    )
    fragments = extract_fragments([record])
    enrich_fragments_with_species(fragments, MetadataCatalog())

    assert len(fragments) == 1
    assert fragments[0].fields["canonical_name_text"].value == "Missingno"
    assert "species_key" not in fragments[0].fields


def test_enrich_fragments_with_fast_and_charged_moves() -> None:
    record = _record(
        features=_features("has_moves"),
        ocr=_ocr(moves="Vine Whip Power Whip"),
    )

    fragments = extract_fragments([record])
    enrich_fragments_with_moves(fragments, _move_catalog())

    fields = fragments[0].fields
    assert fields["moves_text"].value == "Vine Whip Power Whip"
    assert fields["fast_move_name"].value == "Vine Whip"
    assert fields["fast_move_key"].value == "vine-whip"
    assert fields["charged_move_name"].value == "Power Whip"
    assert fields["charged_move_key"].value == "power-whip"
    assert "second_charged_move_name" not in fields


def test_enrich_fragments_with_moves_ignores_damage_and_weather_noise() -> None:
    record = _record(
        features=_features("has_moves"),
        ocr=_ocr(moves="Acid 11+2 WEATHER BONUS Power-Up Punch 50+10 WEATHER BONUS"),
    )

    fragments = extract_fragments([record])
    enrich_fragments_with_moves(fragments, _move_catalog())

    fields = fragments[0].fields
    assert fields["fast_move_name"].value == "Acid"
    assert fields["fast_move_key"].value == "acid"
    assert fields["charged_move_name"].value == "Power-Up Punch"
    assert fields["charged_move_key"].value == "power-up-punch"


def test_enrich_fragments_with_second_charged_move() -> None:
    record = _record(
        features=_features("has_moves"),
        ocr=_ocr(moves="Vine Whip Power Whip Solar Beam"),
    )

    fragments = extract_fragments([record])
    enrich_fragments_with_moves(fragments, _move_catalog())

    fields = fragments[0].fields
    assert fields["fast_move_name"].value == "Vine Whip"
    assert fields["charged_move_name"].value == "Power Whip"
    assert fields["second_charged_move_name"].value == "Solar Beam"
    assert fields["second_charged_move_key"].value == "solar-beam"


def test_enrich_fragments_with_shadow_named_and_frustration_moves() -> None:
    sinistea = _record(
        features=_features("has_moves"),
        ocr=_ocr(moves="Astonish Shadow Ball"),
    )
    scyther = _record(
        features=_features("has_moves"),
        ocr=_ocr(moves="Air Slash Frustration"),
    )

    fragments = extract_fragments([sinistea, scyther])
    enrich_fragments_with_moves(fragments, _move_catalog())

    assert fragments[0].fields["charged_move_name"].value == "Shadow Ball"
    assert fragments[0].fields["charged_move_key"].value == "shadow-ball"
    assert fragments[1].fields["fast_move_name"].value == "Air Slash"
    assert fragments[1].fields["charged_move_name"].value == "Frustration"
    assert fragments[1].fields["charged_move_key"].value == "frustration"


def test_enrich_fragments_ignores_shadow_and_weather_bonus_labels() -> None:
    record = _record(
        features=_features("has_moves"),
        ocr=_ocr(
            moves=(
                "Air Slash 12+2 SHADOW BONUS "
                "Frustration 10+4 WEATHER BONUS SHADOW BONUS"
            )
        ),
    )

    fragments = extract_fragments([record])
    enrich_fragments_with_moves(fragments, _move_catalog())

    fields = fragments[0].fields
    assert fields["fast_move_name"].value == "Air Slash"
    assert fields["charged_move_name"].value == "Frustration"
    assert fields["charged_move_key"].value == "frustration"
    assert fields.get("second_charged_move_name") is None
    assert not any(
        field.value == "Shadow Bone" for field in fields.values() if field.value
    )


def test_enrich_fragments_does_not_fuzzy_match_bonus_labels_as_moves() -> None:
    records = [
        _record(features=_features("has_moves"), ocr=_ocr(moves="SHADOW BONUS")),
        _record(features=_features("has_moves"), ocr=_ocr(moves="WEATHER BONUS")),
    ]

    fragments = extract_fragments(records)
    enrich_fragments_with_moves(fragments, _move_catalog())

    for fragment in fragments:
        assert "fast_move_name" not in fragment.fields
        assert "charged_move_name" not in fragment.fields
        assert "second_charged_move_name" not in fragment.fields


def test_enrich_fragments_prefers_primary_moves_over_indented_bonus_labels() -> None:
    record = _record(
        features=_features("has_moves"),
        ocr=_ocr(
            moves=(
                "Air Slash\n        SHADOW BONUS\nFrustration\n        WEATHER BONUS"
            )
        ),
    )

    fragments = extract_fragments([record])
    enrich_fragments_with_moves(fragments, _move_catalog())

    fields = fragments[0].fields
    assert fields["fast_move_name"].value == "Air Slash"
    assert fields["charged_move_name"].value == "Frustration"
    assert "second_charged_move_name" not in fields


def test_enrich_fragments_with_unique_fuzzy_move_ocr_variants() -> None:
    record = _record(
        features=_features("has_moves"),
        ocr=_ocr(moves="O Vine Whip 6 O Solar Baan 180"),
    )

    fragments = extract_fragments([record])
    enrich_fragments_with_moves(fragments, _move_catalog())

    fields = fragments[0].fields
    assert fields["fast_move_name"].value == "Vine Whip"
    assert fields["charged_move_name"].value == "Solar Beam"
    assert fields["charged_move_key"].value == "solar-beam"


def test_enrich_fragments_rejects_ambiguous_fuzzy_move_ocr() -> None:
    record = _record(
        features=_features("has_moves"),
        ocr=_ocr(moves="Solar Baan"),
    )
    catalog = MetadataCatalog(
        moves=(
            MoveEntry(move_key="solar-beam", move_name="Solar Beam"),
            MoveEntry(move_key="solar-bean", move_name="Solar Bean"),
        )
    )

    fragments = extract_fragments([record])
    enrich_fragments_with_moves(fragments, catalog)

    fields = fragments[0].fields
    assert fields["moves_text"].value == "Solar Baan"
    assert "fast_move_name" not in fields
    assert "charged_move_name" not in fields


def test_generic_max_moves_text_is_not_normalized() -> None:
    record = _record(
        features=_features("has_moves", "has_dynamax"),
        ocr=_ocr(
            moves="NEW ATTACK 10,000 Max Moves",
            special_sections="NEW ATTACK 10,000 Max Moves",
        ),
    )

    fragments = extract_fragments([record])
    enrich_fragments_with_moves(fragments, _move_catalog())

    fields = fragments[0].fields
    assert fields["moves_text"].value == "NEW ATTACK 10,000 Max Moves"
    assert fields["power_section_text"].value == "NEW ATTACK 10,000 Max Moves"
    assert "max_move_name" not in fields
    assert "max_move_key" not in fields


def test_enrich_fragments_with_max_move_from_power_section() -> None:
    record = _record(
        features=_features("has_dynamax"),
        ocr=_ocr(special_sections="Dynamax Level 1 Max Strike"),
    )

    fragments = extract_fragments([record])
    enrich_fragments_with_moves(fragments, _move_catalog())

    fields = fragments[0].fields
    assert fields["power_section_text"].value == "Dynamax Level 1 Max Strike"
    assert fields["max_move_name"].value == "Max Strike"
    assert fields["max_move_key"].value == "max-strike"
    assert fields["max_move_name"].source == "metadata_catalog"
    assert fields["max_move_name"].evidence == (
        "fields.power_section_text + metadata_catalog.moves"
    )


def test_unresolved_move_text_remains_raw_only() -> None:
    record = _record(
        features=_features("has_moves"),
        ocr=_ocr(moves="Unclear OCR Text"),
    )

    fragments = extract_fragments([record])
    enrich_fragments_with_moves(fragments, _move_catalog())

    fields = fragments[0].fields
    assert fields["moves_text"].value == "Unclear OCR Text"
    assert "fast_move_name" not in fields
    assert "charged_move_name" not in fields
    assert "max_move_name" not in fields


def test_extract_appraisal_fragment_from_complete_iv() -> None:
    record = _record(
        raw_classification="appraisal",
        features=_features("has_iv", "has_iv_complete"),
        values={**appraisal_values(), "iv_star_agreement": True},
    )

    fragment = extract_fragment(record)

    assert fragment is not None
    assert fragment.fragment_type == "appraisal"
    assert fragment.fields["iv_complete"].value is True
    assert fragment.fields["iv_attack"].value == 13
    assert fragment.fields["iv_sum"].source == "decoded_iv"
    assert fragment.fields["appraisal_perfect"].value is False


def test_extract_incomplete_appraisal_keeps_iv_audit_values() -> None:
    record = _record(
        raw_classification="appraisal",
        features=_features("has_iv"),
        values={
            **appraisal_values(iv_stamina=None, iv_sum=None),
            "iv_star_agreement": False,
        },
    )

    fragment = extract_fragment(record)

    assert fragment is not None
    assert fragment.fields["iv_complete"].value is False
    assert fragment.fields["iv_attack"].value == 13
    assert fragment.fields["iv_defense"].value == 14
    assert "iv_stamina" not in fragment.fields
    assert fragment.fields["iv_star_agreement"].value is False


def test_extract_skips_non_extractable_and_transition_records() -> None:
    non_extractable = _record(
        classification="non_extractable", features=_features("has_CP")
    )
    transition = _record(features=_features("has_transition", "has_CP"))

    assert extract_fragment(non_extractable) is None
    assert extract_fragment(transition) is None


def test_extract_list_fragment_from_weak_list_evidence() -> None:
    record = _record(
        classification="list",
        raw_classification="list",
        features=_features(
            "has_list_grid",
            "has_list_cp",
            "has_list_display_name",
            "has_list_pokemon_art",
        ),
        ocr=_ocr(cp="CP 1234", display_name="Bulbasaur"),
    )

    fragment = extract_fragment(record)

    assert fragment is not None
    assert fragment.fragment_type == "list"
    assert fragment.fields["has_list_grid"].value is True
    assert fragment.fields["has_list_cp"].source == "feature_gate"
    assert fragment.fields["list_cp_text"].value == "CP 1234"
    assert fragment.fields["list_display_name_text"].value == "Bulbasaur"
