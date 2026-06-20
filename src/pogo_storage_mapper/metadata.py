from __future__ import annotations

import json
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, TypeVar

CATALOG_SCHEMA_VERSION = 2
DEFAULT_CATALOG_RESOURCE = "data/metadata_catalog.json"
CatalogIndexEntry = TypeVar("CatalogIndexEntry")


@dataclass(frozen=True, slots=True)
class SpeciesEntry:
    species_key: str
    species_name: str
    pokedex_id: int
    aliases: tuple[str, ...] = ()
    forms: tuple[str, ...] = ()
    upstream_ids: tuple[str, ...] = ()

    @classmethod
    def from_json_dict(cls, payload: dict[str, object]) -> SpeciesEntry:
        return cls(
            species_key=_required_str(payload, "species_key"),
            species_name=_required_str(payload, "species_name"),
            pokedex_id=_required_int(payload, "pokedex_id"),
            aliases=_string_tuple(payload.get("aliases")),
            forms=_string_tuple(payload.get("forms")),
            upstream_ids=_string_tuple(payload.get("upstream_ids")),
        )

    def to_json_dict(self) -> dict[str, object]:
        return {
            "species_key": self.species_key,
            "species_name": self.species_name,
            "pokedex_id": self.pokedex_id,
            "aliases": list(self.aliases),
            "forms": list(self.forms),
            "upstream_ids": list(self.upstream_ids),
        }


@dataclass(frozen=True, slots=True)
class MoveEntry:
    move_key: str
    move_name: str
    move_type: str | None = None
    category: str | None = None
    upstream_ids: tuple[str, ...] = ()

    @classmethod
    def from_json_dict(cls, payload: dict[str, object]) -> MoveEntry:
        move_type = payload.get("move_type")
        category = payload.get("category")
        return cls(
            move_key=_required_str(payload, "move_key"),
            move_name=_required_str(payload, "move_name"),
            move_type=move_type if isinstance(move_type, str) else None,
            category=category if isinstance(category, str) else None,
            upstream_ids=_string_tuple(payload.get("upstream_ids")),
        )

    def to_json_dict(self) -> dict[str, object]:
        return {
            "move_key": self.move_key,
            "move_name": self.move_name,
            "move_type": self.move_type,
            "category": self.category,
            "upstream_ids": list(self.upstream_ids),
        }


@dataclass(frozen=True, slots=True)
class EvolutionEntry:
    species_key: str
    evolves_to_key: str
    form: str | None = None
    evolves_to_form: str | None = None
    family_id: str | None = None

    @classmethod
    def from_json_dict(cls, payload: dict[str, object]) -> EvolutionEntry:
        form = payload.get("form")
        evolves_to_form = payload.get("evolves_to_form")
        family_id = payload.get("family_id")
        return cls(
            species_key=_required_str(payload, "species_key"),
            evolves_to_key=_required_str(payload, "evolves_to_key"),
            form=form if isinstance(form, str) and form else None,
            evolves_to_form=(
                evolves_to_form
                if isinstance(evolves_to_form, str) and evolves_to_form
                else None
            ),
            family_id=family_id if isinstance(family_id, str) and family_id else None,
        )

    def to_json_dict(self) -> dict[str, object]:
        return {
            "species_key": self.species_key,
            "evolves_to_key": self.evolves_to_key,
            "form": self.form,
            "evolves_to_form": self.evolves_to_form,
            "family_id": self.family_id,
        }


@dataclass(frozen=True, slots=True)
class BaseStatsEntry:
    species_key: str
    form: str
    base_attack: int
    base_defense: int
    base_stamina: int

    @classmethod
    def from_json_dict(cls, payload: dict[str, object]) -> BaseStatsEntry:
        form = payload.get("form")
        return cls(
            species_key=_required_str(payload, "species_key"),
            form=form if isinstance(form, str) and form else "Normal",
            base_attack=_required_int(payload, "base_attack"),
            base_defense=_required_int(payload, "base_defense"),
            base_stamina=_required_int(payload, "base_stamina"),
        )

    def to_json_dict(self) -> dict[str, object]:
        return {
            "species_key": self.species_key,
            "form": self.form,
            "base_attack": self.base_attack,
            "base_defense": self.base_defense,
            "base_stamina": self.base_stamina,
        }


@dataclass(frozen=True, slots=True)
class CpMultiplierEntry:
    level: float
    cpm: float

    @classmethod
    def from_json_dict(cls, payload: dict[str, object]) -> CpMultiplierEntry:
        return cls(
            level=_required_float(payload, "level"),
            cpm=_required_float(payload, "cpm"),
        )

    def to_json_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "cpm": self.cpm,
        }


