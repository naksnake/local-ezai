"""The CLI's connected-mode transport (PR-11, ADR-028; CLI_AND_WEBUI_STRATEGY
§2: "same commands, same outputs; only the transport differs").

``ConnectedOps`` mirrors the direct-mode operations of ``platform_cli``
method for method, but each one is an HTTP call to the control plane —
the PR-9 verb ↔ endpoint mapping — returning the response body, which IS
the operation's JSON (the daemon serves the same functions). Every call
carries the service token, the forwarded identity (`X-EZAI-User` = the
CLI's actor, `X-EZAI-Client: cli`) and, for mutations, a fresh
`Idempotency-Key`. Errors come back as the shared envelope and are raised
as ``ControlPlaneError`` — the CLI prints exactly what the daemon said.

Only ``httpx`` (a core dependency) is used here: connected mode needs no
web framework on the client host.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from agentd.control import (
    API_PREFIX,
    CLI_CLIENT_NAME,
    CLIENT_HEADER,
    PROBE_TIMEOUT_S,
    SERVICE_NAME,
    TOKEN_ENV,
    URL_ENV,
    USER_HEADER,
)
from agentd.control.idempotency import HEADER as IDEMPOTENCY_HEADER

#: Long operations (install downloads weights, benchmark side-loads an
#: engine) must not be cut by a read timeout; connecting stays bounded.
OPS_CONNECT_TIMEOUT_S = 5.0


class ControlPlaneError(Exception):
    """The daemon refused or failed the call — the shared error object."""

    def __init__(self, status: int, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.fix = status, code, message, fix

    @property
    def exit_code(self) -> int:
        return 2 if self.status == 503 or self.code == "platform_unavailable" else 1

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "fix": self.fix}

    @classmethod
    def from_response(cls, response: httpx.Response) -> ControlPlaneError:
        try:
            error = response.json().get("error", {})
        except ValueError:
            error = {}
        return cls(response.status_code, error.get("code") or f"http_{response.status_code}",
                   error.get("message") or response.text[:300] or response.reason_phrase,
                   error.get("fix", ""))


class ControlUnreachable(ControlPlaneError):
    """No daemon answered at the configured URL."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(503, "control_unreachable",
                         f"control plane unreachable at {url} ({reason})",
                         f"start it (make control-up), point {URL_ENV} at it, or use "
                         "--transport direct")


def client_factory(url: str, timeout: float | None) -> httpx.Client:
    """Seam: the HTTP client for ``url`` (tests inject an in-process client)."""
    read = timeout
    connect = min(timeout, OPS_CONNECT_TIMEOUT_S) if timeout is not None else OPS_CONNECT_TIMEOUT_S
    return httpx.Client(base_url=url, timeout=httpx.Timeout(read, connect=connect))


def probe_control_plane(url: str) -> bool:
    """Is a control plane alive at ``url``? One liveness request, bounded."""
    try:
        with client_factory(url, PROBE_TIMEOUT_S) as client:
            response = client.get("/health")
        return response.status_code == 200 and response.json().get("service") == SERVICE_NAME
    except Exception:  # noqa: BLE001 — unreachable is an answer, not an error
        return False


def connect(url: str, *, actor: str, token: str | None) -> ConnectedOps:
    return ConnectedOps(client_factory(url, None), url=url, actor=actor, token=token or "")


