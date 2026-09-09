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

#: Contract version of the OpenAPI document (``info.version``). **Frozen at
#: 1.0.0 by the P2 phase close (PR-12, ADR-028 Accepted):** a change to the
#: surface (operations, parameters, response codes, security, schema names)
#: bumps this version — additive changes the minor, breaking ones the major —
#: and regenerates ``docs/api/ezaid-openapi.json``; the frozen inventory test
#: fails otherwise. **1.1.0 (PR-19, additive):** the project memory
#: operations (``project_memory``, ``project_memory_add``) for the Admin
#: Center's Memory page. **1.2.0 (PR-23, additive):** ``first_run_report``
#: (``GET /v1/first-run``) for the Platform-ready card — every earlier
#: operation unchanged.
CONTRACT_VERSION = "1.2.0"
API_PREFIX = "/v1"
SERVICE_NAME = "ezaid"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8010
#: Product-facing environment (``.env`` / compose); ``AGENTD_CONTROL__*`` and
#: the ``control:`` config section take precedence.
TOKEN_ENV = "EZAI_CONTROL_TOKEN"
PORT_ENV = "EZAI_CONTROL_PORT"
#: CLI connected mode (PR-11): where the CLI finds the daemon (default
#: ``http://localhost:<port>``) and how it chooses the transport
#: (``auto`` | ``connected`` | ``direct``; ``--transport`` wins).
URL_ENV = "EZAI_CONTROL_URL"
TRANSPORT_ENV = "EZAI_TRANSPORT"
TRANSPORTS = ("auto", "connected", "direct")
CLI_CLIENT_NAME = "cli"
#: Liveness probe budget of the auto-detect (one request per invocation).
PROBE_TIMEOUT_S = 1.0

#: Forwarded identity (WEBUI_ADMIN_CENTER §4, OPENWEBUI_INTEGRATION §5.5):
#: the calling surface authenticates with the service token and forwards
#: the human it acts for; the audit actor becomes ``<user> via <client>``.
USER_HEADER = "X-EZAI-User"
CLIENT_HEADER = "X-EZAI-Client"

SPEC_ARTIFACT = "docs/api/ezaid-openapi.json"
