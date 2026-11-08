from .model import (
    ExecutionState,
    FunctionalResult,
    SimulationScenario,
    TransactionError,
    TransactionRequest,
    TransactionResult,
    TransactionType,
)
from .recovery import RecoveryClass, RecoveryPlan, recovery_plan

RecoveryAction = RecoveryClass


def __getattr__(name):
    if name == "TransactionSimulator":
        from .simulator import TransactionSimulator

        return TransactionSimulator
    raise AttributeError(name)


__all__ = [
    "ExecutionState",
    "FunctionalResult",
    "RecoveryAction",
    "RecoveryClass",
    "RecoveryPlan",
    "SimulationScenario",
    "TransactionError",
    "TransactionRequest",
    "TransactionResult",
    "TransactionSimulator",
    "TransactionType",
    "recovery_plan",
]
