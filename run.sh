#!/usr/bin/env bash
set -euo pipefail

OPTIONS="/data/options.json"

if [ -f "$OPTIONS" ] && [ -n "${SUPERVISOR_TOKEN:-}" ]; then
    echo "[run.sh] Add-on mode — reading options"
    eval "$(python -c "
import json, shlex
opts = json.load(open('$OPTIONS'))
mapping = {
    'admin_username': 'ADMIN_USERNAME',
    'admin_password': 'ADMIN_PASSWORD',
    'app_name': 'APP_NAME',
    'contact_message': 'CONTACT_MESSAGE',
    'brand_bg': 'BRAND_BG',
    'brand_primary': 'BRAND_PRIMARY',
    'guest_url': 'GUEST_URL',
    'timezone': 'TIMEZONE',
    'local_network_cidrs': 'LOCAL_NETWORK_CIDRS',
}
for key, env in mapping.items():
    val = opts.get(key, '')
    if val:
        print(f'export {env}={shlex.quote(str(val))}')
")"

    export HA_BASE_URL="http://supervisor/core"
    export HA_TOKEN="${SUPERVISOR_TOKEN}"
    export DB_PATH="/data/db.sqlite"
else
    echo "[run.sh] Standalone mode — using environment variables"
fi

exec python -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-5880}" --workers 1
