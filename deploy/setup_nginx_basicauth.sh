#!/usr/bin/env bash
# Expose the dashboard at https://<DOMAIN> behind HTTPS + a shared basic-auth
# password (simplest "a few people just hit a browser" setup). Run on the box:
#   DOMAIN=btc.example.com EMAIL=you@example.com bash deploy/setup_nginx_basicauth.sh
#
# Prereqs (you, in the Lightsail console + your DNS):
#   - open inbound ports 80 and 443 in the Lightsail firewall
#   - add a DNS A-record: <DOMAIN> -> the instance's public IP
#     (attach a STATIC IP first if you want the link to survive reboots)
set -e
DOMAIN="${DOMAIN:?set DOMAIN=btc.example.com}"
EMAIL="${EMAIL:?set EMAIL=you@example.com}"

sudo apt-get update -y >/dev/null 2>&1 || true
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nginx certbot apache2-utils >/dev/null
sudo mkdir -p /var/www/certbot

# Bootstrap HTTP (serves the ACME challenge; proxies the dashboard meanwhile)
sudo tee /etc/nginx/sites-available/btc >/dev/null <<NGINX
server {
    listen 80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { proxy_pass http://127.0.0.1:3000; proxy_set_header Host \$host; }
}
NGINX
sudo ln -sf /etc/nginx/sites-available/btc /etc/nginx/sites-enabled/btc
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx && sudo systemctl enable nginx >/dev/null 2>&1 || true

# Certificate (auto-renews via certbot.timer; reloads nginx on renew)
sudo certbot certonly --webroot -w /var/www/certbot -d "$DOMAIN" \
  --non-interactive --agree-tos -m "$EMAIL" --deploy-hook "systemctl reload nginx"

# Shared credentials (override SUSER/SPASS to choose your own)
SUSER="${SUSER:-team}"
SPASS="${SPASS:-$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 14)}"
sudo htpasswd -bc /etc/nginx/.btc_htpasswd "$SUSER" "$SPASS" >/dev/null 2>&1

# Owner credentials (GAP A / directive 6): PROMOTED study output — /lab, the
# Today Act rows, the paper book — is gated on the authenticated USER, which
# nginx forwards as X-Auth-User. `team` sees the shared context surfaces only.
OUSER="${OUSER:-owner}"
OPASS="${OPASS:-$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 14)}"
sudo htpasswd -b /etc/nginx/.btc_htpasswd "$OUSER" "$OPASS" >/dev/null 2>&1

# Final HTTPS + basic-auth reverse proxy (API stays localhost-only)
sudo tee /etc/nginx/sites-available/btc >/dev/null <<NGINX
server {
    listen 80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://\$host\$request_uri; }
}
server {
    listen 443 ssl;
    server_name $DOMAIN;
    ssl_certificate /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;

    # Email unsubscribe link target. MUST bypass basic auth (email clients can't
    # authenticate) and go straight to the FastAPI app on :8000 — the dashboard
    # (:3000) has no /api/unsubscribe route, so without this the List-Unsubscribe
    # link prompts for a password and then 404s. The token in the URL is the
    # (unguessable) capability. Exact match so it can't shadow dashboard routes.
    location = /api/unsubscribe {
        auth_basic off;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location / {
        auth_basic "BTC Dashboard";
        auth_basic_user_file /etc/nginx/.btc_htpasswd;
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        # Directive 6: the app gates owner-only surfaces on this header — it
        # must be SET here (never pass through a client-supplied value).
        proxy_set_header X-Auth-User \$remote_user;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
NGINX
sudo nginx -t && sudo systemctl reload nginx
echo "Dashboard live at https://$DOMAIN  (team=$SUSER pass=$SPASS | owner=$OUSER pass=$OPASS)"
echo "Rotate a password later:  sudo htpasswd /etc/nginx/.btc_htpasswd <user>"
