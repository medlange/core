#!/bin/sh
# =====================================================================================
# medos/deploy/compose/gateway-entrypoint.sh -- start medos-gateway, optionally claiming the
# studies that were already in the PACS before the Gateway existed.
#
# WHY THIS SCRIPT EXISTS AT ALL
#   MOS-DATA-012 creates the `studies` row (the tenancy record, MOS-DATA-010) when an
#   instance arrives through STOW-RS. Anything the PACS held BEFORE has no row, and
#   MOS-DATA-013 then makes it invisible -- correctly, because "no row" means "nobody has
#   claimed this data". Someone has to claim it, and MOS-DATA-047 says who: a human,
#   naming a tenant. "Assigning unattributed data to a default tenant is forbidden."
#
#   So `MEDOS_GATEWAY_RECONCILE_TENANT` is EMPTY by default and this script does nothing
#   with it unless an operator has set it. Setting it in docker-compose.yml or .env IS the
#   operator's attribution -- it is a person typing a tenant id, which is what
#   MOS-DATA-047 requires; what it forbids is a default appearing when nobody typed one.
#
#   A production deployment does not set it and runs
#       docker compose exec medos-gateway python -m medos.gateway.reconcile --tenant <uuid>
#   once, by hand, after deciding whose data it is.
#
# `exec` on the last line so uvicorn is PID 1 and receives SIGTERM from `docker stop`
# directly; a shell parent would swallow it and every shutdown would be a 10s SIGKILL.
# =====================================================================================
set -e

if [ -n "${MEDOS_GATEWAY_RECONCILE_TENANT}" ]; then
  echo "[gateway-entrypoint] claiming pre-existing studies for tenant ${MEDOS_GATEWAY_RECONCILE_TENANT}"
  # Non-fatal: a Gateway that cannot reconcile is still a correct Gateway for everything
  # that arrives through STOW-RS from now on, and refusing to boot would take the viewer
  # down over a backfill.
  python -m medos.gateway.reconcile --tenant "${MEDOS_GATEWAY_RECONCILE_TENANT}" \
    || echo "[gateway-entrypoint] reconcile failed; continuing (studies stay unclaimed)"
fi

exec python -m medos.gateway --host 0.0.0.0 --port 8043
