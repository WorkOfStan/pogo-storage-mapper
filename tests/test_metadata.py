from __future__ import annotations

import pytest
from conftest import species, species_catalog

from pogo_storage_mapper import metadata_sync
from pogo_storage_mapper.metadata import (
    MetadataCatalog,
    MoveEntry,
    normalize_catalog_name,
    normalize_move_name,
)
from pogo_storage_mapper.metadata_sync import build_metadata_catalog_from_game_master


def test_build_metadata_catalog_from_game_master_templates() -> None:
    catalog = build_metadata_catalog_from_game_master(
        {
            "itemTemplates": [
                {
                    "templateId": "V0001_POKEMON_BULBASAUR",
                    "pokemonSettings": {
                        "pokemonId": "BULBASAUR",
                        "form": "BULBASAUR_NORMAL",
                        "stats": {
                            "baseAttack": 118,
                            "baseDefense": 111,
                            "baseStamina": 128,
                        },
                        "evolutionBranch": [{"evolution": "IVYSAUR"}],
                    },
                },
                {
                    "templateId": "V0002_POKEMON_IVYSAUR",
                    "pokemonSettings": {
                        "pokemonId": "IVYSAUR",
                        "form": "IVYSAUR_NORMAL",
                    },
                },
                {
                    "templateId": "V0025_POKEMON_PIKACHU_WORLD_CAP",
                    "pokemonSettings": {
                        "pokemonId": "PIKACHU",
                        "form": "PIKACHU_WORLD_CAP",
                    },
                },
                {
                    "templateId": "VINE_WHIP_FAST",
                    "moveSettings": {
                        "movementId": "VINE_WHIP_FAST",
                        "pokemonType": "POKEMON_TYPE_GRASS",
                    },
                },
                {
                    "templateId": "COMBAT_MOVE_POWER_WHIP",
                    "combatMove": {
                        "uniqueId": "POWER_WHIP",
                        "type": "POKEMON_TYPE_GRASS",
                    },
                },
                {
                    "templateId": "PLAYER_LEVEL_SETTINGS",
                    "playerLevel": {"cpMultiplier": [0.1, 0.2]},
                },
            ],
        },
        timestamp="123",
        source_url="https://example.invalid/latest.json",
    )

    bulbasaur = catalog.resolve_species_name("Bulbasaur")
    assert bulbasaur is not None
    assert bulbasaur.species_key == "bulbasaur"
    assert bulbasaur.species_name == "Bulbasaur"
    assert bulbasaur.pokedex_id == 1
    assert bulbasaur.forms == ("Normal",)

    pikachu = catalog.resolve_species_name("Pikachu")
    assert pikachu is not None
    assert pikachu.forms == ("World Cap",)
    assert {move.move_key for move in catalog.moves} == {
        "power-whip",
        "vine-whip-fast",
    }
    vine_whip = next(
        move for move in catalog.moves if move.move_key == "vine-whip-fast"
    )
    assert vine_whip.move_name == "Vine Whip"
    assert vine_whip.move_type == "Grass"
    assert vine_whip.category == "fast"
    assert catalog.evolutions[0].species_key == "bulbasaur"
    assert catalog.evolutions[0].evolves_to_key == "ivysaur"
    assert catalog.base_stats[0].base_attack == 118
    assert catalog.cp_multipliers[1].level == 1.5
    assert catalog.cp_multipliers[1].cpm == pytest.approx(0.158113883)


def test_build_metadata_catalog_from_nested_template_data() -> None:
    catalog = build_metadata_catalog_from_game_master(
        [
            {
                "templateId": "V0001_POKEMON_BULBASAUR",
                "data": {
                    "pokemonSettings": {
                        "pokemonId": "BULBASAUR",
                        "form": "BULBASAUR_NORMAL",
                    },
                },
            },
            {
                "templateId": "COMBAT_V0214_MOVE_VINE_WHIP_FAST",
                "data": {
                    "combatMove": {
                        "uniqueId": "VINE_WHIP_FAST",
                        "type": "POKEMON_TYPE_GRASS",
                    },
                },
            },
            {
                "templateId": "COMBAT_V0116_MOVE_SOLAR_BEAM",
                "data": {
                    "combatMove": {
                        "uniqueId": "SOLAR_BEAM",
                        "type": "POKEMON_TYPE_GRASS",
                    },
                },
            },
        ],
        timestamp="123",
        source_url="https://example.invalid/latest.json",
    )

    bulbasaur = catalog.resolve_species_name("Bulbasaur")
    assert bulbasaur is not None
    assert bulbasaur.pokedex_id == 1
    assert "V0001_POKEMON_BULBASAUR" in bulbasaur.upstream_ids

    vine_whip = catalog.resolve_move_name("Vine Whip")
    solar_beam = catalog.resolve_move_name("Solar Beam")
    assert vine_whip is not None
    assert vine_whip.move_key == "vine-whip-fast"
    assert vine_whip.move_type == "Grass"
    assert "COMBAT_V0214_MOVE_VINE_WHIP_FAST" in vine_whip.upstream_ids
    assert solar_beam is not None
    assert solar_beam.move_key == "solar-beam"
    assert solar_beam.move_type == "Grass"