class ConnectedOps:
    """The direct-mode operations, spoken to the daemon."""

    def __init__(self, client: httpx.Client, *, url: str, actor: str, token: str) -> None:
        self._client, self.url, self.actor, self._token = client, url, actor, token

    @property
    def identity(self) -> str:
        return f"{self.actor} via {CLI_CLIENT_NAME}"

    def _headers(self, mutating: bool) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token}", USER_HEADER: self.actor,
                   CLIENT_HEADER: CLI_CLIENT_NAME}
        if mutating:
            headers[IDEMPOTENCY_HEADER] = str(uuid.uuid4())
        return headers

    def _call(self, method: str, path: str, *, json: dict[str, Any] | None = None,
              params: dict[str, Any] | None = None) -> dict[str, Any]:
        mutating = method not in ("GET", "HEAD")
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            response = self._client.request(method, f"{API_PREFIX}{path}", json=json,
                                            params=clean_params or None,
                                            headers=self._headers(mutating))
        except httpx.HTTPError as exc:
            raise ControlUnreachable(self.url, f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise ControlPlaneError.from_response(response)
        return response.json()

    # ── status ───────────────────────────────────────────────────────────

    def status_snapshot(self) -> dict[str, Any]:
        report = self._call("GET", "/health")
        services = {service["id"]: service["ok"] for service in report.get("services", [])}
        data = dict(report["platform"])
        data["health"] = {"engine": services.get("engine", False),
                          "router": services.get("router", False), "control": True}
        data["transport"] = f"connected {self.url}"
        return data

    # ── models ───────────────────────────────────────────────────────────

    def install_model(self, ref: str, *, name: str | None = None, runtime: str | None = None,
                      group: str | None = None, refetch: bool = False) -> dict[str, Any]:
        return self._call("POST", "/models", json={"ref": ref, "name": name, "runtime": runtime,
                                                   "group": group, "refetch": refetch})

    def benchmark_model(self, name: str, *, base_url: str | None = None) -> dict[str, Any]:
        return self._call("POST", f"/models/{name}/benchmark", json={"base_url": base_url})

    def activate_model(self, name: str, *, group: str | None = None, role: str | None = None,
                       position: int | None = None, reload: bool = False) -> dict[str, Any]:
        return self._call("POST", f"/models/{name}/activate",
                          json={"group": group, "role": role, "position": position,
                                "reload": reload})

    def upgrade_model(self, old: str, new: str, *, reload: bool = False) -> dict[str, Any]:
        return self._call("POST", "/models/upgrade", json={"old": old, "new": new,
                                                           "reload": reload})

    def rollback_generation(self, *, to_generation: int | None = None, reason: str = "",
                            reload: bool = False, notify=None) -> dict[str, Any]:
        data = self._call("POST", "/generations/rollback",
                          json={"to_generation": to_generation, "reason": reason,
                                "reload": reload})
        if notify is not None and data.get("ok"):
            notify(f"ROLLBACK by {self.identity}: {data['message']}")
        return data

    def retire_model(self, name: str) -> dict[str, Any]:
        return self._call("POST", f"/models/{name}/retire")

    def uninstall_model(self, name: str, *, force: bool = False) -> dict[str, Any]:
        return self._call("DELETE", f"/models/{name}", params={"force": "true" if force else None})

    def explain_role(self, role: str) -> dict[str, Any]:
        return self._call("GET", f"/roles/{role}")

    def generation_history(self, *, limit: int = 10) -> dict[str, Any]:
        return self._call("GET", "/generations", params={"limit": max(1, limit)})

    def catalog_listing(self) -> dict[str, Any]:
        return self._call("GET", "/catalog")

    def catalog_recommendations(self, group: str, *, runtime: str | None = None) -> dict[str, Any]:
        return self._call("GET", "/catalog/recommendations", params={"group": group,
                                                                    "runtime": runtime})

    # ── governance ───────────────────────────────────────────────────────

    def list_requests(self, *, status: str | None = None) -> dict[str, Any]:
        return self._call("GET", "/governance", params={"status": status})

    def show_request(self, request_id: str) -> dict[str, Any]:
        return self._call("GET", f"/governance/{request_id}")

    def approve_request(self, request_id: str, *, reason: str = "",
                        reload: bool = False) -> dict[str, Any]:
        return self._call("POST", f"/governance/{request_id}/approve",
                          json={"reason": reason, "reload": reload})

    def reject_request(self, request_id: str, *, reason: str = "") -> dict[str, Any]:
        return self._call("POST", f"/governance/{request_id}/reject", json={"reason": reason})

    # ── projects ─────────────────────────────────────────────────────────

    def list_projects(self) -> dict[str, Any]:
        return self._call("GET", "/projects")

    def add_project(self, path_arg: str, *, name: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/projects", json={"path": path_arg, "name": name})

    def remove_project(self, target: str) -> dict[str, Any]:
        return self._call("DELETE", "/projects", params={"target": target})


__all__ = ["ConnectedOps", "ControlPlaneError", "ControlUnreachable", "TOKEN_ENV",
           "client_factory", "connect", "probe_control_plane"]
