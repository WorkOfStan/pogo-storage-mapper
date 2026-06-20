from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from pogo_storage_mapper.metadata import (
    BaseStatsEntry,
    CpMultiplierEntry,
    EvolutionEntry,
    MetadataCatalog,
    MoveEntry,
    SpeciesEntry,
    default_catalog_path,
    save_metadata_catalog,
)

GAME_MASTER_TIMESTAMP_URL = (
    "https://raw.githubusercontent.com/PokeMiners/game_masters/master/latest/"
    "timestamp.txt"
)
GAME_MASTER_LATEST_URL = (
    "https://raw.githubusercontent.com/PokeMiners/game_masters/master/latest/"
    "latest.json"
)

_SPECIAL_SPECIES_NAMES = {
    "FARFETCHD": "Farfetch'd",
    "SIRFETCHD": "Sirfetch'd",
    "MR_MIME": "Mr. Mime",
    "MIME_JR": "Mime Jr.",
    "HO_OH": "Ho-Oh",
    "PORYGON_Z": "Porygon-Z",
    "TYPE_NULL": "Type: Null",
    "WO_CHIEN": "Wo-Chien",
    "CHI_YU": "Chi-Yu",
    "TING_LU": "Ting-Lu",
    "CHIEN_PAO": "Chien-Pao",
    "JANGMO_O": "Jangmo-o",
    "HAKAMO_O": "Hakamo-o",
    "KOMMO_O": "Kommo-o",
    "NIDORAN_FEMALE": "Nidoran Female",
    "NIDORAN_MALE": "Nidoran Male",
}
_SPECIAL_SPECIES_ALIASES = {
    "MR_MIME": ("Mr Mime", "Mister Mime"),
    "MIME_JR": ("Mime Jr",),
    "NIDORAN_FEMALE": ("Nidoran F", "Nidoran Female"),
    "NIDORAN_MALE": ("Nidoran M", "Nidoran Male"),
}


@dataclass(frozen=True, slots=True)
class SyncMetadataReport:
    timestamp: str
    species_entries: int
    move_entries: int
    evolution_entries: int
    base_stat_entries: int
    cp_multiplier_entries: int
    output_path: Path

    def summary_line(self) -> str:
        return (
            f"Synced metadata {self.timestamp}: {self.species_entries} species, "
            f"{self.move_entries} moves, {self.evolution_entries} evolutions, "
            f"{self.base_stat_entries} base stats, "
            f"{self.cp_multiplier_entries} CP multipliers -> {self.output_path}"
        )


@dataclass(slots=True)
class _SpeciesBuilder:
    species_key: str
    species_name: str
    pokedex_id: int
    aliases: set[str] = field(default_factory=set)
    forms: set[str] = field(default_factory=set)
    upstream_ids: set[str] = field(default_factory=set)


def sync_metadata_catalog(
    output_path: Path | None = None,
    *,
    timestamp_url: str = GAME_MASTER_TIMESTAMP_URL,
    game_master_url: str = GAME_MASTER_LATEST_URL,
) -> SyncMetadataReport:
    target_path = output_path or default_catalog_path()
    timestamp = fetch_text(timestamp_url).strip()
    payload = json.loads(fetch_text(game_master_url))
    catalog = build_metadata_catalog_from_game_master(
        payload,
        timestamp=timestamp,
        source_url=game_master_url,
    )
    if _template_list(payload) and not catalog.species and not catalog.moves:
        msg = (
            "Metadata sync produced an empty catalog from a non-empty "
            "Game Master payload."
        )
        raise ValueError(msg)
    save_metadata_catalog(target_path, catalog)
    return SyncMetadataReport(
        timestamp=timestamp,
        species_entries=len(catalog.species),
        move_entries=len(catalog.moves),
        evolution_entries=len(catalog.evolutions),
        base_stat_entries=len(catalog.base_stats),
        cp_multiplier_entries=len(catalog.cp_multipliers),
        output_path=target_path,
    )


