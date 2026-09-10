# swe-server — the Local-EZAI SWE Tool Server (MCP, behind mcpo)

A thin MCP adapter over the `ezaid` control plane (V1 P3, PR-13, ADR-029;
[docs/OPENWEBUI_INTEGRATION.md](../../docs/OPENWEBUI_INTEGRATION.md) §2).
OpenWebUI reaches it as an OpenAPI tool server through mcpo at
`http://<host>:8200/swe`.

**Start + inspect only.** Tools: `swe_projects`, `swe_plan`, `swe_run`,
`swe_sprint`, `swe_fix`, `swe_evolve`, `swe_status`, `swe_report`,
`swe_journal`, `model_list`, `model_explain`, `governance_queue`. No
approve / reject / activate / rollback / upgrade / retire / uninstall /
install / merge / push / cancel exists here — governance stays with the
Admin Center and the CLI.

Runs inside the mcpo image (vendored like `../qdrant-rag`), configured in
`config/mcpo-config.json` with `EZAI_CONTROL_URL` (default `http://ezaid:8010`,
or `http://host.docker.internal:8010` for a host daemon), `EZAI_CONTROL_TOKEN`
and `EZAI_ADMIN_URL`. It imports nothing from agentd — only `mcp` and
`httpx`, both already pinned in the image.

Tests: `agentd/tests/integration/test_swe_server.py` (the tools against the
in-process control plane, real scripted plan and run pipelines, the
catalog's negative check).
