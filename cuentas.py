"""
Cuentas, usuarios y bóveda de secretos.

- Cada CUENTA es una cerradura con sus inquilinos, su historial y sus ajustes
  (carpeta datos/cuentas/<id>/).
- Cada USUARIO entra al panel con su contraseña y pertenece a una cuenta.
  El rol «admin» puede ver todas las cuentas, crear usuarios y controlar Docker.
- La BÓVEDA guarda los secretos (token de Nuki, claves de WhatsApp) cifrados,
  nunca en archivos de texto normales.
"""

import hashlib
import hmac
import json
import re
import secrets
import shutil
import subprocess
import threading
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

CARPETA = Path(__file__).resolve().parent
DATOS = CARPETA / "datos"
RUTA_USUARIOS = DATOS / "usuarios.json"
RUTA_SISTEMA = DATOS / "sistema.json"
CUENTA_PRINCIPAL = "principal"
CUENTA_SISTEMA = "_sistema"          # secretos comunes (p. ej. la clave de WAHA)
SERVICIO_LLAVERO = "Codigos-Puerta"
ITERACIONES = 600_000
_cerrojo = threading.RLock()

# ---------------------------------------------------------------- contexto

_ctx = threading.local()


def cuenta_actual():
    return getattr(_ctx, "cuenta", None) or CUENTA_PRINCIPAL


def usuario_actual():
    return getattr(_ctx, "usuario", None) or ""


@contextmanager
def en_cuenta(cuenta, usuario=None):
    """Hace que todo lo que se ejecute dentro use los datos de esa cuenta."""
    anterior = (getattr(_ctx, "cuenta", None), getattr(_ctx, "usuario", None))
    _ctx.cuenta = cuenta
    if usuario is not None:
        _ctx.usuario = usuario
    try:
        yield
    finally:
        _ctx.cuenta, _ctx.usuario = anterior


def _leer(ruta, por_defecto):
    if not ruta.exists():
        return por_defecto
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)


def _escribir(ruta, datos):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_suffix(ruta.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)
    tmp.replace(ruta)
    try:
        ruta.chmod(0o600)  # solo tu usuario del Mac puede leerlo
    except OSError:
        pass


def a_slug(texto, maximo=20):
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:maximo] or "cuenta"


# ---------------------------------------------------------------- cuentas

def carpeta_cuenta(cuenta=None):
    c = cuenta or cuenta_actual()
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", c):
        raise RuntimeError("Cuenta no válida.")
    carpeta = DATOS / "cuentas" / c
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta


def listar_cuentas():
    base = DATOS / "cuentas"
    if not base.exists():
        return []
    salida = []
    for d in sorted(base.iterdir()):
        if d.is_dir():
            meta = _leer(d / "cuenta.json", {})
            salida.append({"id": d.name, "nombre": meta.get("nombre", d.name)})
    return salida


def existe_cuenta(cuenta):
    return (DATOS / "cuentas" / cuenta).is_dir()


def crear_cuenta(nombre):
    with _cerrojo:
        base = a_slug(nombre)
        ident, n = base, 2
        while existe_cuenta(ident) or ident == CUENTA_SISTEMA:
            ident, n = f"{base}-{n}", n + 1
        _escribir(carpeta_cuenta(ident) / "cuenta.json",
                  {"nombre": nombre.strip(), "creada": datetime.now().isoformat(timespec="seconds")})
        return ident


def renombrar_cuenta(cuenta, nombre):
    ruta = carpeta_cuenta(cuenta) / "cuenta.json"
    meta = _leer(ruta, {})
    meta["nombre"] = nombre.strip()
    _escribir(ruta, meta)


# ---------------------------------------------------------------- bóveda cifrada
#
# Los secretos se guardan cifrados (AES, formato Fernet) en datos/boveda.cifrada.
# La clave maestra NO está junto a los datos: se lee de la variable CLAVE_MAESTRA_ARCHIVO
# (en Docker, un «secret» montado en /run/secrets/clave_maestra). Así, una copia de la
# carpeta datos/ no sirve de nada sin la clave.
# En un Mac sin la librería «cryptography» se usa el Llavero de macOS como respaldo.

import os
import sys

RUTA_BOVEDA = DATOS / "boveda.cifrada"


def _clave_maestra():
    ruta = os.environ.get("CLAVE_MAESTRA_ARCHIVO", "/run/secrets/clave_maestra")
    if os.path.exists(ruta):
        return Path(ruta).read_text().strip().encode()
    return None


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    clave = _clave_maestra()
    return Fernet(clave) if clave else None