def build_metadata_catalog_from_game_master(
    payload: object,
    *,
    timestamp: str | None = None,
    source_url: str | None = None,
) -> MetadataCatalog:
    templates = _template_list(payload)
    return MetadataCatalog(
        species=_build_species_entries(templates),
        moves=_build_move_entries(templates),
        evolutions=_build_evolution_entries(templates),
        base_stats=_build_base_stats_entries(templates),
        cp_multipliers=_build_cp_multiplier_entries(templates),
        timestamp=timestamp,
        source_url=source_url,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def fetch_text(url: str) -> str:
    try:
        with urlopen(url, timeout=60) as response:
            return response.read().decode("utf-8")
    except (HTTPError, URLError) as exc:
        msg = f"Failed to fetch {url}: {exc}"
        raise RuntimeError(msg) from exc


def _template_list(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, dict):
        for key in ("itemTemplates", "templates"):
            value = payload.get(key)
            if isinstance(value, list):
                return [
                    _template_data(item) for item in value if isinstance(item, dict)
                ]
    if isinstance(payload, list):
        return [_template_data(item) for item in payload if isinstance(item, dict)]
    msg = "Unexpected Game Master payload."
    raise ValueError(msg)


def _template_data(template: dict[str, object]) -> dict[str, object]:
    data = _dict_value(template.get("data"))
    if not data:
        return template

    normalized = dict(data)
    if "templateId" not in normalized:
        template_id = _string_value(template.get("templateId"))
        if template_id is not None:
            normalized["templateId"] = template_id
    return normalized


def _build_species_entries(
    templates: list[dict[str, object]],
) -> tuple[SpeciesEntry, ...]:
    builders: dict[str, _SpeciesBuilder] = {}
    for template in templates:
        settings = _dict_value(template.get("pokemonSettings"))
        if not settings:
            continue
        pokemon_id = _string_value(
            settings.get("pokemonId"), settings.get("pokemon_id")
        )
        if pokemon_id is None:
            continue
        pokedex_id = _pokedex_id(settings, _string_value(template.get("templateId")))
        if pokedex_id is None:
            continue

        key = _key_from_game_id(pokemon_id)
        builder = builders.get(key)
        if builder is None:
            builder = _SpeciesBuilder(
                species_key=key,
                species_name=_species_name_from_id(pokemon_id),
                pokedex_id=pokedex_id,
            )
            builders[key] = builder
        builder.pokedex_id = min(builder.pokedex_id, pokedex_id)
        builder.aliases.update(_species_aliases(pokemon_id))
        form = _form_name(_string_value(settings.get("form")), pokemon_id)
        if form is not None:
            builder.forms.add(form)
        builder.upstream_ids.add(pokemon_id)
        template_id = _string_value(template.get("templateId"))
        if template_id is not None:
            builder.upstream_ids.add(template_id)

    entries = [
        SpeciesEntry(
            species_key=builder.species_key,
            species_name=builder.species_name,
            pokedex_id=builder.pokedex_id,
            aliases=tuple(sorted(builder.aliases)),
            forms=tuple(sorted(builder.forms)),
            upstream_ids=tuple(sorted(builder.upstream_ids)),
        )
        for builder in builders.values()
    ]
    return tuple(
        sorted(entries, key=lambda entry: (entry.pokedex_id, entry.species_key))
    )


def _build_move_entries(templates: list[dict[str, object]]) -> tuple[MoveEntry, ...]:
    builders: dict[str, dict[str, object]] = {}
    for template in templates:
        template_id = _string_value(template.get("templateId"))
        for settings_key in ("moveSettings", "combatMove", "combatMoveSettings"):
            settings = _dict_value(template.get(settings_key))
            if not settings:
                continue
            move_id = _string_value(
                settings.get("movementId"),
                settings.get("uniqueId"),
                settings.get("moveId"),
            )
            if move_id is None:
                continue
            move_key = _key_from_game_id(move_id)
            builder = builders.setdefault(
                move_key,
                {
                    "move_key": move_key,
                    "move_name": _move_name_from_id(move_id),
                    "move_type": _move_type(settings),
                    "category": _move_category(move_id, settings_key),
                    "upstream_ids": set(),
                },
            )
            if builder["move_type"] is None:
                builder["move_type"] = _move_type(settings)
            if builder["category"] is None:
                builder["category"] = _move_category(move_id, settings_key)
            upstream_ids = _as_set(builder["upstream_ids"])
            upstream_ids.add(move_id)
            if template_id is not None:
                upstream_ids.add(template_id)

    entries = [
        MoveEntry(
            move_key=str(builder["move_key"]),
            move_name=str(builder["move_name"]),
            move_type=(
                str(builder["move_type"]) if builder["move_type"] is not None else None
            ),
            category=(
                str(builder["category"]) if builder["category"] is not None else None
            ),
            upstream_ids=tuple(sorted(_as_set(builder["upstream_ids"]))),
        )
        for builder in builders.values()
    ]
    return tuple(sorted(entries, key=lambda entry: entry.move_key))


def _build_evolution_entries(
    templates: list[dict[str, object]],
) -> tuple[EvolutionEntry, ...]:
    entries: set[EvolutionEntry] = set()
    for template in templates:
        settings = _dict_value(template.get("pokemonSettings"))
        if not settings:
            continue
        pokemon_id = _string_value(
            settings.get("pokemonId"), settings.get("pokemon_id")
        )
        if pokemon_id is None:
            continue
        species_key = _key_from_game_id(pokemon_id)
        form = _form_name(_string_value(settings.get("form")), pokemon_id)
        branches = settings.get("evolutionBranch")
        if not isinstance(branches, list):
            continue
        for branch in branches:
            if not isinstance(branch, dict):
                continue
            evolution_id = _string_value(
                branch.get("evolution"),
                branch.get("evolutionPokemonId"),
                branch.get("pokemonId"),
            )
            if evolution_id is None:
                continue
            evolves_to_form = _form_name(
                _string_value(
                    branch.get("evolutionForm"),
                    branch.get("form"),
                    branch.get("pokemonDisplayForm"),
                ),
                evolution_id,
            )
            entries.add(
                EvolutionEntry(
                    species_key=species_key,
                    evolves_to_key=_key_from_game_id(evolution_id),
                    form=form,
                    evolves_to_form=evolves_to_form,
                )
            )
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                entry.species_key,
                entry.form or "",
                entry.evolves_to_key,
                entry.evolves_to_form or "",
            ),
        )
    )


