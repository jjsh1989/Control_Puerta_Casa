#!/bin/bash
# Trae a este ordenador las copias cifradas del panel, sin descifrarlas:
#   1. pide al servidor la lista con la huella SHA-256 de cada copia;
#   2. descarga las que faltan aparte y solo les da su nombre si la huella coincide;
#   3. conserva las GUARDAR más recientes (nunca borra si no ha podido traer las nuevas);
#   4. avisa al servidor de que las ha recogido (si deja de hacerlo, el panel avisa por Telegram).
# Si algo falla, lo apunta en registro.log y muestra una notificación.
# Ajustes (variables de entorno): PUERTA_COPIAS_SSH (alias de ~/.ssh/config, por defecto «puerta-copias»),
# PUERTA_COPIAS_DESTINO (carpeta, por defecto ~/Copias Puerta) y PUERTA_COPIAS_GUARDAR (por defecto 3).
set -uo pipefail

SSH_DESTINO="${PUERTA_COPIAS_SSH:-puerta-copias}"
DESTINO="${PUERTA_COPIAS_DESTINO:-$HOME/Copias Puerta}"
GUARDAR="${PUERTA_COPIAS_GUARDAR:-3}"
PATRON='^copia-panel-[0-9]{8}-[0-9]{4}\.lslab$'
REGISTRO="$DESTINO/registro.log"

mkdir -p "$DESTINO" && chmod 700 "$DESTINO" || exit 1
anota() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$REGISTRO"; echo "$*"; }
falla() {
  anota "ERROR: $*"
  command -v osascript >/dev/null && osascript -e 'on run argv
display notification (item 1 of argv) with title "Copias de la puerta"
end run' "$*" >/dev/null 2>&1
  exit 1
}
servidor() { ssh -n -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 "$SSH_DESTINO" "$@"; }
huella() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }

lista=$(servidor listar) || falla "No se pudo conectar con el servidor (¿Tailscale encendido?)."
[ -n "$lista" ] || falla "El servidor no tiene ninguna copia."

nuevas=0
while read -r h nombre; do
  [[ "$h" =~ ^[0-9a-f]{64}$ && "$nombre" =~ $PATRON ]] || falla "El servidor ha respondido algo inesperado."
  [ -f "$DESTINO/$nombre" ] && continue
  tmp="$DESTINO/.$nombre.tmp"
  ( umask 077; servidor traer "$nombre" > "$tmp" ) || { rm -f "$tmp"; falla "No se pudo descargar $nombre."; }
  [ "$(huella "$tmp")" = "$h" ] || { rm -f "$tmp"; falla "$nombre ha llegado dañada (la huella no coincide)."; }
  mv -f "$tmp" "$DESTINO/$nombre"
  nuevas=$((nuevas + 1))
  anota "Traída $nombre"
done <<< "$lista"

ls "$DESTINO" | grep -E "$PATRON" | sort -r | tail -n +$((GUARDAR + 1)) | while read -r vieja; do
  rm -f "$DESTINO/$vieja" && anota "Borrada $vieja (se conservan las $GUARDAR más recientes)"
done

servidor recogida || falla "Copias traídas, pero no se pudo avisar al servidor."
anota "Correcto: $nuevas nueva(s), $(ls "$DESTINO" | grep -cE "$PATRON") guardada(s) en este ordenador."
tail -n 500 "$REGISTRO" > "$REGISTRO.tmp" && mv -f "$REGISTRO.tmp" "$REGISTRO"
