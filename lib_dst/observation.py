"""Canonical, ID-independent observations of selected workspace state."""

from __future__ import annotations

import math
from typing import Any

from lib_dst.schemas import CanonicalObservation, ObservationSpec
from lib_openm.rule_eval.utils import CellError
from lib_openm.technical_ids import AT_ID_TO_CHANNEL


def _canonical_value(value: Any) -> Any:
    if isinstance(value, CellError):
        return {"error": value.code}
    if isinstance(value, float):
        if math.isnan(value):
            return {"number": "NaN"}
        if math.isinf(value):
            return {"number": "Infinity" if value > 0 else "-Infinity"}
        if value == 0:
            return 0.0
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    return value


def observe_workspace(workspace: Any, spec: ObservationSpec) -> CanonicalObservation:
    dimensions_by_name = {dim.name: dim for dim in workspace.dimensions.values()}
    cubes_by_name = {cube.name: cube for cube in workspace.cubes.values()}

    dimensions: list[dict[str, Any]] = []
    for name in sorted(spec.dimensions):
        dimension = dimensions_by_name[name]
        dimensions.append(
            {
                "name": dimension.name,
                "type": dimension.dim_type,
                "items": [item.name for item in dimension.items],
            }
        )

    cubes: list[dict[str, Any]] = []
    for name in sorted(spec.cubes):
        cube = cubes_by_name[name]
        dimension_names = [
            "@" if dim_id == "@" else workspace.dimensions[dim_id].name
            for dim_id in cube.dimension_ids
        ]
        values = []
        for address, value in cube.data.items():
            logical_address = []
            for dim_id, item_id in zip(cube.dimension_ids, address):
                if dim_id == "@":
                    channel = AT_ID_TO_CHANNEL.get(item_id, item_id)
                    logical_address.append(f"@.{channel}")
                else:
                    dimension = workspace.dimensions[dim_id]
                    item = next(item for item in dimension.items if item.id == item_id)
                    logical_address.append(item.name)
            values.append(
                {
                    "address": logical_address,
                    "value": _canonical_value(value),
                    "user_override": address in cube.user_override_addrs,
                }
            )
        values.sort(key=lambda entry: tuple(entry["address"]))
        cubes.append(
            {
                "name": cube.name,
                "dimensions": dimension_names,
                "values": values,
            }
        )

    return CanonicalObservation(state={"dimensions": dimensions, "cubes": cubes})
