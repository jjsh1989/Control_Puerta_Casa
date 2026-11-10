# Instalar «Control Puerta Casa»

Guía para instalarlo en un servidor **Proxmox** dentro de un contenedor **LXC** con Docker. También sirve cualquier Linux con Docker: en ese caso empieza en el paso 2.

| Contenedor Docker | Qué hace | Memoria máx. |
|---|---|---|
| `puerta-panel` | Panel web, avisos, renovación semanal y vigilancia | 320 MB |
| `puerta-waha` | WhatsApp (motor NOWEB, sin navegador) | 512 MB |
| `puerta-eufy` | Puente con las cámaras Eufy (eufy-security-ws) | 384 MB |
| `puerta-docker-proxy` | Deja al panel arrancar/parar/reiniciar sus contenedores y nada más | 64 MB |

Consumo real: unos 400–600 MB de RAM y muy poca CPU (sube un poco mientras se ve una cámara en directo).

---

## 1. Crear el contenedor LXC (web de Proxmox)

1. **Plantilla**: `local` → *CT Templates* → *Templates* → `debian-12-standard` → *Download*.
2. **Create CT**:
   - *General*: hostname `puerta`, **Unprivileged container ✓**, contraseña de root.
   - *Template*: `debian-12-standard`.
   - *Disks*: **16 GB** (las imágenes de Docker ocupan unos 6 GB y el resto es para fotos y clips de las cámaras). Si se queda corto, se amplía en caliente desde el host: `pct resize ID rootfs 24G`.
   - *CPU*: **2** núcleos (1 vale si no vas a usar el directo).
   - *Memory*: **1536 MB**, Swap 512 MB.
   - *Network*: IPv4 **DHCP**.
   - **No lo arranques todavía.**
3. *Options* → *Features* → marca **nesting** y **keyctl** (Docker los necesita).
4. Para Tailscale, en la **consola del host Proxmox** (no la del contenedor), cambiando `ID` por el número del contenedor:
   ```bash
   cat >> /etc/pve/lxc/ID.conf <<'EOF'
   lxc.cgroup2.devices.allow: c 10:200 rwm
   lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
   EOF
   ```
5. Arranca el contenedor y pon *Options* → **Start at boot: Yes**.

## 2. Preparar el contenedor (consola del contenedor, como root)

```bash
apt update && apt full-upgrade -y && apt install -y curl ca-certificates git python3
curl -fsSL https://get.docker.com | sh
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh
```
El último comando muestra un enlace: ábrelo y autoriza el equipo en tu cuenta de Tailscale.

## 3. Descargar e instalar

```bash
git clone https://github.com/jjsh1989/Control_Puerta_Casa.git /opt/puerta
cd /opt/puerta
./instalar.sh
```

`instalar.sh`:
- crea la **clave maestra** de la bóveda (`secretos/clave_maestra`) y el archivo `.env` (mira `.env.ejemplo`);
- construye y arranca los cuatro contenedores;
- muestra el **código de instalación** para crear el primer administrador.

> Si vienes de la versión antigua para Mac: copia `config.json`, `estado.json` y `gestion.jsonl` a `datos/importar/` **antes** del primer arranque. Se importan solos y el token pasa a la bóveda cifrada.

## 4. Entrar desde cualquier sitio (Tailscale)

```bash
./instalar.sh tailscale
```
Publica el panel en `https://puerta.<tu-red>.ts.net` con certificado HTTPS válido, solo para los dispositivos de tu red Tailscale, y guarda esa dirección en `.env` (`PANEL_URL_PUBLICA`) para que los enlaces de los avisos funcionen.

Ábrela, escribe el **código de instalación** y crea tu usuario administrador.

En el iPhone: Safari → Compartir → **Añadir a pantalla de inicio**, y se abre como una app.

## 5. Primeros ajustes (en el panel → Ajustes)

Los ajustes están agrupados: **Casas · Nuki · Cámaras · Comunicación**.

### Nuki
1. En https://web.nuki.io → *Menú* → **API** → genera un token de API con permisos de cerraduras y del teclado.
2. Ajustes → **Casas** → *Editar casa* → **Nuki: sí** → pega el token, pulsa **Buscar** y elige tu cerradura → Guardar.
3. Ajustes → **Nuki** → *Renovación semanal*: elige día y hora para los códigos «Semanal».

### Telegram (recomendado para avisos)
1. En Telegram, habla con **@BotFather** → `/newbot` → copia el token.
2. Ajustes → **Comunicación** → *Telegram*: pega el token.
3. Cada usuario del panel pulsa **Vincular mi Telegram** para recibir los avisos en su móvil. Las personas con código pulsan el botón de Telegram de su ficha.

