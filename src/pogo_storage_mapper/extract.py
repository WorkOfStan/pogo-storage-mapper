from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol, TypeAlias

from pogo_storage_mapper.metadata import (
    MetadataCatalog,
    MoveEntry,
    SpeciesEntry,
    edit_distance_within,
    normalize_move_name,
)

JsonScalar: TypeAlias = str | int | float | bool
IV_NUMERIC_FIELD_NAMES = (
    "iv_attack",
    "iv_defense",
    "iv_stamina",
    "iv_sum",
    "appraisal_star_count",
)

_CATCH_STORY_RE = re.compile(
    r"\bThis\s+(?P<canonical_name>.+?)\s+was\s+caught\s+on\s+"
    r"(?P<catch_date>\d{1,2}/\d{1,2}/\d{4})\s+around\s+"
    r"(?P<location>[^.!?]*\S)\s*[.!?]",
    re.IGNORECASE,
)
_NORMAL_MOVE_FIELD_PREFIXES = (
    "fast_move",
    "charged_move",
    "second_charged_move",
)
_GENERIC_MAX_MOVE_NAMES = {"max move", "max moves"}
_MOVE_ANNOTATION_RE = re.compile(r"\b[a-z0-9]+\s+bonus\b")


class FrameRecordLike(Protocol):
    source_file: str
    source_type: str
    classification: str
    raw_classification: str
    frame_index: int
    timestamp_s: float
    features: dict[str, bool]
    values: dict[str, object | None]
    ocr: dict[str, dict[str, object]]


@dataclass(frozen=True, slots=True)
class CatchStory:
    canonical_name_text: str
    catch_date_text: str
    location_text: str
    catch_country_text: str


@dataclass(frozen=True, slots=True)
class _MoveMention:
    start: int
    end: int
    entry: MoveEntry

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class FragmentField:
    value: JsonScalar
    source: str
    evidence: str

    def to_json_dict(self) -> dict[str, JsonScalar]:
        return {
            "value": self.value,
            "source": self.source,
            "evidence": self.evidence,
        }


@dataclass(slots=True)
class PokemonFragment:
    source_file: str
    source_type: str
    frame_index: int
    timestamp_s: float
    classification: str
    raw_classification: str
    fragment_type: str
    fields: dict[str, FragmentField] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, object]:
        return {
            "source_file": self.source_file,
            "source_type": self.source_type,
            "frame_index": self.frame_index,
            "timestamp_s": round(self.timestamp_s, 6),
            "classification": self.classification,
            "raw_classification": self.raw_classification,
            "fragment_type": self.fragment_type,
            "fields": {
                name: field_value.to_json_dict()
                for name, field_value in sorted(self.fields.items())
            },
        }


def story_text_has_keywords(text: str | None) -> bool:
    if not text:
        return False
    normalized = " ".join(text.casefold().split())
    return all(keyword in normalized for keyword in ("this", "caught", "around"))


def parse_catch_story(text: str | None) -> CatchStory | None:
    if not text:
        return None
    normalized = " ".join(text.split())
    for start_match in re.finditer(r"\bThis\s+", normalized, re.IGNORECASE):
        match = _CATCH_STORY_RE.match(normalized, start_match.start())
        if match is None:
            continue

        canonical_name = match.group("canonical_name").strip()
        if re.search(r"[.!?]\s+This\b", canonical_name, re.IGNORECASE):
            continue
        catch_date = match.group("catch_date").strip()
        location = match.group("location").strip()
        if not canonical_name or not catch_date or not location:
            continue

        location_parts = [part.strip() for part in location.split(",") if part.strip()]
        country = location_parts[-1] if location_parts else location
        if not country:
            continue

        return CatchStory(
            canonical_name_text=canonical_name,
            catch_date_text=catch_date,
            location_text=location,
            catch_country_text=country,
        )

    return None


def story_text_is_complete(text: str | None) -> bool:
    return parse_catch_story(text) is not None


