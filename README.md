# Control Puerta Casa

Panel web para gestionar la puerta de casa: **códigos del teclado Nuki**, envío por **WhatsApp o Telegram**, **avisos** y **cámaras Eufy** (fotos, clips y directo).
Pensado para funcionar 24 h en un servidor doméstico (Proxmox + Docker) y entrar desde el móvil a través de **Tailscale**.

➡️ **Instalación paso a paso: [INSTALAR.md](INSTALAR.md)** · 🤝 **¿Quieres ayudar? [CONTRIBUIR.md](CONTRIBUIR.md)**

> **Proyecto personal y comunitario.** Nació para una casa concreta y se comparte por si le sirve a alguien. Se agradecen mucho los avisos de fallos, las ideas y las mejoras, pero no hay soporte garantizado.
>
> **No es un producto oficial** de Nuki ni de Eufy (Anker), ni está relacionado con ellas. Las marcas son de sus dueños. Usa el puente no oficial [eufy-security-ws](https://github.com/bropat/eufy-security-ws), que puede dejar de funcionar si Eufy cambia algo. Se trata de la puerta de tu casa: **úsalo bajo tu responsabilidad** (ver [LICENSE](LICENSE)).

<details>
<summary>🇬🇧 English summary</summary>

Self-hosted web panel for your front door: it manages **Nuki Keypad codes** (weekly auto-rotating, temporary, fixed and guest codes, with per-person schedules), sends them via **WhatsApp or Telegram**, sends **alerts** (wrong code, door left open, lock offline, low battery…) and integrates **Eufy cameras** (live view with pan/tilt, snapshots and clips attached to alerts, privacy mode, automations tied to the lock).
Supports several homes and users, encrypted secrets vault, and is meant to run 24/7 on a home server (Proxmox LXC + Docker) reachable only through **Tailscale**.
The UI and docs are in Spanish; issues and pull requests in English are welcome too. Not affiliated with Nuki or Eufy/Anker. MIT licensed, no warranty.
</details>

## Qué hace

**Casas y usuarios**
- **Varias casas** (cuentas): cada una con su cerradura Nuki, **o solo con cámaras** (sin Nuki).
- Asistente **Nueva casa** (nombre → ¿tiene Nuki? → cámaras) y **Editar casa** para añadir o quitar la Nuki y elegir qué cámaras son de cada casa.
- Usuarios con contraseña: los *administradores* ven todas las casas; los *gestores*, solo la suya. Cada uno puede recibir los avisos en su propio Telegram.
- **Cierre de sesión por inactividad** (5, 15, 30, 60 min o 4 h, a elección de cada usuario) con cuenta atrás visible.
- **Recuperar la cuenta**: código por Telegram desde la pantalla de entrada, o contraseña temporal desde la consola del servidor.

**Códigos Nuki**
- Por persona: *Semanal* (se renueva solo), *Temporal* (lo renuevas tú) o *Fijo* (no caduca).
- **Horario por persona**: un código puede funcionar solo ciertos días y horas (p. ej. limpieza, L-V de 9 a 14).
- **Invitados**: códigos de 2 horas a 1 semana que caducan solos.
- Los códigos **nunca se repiten**, y los antiguos se **vigilan** unos días: si alguien teclea uno, sale en Entradas con 🚨.
- **Orden seguro al renovar**: primero crea el nuevo, lo envía y solo entonces retira el viejo.

**Puerta**
- Estado de la cerradura y del sensor de puerta, y botones **Bloquear / Desbloquear / Abrir** (y **Lock'n'Go** y **Abrir portal** si hay Opener).
- **Programar apertura y cierre**: p. ej. bloquear todas las noches a las 23:00 (solo si la puerta está cerrada) con aviso.
- **Ajustes de la cerradura** (bloqueo automático, pestillo, LED, botón…) y **accesos** (apps de Nuki, llaveros): ver, desactivar o eliminar.
- **Se adapta a cada casa**: el panel pregunta a Nuki qué tiene la cerradura (teclado, sensor, llavero, Opener, ajustes de ese modelo) y solo muestra lo que sirve.
- **Entradas** (registro de la cerradura con filtros) e **Historial** (todo lo que se hace en el panel y quién).

**Avisos** (Telegram, WhatsApp o correo, según el tipo)
- Código incorrecto, entradas de noche, códigos desactivados, batería baja.
- **Cerradura sin conexión** (wifi, bridge o pilas) y **puerta abierta más de X minutos** (y aviso cuando se cierra), **cerradura atascada** y **bloqueo**.
- Con cámara: el aviso lleva **foto y clip** de la **cámara predeterminada** de esa casa. En Telegram es un solo mensaje: llega la foto y, cuando está listo, se convierte en el clip.
- Botones bajo cada aviso de Telegram: **🔕 Silenciar 1 h** esa cámara, **📌 No borrar**, **🚪 Puerta** y **📹 En directo**.

**La puerta desde Telegram** (solo usuarios del panel con su Telegram vinculado; los invitados no)
- Escribe **/puerta** al bot: estado de la cerradura, puerta y batería, y botones para **cerrar con llave**, **abrir** y abrir el **portal** (si hay Opener).
- **Abrir pide un PIN** que cada usuario pone en el panel (Ajustes → Comunicación → «La puerta desde Telegram», con su contraseña). El bot borra el mensaje del PIN, la petición caduca a los 2 minutos y 3 fallos seguidos bloquean 15 minutos. Sin PIN no se puede abrir desde Telegram.
- Escribe **/camara** para elegir una cámara y recibir una **foto al momento**, con botones para otra foto, un **clip de 15 s** o el **directo**.
- **/codigo**: un código temporal (2 h, hasta esta noche, 24 h o 3 días) para una visita, con PIN, listo para reenviar y con botón para anularlo.
- **🏠 En casa / 🚶 Fuera** en el menú de /puerta: aplica a las cámaras lo mismo que «Cámaras y cerradura» al entrar o al salir.
- Todo queda en el Historial y los demás que reciben avisos se enteran («Fulano ha abierto la puerta desde Telegram»).

**Avisos extra** (Ajustes → Comunicación)
- **Recordatorio de noche** si la puerta no está cerrada con llave, con botón «🔒 Cerrar ahora».
- **Resumen del día**: aperturas y quién, avisos, detecciones de cámara y batería.
- **Avisos de cámara sin sonido** en una franja horaria (todas las cámaras o solo las elegidas).
- Si alguien se queda delante de la cámara, las detecciones seguidas se juntan en **un solo mensaje** que se va actualizando.
- Para los administradores: aviso si el **disco** pasa del 90 % o las **cámaras** llevan 15 min sin conexión.

**Copia de seguridad** (Ajustes → Casas): semanal y cifrada con tu contraseña; se guardan 4 en el servidor y cada una te llega por Telegram. Cómo recuperarla, en INSTALAR.md.

**Cámaras Eufy** (con una cuenta secundaria de Eufy)
- ⭐ Cámara predeterminada por casa, 📷 avisos por movimiento o timbre, y cada cámara asignada a su casa (o oculta).
- Pestaña **Cámara**: **directo** ordenado por casas y galería de avisos separada en **Clips** y **Fotos**. Cada aviso se puede **borrar** o marcar con 📌 **No borrar**.
- **Borrado automático** (Ajustes → Cámaras → Espacio): por antigüedad (7 a 180 días) o **una vez por semana** (día y hora a elegir). Lo marcado con 📌 nunca se borra solo; también hay un botón para borrar ya todo lo no marcado.
- **Controles en el directo**, solo los que admite cada modelo: **girar** (mantén pulsada la flecha para seguir girando), **sirena**, **foco**, **apagar la cámara** (modo privacidad) y **modo** (En casa, Fuera, Programado, Desactivado).
- **Ajustes de cada cámara** (sensibilidad, grabación, luces, sonido…) y **qué detecciones avisan** (personas, mascotas, llanto, sonido, movimiento).
- **Cámaras y cerradura**: al entrar con un código, apagar las cámaras de dentro o cambiar el modo; al bloquear desde el teclado, encenderlas o poner «Fuera». También se pueden **programar** por horas.
- **Foto de cada entrada** con el teclado en la pestaña Entradas, y **aviso** si una cámara se queda sin conexión o con poca batería.
- Las grabaciones de la tarjeta SD de la cámara **no** se pueden ver: el puente de Eufy no permite consultarlas.

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
| `nuki_extra.py` | Lo que depende de cada cerradura: capacidades, ajustes, accesos, Opener y programaciones |
| `camara.py` | Cámaras Eufy: eventos, fotos, clips y directo |
| `telegram.py` | Bot de Telegram (códigos y avisos) |
| `extras.py` | Recordatorio de la llave, resumen diario, avisos sin sonido y salud del servidor |
| `copias.py` | Copia de seguridad cifrada y cómo recuperarla |
| `bot_puerta.py` | La puerta desde Telegram: /puerta, botones de los avisos y PIN para abrir |
| `waha.py` | WhatsApp con WAHA y control limitado de Docker |
| `bateria.py` | Batería de la cerradura y correo (SMTP) |
| `Dockerfile`, `docker-compose.yml`, `instalar.sh`, `.env.ejemplo` | Instalación en el servidor |

## Lo que **no** está en este repositorio (a propósito)

`datos/`, `secretos/`, `.env`, `config.json`, `estado.json`, `gestion.jsonl` y los registros: contienen tokens, contraseñas, teléfonos y la **clave maestra**. Están en `.gitignore`. Para guardarlos usa `./instalar.sh copia` y guarda esa copia en un sitio seguro, **no en git**.

## Sobre la vigilancia de códigos desactivados

Nuki **no dice qué número se tecleó** cuando un código es incorrecto. Por eso los códigos viejos se dejan en la cerradura *desactivados* y con nombre («Baja …»): si Nuki registra el intento contra ese código, se sabe de quién era. El teclado admite 100 códigos; con 3 personas semanales y 30 días de vigilancia se usan unos 15.

## Probado con

| Equipo | Modelo |
|---|---|
| Cerradura | Nuki Smart Lock 3.0/4.0 con **Keypad** (1.ª generación) y sensor de puerta |
| Cámaras | Eufy de interior con giro (modelo T8410) y Eufy de exterior con foco (modelo T8424) |
| Servidor | Proxmox 8, contenedor LXC Debian 12 con Docker |

El panel pregunta a cada cerradura y cámara qué admite y solo muestra eso, así que otros modelos deberían funcionar, pero **no están probados**. Si lo usas con otro equipo, cuéntalo en una *issue* (funcione o no) y lo añadimos aquí.

## Colaborar

Toda ayuda es bienvenida: avisar de fallos, proponer ideas, mejorar textos o enviar código. Lee [CONTRIBUIR.md](CONTRIBUIR.md). Si encuentras un **fallo de seguridad**, no abras una *issue* pública: usa *Security → Report a vulnerability* en GitHub.

## Desde el terminal (dentro del servidor)

```bash
docker compose exec panel python nuki_codigos.py --cuenta principal entradas
docker compose exec panel python nuki_codigos.py --cuenta principal historial
docker exec -it puerta-panel python panel.py --recuperar USUARIO
```

## Licencia

[MIT](LICENSE): puedes usarlo, cambiarlo y compartirlo libremente, sin ninguna garantía.