@dataclass(slots=True)
class MetadataCatalog:
    species: tuple[SpeciesEntry, ...] = ()
    moves: tuple[MoveEntry, ...] = ()
    evolutions: tuple[EvolutionEntry, ...] = ()
    base_stats: tuple[BaseStatsEntry, ...] = ()
    cp_multipliers: tuple[CpMultiplierEntry, ...] = ()
    timestamp: str | None = None
    source_url: str | None = None
    generated_at: str | None = None
    _species_index: dict[str, SpeciesEntry | None] = field(
        default_factory=dict, init=False, repr=False
    )
    _move_index: dict[str, MoveEntry | None] = field(
        default_factory=dict, init=False, repr=False
    )

    @classmethod
    def from_json_dict(cls, payload: dict[str, object]) -> MetadataCatalog:
        species_payload = payload.get("species", [])
        move_payload = payload.get("moves", [])
        evolution_payload = payload.get("evolutions", [])
        stats_payload = payload.get("base_stats", [])
        cpm_payload = payload.get("cp_multipliers", [])
        if (
            not isinstance(species_payload, list)
            or not isinstance(move_payload, list)
            or not isinstance(evolution_payload, list)
            or not isinstance(stats_payload, list)
            or not isinstance(cpm_payload, list)
        ):
            msg = (
                "Metadata catalog must contain species, moves, evolutions, "
                "base_stats, and cp_multipliers lists."
            )
            raise ValueError(msg)
        upstream = payload.get("upstream")
        source_url = None
        timestamp = None
        if isinstance(upstream, dict):
            source = upstream.get("game_master_url")
            source_url = source if isinstance(source, str) else None
            raw_timestamp = upstream.get("timestamp")
            timestamp = raw_timestamp if isinstance(raw_timestamp, str) else None
        raw_generated_at = payload.get("generated_at")
        return cls(
            species=tuple(
                SpeciesEntry.from_json_dict(item)
                for item in species_payload
                if isinstance(item, dict)
            ),
            moves=tuple(
                MoveEntry.from_json_dict(item)
                for item in move_payload
                if isinstance(item, dict)
            ),
            evolutions=tuple(
                EvolutionEntry.from_json_dict(item)
                for item in evolution_payload
                if isinstance(item, dict)
            ),
            base_stats=tuple(
                BaseStatsEntry.from_json_dict(item)
                for item in stats_payload
                if isinstance(item, dict)
            ),
            cp_multipliers=tuple(
                CpMultiplierEntry.from_json_dict(item)
                for item in cpm_payload
                if isinstance(item, dict)
            ),
            timestamp=timestamp,
            source_url=source_url,
            generated_at=(
                raw_generated_at if isinstance(raw_generated_at, str) else None
            ),
        )

    def to_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "upstream": {
                "timestamp": self.timestamp,
                "game_master_url": self.source_url,
            },
            "species": [entry.to_json_dict() for entry in self.species],
            "moves": [entry.to_json_dict() for entry in self.moves],
            "evolutions": [entry.to_json_dict() for entry in self.evolutions],
            "base_stats": [entry.to_json_dict() for entry in self.base_stats],
            "cp_multipliers": [entry.to_json_dict() for entry in self.cp_multipliers],
        }

    def resolve_species_name(self, text: str | None) -> SpeciesEntry | None:
        normalized = normalize_catalog_name(text)
        if not normalized:
            return None
        if not self._species_index:
            self._species_index.update(_build_species_index(self.species))
        return self._species_index.get(normalized)

    def resolve_species_name_fuzzy(
        self,
        text: str | None,
        *,
        max_distance: int = 1,
        min_length: int = 5,
    ) -> SpeciesEntry | None:
        normalized = normalize_catalog_name(text)
        if len(normalized) < min_length:
            return None
        exact = self.resolve_species_name(normalized)
        if exact is not None:
            return exact

        matches: set[SpeciesEntry] = set()
        for entry in self.species:
            for name in (entry.species_name, *entry.aliases):
                candidate = normalize_catalog_name(name)
                if len(candidate) < min_length:
                    continue
                if abs(len(candidate) - len(normalized)) > max_distance:
                    continue
                if edit_distance_within(normalized, candidate, max_distance):
                    matches.add(entry)
                    break
            if len(matches) > 1:
                return None
        return next(iter(matches)) if len(matches) == 1 else None

    def resolve_move_name(self, text: str | None) -> MoveEntry | None:
        normalized = normalize_move_name(text)
        if not normalized:
            return None
        if not self._move_index:
            self._move_index.update(_build_move_index(self.moves))
        return self._move_index.get(normalized)


def normalize_catalog_name(text: str | None) -> str:
    return _normalize_name(text, punctuation_as_space=False)


def normalize_move_name(text: str | None) -> str:
    return _normalize_name(text, punctuation_as_space=True)


