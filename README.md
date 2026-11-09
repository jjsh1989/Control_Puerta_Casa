# Control Puerta Casa

Panel web para gestionar la puerta de casa: **códigos del teclado Nuki**, envío por **WhatsApp o Telegram**, **avisos** y **cámaras Eufy** (fotos, clips y directo).
Pensado para funcionar 24 h en un servidor doméstico (Proxmox + Docker) y entrar desde el móvil a través de **Tailscale**.

➡️ **Instalación paso a paso: [INSTALAR.md](INSTALAR.md)**

## Qué hace

**Casas y usuarios**
- **Varias casas** (cuentas): cada una con su cerradura Nuki, **o solo con cámaras** (sin Nuki).
- Asistente **Nueva casa** (nombre → ¿tiene Nuki? → cámaras) y **Editar casa** para añadir o quitar la Nuki y elegir qué cámaras son de cada casa.
- Usuarios con contraseña: los *administradores* ven todas las casas; los *gestores*, solo la suya. Cada uno puede recibir los avisos en su propio Telegram.
- **Cierre de sesión por inactividad** (5, 15, 30, 60 min o 4 h, a elección de cada usuario) con cuenta atrás visible.
- **Recuperar la cuenta**: código por Telegram desde la pantalla de entrada, o contraseña temporal desde la consola del servidor.

**Códigos Nuki**
- Por persona: *Semanal* (se renueva solo), *Temporal* (lo renuevas tú) o *Fijo* (no caduca).
- **Invitados**: códigos de 2 horas a 1 semana que caducan solos.
- Los códigos **nunca se repiten**, y los antiguos se **vigilan** unos días: si alguien teclea uno, sale en Entradas con 🚨.
- **Orden seguro al renovar**: primero crea el nuevo, lo envía y solo entonces retira el viejo.

**Puerta**
- Estado de la cerradura y del sensor de puerta, y botones **Bloquear / Desbloquear / Abrir**.
- **Entradas** (registro de la cerradura con filtros) e **Historial** (todo lo que se hace en el panel y quién).

**Avisos** (Telegram, WhatsApp o correo, según el tipo)
- Código incorrecto, entradas de noche, códigos desactivados, batería baja.
- **Puerta abierta más de X minutos** (y aviso cuando se cierra), **cerradura atascada** y **bloqueo**.
- Con cámara: el aviso lleva **foto y clip** de la **cámara predeterminada** de esa casa.

**Cámaras Eufy** (con una cuenta secundaria de Eufy)
- ⭐ Cámara predeterminada por casa, 📷 avisos por movimiento o timbre, y cada cámara asignada a su casa (o oculta).
- Pestaña **Cámara**: **directo** ordenado por casas y galería de avisos con foto y vídeo (se guardan 30 días).

**Seguridad**
- El panel solo escucha dentro del servidor; desde fuera solo se entra por Tailscale con HTTPS.
- Tokens y claves **cifrados** en una bóveda; teléfonos y códigos ocultos hasta pulsar «Mostrar» (queda anotado quién los vio).
- El panel solo puede arrancar, parar y reiniciar sus contenedores; nada más de Docker.

## Archivos

| Archivo | Para qué |
|---|---|
| `panel.py`, `panel.html` | Panel web, usuarios, sesiones, avisos y renovación programada |
| `nuki_codigos.py` | Nuki: códigos, invitados, registro y acciones de la puerta (también desde el terminal) |
| `cuentas.py` | Casas, usuarios y bóveda cifrada |
| `alertas.py` | Reglas de aviso y vigilancia del estado de la puerta |
| `camara.py` | Cámaras Eufy: eventos, fotos, clips y directo |
| `telegram.py` | Bot de Telegram (códigos y avisos) |
| `waha.py` | WhatsApp con WAHA y control limitado de Docker |
| `bateria.py` | Batería de la cerradura y correo (SMTP) |
| `Dockerfile`, `docker-compose.yml`, `instalar.sh`, `.env.ejemplo` | Instalación en el servidor |

## Lo que **no** está en este repositorio (a propósito)

`datos/`, `secretos/`, `.env`, `config.json`, `estado.json`, `gestion.jsonl` y los registros: contienen tokens, contraseñas, teléfonos y la **clave maestra**. Están en `.gitignore`. Para guardarlos usa `./instalar.sh copia` y guarda esa copia en un sitio seguro, **no en git**.

## Sobre la vigilancia de códigos desactivados

Nuki **no dice qué número se tecleó** cuando un código es incorrecto. Por eso los códigos viejos se dejan en la cerradura *desactivados* y con nombre («Baja …»): si Nuki registra el intento contra ese código, se sabe de quién era. El teclado admite 100 códigos; con 3 personas semanales y 30 días de vigilancia se usan unos 15.

## Desde el terminal (dentro del servidor)

```bash
docker compose exec panel python nuki_codigos.py --cuenta principal entradas
docker compose exec panel python nuki_codigos.py --cuenta principal historial
docker exec -it puerta-panel python panel.py --recuperar USUARIO
```