def _usar_llavero():
    return _fernet() is None and sys.platform == "darwin"


def _boveda_cargar():
    f = _fernet()
    if f is None:
        raise RuntimeError("No hay clave maestra para la bóveda (falta /run/secrets/clave_maestra).")
    if not RUTA_BOVEDA.exists():
        return {}
    return json.loads(f.decrypt(RUTA_BOVEDA.read_bytes()).decode())


def _boveda_guardar_todo(datos):
    f = _fernet()
    RUTA_BOVEDA.parent.mkdir(parents=True, exist_ok=True)
    tmp = RUTA_BOVEDA.with_suffix(".tmp")
    tmp.write_bytes(f.encrypt(json.dumps(datos).encode()))
    tmp.replace(RUTA_BOVEDA)
    try:
        RUTA_BOVEDA.chmod(0o600)
    except OSError:
        pass


def boveda_guardar(clave, valor, cuenta=None):
    cuenta = cuenta or cuenta_actual()
    if _usar_llavero():
        r = subprocess.run(
            ["/usr/bin/security", "add-generic-password", "-U", "-s", SERVICIO_LLAVERO,
             "-a", f"{cuenta}:{clave}", "-l", f"Códigos de la puerta ({cuenta})", "-w", valor],
            capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            raise RuntimeError(f"No se pudo guardar en el Llavero de macOS: {r.stderr.strip()}")
        return
    with _cerrojo:
        datos = _boveda_cargar()
        datos.setdefault(cuenta, {})[clave] = valor
        _boveda_guardar_todo(datos)


def boveda_leer(clave, cuenta=None):
    cuenta = cuenta or cuenta_actual()
    if _usar_llavero():
        r = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", SERVICIO_LLAVERO,
             "-a", f"{cuenta}:{clave}", "-w"], capture_output=True, text=True, timeout=20)
        return r.stdout.rstrip("\n") if r.returncode == 0 else ""
    with _cerrojo:
        return _boveda_cargar().get(cuenta, {}).get(clave, "")


def boveda_borrar(clave, cuenta=None):
    cuenta = cuenta or cuenta_actual()
    if _usar_llavero():
        subprocess.run(["/usr/bin/security", "delete-generic-password", "-s", SERVICIO_LLAVERO,
                        "-a", f"{cuenta}:{clave}"], capture_output=True, timeout=20)
        return
    with _cerrojo:
        datos = _boveda_cargar()
        if datos.get(cuenta, {}).pop(clave, None) is not None:
            _boveda_guardar_todo(datos)


def boveda_exportar_cuenta(cuenta):
    """Todos los secretos de una cuenta (para trasladarla a otro servidor)."""
    if _usar_llavero():
        claves = ["nuki_token", "twilio_token", "waha_api_key"]
        cfg = _leer(carpeta_cuenta(cuenta) / "config.json", {}) if cuenta != CUENTA_SISTEMA else {}
        claves += [f"callmebot:{d['id']}" for d in cfg.get("destinatarios", [])]
        return {c: v for c in claves if (v := boveda_leer(c, cuenta))}
    with _cerrojo:
        return dict(_boveda_cargar().get(cuenta, {}))


# ---------------------------------------------------------------- sistema (ajustes comunes)

def leer_sistema():
    return _leer(RUTA_SISTEMA, {})


def guardar_sistema(datos):
    _escribir(RUTA_SISTEMA, datos)


# ---------------------------------------------------------------- usuarios

def _hash(password, sal=None, iteraciones=ITERACIONES):
    sal = sal or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(sal), iteraciones).hex()
    return {"hash": h, "sal": sal, "iter": iteraciones}


def validar_password(password):
    if len(password or "") < 10:
        raise RuntimeError("La contraseña debe tener al menos 10 caracteres.")


def leer_usuarios():
    return _leer(RUTA_USUARIOS, {"usuarios": []})["usuarios"]


def _guardar_usuarios(lista):
    _escribir(RUTA_USUARIOS, {"usuarios": lista})


def hay_usuarios():
    return bool(leer_usuarios())


def _nueva_generacion():
    """Marca aleatoria de las sesiones de un usuario. Al cambiarla, todas sus sesiones abiertas dejan de valer
    (también tras reiniciar el panel o si el cambio se hizo desde la consola en otro proceso)."""
    return secrets.token_hex(8)


