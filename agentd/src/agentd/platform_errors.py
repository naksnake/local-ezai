"""One error vocabulary for every management surface (PR-9; ADR-028;
docs/CLI_AND_WEBUI_STRATEGY.md §6: "errors are the same objects everywhere").

The platform modules raise their own exception types with messages that
name the fix (house style). ``classify`` maps any of them to the shared
error object — ``code`` (stable, machine-readable), ``message``, ``fix`` —
plus the HTTP status the control plane answers with and the exit code the
CLI returns. The CLI prints this object in ``--json`` mode; the API returns
it as ``{"error": {...}}``; the Admin Center shows the same text.
"""

from __future__ import annotations

from dataclasses import dataclass


class PlatformError(Exception):
    """No platform to act on (usage-level failure, exit 2 / HTTP 503)."""


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    status: int
    message: str
    fix: str = ""
    exit_code: int = 1

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "fix": self.fix}


#: Messages that mean "the thing you named does not exist" → 404, whatever
#: module raised them (they are the house phrasing of those modules).
NOT_FOUND_MARKERS = ("unknown model", "no change request", "is not defined in the registry",
                     "no registered project", "no catalog entry", "unknown catalog id")


def _catalogue() -> tuple[tuple[type[BaseException], str, int], ...]:
    # Imported lazily: this module must stay importable from anywhere.
    from agentd.bootstrap import BootstrapError
    from agentd.catalog import CatalogError
    from agentd.governance import GovernanceError
    from agentd.lifecycle import LifecycleError
    from agentd.registry_v2 import RegistryError, RegistryResolutionError
    from agentd.render import RenderError
    from agentd.runtime_descriptor import DescriptorError

    return (
        (GovernanceError, "governance_rule", 409),
        (LifecycleError, "lifecycle_refused", 409),
        (RegistryResolutionError, "resolution_incomplete", 409),
        (RegistryError, "registry_invalid", 422),
        (RenderError, "render_refused", 422),
        (CatalogError, "catalog_refused", 422),
        (BootstrapError, "bootstrap_refused", 422),
        (DescriptorError, "descriptor_invalid", 500),
    )


def platform_exceptions() -> tuple[type[BaseException], ...]:
    """The exception types a management surface turns into the shared error
    object (everything else is a bug and propagates)."""
    return (PlatformError, *(cls for cls, _, _ in _catalogue()))


def classify(exc: BaseException) -> ErrorInfo:
    message = str(exc)
    fix = str(getattr(exc, "fix", "") or "")
    if isinstance(exc, PlatformError):
        return ErrorInfo("platform_unavailable", 503, message, fix, exit_code=2)
    lowered = message.lower()
    if any(marker in lowered for marker in NOT_FOUND_MARKERS):
        return ErrorInfo("not_found", 404, message, fix)
    for cls, code, status in _catalogue():
        if isinstance(exc, cls):
            return ErrorInfo(code, status, message, fix)
    if isinstance(exc, ValueError):
        return ErrorInfo("invalid_request", 422, message, fix)
    return ErrorInfo("internal_error", 500, message, fix)
