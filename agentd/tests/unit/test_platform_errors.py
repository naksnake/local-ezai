"""The shared error vocabulary (PR-9): one classification for the CLI and
the control plane — same code, message and fix everywhere."""

from __future__ import annotations

from agentd.catalog import CatalogError
from agentd.governance import GovernanceError
from agentd.lifecycle import LifecycleError
from agentd.platform_errors import PlatformError, classify, platform_exceptions
from agentd.registry_v2 import RegistryError, RegistryResolutionError
from agentd.render import RenderError


def test_classify_maps_every_platform_exception_to_the_shared_object():
    cases = [
        (LifecycleError("beta serves role coder — activate a replacement first"),
         "lifecycle_refused", 409),
        (GovernanceError("a rejection needs a reason"), "governance_rule", 409),
        (RegistryResolutionError("2 role(s) unresolvable"), "resolution_incomplete", 409),
        (RegistryError("group 'x' is not a list"), "registry_invalid", 422),
        (RenderError("rendered files drifted"), "render_refused", 422),
        (CatalogError("no catalog variant fits"), "catalog_refused", 422),
        (ValueError("plain"), "invalid_request", 422),
        (RuntimeError("boom"), "internal_error", 500),
    ]
    for exc, code, status in cases:
        info = classify(exc)
        assert (info.code, info.status, info.exit_code) == (code, status, 1), exc
        assert info.message == str(exc) and info.as_dict() == {"code": code, "message": str(exc),
                                                                "fix": ""}
    platform = classify(PlatformError("no Local-EZAI platform found"))
    assert (platform.code, platform.status, platform.exit_code) == ("platform_unavailable", 503, 2)


def test_not_found_phrasing_wins_whatever_module_raised_it():
    for message in ("unknown model 'nope'", "no change request 'cr-9' in /q",
                    "role 'x' is not defined in the registry (generation 1)",
                    "no registered project named or located at 'z'"):
        for exc in (LifecycleError(message), GovernanceError(message),
                    RegistryResolutionError(message)):
            info = classify(exc)
            assert (info.code, info.status) == ("not_found", 404), message


def test_platform_exceptions_is_the_catch_list_of_every_surface():
    classes = platform_exceptions()
    assert PlatformError in classes and LifecycleError in classes and GovernanceError in classes
    assert all(issubclass(cls, BaseException) for cls in classes)