def extract_fragments(records: Iterable[FrameRecordLike]) -> list[PokemonFragment]:
    fragments: list[PokemonFragment] = []
    for record in records:
        fragment = extract_fragment(record)
        if fragment is not None:
            fragments.append(fragment)
    return fragments


def enrich_fragments_with_species(
    fragments: Iterable[PokemonFragment],
    catalog: MetadataCatalog,
) -> None:
    for fragment in fragments:
        species, source_field, fuzzy = _resolve_fragment_species(fragment, catalog)
        if species is None or source_field is None:
            continue
        evidence = (
            f"fields.{source_field} + metadata_catalog.species"
            f"{'_fuzzy' if fuzzy else ''}"
        )
        if source_field == "display_name_text" or fuzzy:
            _add_field(
                fragment.fields,
                "canonical_name_text",
                species.species_name,
                "metadata_catalog",
                evidence,
            )
        _add_species_fields(fragment, species, evidence)


def enrich_fragments_with_moves(
    fragments: Iterable[PokemonFragment],
    catalog: MetadataCatalog,
) -> None:
    for fragment in fragments:
        moves_text = _fragment_text(fragment, "moves_text")
        if moves_text:
            normal_moves = _resolved_move_mentions(
                moves_text,
                catalog,
                max_moves=False,
            )
            for field_prefix, move in zip(
                _NORMAL_MOVE_FIELD_PREFIXES, normal_moves, strict=False
            ):
                _add_move_fields(fragment, field_prefix, move, "moves_text")

        max_move = None
        max_source_field = "moves_text"
        for source_field in ("power_section_text", "moves_text"):
            source_text = _fragment_text(fragment, source_field)
            if not source_text:
                continue
            max_move = _first_max_move(source_text, catalog)
            max_source_field = source_field
            if max_move is not None:
                break
        if max_move is not None:
            _add_move_fields(fragment, "max_move", max_move, max_source_field)


def extract_fragment(record: FrameRecordLike) -> PokemonFragment | None:
    if (
        record.features.get("has_transition")
        or record.classification == "non_extractable"
    ):
        return None
    if record.classification == "list":
        fields = _extract_list_fields(record)
        fragment_type = "list"
    elif record.classification in {"detail", "appraisal"}:
        fields = _extract_detail_fields(record)
        fragment_type = record.classification
    else:
        return None

    if not fields:
        return None
    return PokemonFragment(
        source_file=record.source_file,
        source_type=record.source_type,
        frame_index=record.frame_index,
        timestamp_s=record.timestamp_s,
        classification=record.classification,
        raw_classification=record.raw_classification,
        fragment_type=fragment_type,
        fields=fields,
    )


