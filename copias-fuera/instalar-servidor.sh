#!/bin/bash
# En el servidor, como root: deja que la llave de copias del ordenador solo pueda recoger las copias.
#   bash instalar-servidor.sh 'ssh-ed25519 AAAA… puerta-copias' [LXC] [--puerto 2222]
# LXC: número del contenedor de Proxmox donde va el panel (si va directamente en esta máquina, nada).
# --puerto: además del 22, el SSH normal escucha en ese puerto. Hace falta si la máquina tiene Tailscale SSH:
#   por la dirección de Tailscale el 22 lo atiende Tailscale, que no mira las llaves (ni su restricción).
# La carpeta de las copias se puede cambiar en /etc/puerta-copias.conf (CARPETA=…).
set -euo pipefail

LLAVE="${1:?Uso: bash instalar-servidor.sh 'ssh-ed25519 AAAA… puerta-copias' [LXC]}"
LXC=""; PUERTO=""
shift
while [ $# -gt 0 ]; do
  case "$1" in
    --puerto) PUERTO="${2:-}"; shift 2 ;;
    *) LXC="$1"; shift ;;
  esac
done
[[ -z "$PUERTO" || "$PUERTO" =~ ^[0-9]+$ ]] || { echo "El puerto debe ser un número." >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo "Ejecútalo como root." >&2; exit 1; }
[[ "$LLAVE" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+(\ [A-Za-z0-9._@-]+)?$ ]] || { echo "Eso no parece una llave ssh-ed25519." >&2; exit 1; }
[[ -z "$LXC" || "$LXC" =~ ^[0-9]+$ ]] || { echo "El contenedor LXC debe ser un número." >&2; exit 1; }
AQUI="$(cd "$(dirname "$0")" && pwd)"

install -m 755 -o root -g root "$AQUI/servidor-puerta-copias.sh" /usr/local/sbin/puerta-copias
if [ ! -f /etc/puerta-copias.conf ]; then
  printf 'CARPETA=/opt/puerta/datos/copias\nLXC=%s\n' "$LXC" > /etc/puerta-copias.conf
  chmod 644 /etc/puerta-copias.conf
fi

AUTH=/root/.ssh/authorized_keys
CUERPO="$(echo "$LLAVE" | cut -d' ' -f2)"
if grep -qF "$CUERPO" "$AUTH" 2>/dev/null; then
  echo "La llave ya estaba autorizada; no se cambia."
else
  cp -p "$AUTH" "$AUTH.antes-puerta-copias" 2>/dev/null || true
  echo "command=\"/usr/local/sbin/puerta-copias\",restrict $LLAVE" >> "$AUTH"
  echo "Llave añadida: solo puede listar, traer y marcar la recogida de las copias."
fi

if [ -n "$PUERTO" ] && [ "$PUERTO" != 22 ]; then
  F=/etc/ssh/sshd_config.d/20-puerto-copias.conf
  printf '# Puerto %s: SSH normal (solo llaves) también donde el 22 lo atiende Tailscale SSH.\nPort 22\nPort %s\n' "$PUERTO" "$PUERTO" > "$F"
  if sshd -t; then
    systemctl reload ssh 2>/dev/null || systemctl reload sshd
    echo "SSH escuchando también en el puerto $PUERTO."
  else
    rm -f "$F"; echo "La configuración de SSH no es válida: no se ha cambiado el puerto." >&2; exit 1
  fi
fi
