#!/bin/sh
# Render our config over the stock /etc/asterisk (env vars like ${ARI_PASSWORD} are substituted).
set -e
for f in /etc/asterisk/custom/*.conf; do
  envsubst '${ARI_USER} ${ARI_PASSWORD} ${PUBLIC_HOSTNAME}' < "$f" > "/etc/asterisk/$(basename "$f")"
done
exec asterisk -f -U asterisk -G asterisk -vvv