def _build_base_stats_entries(
    templates: list[dict[str, object]],
) -> tuple[BaseStatsEntry, ...]:
    entries: dict[tuple[str, str], BaseStatsEntry] = {}
    for template in templates:
        settings = _dict_value(template.get("pokemonSettings"))
        if not settings:
            continue
        pokemon_id = _string_value(
            settings.get("pokemonId"), settings.get("pokemon_id")
        )
        if pokemon_id is None:
            continue
        stats = _dict_value(settings.get("stats"))
        if not stats:
            stats = _dict_value(settings.get("pokemonBaseStats"))
        base_attack = _int_value(stats.get("baseAttack"), stats.get("base_attack"))
        base_defense = _int_value(stats.get("baseDefense"), stats.get("base_defense"))
        base_stamina = _int_value(stats.get("baseStamina"), stats.get("base_stamina"))
        if base_attack is None or base_defense is None or base_stamina is None:
            continue
        species_key = _key_from_game_id(pokemon_id)
        form = _form_name(_string_value(settings.get("form")), pokemon_id) or "Normal"
        entries[(species_key, form)] = BaseStatsEntry(
            species_key=species_key,
            form=form,
            base_attack=base_attack,
            base_defense=base_defense,
            base_stamina=base_stamina,
        )
    return tuple(
        sorted(entries.values(), key=lambda entry: (entry.species_key, entry.form))
    )


def _build_cp_multiplier_entries(
    templates: list[dict[str, object]],
) -> tuple[CpMultiplierEntry, ...]:
    for template in templates:
        player_level = _dict_value(template.get("playerLevel"))
        if not player_level:
            continue
        multipliers = player_level.get("cpMultiplier")
        if not isinstance(multipliers, list):
            continue
        full_level_cpms = []
        for raw_value in multipliers:
            if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
                continue
            full_level_cpms.append(float(raw_value))
        entries = []
        for index, cpm in enumerate(full_level_cpms, start=1):
            if index > 50:
                break
            entries.append(CpMultiplierEntry(level=float(index), cpm=cpm))
            if index < 50 and index < len(full_level_cpms):
                next_cpm = full_level_cpms[index]
                half_level_cpm = math.sqrt((cpm * cpm + next_cpm * next_cpm) / 2)
                entries.append(CpMultiplierEntry(level=index + 0.5, cpm=half_level_cpm))
        if entries:
            return tuple(entries)
    return ()


def _pokedex_id(settings: dict[str, object], template_id: str | None) -> int | None:
    for key in ("pokedexId", "pokedex_id", "pokedexNumber"):
        value = settings.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    if template_id is None:
        return None
    match = re.match(r"V(?P<dex>\d{4})_POKEMON(?:_|$)", template_id)
    if match is None:
        return None
    return int(match.group("dex"))


def _species_aliases(pokemon_id: str) -> tuple[str, ...]:
    species_name = _species_name_from_id(pokemon_id)
    aliases = {
        species_name,
        pokemon_id.replace("_", " ").title(),
        pokemon_id,
    }
    aliases.update(_SPECIAL_SPECIES_ALIASES.get(pokemon_id, ()))
    return tuple(sorted(alias for alias in aliases if alias))


def _species_name_from_id(pokemon_id: str) -> str:
    if pokemon_id in _SPECIAL_SPECIES_NAMES:
        return _SPECIAL_SPECIES_NAMES[pokemon_id]
    return _title_from_game_id(pokemon_id)


def _move_name_from_id(move_id: str) -> str:
    raw = move_id.removesuffix("_FAST")
    return _title_from_game_id(raw)


def _form_name(form_id: str | None, pokemon_id: str) -> str | None:
    if form_id is None:
        return None
    raw = form_id
    prefix = f"{pokemon_id}_"
    if raw.startswith(prefix):
        raw = raw.removeprefix(prefix)
    if raw in {"", "NORMAL"}:
        return "Normal"
    return _title_from_game_id(raw)


def _move_type(settings: dict[str, object]) -> str | None:
    raw_type = _string_value(
        settings.get("pokemonType"),
        settings.get("pokemon_type"),
        settings.get("type"),
    )
    if raw_type is None:
        return None
    return _title_from_game_id(raw_type.removeprefix("POKEMON_TYPE_"))


def _move_category(move_id: str, settings_key: str) -> str:
    if move_id.endswith("_FAST"):
        return "fast"
    if settings_key == "combatMove":
        return "combat"
    return "charged"


def _title_from_game_id(value: str) -> str:
    return " ".join(part.capitalize() for part in value.split("_") if part)


def _key_from_game_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold())
    return normalized.strip("-")


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _string_value(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _int_value(*values: object) -> int | None:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _as_set(value: object) -> set[str]:
    if isinstance(value, set):
        return value
    msg = "Internal metadata builder expected a set."
    raise TypeError(msg)
