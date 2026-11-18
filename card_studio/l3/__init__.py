from .model import AssertionResult, GateResult, L3CaseResult, L3Report


def __getattr__(name):
    if name == "L3Runner":
        from .runner import L3Runner

        return L3Runner
    raise AttributeError(name)


__all__ = ["AssertionResult", "GateResult", "L3CaseResult", "L3Report", "L3Runner"]