def write_fragments_jsonl(path: Path, fragments: Iterable[PokemonFragment]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for fragment in fragments:
            handle.write(json.dumps(fragment.to_json_dict(), ensure_ascii=True))
            handle.write("\n")


def _extract_detail_fields(record: FrameRecordLike) -> dict[str, FragmentField]:
    fields: dict[str, FragmentField] = {}
    features = record.features
    values = record.values

    cp = _int_value(values.get("cp"))
    if features.get("has_CP") and cp is not None:
        _add_field(fields, "cp", cp, "ocr_value", "features.has_CP + values.cp")

    display_name_text = _clean_display_name_text(_ocr_text(record, "display_name"))
    if features.get("has_display_name") and display_name_text:
        _add_field(
            fields,
            "display_name_text",
            display_name_text,
            "ocr_text",
            "features.has_display_name + ocr.display_name.text",
        )

    hp = values.get("hp")
    if features.get("has_hp") and isinstance(hp, str):
        hp_parts = _hp_parts(hp)
        if hp_parts is not None:
            current, maximum = hp_parts
            _add_field(
                fields,
                "hp_current",
                current,
                "ocr_value",
                "features.has_hp + values.hp",
            )
            _add_field(
                fields,
                "hp_max",
                maximum,
                "ocr_value",
                "features.has_hp + values.hp",
            )

    weight = _float_value(values.get("weight_kg"))
    if features.get("has_weight") and weight is not None:
        _add_field(
            fields,
            "weight_kg",
            weight,
            "ocr_value",
            "features.has_weight + values.weight_kg",
        )

    height = _float_value(values.get("height_m"))
    if features.get("has_height") and height is not None:
        _add_field(
            fields,
            "height_m",
            height,
            "ocr_value",
            "features.has_height + values.height_m",
        )

    moves_text = _ocr_text(record, "moves")
    if features.get("has_moves") and moves_text:
        _add_field(
            fields,
            "moves_text",
            moves_text,
            "ocr_text",
            "features.has_moves + ocr.moves.text",
        )

    power_section_text = _ocr_text(record, "special_sections")
    if (
        features.get("has_dynamax") or features.get("has_gigantamax")
    ) and power_section_text:
        _add_field(
            fields,
            "power_section_text",
            power_section_text,
            "ocr_text",
            "features.has_dynamax/has_gigantamax + ocr.special_sections.text",
        )

    story_text = _text_value(values.get("story_text")) or _ocr_text(record, "story")
    if features.get("has_story"):
        story = parse_catch_story(story_text)
        if story is not None:
            _add_story_fields(fields, story)

    if features.get("has_iv"):
        _add_iv_fields(fields, features, values)

    for feature_name in (
        "is_shadow",
        "has_dynamax",
        "has_gigantamax",
        "has_tag_chips",
    ):
        _add_true_feature(fields, features, feature_name)

    return fields


def _resolve_fragment_species(
    fragment: PokemonFragment,
    catalog: MetadataCatalog,
) -> tuple[SpeciesEntry | None, str | None, bool]:
    source_fields = ["canonical_name_text"]
    if "canonical_name_text" not in fragment.fields:
        source_fields.append("display_name_text")

    for source_field in source_fields:
        value = _fragment_text(fragment, source_field)
        if not value:
            continue
        exact = catalog.resolve_species_name(value)
        if exact is not None:
            return exact, source_field, False
        fuzzy = catalog.resolve_species_name_fuzzy(value)
        if fuzzy is not None:
            return fuzzy, source_field, True
    return None, None, False


def _add_species_fields(
    fragment: PokemonFragment,
    species: SpeciesEntry,
    evidence: str,
) -> None:
    _add_field(
        fragment.fields,
        "species_key",
        species.species_key,
        "metadata_catalog",
        evidence,
    )
    _add_field(
        fragment.fields,
        "species_name",
        species.species_name,
        "metadata_catalog",
        evidence,
    )
    _add_field(
        fragment.fields,
        "pokedex_id",
        species.pokedex_id,
        "metadata_catalog",
        evidence,
    )


def _extract_list_fields(record: FrameRecordLike) -> dict[str, FragmentField]:
    fields: dict[str, FragmentField] = {}
    features = record.features
    for feature_name in (
        "has_list_grid",
        "has_list_cp",
        "has_list_display_name",
        "has_list_pokemon_art",
    ):
        _add_true_feature(fields, features, feature_name)

    list_cp_text = _ocr_text(record, "cp")
    if features.get("has_list_cp") and list_cp_text:
        _add_field(
            fields,
            "list_cp_text",
            list_cp_text,
            "ocr_text",
            "features.has_list_cp + ocr.cp.text",
        )

    list_display_name_text = _ocr_text(record, "display_name")
    if features.get("has_list_display_name") and list_display_name_text:
        _add_field(
            fields,
            "list_display_name_text",
            list_display_name_text,
            "ocr_text",
            "features.has_list_display_name + ocr.display_name.text",
        )

    return fields


def _add_story_fields(fields: dict[str, FragmentField], story: CatchStory) -> None:
    for field_name in (
        "canonical_name_text",
        "catch_date_text",
        "location_text",
        "catch_country_text",
    ):
        _add_field(
            fields,
            field_name,
            getattr(story, field_name),
            "story_ocr",
            "features.has_story + values.story_text",
        )


def _add_iv_fields(
    fields: dict[str, FragmentField],
    features: dict[str, bool],
    values: dict[str, object | None],
) -> None:
    _add_field(
        fields,
        "iv_complete",
        bool(features.get("has_iv_complete")),
        "feature_gate",
        "features.has_iv_complete",
    )

    for field_name in IV_NUMERIC_FIELD_NAMES:
        value = _int_value(values.get(field_name))
        if value is not None:
            _add_field(fields, field_name, value, "decoded_iv", f"values.{field_name}")

    for field_name in ("appraisal_perfect", "iv_star_agreement"):
        value = _bool_value(values.get(field_name))
        if value is not None:
            _add_field(fields, field_name, value, "decoded_iv", f"values.{field_name}")


def _add_move_fields(
    fragment: PokemonFragment,
    field_prefix: str,
    move: MoveEntry,
    source_field: str,
) -> None:
    evidence = f"fields.{source_field} + metadata_catalog.moves"
    _add_field(
        fragment.fields,
        f"{field_prefix}_name",
        move.move_name,
        "metadata_catalog",
        evidence,
    )
    _add_field(
        fragment.fields,
        f"{field_prefix}_key",
        move.move_key,
        "metadata_catalog",
        evidence,
    )


def _fragment_text(fragment: PokemonFragment, field_name: str) -> str:
    field_value = fragment.fields.get(field_name)
    if field_value is None or not isinstance(field_value.value, str):
        return ""
    return field_value.value


def _resolved_move_mentions(
    text: str,
    catalog: MetadataCatalog,
    *,
    max_moves: bool,
) -> list[MoveEntry]:
    normalized_text = _move_resolution_text(text)
    if not normalized_text:
        return []

    matches: list[_MoveMention] = []
    for move in catalog.moves:
        if _is_specific_max_move(move) != max_moves:
            continue
        normalized_name = normalize_move_name(move.move_name)
        if not normalized_name or catalog.resolve_move_name(move.move_name) != move:
            continue
        pattern = rf"(?<!\w){re.escape(normalized_name)}(?!\w)"
        for name_match in re.finditer(pattern, normalized_text):
            matches.append(_MoveMention(name_match.start(), name_match.end(), move))
    matches.extend(_fuzzy_move_mentions(normalized_text, catalog, max_moves=max_moves))

    selected: list[_MoveMention] = []
    for move_match in sorted(matches, key=lambda item: (item.start, -item.length)):
        overlaps_selected = any(
            move_match.start < other.end and move_match.end > other.start
            for other in selected
        )
        if overlaps_selected:
            continue
        selected.append(move_match)

    ordered_moves: list[MoveEntry] = []
    seen_keys: set[str] = set()
    for move_match in sorted(selected, key=lambda item: item.start):
        if move_match.entry.move_key in seen_keys:
            continue
        ordered_moves.append(move_match.entry)
        seen_keys.add(move_match.entry.move_key)
    return ordered_moves


def _move_resolution_text(text: str) -> str:
    normalized = normalize_move_name(text)
    if not normalized:
        return ""
    return " ".join(_MOVE_ANNOTATION_RE.sub(" ", normalized).split())


def _fuzzy_move_mentions(
    normalized_text: str,
    catalog: MetadataCatalog,
    *,
    max_moves: bool,
) -> list[_MoveMention]:
    token_spans = [
        (match.group(0), match.start(), match.end())
        for match in re.finditer(r"\w+", normalized_text)
    ]
    if not token_spans:
        return []

    moves_by_token_count: dict[int, list[tuple[str, MoveEntry]]] = {}
    for move in catalog.moves:
        if _is_specific_max_move(move) != max_moves:
            continue
        normalized_name = normalize_move_name(move.move_name)
        if (
            not normalized_name
            or len(normalized_name) < 5
            or catalog.resolve_move_name(move.move_name) != move
        ):
            continue
        moves_by_token_count.setdefault(len(normalized_name.split()), []).append(
            (normalized_name, move)
        )

    matches: list[_MoveMention] = []
    for token_count, moves in moves_by_token_count.items():
        if token_count > len(token_spans):
            continue
        for start_index in range(len(token_spans) - token_count + 1):
            window = token_spans[start_index : start_index + token_count]
            phrase = " ".join(token for token, _start, _end in window)
            if _move_annotation_phrase(phrase):
                continue
            exact = catalog.resolve_move_name(phrase)
            if exact is not None:
                continue
            candidates = {
                move
                for normalized_name, move in moves
                if _move_name_fuzzy_match(phrase, normalized_name)
            }
            if len(candidates) != 1:
                continue
            start = window[0][1]
            end = window[-1][2]
            matches.append(_MoveMention(start, end, next(iter(candidates))))
    return matches


def _move_annotation_phrase(text: str) -> bool:
    normalized = normalize_move_name(text)
    return bool(normalized) and normalized.endswith(" bonus")


def _move_name_fuzzy_match(text: str, move_name: str) -> bool:
    if abs(len(text) - len(move_name)) > 2:
        return False
    max_distance = 2 if " " in move_name and len(move_name) >= 9 else 1
    return edit_distance_within(text, move_name, max_distance)


def _first_max_move(text: str, catalog: MetadataCatalog) -> MoveEntry | None:
    matches = _resolved_move_mentions(text, catalog, max_moves=True)
    return matches[0] if matches else None


def _is_specific_max_move(move: MoveEntry) -> bool:
    normalized_name = normalize_move_name(move.move_name)
    if normalized_name in _GENERIC_MAX_MOVE_NAMES:
        return False
    return normalized_name.startswith("max ") or normalized_name.startswith("g max ")


def _add_true_feature(
    fields: dict[str, FragmentField],
    features: dict[str, bool],
    feature_name: str,
) -> None:
    if features.get(feature_name):
        _add_field(
            fields,
            feature_name,
            True,
            "feature_gate",
            f"features.{feature_name}",
        )


def _add_field(
    fields: dict[str, FragmentField],
    name: str,
    value: JsonScalar,
    source: str,
    evidence: str,
) -> None:
    fields[name] = FragmentField(value=value, source=source, evidence=evidence)


def _ocr_text(record: FrameRecordLike, field_name: str) -> str:
    payload = record.ocr.get(field_name)
    if payload is None:
        return ""
    text = payload.get("text")
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())