def test_sync_metadata_rejects_empty_catalog_before_saving(
    tmp_path, monkeypatch
) -> None:
    def fake_fetch_text(url: str) -> str:
        if url.endswith("timestamp.txt"):
            return "123"
        return '[{"templateId":"UNKNOWN","data":{"templateId":"UNKNOWN"}}]'

    def fail_save_metadata_catalog(*_args: object) -> None:
        raise AssertionError("empty catalog should not be saved")

    output_path = tmp_path / "catalog.json"
    monkeypatch.setattr(metadata_sync, "fetch_text", fake_fetch_text)
    monkeypatch.setattr(
        metadata_sync, "save_metadata_catalog", fail_save_metadata_catalog
    )

    with pytest.raises(ValueError, match="empty catalog"):
        metadata_sync.sync_metadata_catalog(output_path)

    assert not output_path.exists()


def test_species_resolution_normalizes_punctuation_and_aliases() -> None:
    catalog = species_catalog(
        species("mr-mime", "Mr. Mime", 122, aliases=("Mr Mime", "Mister Mime"))
    )

    assert normalize_catalog_name("  Mr. Mime  ") == "mr mime"
    mr_mime = catalog.resolve_species_name("mr mime")
    mister_mime = catalog.resolve_species_name("MISTER MIME")
    assert mr_mime is not None
    assert mister_mime is not None
    assert mr_mime.species_key == "mr-mime"
    assert mister_mime.species_key == "mr-mime"
    assert catalog.resolve_species_name("Missingno") is None


def test_species_resolution_rejects_ambiguous_alias() -> None:
    catalog = species_catalog(
        species("alpha", "Alpha", 1, aliases=("Shared",)),
        species("beta", "Beta", 2, aliases=("Shared",)),
    )

    assert catalog.resolve_species_name("Shared") is None


def test_species_resolution_accepts_unique_one_edit_fuzzy_match() -> None:
    catalog = species_catalog(
        species("ivysaur", "Ivysaur", 2),
        species("venusaur", "Venusaur", 3),
    )

    ivysaur = catalog.resolve_species_name_fuzzy("lvysaur")

    assert ivysaur is not None
    assert ivysaur.species_key == "ivysaur"
    assert catalog.resolve_species_name_fuzzy("Ivy") is None


def test_species_resolution_rejects_ambiguous_fuzzy_match() -> None:
    catalog = species_catalog(
        species("alpha", "Abcde", 1),
        species("beta", "Xbcde", 2),
    )

    assert catalog.resolve_species_name_fuzzy("bbcde") is None


def test_move_resolution_normalizes_punctuation_and_keys() -> None:
    catalog = MetadataCatalog(
        moves=(
            MoveEntry(
                move_key="power-up-punch",
                move_name="Power-Up Punch",
                upstream_ids=("COMBAT_MOVE_POWER_UP_PUNCH",),
            ),
        )
    )

    assert normalize_move_name("  Power-Up Punch  ") == "power up punch"
    assert normalize_move_name("Power/Up Punch") == "power up punch"
    power_up_punch = catalog.resolve_move_name("Power Up Punch")
    upstream_name = catalog.resolve_move_name("POWER_UP_PUNCH")

    assert power_up_punch is not None
    assert upstream_name is not None
    assert power_up_punch.move_key == "power-up-punch"
    assert upstream_name.move_key == "power-up-punch"


def test_move_resolution_rejects_ambiguous_names() -> None:
    catalog = MetadataCatalog(
        moves=(
            MoveEntry(move_key="alpha", move_name="Shared Move"),
            MoveEntry(move_key="beta", move_name="Shared-Move"),
        )
    )

    assert catalog.resolve_move_name("Shared Move") is None
