"""SolverCanonicalApplyPort implementation: Engine-backed atomic apply.

Applies solver result values to the workspace via a single Engine
batch operation with revision revalidation.
"""

from __future__ import annotations

import logging
from typing import Any

from lib_openm.api import Engine

from lib_runtime.solver.solver_ports import SolverApplyContext
from lib_runtime.solver.solver_errors import (
    SolverException,
    SOLVER_RESULT_STALE,
)
from lib_runtime.solver_adapters.engine_solver_evaluation import (
    _compute_workspace_revision,
)

logger = logging.getLogger(__name__)


class EngineSolverApplyAdapter:
    """Implements ``SolverCanonicalApplyPort`` against a live Engine.

    Applies solver result values atomically using the Engine's
    ``batch_set_cell_hardvalues_by_addr`` method, with revision
    revalidation before applying.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def commit_solver_result_values(
        self,
        context: SolverApplyContext,
        variable_values: tuple,
        base_revision: str,
        postcheck_spec: dict,
    ) -> dict:
        """Atomic canonical apply.

        1. Revalidate revision.
        2. Apply values via batch_set_cell_hardvalues_by_addr.
        3. Recalculate.
        4. Return committed revision.
        """
        # 1. Revalidate revision.
        current_rev = _compute_workspace_revision(self._engine)
        if current_rev != base_revision:
            raise SolverException(
                SOLVER_RESULT_STALE,
                f"Revision mismatch during apply: expected {base_revision}, "
                f"current {current_rev}",
            )

        # 2. Apply values.
        # The variable_values tuple aligns with the problem's variable list.
        # The caller must provide the variable cell addresses via
        # postcheck_spec or context.
        #
        # For Phase 1, we expect the postcheck_spec to contain
        # "variable_cells": [(cube_id, addr), ...]
        variable_cells = postcheck_spec.get("variable_cells", [])
        if len(variable_cells) != len(variable_values):
            raise SolverException(
                SOLVER_RESULT_STALE,
                f"Variable count mismatch: {len(variable_cells)} cells vs "
                f"{len(variable_values)} values",
            )

        # Apply each value using the Engine's batch API.
        # Group by cube_id for efficiency.
        from collections import defaultdict
        entries_by_cube: dict[str, list[tuple[tuple[str, ...], float]]] = defaultdict(list)
        for (cube_id, addr), value in zip(variable_cells, variable_values):
            entries_by_cube[cube_id].append((addr, float(value)))

        for cube_id, entries in entries_by_cube.items():
            self._engine.batch_set_cell_hardvalues_by_addr(cube_id, entries)

        # 3. Recalculate.
        self._engine.recalculate_all()

        # 4. Return committed revision.
        committed_rev = _compute_workspace_revision(self._engine)
        return {"committed_revision": committed_rev}