def generacion_sesiones(u):
    return (u or {}).get("sesiones_gen", "")


def buscar_usuario(usuario):
    usuario = (usuario or "").strip().lower()
    return next((u for u in leer_usuarios() if u["usuario"] == usuario), None)


def crear_usuario(usuario, nombre, password, rol, cuenta):
    usuario = (usuario or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,30}", usuario):
        raise RuntimeError("El usuario debe tener de 3 a 30 letras, números, puntos o guiones (sin espacios ni tildes).")
    if rol not in ("admin", "gestor"):
        raise RuntimeError("Rol no válido.")
    if not existe_cuenta(cuenta):
        raise RuntimeError("La cuenta no existe.")
    validar_password(password)
    with _cerrojo:
        lista = leer_usuarios()
        if any(u["usuario"] == usuario for u in lista):
            raise RuntimeError("Ese nombre de usuario ya existe.")
        lista.append({"usuario": usuario, "nombre": (nombre or usuario).strip(), "rol": rol,
                      "cuenta": cuenta, "creado": datetime.now().isoformat(timespec="seconds"),
                      "sesiones_gen": _nueva_generacion(), **_hash(password)})
        _guardar_usuarios(lista)


def verificar_password(usuario, password):
    u = buscar_usuario(usuario)
    if not u:
        _hash(password or "x")  # mismo tiempo de respuesta exista o no el usuario
        return None
    calculado = _hash(password or "", u["sal"], u["iter"])["hash"]
    return u if hmac.compare_digest(calculado, u["hash"]) else None


def cambiar_password(usuario, nueva):
    """Cambia la contraseña y anula todas las sesiones del usuario. Devuelve la marca nueva de sesiones."""
    validar_password(nueva)
    with _cerrojo:
        lista = leer_usuarios()
        for u in lista:
            if u["usuario"] == usuario:
                u.update(_hash(nueva))
                u["sesiones_gen"] = _nueva_generacion()
                _guardar_usuarios(lista)
                return u["sesiones_gen"]
    raise RuntimeError("Usuario no encontrado.")


# ---------------------------------------------------------------- PIN para abrir desde Telegram

_FALLOS_PIN = {}  # usuario -> [fallos seguidos, bloqueado hasta (epoch)]
MAX_FALLOS_PIN, BLOQUEO_PIN = 3, 15 * 60


def validar_pin(pin):
    pin = (pin or "").strip()
    if not re.fullmatch(r"\d{4,8}", pin):
        raise RuntimeError("El PIN tiene que tener de 4 a 8 cifras.")
    if len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
        raise RuntimeError("Ese PIN es demasiado fácil de adivinar (repetido o seguido). Elige otro.")
    return pin


def poner_pin(usuario, pin):
    pin = validar_pin(pin)
    with _cerrojo:
        lista = leer_usuarios()
        for u in lista:
            if u["usuario"] == usuario:
                u["pin_tg"] = _hash(pin)
                _guardar_usuarios(lista)
                _FALLOS_PIN.pop(usuario, None)
                return
    raise RuntimeError("Usuario no encontrado.")


def quitar_pin(usuario):
    with _cerrojo:
        lista = leer_usuarios()
        for u in lista:
            if u["usuario"] == usuario:
                u.pop("pin_tg", None)
                _guardar_usuarios(lista)
                return


def tiene_pin(usuario):
    u = buscar_usuario(usuario)
    return bool(u and u.get("pin_tg"))


def comprobar_pin(usuario, pin):
    """Devuelve (correcto, mensaje). Tras 3 fallos seguidos bloquea 15 minutos."""
    import time
    u = buscar_usuario(usuario)
    if not u or not u.get("pin_tg"):
        return False, "No tienes PIN puesto. Ponlo en el panel: Ajustes → Comunicación → «La puerta desde Telegram»."
    fallos, hasta = _FALLOS_PIN.get(usuario, [0, 0])
    if hasta > time.time():
        return False, f"Demasiados intentos. Prueba de nuevo dentro de {int((hasta - time.time()) // 60) + 1} min."
    p = u["pin_tg"]
    if hmac.compare_digest(_hash((pin or "").strip(), p["sal"], p["iter"])["hash"], p["hash"]):
        _FALLOS_PIN.pop(usuario, None)
        return True, ""
    fallos += 1
    if fallos >= MAX_FALLOS_PIN:
        _FALLOS_PIN[usuario] = [0, time.time() + BLOQUEO_PIN]
        return False, "PIN incorrecto. Bloqueado 15 minutos por seguridad."
    _FALLOS_PIN[usuario] = [fallos, 0]
    return False, f"PIN incorrecto. Te quedan {MAX_FALLOS_PIN - fallos} intentos."


