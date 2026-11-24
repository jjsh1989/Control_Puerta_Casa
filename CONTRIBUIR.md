# Cómo colaborar

¡Gracias por querer ayudar! Este proyecto nació para una casa concreta, así que cualquier aviso de fallo, idea o mejora lo hace útil para más gente.

## Avisar de un fallo o pedir una función

Abre una *issue* con la plantilla que corresponda. Ayuda mucho indicar:

- **Tu equipo:** modelo de cerradura Nuki, si tienes Keypad / Keypad 2 / Opener / bridge, y modelos de cámara Eufy.
- **Qué hiciste, qué esperabas y qué pasó.**
- **El registro**, si hay un error: `docker compose logs --tail 100 panel` (o `eufy`).

⚠️ **Antes de pegar nada, quita tus datos:** tokens, contraseñas, teléfonos, correos, números de serie de las cámaras y la dirección de tu panel. Nunca subas `config.json`, `datos/`, `secretos/` ni `.env`.

**Fallos de seguridad:** no los publiques en una *issue*. Usa *Security → Report a vulnerability* en GitHub para avisar en privado.

## Enviar cambios (pull request)

1. Haz un *fork* y crea una rama con un nombre que diga qué cambia (`arreglo-avisos-bateria`, `soporte-keypad-2`…).
2. Prueba el cambio en tu instalación. Si toca la cerradura o las cámaras, di con qué modelos lo probaste.
3. Explica en el *pull request* qué cambia y por qué. Mejor varios cambios pequeños que uno enorme.
4. Las **pruebas automáticas** se ejecutan solas en cada *pull request* (pestaña *Actions*); tienen que pasar.

### Pruebas automáticas

Están en `tests/` y comprueban, con datos ficticios y servicios simulados, lo que no puede fallar: que la renovación
no deje a nadie sin código (envío manual pendiente, fallo al enviar a una persona) y que las copias de seguridad
no queden a medias y sirvan para recuperar todo en una instalación nueva. No usan claves, ni la cerradura, ni internet:
cada prueba monta una instalación en una carpeta temporal y la borra al acabar.

En local, con Python 3.12:

```bash
pip install "cryptography==43.0.3" "websocket-client==1.8.0" "paho-mqtt==2.1.0"
python3 -m unittest discover -s tests -v
```

Para ver que una prueba detecta el fallo que corrige, se puede ejecutar contra otra versión del código
(por ejemplo, una anterior extraída con `git archive`): `PUERTA_CODIGO=/ruta/a/esa/version python3 -m unittest discover -s tests`.

Si añades un arreglo de algo que podría volver a romperse, añade su prueba: el escenario en `tests/escenarios.py`
(se ejecuta dentro de la instalación de prueba) y las comprobaciones en un `tests/test_*.py`.

### Cómo está hecho

- **Python 3 sin frameworks** (servidor HTTP propio en `panel.py`) y **una sola página** `panel.html` con HTML, CSS y JavaScript sin dependencias ni compilación. Se busca que siga siendo así: fácil de leer y de desplegar.
- **Lo que depende de cada cerradura o cámara se pregunta al equipo** (`nuki_extra.py` → `capacidades`, `camara.py` → `capacidades`) y la página solo muestra lo que el equipo admite. Las funciones nuevas deberían seguir esa idea en vez de suponer un modelo concreto.
- **Textos en castellano** (de España) en la interfaz y la documentación. Las *issues* y los *pull requests* pueden ser en inglés.
- **Nada de secretos en el código:** los tokens y claves se guardan cifrados en la bóveda (`cuentas.py`).
- Todo lo que se hace en el panel queda anotado en el historial (`nk.registrar`); las acciones nuevas deberían anotarse también.

## Ideas abiertas

- Probar y adaptar otros modelos (Keypad 2, Opener, Smart Lock Ultra/Pro, otras cámaras Eufy).
- Traducción de la interfaz a otros idiomas.
- Otros proveedores de mensajería o de cámaras.
