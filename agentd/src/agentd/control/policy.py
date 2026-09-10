"""Client capability policy of the control plane (PR-15, ADR-029;
OPENWEBUI_INTEGRATION §5 "capability ceiling", CLI_AND_WEBUI §4 asymmetry 1).

The chat-ops surface (the SWE tool server, ``X-EZAI-Client: swe-server``)
must never reach the governance boundary. Its tool catalog has no such
verbs — that is the primary boundary (PR-13). This module is the second
one, inside the daemon: a caller identifying as the chat-ops client may
**start runs and read**; every other mutation (governance decisions, model
lifecycle, project registration, run cancellation) is refused and audited,
whatever tools some future version of the tool server might grow.

The client name is set by the tool server's code, never by the model, so
this is defense in depth — not the reason the boundary holds.
"""

from __future__ import annotations

CHAT_OPS_CLIENT = "swe-server"

#: Mutating operations (by operationId) a restricted client may perform.
#: Clients not listed here are unrestricted (the CLI, the Admin Center).
CLIENT_MUTATIONS: dict[str, frozenset[str]] = {
    CHAT_OPS_CLIENT: frozenset({"run_start"}),
}


def is_restricted(client: str) -> bool:
    return client in CLIENT_MUTATIONS


def client_may(client: str, operation_id: str, method: str) -> bool:
    """May ``client`` perform ``operation_id`` (HTTP ``method``)? Reads are
    always allowed; mutations only when the client's list names them."""
    if method.upper() in ("GET", "HEAD", "OPTIONS"):
        return True
    allowed = CLIENT_MUTATIONS.get(client)
    return True if allowed is None else operation_id in allowed


def refusal(client: str, operation_id: str) -> tuple[str, str]:
    """(message, fix) for the shared error object."""
    return (f"client '{client}' may start and inspect runs only — '{operation_id}' is a "
            "governance or lifecycle mutation",
            "decisions and model changes are made in the Admin Center or with the CLI "
            "(local-ezai governance …, local-ezai model …), never from chat")
