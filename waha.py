"""
Control de WAHA (WhatsApp HTTP API) cuando todo corre con docker compose.

- WAHA es un servicio del mismo docker compose (no está expuesto fuera).
- El panel controla el contenedor de WAHA a través de «docker-socket-proxy»,
  configurado para permitir SOLO ver estado/registro y arrancar, parar o
  reiniciar contenedores. No puede crear ni borrar nada.
- Todo cambio queda anotado en el historial de la cuenta principal.

WAHA no es oficial de WhatsApp: úsalo con pocos mensajes y a contactos conocidos.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import cuentas
import nuki_codigos as nk

WAHA_URL = os.environ.get("WAHA_URL", "http://waha:3000")
WAHA_API_KEY = os.environ.get("WAHA_API_KEY", "")
WAHA_SESION = os.environ.get("WAHA_SESION", "default")
CONTENEDOR = os.environ.get("WAHA_CONTENEDOR", "puerta-waha")
DOCKER_PROXY = os.environ.get("DOCKER_PROXY", "http://docker-proxy:2375")


def _cat(accion, detalle="", ok=True):
    """Anota en el historial de la cuenta principal (allí se ve el estado del sistema)."""
    with cuentas.en_cuenta(cuentas.CUENTA_PRINCIPAL):
        nk.registrar(accion, "Docker / WhatsApp", detalle, ok=ok)


# ---------------------------------------------------------------- HTTP

def _http(metodo, url, cuerpo=None, cabeceras=None, timeout=20, binario=False):
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    cab = {"Content-Type": "application/json", **(cabeceras or {})}
    req = urllib.request.Request(url, data=datos, method=metodo, headers=cab)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            contenido = r.read()
            if binario:
                return r.status, contenido
            texto = contenido.decode(errors="replace")
            try:
                return r.status, json.loads(texto) if texto.strip() else {}
            except ValueError:
                return r.status, texto
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"No responde ({getattr(e, 'reason', e)})") from None


# ---------------------------------------------------------------- contenedor (vía docker-socket-proxy)

def estado_contenedor():
    """'running', 'exited', 'restarting'... o 'no-existe' / 'sin-acceso'."""
    try:
        codigo, r = _http("GET", f"{DOCKER_PROXY}/containers/{CONTENEDOR}/json", timeout=5)
    except RuntimeError:
        return "sin-acceso"
    if codigo == 404:
        return "no-existe"
    if codigo != 200 or not isinstance(r, dict):
        return "sin-acceso"
    return r.get("State", {}).get("Status", "desconocido")


def info_contenedor():
    try:
        codigo, r = _http("GET", f"{DOCKER_PROXY}/containers/{CONTENEDOR}/json", timeout=5)
    except RuntimeError:
        return {}
    if codigo != 200 or not isinstance(r, dict):
        return {}
    st = r.get("State", {})
    return {"iniciado": st.get("StartedAt"), "reinicios": r.get("RestartCount", 0),
            "imagen": r.get("Config", {}).get("Image")}


def accion_contenedor(accion, contenedor=None):
    """arrancar | parar | reiniciar  (por defecto el contenedor de WAHA)"""
    rutas = {"arrancar": "start", "parar": "stop", "reiniciar": "restart"}
    if accion not in rutas:
        raise RuntimeError(f"Acción no permitida: {accion}")
    if contenedor and contenedor != CONTENEDOR:
        codigo, r = _http("POST", f"{DOCKER_PROXY}/containers/{contenedor}/{rutas[accion]}", timeout=90)
        if codigo not in (204, 304):
            raise RuntimeError(f"Docker respondió {codigo}: {str(r)[:300]}")
        return
    codigo, r = _http("POST", f"{DOCKER_PROXY}/containers/{CONTENEDOR}/{rutas[accion]}", timeout=90)
    if codigo not in (204, 304):
        _cat(f"Error al {accion} WAHA", str(r)[:300], ok=False)
        raise RuntimeError(f"Docker respondió {codigo}: {str(r)[:300]}")
    _cat({"arrancar": "WAHA arrancado", "parar": "WAHA parado", "reiniciar": "WAHA reiniciado"}[accion])


def logs_contenedor(lineas=200):
    codigo, crudo = _http("GET", f"{DOCKER_PROXY}/containers/{CONTENEDOR}/logs?"
                          f"stdout=1&stderr=1&tail={int(lineas)}", timeout=15, binario=True)
    if codigo != 200:
        return f"No se pudo leer el registro (código {codigo})."
    # el flujo de Docker lleva 8 bytes de cabecera por bloque: se quitan
    salida, i = [], 0
    while i + 8 <= len(crudo):
        largo = int.from_bytes(crudo[i + 4:i + 8], "big")
        salida.append(crudo[i + 8:i + 8 + largo])
        i += 8 + largo
    texto = b"".join(salida).decode(errors="replace") if salida else crudo.decode(errors="replace")
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", texto)  # quita colores


# ---------------------------------------------------------------- API de WAHA

def _api(metodo, ruta, cuerpo=None, binario=False, timeout=20):
    return _http(metodo, WAHA_URL.rstrip("/") + ruta, cuerpo,
                 {"X-Api-Key": WAHA_API_KEY, "Accept": "image/png" if binario else "application/json"},
                 timeout=timeout, binario=binario)


def estado_sesion():
    """STOPPED, STARTING, SCAN_QR_CODE, WORKING, FAILED o NO_RESPONDE."""
    try:
        codigo, r = _api("GET", f"/api/sessions/{WAHA_SESION}", timeout=5)
    except RuntimeError:
        return {"estado": "NO_RESPONDE"}
    if codigo == 404:
        return {"estado": "STOPPED"}
    if codigo == 401:
        return {"estado": "CLAVE_INCORRECTA"}
    if isinstance(r, dict):
        me = r.get("me") or {}
        return {"estado": r.get("status", "DESCONOCIDO"),
                "numero": (me.get("id") or "").split("@")[0], "nombre": me.get("pushName")}
    return {"estado": "DESCONOCIDO"}


def iniciar_sesion():
    codigo, r = _api("POST", f"/api/sessions/{WAHA_SESION}/start")
    if codigo == 404:
        codigo, r = _api("POST", "/api/sessions", {"name": WAHA_SESION, "start": True})
    if codigo >= 400 and "already" not in str(r).lower():
        _cat("Error al iniciar la sesión de WhatsApp", str(r)[:300], ok=False)
        raise RuntimeError(f"WAHA respondió {codigo}: {str(r)[:300]}")
    _cat("Sesión de WhatsApp iniciada")


def cerrar_sesion():
    _api("POST", f"/api/sessions/{WAHA_SESION}/logout")
    _cat("WhatsApp desvinculado (habrá que volver a escanear el QR)")


def qr_png():
    codigo, contenido = _api("GET", f"/api/{WAHA_SESION}/auth/qr?format=image", binario=True)
    if codigo != 200:
        raise RuntimeError("El QR no está disponible ahora mismo. Espera unos segundos.")
    return contenido


def estado_completo():
    cont = estado_contenedor()
    datos = {"contenedor": cont, "info": info_contenedor(), "sesion": {"estado": "NO_RESPONDE"}}
    if cont == "running":
        datos["sesion"] = estado_sesion()
    return datos


def asegurar_listo(esperar=90):
    """Antes de enviar: arranca WAHA y la sesión si hace falta, y lo anota."""
    est = estado_contenedor()
    if est not in ("running", "sin-acceso", "no-existe"):
        _cat("WAHA estaba parado: arrancándolo", f"Estado anterior: {est}")
        accion_contenedor("arrancar")
    limite = time.time() + esperar
    intentado = False
    while time.time() < limite:
        s = estado_sesion()["estado"]
        if s == "WORKING":
            return
        if s == "SCAN_QR_CODE":
            _cat("WhatsApp desvinculado: hay que escanear el QR", ok=False)
            raise RuntimeError("WhatsApp necesita que se vuelva a escanear el QR (pestaña WhatsApp).")
        if s in ("STOPPED", "FAILED") and not intentado:
            intentado = True
            try:
                iniciar_sesion()
            except RuntimeError:
                pass
        time.sleep(3)
    _cat("WAHA no quedó listo a tiempo", ok=False)
    raise RuntimeError("El WhatsApp automático (WAHA) no está listo. Avisa al administrador.")


def enviar(config, telefono, texto):
    asegurar_listo()
    numero = telefono.replace(" ", "").lstrip("+")
    codigo, r = _api("POST", "/api/sendText",
                     {"session": WAHA_SESION, "chatId": f"{numero}@c.us", "text": texto}, timeout=60)
    if codigo >= 400:
        raise RuntimeError(f"WAHA no pudo enviar el mensaje ({codigo}): {str(r)[:300]}")


# ---------------------------------------------------------------- vigilancia

class Vigilante(threading.Thread):
    """Cada minuto comprueba WAHA y anota en el historial los cambios de estado."""

    def __init__(self, intervalo=60):
        super().__init__(daemon=True)
        self.intervalo = intervalo
        self.anterior = None

    def run(self):
        while True:
            try:
                e = estado_completo()
                actual = (e["contenedor"], e["sesion"]["estado"])
                if actual != self.anterior:
                    if self.anterior is None:
                        _cat("Panel iniciado", f"WAHA: {actual[0]} · WhatsApp: {actual[1]}",
                             ok=actual[1] == "WORKING")
                    else:
                        _cat("Cambio de estado", f"WAHA: {actual[0]} · WhatsApp: {actual[1]}",
                             ok=actual[1] == "WORKING")
                    self.anterior = actual
            except Exception:
                pass
            time.sleep(self.intervalo)
