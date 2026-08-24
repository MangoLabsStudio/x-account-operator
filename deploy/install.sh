#!/usr/bin/env bash
set -euo pipefail

source_dir=${1:-/tmp/x-account-operator-staging}
useradd --system --home /var/lib/x-account-operator --shell /usr/sbin/nologin xops 2>/dev/null || true
mkdir -p /opt/x-account-operator /var/lib/x-account-operator
cp -a "$source_dir"/. /opt/x-account-operator/
chown -R root:root /opt/x-account-operator
chown -R xops:xops /var/lib/x-account-operator
chmod 0700 /var/lib/x-account-operator
find /var/lib/x-account-operator -type f -exec chmod 0600 {} +

python3 -m venv /opt/x-account-operator/venv
/opt/x-account-operator/venv/bin/pip install -q -r /opt/x-account-operator/requirements.txt

if [[ ! -f /etc/x-account-operator.env ]]; then
    install -m 0600 /dev/null /etc/x-account-operator.env
    printf '%s\n' \
        'XOPS_DATA_DIR=/var/lib/x-account-operator' \
        'XOPS_BASE_URL=https://siriuszzz-api.uk/xops' \
        'XOPS_TIMEZONE=Asia/Shanghai' \
        'XOPS_DAILY_CONTEXT_ENABLED=false' \
        'XOPS_DAILY_CONTEXT_RUN_TIME=08:15' \
        > /etc/x-account-operator.env
fi

if grep -q '^XOPS_BASE_URL=' /etc/x-account-operator.env; then
    sed -i 's|^XOPS_BASE_URL=.*|XOPS_BASE_URL=https://siriuszzz-api.uk/xops|' /etc/x-account-operator.env
else
    printf '%s\n' 'XOPS_BASE_URL=https://siriuszzz-api.uk/xops' >> /etc/x-account-operator.env
fi

if grep -q '^TWITTER241_RAPIDAPI_KEY=.' /etc/x-account-operator.env \
    && grep -q '^XOPS_LLM_API_KEY=.' /etc/x-account-operator.env; then
    daily_context_enabled=true
else
    daily_context_enabled=false
    printf '%s\n' 'Daily context scheduler remains disabled until both runtime API keys are configured.' >&2
fi

if grep -q '^XOPS_DAILY_CONTEXT_ENABLED=' /etc/x-account-operator.env; then
    sed -i "s|^XOPS_DAILY_CONTEXT_ENABLED=.*|XOPS_DAILY_CONTEXT_ENABLED=$daily_context_enabled|" /etc/x-account-operator.env
else
    printf 'XOPS_DAILY_CONTEXT_ENABLED=%s\n' "$daily_context_enabled" >> /etc/x-account-operator.env
fi

if grep -q '^XOPS_DAILY_CONTEXT_RUN_TIME=' /etc/x-account-operator.env; then
    sed -i 's|^XOPS_DAILY_CONTEXT_RUN_TIME=.*|XOPS_DAILY_CONTEXT_RUN_TIME=08:15|' /etc/x-account-operator.env
else
    printf '%s\n' 'XOPS_DAILY_CONTEXT_RUN_TIME=08:15' >> /etc/x-account-operator.env
fi

install -m 0644 /opt/x-account-operator/deploy/x-account-operator.service /etc/systemd/system/x-account-operator.service
systemctl daemon-reload
systemctl enable --now x-account-operator.service
