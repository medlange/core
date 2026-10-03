#!/bin/sh
# =====================================================================================
# medos/deploy/compose/web-entrypoint.sh -- render nginx.conf.template with the viewer's
# Gateway token, then start nginx.
#
# WHY THIS FILE EXISTS
#   Nothing else renders the template. `nginx:1.27-alpine`'s entrypoint substitutes
#   variables in `/etc/nginx/templates/*.template` and nowhere else, and this repository's
#   template is mounted at `/usr/src/default.conf.template` -- the path `ohif/app:v3.9.2`
#   used, kept when the `web` service replaced it at 0.4.0 so that the replacement touched
#   the image and this script and not every reference to the mount.
#
#   So if the `command:` line in docker-compose.yml ever goes, nginx starts on the image's
#   stock `/etc/nginx/conf.d/default.conf`, which is (read out of the image itself):
#
#       server { listen 80; location / { root /usr/share/nginx/html; index index.html; } }
#
#   `/mos-viewer/` STILL RESOLVES under that, because the tree is bind-mounted inside the
#   stock document root -- served with no `Cache-Control: no-store`, with `.mjs` as
#   application/octet-stream, and with `/api/` and `/dicomweb/` not proxied at all. That is
#   the worst shape a failure can take here: a page that loads and then does not work.
#   `set -e` and the `:?` below are what keep THIS script from failing that quietly.
#
#   THE ALLOWLIST IS ALSO WHAT KEEPS nginx ALIVE. `ohif/app`'s entrypoint rendered the
#   template with `envsubst '${PORT}'`, so `${MEDOS_GATEWAY_VIEWER_KEY}` survived verbatim,
#   nginx read `$MEDOS_...` as one of its own variables, and the container died on
#   `[emerg] unknown "medos_gateway_viewer_key" variable`. Not a hypothetical: it is what
#   happened on the first boot after the viewer was re-pointed at the Gateway, and it is
#   the defect this script was written for. The image changed; the allowlist stays, for
#   the reason below.
#
# WHY THE ALLOWLIST IS STILL EXPLICIT
#   A bare `envsubst` with no argument substitutes EVERY environment variable it finds,
#   and `nginx.conf.template` is full of nginx's own `$host`, `$request_uri`,
#   `$remote_addr`, `$proxy_add_x_forwarded_for`. Those are not environment variables today,
#   but one `HOST=` in the environment and the proxy silently starts sending the wrong
#   `Host` header. Naming the two variables keeps that impossible.
#
# WHAT IS IN THE RENDERED FILE AND WHAT IS NOT
#   The rendered `/etc/nginx/conf.d/default.conf` contains the viewer's Gateway token, and
#   it lives only inside this container's filesystem. It is NEVER served: nginx serves
#   /usr/share/nginx/html, and tests/e2e/test_demo.py greps every file this stack does
#   serve for `authorization`, `bearer` and `btoa(` with comments stripped.
#
#   It is a GATEWAY token (MOS-DATA-017), not a PACS credential. MOS-DATA-002 restricts the
#   latter to medos-gateway alone, and this container has neither a PACS credential nor a
#   network route to the PACS (MOS-DATA-006).
# =====================================================================================
set -e

: "${MEDOS_GATEWAY_VIEWER_KEY:?the viewer needs a Gateway token (MOS-DATA-017); set MEDOS_GATEWAY_VIEWER_KEY}"

# MEDOS_API_VIEWER_AUTHORIZATION is OPTIONAL and defaults to empty: see the `map` at the
# top of nginx.conf.template. It must still be NAMED in the allowlist -- envsubst leaves an
# unlisted `${...}` in the file verbatim, nginx then reads it as one of its own variables
# and the container dies on `[emerg] unknown "medos_api_viewer_authorization" variable`,
# which is exactly what happened to MEDOS_GATEWAY_VIEWER_KEY and is why this script exists.
: "${MEDOS_API_VIEWER_AUTHORIZATION:=}"
# MEDOS_GATEWAY_UPLOADER_KEY IS OPTIONAL AND CHOSEN BY METHOD (U4): reads at /dicomweb/
# keep the read-only viewer key; STOW-RS POSTs carry this principal (study.write).
# UNSET means the POST expansion is "Bearer " and the Gateway answers 401 -- the correct
# answer for a deployment that has not enabled uploads. Named in the allowlist for the
# same reason MEDOS_API_VIEWER_AUTHORIZATION is: an unlisted ${...} survives envsubst
# verbatim and kills nginx as an unknown variable.
: "${MEDOS_GATEWAY_UPLOADER_KEY:=}"
# THE API SPEAKS `Authorization: Bearer <key>`, and a RAW key placed here used to pass
# through verbatim and fail with a bare 401 that named no cause -- found by the
# user-journey E2E of 2026-10-02, where following the README exactly produced the raw
# form. Prefix when the scheme is absent so both spellings work.
if [ -n "${MEDOS_API_VIEWER_AUTHORIZATION}" ] \
  && ! printf '%s' "${MEDOS_API_VIEWER_AUTHORIZATION}" | grep -qi '^Bearer[[:space:]]'; then
  MEDOS_API_VIEWER_AUTHORIZATION="Bearer ${MEDOS_API_VIEWER_AUTHORIZATION}"
fi
export MEDOS_API_VIEWER_AUTHORIZATION

# `${PORT}` WAS THE OHIF IMAGE'S OWN VARIABLE and is dropped with it: a plain nginx
# listens where its config says. The allowlist stays an allowlist -- envsubst with no
# arguments would substitute EVERY `$name` in the file, and this template is full of
# nginx's own ($uri, $request_uri, $http_authorization), which would be blanked.
envsubst '${MEDOS_GATEWAY_VIEWER_KEY}${MEDOS_API_VIEWER_AUTHORIZATION}${MEDOS_GATEWAY_UPLOADER_KEY}' \
  < /usr/src/default.conf.template \
  > /etc/nginx/conf.d/default.conf

if [ -n "${MEDOS_API_VIEWER_AUTHORIZATION}" ]; then
  # Never the value. MOS-SEC-136: "Secret values MUST never be logged, echoed in an API
  # response, included in a problem+json `detail`, or written to a trace."
  echo "[web-entrypoint] /api/ carries a platform credential when the browser sends none"
else
  echo "[web-entrypoint] /api/ forwards the browser's Authorization header only;" \
       "MEDOS_API_VIEWER_AUTHORIZATION is unset, so an anonymous button gets 401"
fi

echo "[web-entrypoint] rendered default.conf; viewer points at the MedicalOS Gateway"

exec nginx -g 'daemon off;'
