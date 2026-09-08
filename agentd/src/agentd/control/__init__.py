"""``ezaid`` — the Local-EZAI Platform Control Plane (P2, ADR-028;
docs/CLI_AND_WEBUI_STRATEGY.md §1/§6, docs/TARGET_PRODUCT_V1.md §4).

One authenticated OpenAPI service wrapping the Python functions the
platform already has — Registry v2, lifecycle, governance queue, health —
so the CLI (connected mode, PR-11), the Admin Center (P4) and the SWE tool
server (P3) are thin clients of one contract and one audit log. The CLI's
direct mode keeps working with this service down.

This package root imports no web framework: the constants below are safe
for the CLI to use (``local-ezai status`` probes the control plane) when the
``agentd[control]`` extra is not installed. The app lives in ``app.py``.
"""

from __future__ import annotations

#: Contract version of the OpenAPI document (``info.version``). PR-12 freezes
#: it as ``1.0.0``; until then every PR that changes the surface bumps the
#: pre-release tag and regenerates ``docs/api/ezaid-openapi.json``.
CONTRACT_VERSION = "1.0.0-draft.8"
API_PREFIX = "/v1"
SERVICE_NAME = "ezaid"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8010
#: Product-facing environment (``.env`` / compose); ``AGENTD_CONTROL__*`` and
#: the ``control:`` config section take precedence.
TOKEN_ENV = "EZAI_CONTROL_TOKEN"
PORT_ENV = "EZAI_CONTROL_PORT"

#: Forwarded identity (WEBUI_ADMIN_CENTER §4, OPENWEBUI_INTEGRATION §5.5):
#: the calling surface authenticates with the service token and forwards
#: the human it acts for; the audit actor becomes ``<user> via <client>``.
USER_HEADER = "X-EZAI-User"
CLIENT_HEADER = "X-EZAI-Client"

SPEC_ARTIFACT = "docs/api/ezaid-openapi.json"
