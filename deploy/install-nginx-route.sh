#!/usr/bin/env bash
set -euo pipefail

target=/etc/nginx/sites-enabled/mango-gateway

if grep -q 'Mango X Ops' "$target"; then
    exit 0
fi

backup="${target}.bak.xops-$(date +%Y%m%dT%H%M%S)"
cp -a "$target" "$backup"

awk '
/    # === Block common attack paths ===/ {
    print "    # === Mango X Ops ==="
    print "    location = /xops {"
    print "        return 302 https://$host/xops/;"
    print "    }"
    print ""
    print "    location ^~ /xops/ {"
    print "        proxy_pass http://127.0.0.1:8788/;"
    print "        proxy_set_header Host $host;"
    print "        proxy_set_header X-Real-IP $remote_addr;"
    print "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;"
    print "        proxy_set_header X-Forwarded-Proto $scheme;"
    print "    }"
    print ""
}
{ print }
' "$target" > "${target}.new"

chown --reference="$target" "${target}.new"
chmod --reference="$target" "${target}.new"
mv "${target}.new" "$target"

if ! nginx -t; then
    cp -a "$backup" "$target"
    nginx -t
    exit 1
fi

systemctl reload nginx
