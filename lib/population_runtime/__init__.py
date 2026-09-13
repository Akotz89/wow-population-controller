"""Policy-only runtime for deterministic Playerbots population scheduling."""

from .adapter import AzerothCoreReadOnlyAdapter, PopulationPlanningAdapter
from .controller import PopulationController
from .errors import LedgerConflict, LedgerError, PopulationError
from .ledger import (
    DurableLedger,
    LedgerEvent,
    LedgerIdentity,
    LedgerRow,
    block_conflict,
    identity_from_manifest,
    load_ledger,
    new_ledger,
    reserve_identity,
    save_ledger,
    set_session_requested,
    transition_identity,
)
from .phase_veto import evaluate_phase_veto
from .planning import (
    AdmissionIntent,
    PopulationPlan,
    build_readonly_admission_plan,
    build_readonly_plan_from_adapter,
    population_plan_document,
)

__all__ = [
    "AdmissionIntent",
    "AzerothCoreReadOnlyAdapter",
    "DurableLedger",
    "LedgerConflict",
    "LedgerError",
    "LedgerEvent",
    "LedgerIdentity",
    "LedgerRow",
    "PopulationController",
    "PopulationError",
    "PopulationPlan",
    "PopulationPlanningAdapter",
    "block_conflict",
    "build_readonly_admission_plan",
    "build_readonly_plan_from_adapter",
    "evaluate_phase_veto",
    "identity_from_manifest",
    "load_ledger",
    "new_ledger",
    "population_plan_document",
    "reserve_identity",
    "save_ledger",
    "set_session_requested",
    "transition_identity",
]