def _clean_display_name_text(text: str) -> str:
    if not text or _looks_like_display_noise(text):
        return ""
    return text


def _looks_like_display_noise(text: str) -> bool:
    normalized = text.casefold().strip()
    letter_count = sum(character.isalpha() for character in normalized)
    alnum_count = sum(character.isalnum() for character in normalized)
    punctuation_count = sum(
        not character.isalnum() and not character.isspace() for character in normalized
    )
    if letter_count < 2:
        return True
    if punctuation_count > letter_count:
        return True
    if re.search(r"\d+(?:[.,]\d+)?\s*(?:kg|m)\b", normalized):
        return True
    if re.search(r"\d+\s*/\s*\d+", normalized):
        return True
    if any(character.isdigit() for character in normalized) and letter_count <= 3:
        return True
    if re.search(
        r"\b(?:cp|hp|weight|height|stardust|candy|power up|"
        r"gyms|raids|trainer battles)\b",
        normalized,
    ):
        return True
    return alnum_count == 0


def _text_value(value: object | None) -> str:
    return value if isinstance(value, str) else ""


def _int_value(value: object | None) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _float_value(value: object | None) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _bool_value(value: object | None) -> bool | None:
    return value if isinstance(value, bool) else None


def _hp_parts(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"(?P<current>\d{1,3})/(?P<maximum>\d{2,3})", value)
    if match is None:
        return None
    return int(match.group("current")), int(match.group("maximum"))
