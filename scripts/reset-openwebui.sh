#!/usr/bin/env bash
# scripts/reset-openwebui.sh
# ─────────────────────────────────────────────────────────────────────────────
# Reset OpenWebUI accounts.
#
#   wipe                        Factory reset: delete the openwebui-data
#                               volume (ALL users, passwords, chats, settings).
#                               The first account registered afterwards
#                               becomes the new admin.
#
#   password <email> <newpass>  Reset one user's password in place.
#   password all <newpass>      Reset EVERY user's password to <newpass>.
#
# Usage:  make reset-webui
#         make reset-password EMAIL=you@example.com PASSWORD=newpass
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
MODE="${1:-}"

case "$MODE" in

wipe)
    echo -e "${YELLOW}⚠  This deletes ALL OpenWebUI users, passwords, chats and settings.${NC}"
    read -r -p "Continue? (y/N): " confirm
    [[ "$confirm" == "y" || "$confirm" == "Y" ]] || { echo "Aborted."; exit 1; }

    echo "Stopping and removing the openwebui container..."
    docker compose rm -sf openwebui

    # Compose labels its volumes, so this finds the right one regardless of
    # the project name (folder name or COMPOSE_PROJECT_NAME).
    vol=$(docker volume ls -q --filter label=com.docker.compose.volume=openwebui-data | head -1)
    if [[ -z "$vol" ]]; then
        vol=$(docker volume ls -q | grep -E '_openwebui-data$' | head -1 || true)
    fi
    if [[ -n "$vol" ]]; then
        echo "Deleting volume $vol ..."
        docker volume rm "$vol"
    else
        echo -e "${YELLOW}No openwebui-data volume found — nothing to delete.${NC}"
    fi

    echo "Recreating openwebui..."
    # --no-deps: don't touch the other running services, whichever profile
    # (cpu/n97/gpu) they were started with
    docker compose up -d --no-deps openwebui

    port="${OPENWEBUI_PORT:-3000}"
    echo ""
    echo -e "${GREEN}✅ OpenWebUI reset to factory defaults.${NC}"
    echo "   Open http://localhost:${port} and click 'Sign up' —"
    echo "   the FIRST account created becomes the admin."
    ;;

password)
    EMAIL="${2:-}"; NEWPASS="${3:-}"
    if [[ -z "$EMAIL" || -z "$NEWPASS" ]]; then
        echo -e "${RED}Usage: make reset-password EMAIL=you@example.com PASSWORD=newpass${NC}"
        echo "       (EMAIL=all resets every account to the same password)"
        exit 1
    fi

    if ! docker ps --format '{{.Names}}' | grep -qx openwebui; then
        echo -e "${RED}The openwebui container is not running — start the stack first.${NC}"
        exit 1
    fi

    # OpenWebUI ships bcrypt and stores accounts in /app/backend/data/webui.db
    # (table 'auth'); update the hash in place with the container's own python.
    docker exec -i \
        -e RESET_EMAIL="$EMAIL" \
        -e RESET_PASSWORD="$NEWPASS" \
        openwebui python3 - <<'PY'
import os, sqlite3, sys
import bcrypt

email = os.environ["RESET_EMAIL"]
pw = os.environ["RESET_PASSWORD"]
hashed = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

db = sqlite3.connect("/app/backend/data/webui.db")
if email == "all":
    cur = db.execute("UPDATE auth SET password = ?", (hashed,))
else:
    cur = db.execute("UPDATE auth SET password = ? WHERE email = ?", (hashed, email))
db.commit()

if cur.rowcount == 0:
    print(f"No account found for '{email}'. Existing accounts:")
    for row in db.execute("SELECT email FROM auth"):
        print(f"  - {row[0]}")
    sys.exit(1)
print(f"Password updated for {cur.rowcount} account(s).")
PY
    ;;

*)
    echo "Usage: $0 wipe | password <email|all> <newpass>"
    exit 1
    ;;
esac
