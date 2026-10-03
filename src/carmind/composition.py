"""Explicit provider composition; CarMindApp has one business path in both modes."""

from carmind.app import CarMindApp
from carmind.planner_provider import FakePlannerProvider, XAIPlannerProvider
from carmind.router_provider import JevCapabilityRouter
from carmind.routing import ExecutionMode


def compose_app(store, *, live=False, provider=None, router=None, timeout_seconds=30.0,
                max_model_calls=4, manual_index=None):
    """Default to an offline fake provider; only explicit live mode constructs SDKs.

    Injected providers let offline acceptance test the identical application path.
    The runner supplies budget wrappers around real adapters before first use.
    """
    if type(live) is not bool:
        raise ValueError("live must be explicit boolean")
    if live:
        provider = provider if provider is not None else XAIPlannerProvider(timeout_seconds=timeout_seconds)
        router = router if router is not None else JevCapabilityRouter(timeout_seconds=timeout_seconds)
        mode = ExecutionMode.ROUTED
    else:
        provider = provider if provider is not None else FakePlannerProvider([])
        mode = ExecutionMode.ROUTED if router is not None else ExecutionMode.FULL
    return CarMindApp(store, provider, mode=mode, router=router,
                      max_model_calls=max_model_calls, manual_index=manual_index)
