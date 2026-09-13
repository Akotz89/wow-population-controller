"""Fail-closed error hierarchy."""


class PopulationError(RuntimeError):
    """Any invalid artifact, state, bridge result, or policy transition."""


class ArtifactError(PopulationError):
    """An immutable input or durable state artifact is invalid."""


class BridgeError(PopulationError):
    """The typed native adapter rejected or omitted required state."""


class LedgerError(PopulationError):
    """An identity-ledger transition or durable ledger artifact is invalid."""


class LedgerConflict(LedgerError):
    """A compare-and-swap transition observed state different from its intent."""


class PendingOperation(PopulationError):
    """Native creation is confirmed pending; retry the same identity on a later tick."""
