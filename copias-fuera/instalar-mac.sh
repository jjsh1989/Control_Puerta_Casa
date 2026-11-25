#!/bin/bash
# Prepara este Mac para recoger cada día las copias cifradas del panel.
#   bash copias-fuera/instalar-mac.sh usuario@servidor [puerto]
# (el servidor donde está el panel o, con Proxmox, la máquina que lo aloja; tiene que ser accesible por SSH,
#  p. ej. con Tailscale)
# ⚠️ Si el servidor tiene activado Tailscale SSH, por su dirección de Tailscale el puerto 22 lo atiende
#    Tailscale y deja entrar sin mirar las llaves (también saltándose la restricción de esta). Usa entonces
#    otro puerto del SSH normal (instalar-servidor.sh --puerto 2222) y pásalo aquí como [puerto].
# Hace, sin tocar nada más:
#   - una llave SSH solo para las copias (~/.ssh/puerta_copias, sin frase: la usa una tarea sin nadie delante);
#   - el alias «puerta-copias» en ~/.ssh/config;
#   - instala traer-copias.sh en ~/Library/Application Support/Puerta Copias/;
#   - la carpeta ~/Copias Puerta con «Traer copias ahora.command» (doble clic) y registro.log;
#   - una tarea de launchd que las trae a las 10:00 y a las 18:00 (si el Mac duerme, al despertar).
# Al final enseña la línea que hay que poner en el servidor con instalar-servidor.sh.
set -euo pipefail

SERVIDOR="${1:?Uso: bash instalar-mac.sh usuario@servidor [puerto]}"
PUERTO="${2:-22}"
[[ "$PUERTO" =~ ^[0-9]+$ ]] || { echo "El puerto debe ser un número." >&2; exit 1; }
[ "$(uname)" = Darwin ] || { echo "Este instalador es para macOS." >&2; exit 1; }
AQUI="$(cd "$(dirname "$0")" && pwd)"
LLAVE="$HOME/.ssh/puerta_copias"
APOYO="$HOME/Library/Application Support/Puerta Copias"
DESTINO="$HOME/Copias Puerta"
ETIQUETA="puerta.copias"
PLIST="$HOME/Library/LaunchAgents/$ETIQUETA.plist"

mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
[ -f "$LLAVE" ] || ssh-keygen -q -t ed25519 -N "" -C "puerta-copias" -f "$LLAVE"

USUARIO="${SERVIDOR%@*}"; MAQUINA="${SERVIDOR#*@}"
[ "$USUARIO" != "$SERVIDOR" ] || USUARIO="$(id -un)"
if ! grep -qE '^Host puerta-copias$' "$HOME/.ssh/config" 2>/dev/null; then
  { echo ""; echo "Host puerta-copias"; echo "  HostName $MAQUINA"; echo "  Port $PUERTO"; echo "  User $USUARIO"
    echo "  IdentityFile $LLAVE"; echo "  IdentitiesOnly yes"; } >> "$HOME/.ssh/config"
  chmod 600 "$HOME/.ssh/config"
fi

mkdir -p "$APOYO" "$DESTINO" "$HOME/Library/LaunchAgents"
chmod 700 "$DESTINO"
install -m 755 "$AQUI/traer-copias.sh" "$APOYO/traer-copias.sh"
cat > "$DESTINO/Traer copias ahora.command" <<FIN
#!/bin/bash
/bin/bash "$APOYO/traer-copias.sh"
echo ""; read -n 1 -s -r -p "Pulsa una tecla para cerrar…"; echo ""
FIN
chmod 755 "$DESTINO/Traer copias ahora.command"

cat > "$PLIST" <<FIN
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$ETIQUETA</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$APOYO/traer-copias.sh</string></array>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>10</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>18</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>$APOYO/errores.log</string>
</dict>
</plist>
FIN
launchctl bootout "gui/$(id -u)/$ETIQUETA" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Listo en este Mac. Copias en: $DESTINO"
echo ""
echo "Falta el servidor: copia copias-fuera/instalar-servidor.sh allí y ejecútalo como root con esta llave:"
echo "  bash instalar-servidor.sh '$(cat "$LLAVE.pub")' [número del contenedor LXC, si el panel va en Proxmox] [--puerto $PUERTO]"
echo "Y, la primera vez, añade la huella del servidor en este Mac (léela en el servidor, no la aceptes a ciegas):"
echo "  echo \"[$MAQUINA]:$PUERTO \$(ssh $SERVIDOR cat /etc/ssh/ssh_host_ed25519_key.pub)\" >> ~/.ssh/known_hosts"
echo "Comprueba con «Traer copias ahora.command» en $DESTINO."
