from __future__ import annotations

import re
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from pogo_storage_mapper.metadata import MetadataCatalog, SpeciesEntry

_TMP_ROOT = Path(__file__).resolve().parents[1] / ".pytest-local-tmp"


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest) -> Iterator[Path]:
    _TMP_ROOT.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)[:80]
    path = _TMP_ROOT / f"{safe_name}-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def species(
    species_key: str,
    species_name: str,
    pokedex_id: int,
    *,
    aliases: tuple[str, ...] = (),
) -> SpeciesEntry:
    return SpeciesEntry(
        species_key=species_key,
        species_name=species_name,
        pokedex_id=pokedex_id,
        aliases=aliases,
    )


def species_catalog(*entries: SpeciesEntry) -> MetadataCatalog:
    return MetadataCatalog(species=entries)


def appraisal_values(
    *,
    iv_attack: int | None = 13,
    iv_defense: int | None = 14,
    iv_stamina: int | None = 15,
    iv_sum: int | None = 42,
    appraisal_star_count: int | None = 3,
    appraisal_perfect: bool = False,
) -> dict[str, int | bool | None]:
    return {
        "iv_attack": iv_attack,
        "iv_defense": iv_defense,
        "iv_stamina": iv_stamina,
        "iv_sum": iv_sum,
        "appraisal_star_count": appraisal_star_count,
        "appraisal_perfect": appraisal_perfect,
    }