def _normalize_name(text: str | None, *, punctuation_as_space: bool) -> str:
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text).casefold().strip()
    normalized = normalized.replace("&", " and ")
    characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if category.startswith("M"):
            continue
        if character.isalnum() or character.isspace():
            characters.append(character)
        elif punctuation_as_space:
            characters.append(" ")
    return " ".join("".join(characters).split())


def edit_distance_within(left: str, right: str, max_distance: int) -> bool:
    if max_distance < 0:
        return False
    if left == right:
        return True
    if abs(len(left) - len(right)) > max_distance:
        return False

    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        row_min = current[0]
        for right_index, right_character in enumerate(right, start=1):
            substitution_cost = 0 if left_character == right_character else 1
            current.append(
                min(
                    previous[right_index] + 1,
                    current[right_index - 1] + 1,
                    previous[right_index - 1] + substitution_cost,
                )
            )
            row_min = min(row_min, current[-1])
        if row_min > max_distance:
            return False
        previous = current
    return previous[-1] <= max_distance


def default_catalog_path() -> Path:
    return Path(__file__).with_name("data") / "metadata_catalog.json"


def load_metadata_catalog(path: Path) -> MetadataCatalog:
    if not path.exists():
        return MetadataCatalog()
    return MetadataCatalog.from_json_dict(json.loads(path.read_text(encoding="utf-8")))


def load_default_metadata_catalog() -> MetadataCatalog:
    try:
        resource = resources.files("pogo_storage_mapper").joinpath(
            DEFAULT_CATALOG_RESOURCE
        )
        with resource.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return MetadataCatalog()
    return MetadataCatalog.from_json_dict(payload)


def save_metadata_catalog(path: Path, catalog: MetadataCatalog) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(catalog.to_json_dict(), indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _build_species_index(
    species: tuple[SpeciesEntry, ...],
) -> dict[str, SpeciesEntry | None]:
    return build_name_index(
        species,
        lambda entry: (entry.species_name, *entry.aliases),
        normalize_catalog_name,
    )


def _build_move_index(moves: tuple[MoveEntry, ...]) -> dict[str, MoveEntry | None]:
    return build_name_index(moves, _move_index_names, normalize_move_name)


def build_name_index(
    entries: Iterable[CatalogIndexEntry],
    names_for: Callable[[CatalogIndexEntry], Iterable[str]],
    normalize: Callable[[str], str],
) -> dict[str, CatalogIndexEntry | None]:
    index: dict[str, CatalogIndexEntry | None] = {}
    for entry in entries:
        for name in names_for(entry):
            normalized = normalize(name)
            if not normalized:
                continue
            if normalized in index and index[normalized] != entry:
                index[normalized] = None
            else:
                index[normalized] = entry
    return index


def build_family_index(
    species_entries: tuple[SpeciesEntry, ...],
    evolutions: tuple[EvolutionEntry, ...],
    stats_entries: tuple[BaseStatsEntry, ...],
) -> dict[str, str]:
    species_keys = {entry.species_key for entry in species_entries}
    species_keys.update(entry.species_key for entry in stats_entries)
    for edge in evolutions:
        species_keys.add(edge.species_key)
        species_keys.add(edge.evolves_to_key)

    neighbors = {species_key: set[str]() for species_key in species_keys}
    for edge in evolutions:
        neighbors.setdefault(edge.species_key, set()).add(edge.evolves_to_key)
        neighbors.setdefault(edge.evolves_to_key, set()).add(edge.species_key)

    dex_by_key = {entry.species_key: entry.pokedex_id for entry in species_entries}
    family_by_species: dict[str, str] = {}
    remaining = set(species_keys)
    while remaining:
        start = remaining.pop()
        component = {start}
        queue = [start]
        while queue:
            current = queue.pop(0)
            for neighbor in neighbors.get(current, set()):
                if neighbor not in component:
                    component.add(neighbor)
                    remaining.discard(neighbor)
                    queue.append(neighbor)
        root = min(component, key=lambda key: (dex_by_key.get(key, 9999), key))
        for species_key in component:
            family_by_species[species_key] = root
    return family_by_species


def _move_index_names(entry: MoveEntry) -> tuple[str, ...]:
    names = [entry.move_name, entry.move_key.replace("-", " ")]
    for upstream_id in entry.upstream_ids:
        raw = upstream_id
        for prefix in ("COMBAT_MOVE_", "MOVE_"):
            raw = raw.removeprefix(prefix)
        raw = raw.removesuffix("_FAST")
        names.append(raw.replace("_", " "))
    return tuple(names)


def _required_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        msg = f"Metadata catalog field {key!r} must be a non-empty string."
        raise ValueError(msg)
    return value


def _required_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"Metadata catalog field {key!r} must be an integer."
        raise ValueError(msg)
    return value


def _required_float(payload: dict[str, object], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"Metadata catalog field {key!r} must be numeric."
        raise ValueError(msg)
    return float(value)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)