### WhatsApp (WAHA)
Ajustes → **Comunicación** → *WhatsApp*: escanea el QR con el WhatsApp del número que enviará los códigos (Dispositivos vinculados). Mejor un número secundario.

### Correo (opcional)
Ajustes → **Comunicación** → *Correo*: servidor SMTP (con Gmail, usa una **contraseña de aplicación**).

### Cámaras Eufy
1. En la app Eufy, crea una **cuenta secundaria** (otro correo) y **compártele** las cámaras desde la cuenta principal (*Ajustes → Compartir dispositivo*). No uses tu cuenta principal: el puente cerraría tu sesión en el móvil.
2. Ajustes → **Cámaras** → *Cámara Eufy*: correo y contraseña de esa cuenta secundaria → **Guardar y conectar**.
3. Si Eufy pide un **captcha** o un **código por correo**, el panel te lo muestra para que lo escribas.
4. En la lista de cámaras: ⭐ **Predeterminada** (la que manda foto y clip en los avisos de la puerta), 📷 **Movimiento** (avisa al detectar movimiento o timbre) y la **casa** de cada una.

### Avisos
Ajustes → **Comunicación** → *Avisos*: activa los que quieras (código incorrecto, de noche, puerta abierta X minutos, cerradura atascada, bloqueo, batería…) y si llevan clip de vídeo.

## 6. Más casas y más usuarios

- **Nueva casa**: Ajustes → **Casas** → *Nueva casa*. El asistente pregunta el nombre, si tiene Nuki (token y cerradura) y qué cámaras son suyas. Una casa puede ser **solo de cámaras**.
- **Editar casa**: mismo sitio → *Editar casa*: cambiar el nombre, **añadir o quitar la Nuki** y marcar sus cámaras.
- **Usuarios**: en la tarjeta de cada casa → *Nuevo usuario*, rol **Gestor** (solo esa casa) o **Administrador** (todas).
- Para que otra persona entre desde su móvil: en https://login.tailscale.com → **Share** del equipo `puerta` (o *Invite* a tu red). Instala Tailscale, acepta y entra en la dirección del paso 4.

## 7. Si alguien se queda fuera

- **Desde la pantalla de entrada**: «¿Has olvidado la contraseña o te has quedado bloqueado?» → manda un código al **Telegram vinculado** de ese usuario.
- **Desde el servidor** (último recurso):
  ```bash
  docker exec -it puerta-panel python panel.py --recuperar USUARIO
  ```
  Da una contraseña temporal. Si sale «Demasiados intentos»: `docker restart puerta-panel`.

## 8. Mantenimiento

| Qué | Cómo (en `/opt/puerta`) |
|---|---|
| Ver estado | `docker compose ps` |
| Ver registro | `docker compose logs -f panel` (o `eufy`, `waha`) |
| Actualizar | `git pull && ./instalar.sh` |
| Copia de seguridad | `./instalar.sh copia` → `copia-puerta-FECHA.tar.gz` |
| Reiniciar todo | `docker compose restart` |

⚠️ **La copia incluye la clave maestra y todos los secretos.** Guárdala fuera del servidor y **nunca en git**. Si pierdes `secretos/clave_maestra`, no se pueden leer los tokens de la bóveda: habría que volver a pegarlos.

## 9. (Opcional) Un dominio propio

Se puede usar un dominio (p. ej. `puerta.midominio.es`) apuntando a la IP de Tailscale del servidor (`100.x.y.z`), de modo que **solo funcione dentro de Tailscale**. Hace falta un certificado HTTPS por DNS (Caddy o similar con la API de tu proveedor de DNS). La dirección `*.ts.net` del paso 4 ya funciona sin nada de esto.

## Seguridad, en resumen

- **El panel solo escucha dentro del servidor** (127.0.0.1). Desde fuera solo se llega por Tailscale, con HTTPS.
- **Contraseñas con PBKDF2** (600 000 iteraciones). Tras 5 fallos, el usuario queda bloqueado 15 minutos.
- **Sesiones** con cookie `HttpOnly` + `SameSite=Strict`, protección CSRF y cierre por inactividad.
- **Tokens y claves cifrados** (Fernet/AES) en `datos/boveda.cifrada`; la clave maestra va aparte, en `secretos/`.
- **Teléfonos y códigos ocultos** hasta pulsar «Mostrar»; queda anotado quién los ve.
- **Docker limitado**: el panel solo puede arrancar, parar y reiniciar sus contenedores.
- **Contenedor del panel de solo lectura**, sin privilegios nuevos y con memoria limitada.
