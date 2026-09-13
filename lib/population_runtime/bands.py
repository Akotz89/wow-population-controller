"""Earned-progress rank slicing and owner-relative ceilings."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from .models import BandAssignment, BandDefinition, EarnedProgress


def allocate_exact(total: int, bands: tuple[BandDefinition, ...]) -> tuple[int, ...]:
    """Largest-remainder allocation with declared band order as the stable tie-break."""
    # Reserve requested minima in declared rank order when the cohort is tiny.
    reserved = []
    available = total
    for band in bands:
        count = min(available, band.minimum_slots)
        reserved.append(count)
        available -= count
    exact = [Decimal(available) * band.share for band in bands]
    floors = [int(value) for value in exact]
    remaining = available - sum(floors)
    order = sorted(range(len(bands)), key=lambda i: (-(exact[i] - floors[i]), i))
    for index in order[:remaining]:
        floors[index] += 1
    return tuple(floor + minimum for floor, minimum in zip(floors, reserved, strict=True))


def classify_earned_progress(
    records: Iterable[EarnedProgress],
    bands: tuple[BandDefinition, ...],
    owner_level: int,
    global_phase_ceiling: int,
    rank_population_size: int | None = None,
) -> tuple[BandAssignment, ...]:
    ordered = sorted(
        records,
        key=lambda item: (-item.level, -item.xp, -item.played_seconds, item.character_guid),
    )
    capacities = allocate_exact(max(len(ordered), rank_population_size or 0), bands)
    result: list[BandAssignment] = []
    cursor = 0
    for band, count in zip(bands, capacities, strict=True):
        ceiling = (global_phase_ceiling if band.ceiling_mode == "phase_cap" else
                   min(global_phase_ceiling, max(1, owner_level - band.owner_level_offset)))
        for rank, record in enumerate(ordered[cursor : cursor + count], start=cursor + 1):
            result.append(
                BandAssignment(
                    record.identity_key,
                    record.character_guid,
                    band.name,
                    ceiling,
                    rank,
                )
            )
        cursor += count
    return tuple(result)
