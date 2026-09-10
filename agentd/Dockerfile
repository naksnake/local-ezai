# ezaid — Local-EZAI Platform Control Plane (V1 P2, PR-8 / ADR-028).
#
# One authenticated OpenAPI service over the platform's declarative state
# (config/models, config/providers, config/governance, config/rendered).
# Started as a compose overlay:  make control-up   (docker-compose.control.yml)
#
# Pinned like the other local builds (architecture rule 8); the versions are
# the ones the test suite ran against.
FROM python:3.11-slim

WORKDIR /app

COPY agentd/pyproject.toml agentd/README.md ./agentd/
COPY agentd/src ./agentd/src

RUN pip install --no-cache-dir \
    './agentd[control]' \
    fastapi==0.141.1 \
    uvicorn==0.52.4

# The platform is bind-mounted here by the overlay (config/ of the checkout).
ENV AGENTD_PLATFORM__CONFIG_DIR=/platform/config

EXPOSE 8010

# Liveness only (unauthenticated); /v1/health is the aggregated report.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8010/health', timeout=4).status == 200 else 1)"

CMD ["ezaid"]