def modificar_usuario(usuario, **cambios):
    """Si cambia el rol o la cuenta (vivienda), anula todas las sesiones del usuario. Devuelve la marca de sesiones."""
    with _cerrojo:
        lista = leer_usuarios()
        for u in lista:
            if u["usuario"] == usuario:
                if cambios.get("cuenta") is not None and not existe_cuenta(cambios["cuenta"]):
                    raise RuntimeError("La cuenta no existe.")
                if cambios.get("rol") not in (None, "admin", "gestor"):
                    raise RuntimeError("Rol no válido.")
                antes = (u.get("rol"), u.get("cuenta"))
                u.update({k: v for k, v in cambios.items() if v is not None})
                if not any(x["rol"] == "admin" for x in lista):
                    raise RuntimeError("Tiene que quedar al menos un administrador.")
                if (u.get("rol"), u.get("cuenta")) != antes:
                    u["sesiones_gen"] = _nueva_generacion()
                _guardar_usuarios(lista)
                return generacion_sesiones(u)
    raise RuntimeError("Usuario no encontrado.")


def borrar_usuario(usuario):
    with _cerrojo:
        lista = [u for u in leer_usuarios() if u["usuario"] != usuario]
        if not any(u["rol"] == "admin" for u in lista):
            raise RuntimeError("No puedes borrar al último administrador.")
        _guardar_usuarios(lista)


def usuarios_publicos():
    return [{k: u[k] for k in ("usuario", "nombre", "rol", "cuenta", "creado")} for u in leer_usuarios()]


# ---------------------------------------------------------------- migración desde la versión de una sola cuenta

def migrar_si_hace_falta(log=print):
    """Mueve config.json, estado.json y gestion.jsonl de la carpeta principal a
    datos/cuentas/principal/ y pasa los secretos al Llavero."""
    origen = next((d for d in (DATOS / "importar", CARPETA) if (d / "config.json").exists()), None)
    if origen is None:
        return False
    viejo = origen / "config.json"
    destino = DATOS / "cuentas" / CUENTA_PRINCIPAL
    if not viejo.exists() or (destino / "config.json").exists():
        return False
    destino.mkdir(parents=True, exist_ok=True)
    cfg = _leer(viejo, {})
    # secretos -> bóveda
    token = cfg.get("nuki", {}).pop("token", "")
    if token:
        boveda_guardar("nuki_token", token, CUENTA_PRINCIPAL)
    tw = cfg.get("whatsapp", {}).get("twilio", {})
    if tw.get("auth_token"):
        boveda_guardar("twilio_token", tw.pop("auth_token"), CUENTA_PRINCIPAL)
    waha = cfg.get("whatsapp", {}).pop("waha", None)
    if waha:
        if waha.get("api_key"):
            boveda_guardar("waha_api_key", waha["api_key"], CUENTA_SISTEMA)
        sistema = leer_sistema()
        sistema["waha"] = {"url": waha.get("url", "http://127.0.0.1:3000"),
                           "sesion": waha.get("sesion", "default")}
        guardar_sistema(sistema)
    for d in cfg.get("destinatarios", []):
        if d.get("callmebot_apikey"):
            boveda_guardar(f"callmebot:{d['id']}", d.pop("callmebot_apikey"), CUENTA_PRINCIPAL)
    _escribir(destino / "config.json", cfg)
    if not (destino / "cuenta.json").exists():
        _escribir(destino / "cuenta.json", {"nombre": "Mi casa",
                                            "creada": datetime.now().isoformat(timespec="seconds")})
    for nombre in ("estado.json", "gestion.jsonl"):
        if (origen / nombre).exists():
            shutil.move(str(origen / nombre), str(destino / nombre))
    viejo.rename(origen / "config.json.migrado")
    # el archivo antiguo aún tenía el token en claro: se vacía
    copia = _leer(origen / "config.json.migrado", {})
    copia.get("nuki", {}).pop("token", None)
    copia.get("whatsapp", {}).pop("waha", None)
    copia.get("whatsapp", {}).get("twilio", {}).pop("auth_token", None)
    _escribir(origen / "config.json.migrado", copia)
    log("Datos importados en la cuenta principal; los secretos se han guardado en la bóveda.")
    return True
