#!/usr/bin/env bash
# Instala o actualiza «Códigos de la puerta» en un servidor Linux con Docker.
#   ./instalar.sh            instala / actualiza y arranca
#   ./instalar.sh tailscale  publica el panel en tu red Tailscale (https://<equipo>.<red>.ts.net)
#   ./instalar.sh codigo     muestra el código de instalación del primer arranque
#   ./instalar.sh copia      hace una copia de seguridad de datos/ y secretos/
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  echo "Falta Docker. Instálalo con:  curl -fsSL https://get.docker.com | sh"; exit 1
fi

case "${1:-}" in
  tailscale)
    command -v tailscale >/dev/null || { echo "Falta Tailscale: curl -fsSL https://tailscale.com/install.sh | sh"; exit 1; }
    PUERTO=$(grep -E '^PANEL_PUERTO_HOST=' .env | cut -d= -f2)
    # 443 = dirección sin puerto. Si ya usas el 443 para otra cosa:  HTTPS_PUERTO=8443 ./instalar.sh tailscale
    tailscale serve --bg --https="${HTTPS_PUERTO:-443}" "http://127.0.0.1:${PUERTO:-8790}"
    DNS=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)
    if [ -n "$DNS" ]; then
      URL="https://$DNS"; [ "${HTTPS_PUERTO:-443}" = "443" ] || URL="$URL:${HTTPS_PUERTO}"
      if grep -q '^PANEL_URL_PUBLICA=' .env; then sed -i "s|^PANEL_URL_PUBLICA=.*|PANEL_URL_PUBLICA=$URL|" .env; else echo "PANEL_URL_PUBLICA=$URL" >> .env; fi
      docker compose up -d panel >/dev/null
      echo "Panel publicado en: $URL"
    fi
    echo; tailscale serve status; exit 0 ;;
  codigo)
    cat datos/codigo_instalacion.txt 2>/dev/null || docker compose logs panel | grep -i "código de instalación" | tail -1; exit 0 ;;
  copia)
    f="copia-puerta-$(date +%Y%m%d-%H%M).tar.gz"
    tar czf "$f" datos secretos .env && chmod 600 "$f"
    echo "Copia creada: $f  (contiene la clave maestra: guárdala en un sitio seguro)"; exit 0 ;;
esac

umask 077
mkdir -p datos secretos

# Clave maestra de la bóveda (se crea una sola vez; sin ella no se pueden leer los secretos)
if [ ! -s secretos/clave_maestra ]; then
  python3 - <<'EOF' > secretos/clave_maestra 2>/dev/null || head -c 32 /dev/urandom | base64 | tr '+/' '-_' > secretos/clave_maestra
import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())
EOF
  echo "Clave maestra creada en secretos/clave_maestra"
fi
# El contenedor del panel corre con el usuario 1000
chown -R 1000:1000 datos secretos 2>/dev/null || sudo chown -R 1000:1000 datos secretos
chmod 700 datos secretos; chmod 400 secretos/clave_maestra

# Ajustes del servidor
if [ ! -f .env ]; then
  ARQ=$(uname -m)
  IMAGEN="devlikeapro/waha:noweb"; [ "$ARQ" = "aarch64" ] || [ "$ARQ" = "arm64" ] && IMAGEN="devlikeapro/waha:noweb-arm"
  cat > .env <<EOF
TZ=Europe/Madrid
PANEL_PUERTO_HOST=8790
PANEL_HOSTS=
WAHA_IMAGEN=$IMAGEN
WAHA_API_KEY=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
PANEL_URL_PUBLICA=
EOF
  echo ".env creado"
fi

docker compose pull waha eufy docker-proxy
docker compose up -d --build
echo
echo "Listo. Estado:"; docker compose ps
echo
if [ -f datos/codigo_instalacion.txt ] || docker compose logs panel 2>/dev/null | grep -qi "código de instalación"; then
  sleep 3; echo "Código de instalación para crear el administrador:"; ./instalar.sh codigo
fi
echo "Para entrar desde cualquier sitio con Tailscale:  ./instalar.sh tailscale"
