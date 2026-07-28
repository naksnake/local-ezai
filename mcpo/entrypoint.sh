#!/bin/sh
set -e

# mcpo does no variable substitution in its config file, so expand
# ${VAR} / ${VAR:-default} placeholders here before handing it over.
python3 - <<'PY'
import os, re

src = open("/app/mcpo-config.json").read()
rendered = re.sub(
    r"\$\{(\w+)(?::-([^}]*))?\}",
    lambda m: os.environ.get(m.group(1)) or m.group(2) or "",
    src,
)
open("/tmp/mcpo-config.rendered.json", "w").write(rendered)
PY

exec mcpo --port 8200 --api-key "${MCP_API_KEY}" --config /tmp/mcpo-config.rendered.json
