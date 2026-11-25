#!/bin/bash
# Lo único que puede hacer la llave SSH de copias en el servidor (orden forzada en authorized_keys):
#   command="/usr/local/sbin/puerta-copias",restrict ssh-ed25519 AAAA… puerta-copias
# Órdenes:
#   listar           huella SHA-256 y nombre de cada copia cifrada guardada
#   traer NOMBRE     el contenido de esa copia (tal cual, cifrada)
#   recogida         deja la marca datos/copias/.recogida (el panel avisa si deja de llegar)
# Ajustes en /etc/puerta-copias.conf (opcional):
#   CARPETA=/opt/puerta/datos/copias    carpeta de las copias
#   LXC=104                             si el panel va dentro de un contenedor LXC de Proxmox
set -euo pipefail
set -f  # la orden llega del cliente: sin comodines

CARPETA=/opt/puerta/datos/copias
LXC=""
CONF="${PUERTA_COPIAS_CONF:-/etc/puerta-copias.conf}"
[ -r "$CONF" ] && . "$CONF"

en() { if [ -n "$LXC" ]; then pct exec "$LXC" -- "$@"; else "$@"; fi; }

# shellcheck disable=SC2086
set -- ${SSH_ORIGINAL_COMMAND:-}
case "${1:-}" in
  listar)
    [ $# -eq 1 ] || exit 2
    en sh -c 'cd "$1" 2>/dev/null || exit 0
              for f in copia-panel-*.lslab; do [ -f "$f" ] && sha256sum "$f"; done; exit 0' _ "$CARPETA"
    ;;
  traer)
    [ $# -eq 2 ] && [[ "$2" =~ ^copia-panel-[0-9]{8}-[0-9]{4}\.lslab$ ]] || { echo "Nombre de copia no válido" >&2; exit 2; }
    en cat "$CARPETA/$2"
    ;;
  recogida)
    [ $# -eq 1 ] || exit 2
    en sh -c 'date -Iseconds > "$1/.recogida.tmp" && chmod 644 "$1/.recogida.tmp" && mv -f "$1/.recogida.tmp" "$1/.recogida"' _ "$CARPETA"
    ;;
  *)
    echo "Esta llave solo sirve para las copias: listar | traer NOMBRE | recogida" >&2
    exit 2
    ;;
esac
