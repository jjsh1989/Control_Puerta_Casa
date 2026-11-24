#!/usr/bin/env python3
"""
Generador de códigos de acceso semanales para cerraduras Nuki (Keypad)
con envío por WhatsApp.

Uso:
  python3 nuki_codigos.py cerraduras                 # lista tus cerraduras y su ID
  python3 nuki_codigos.py listar                     # lista los códigos del teclado
  python3 nuki_codigos.py nuevo --para maria         # código nuevo (manual) para un destinatario
  python3 nuki_codigos.py rotar                      # rotación semanal automática
  python3 nuki_codigos.py probar-whatsapp --para maria

Añade --simular a cualquier orden para ver qué haría sin tocar la cerradura
ni enviar mensajes.

Con varias cuentas, añade --cuenta <id> (por defecto «principal»). «rotar» sin
--cuenta renueva todas las cuentas.
"""

import argparse
import base64
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cuentas

CARPETA = Path(__file__).resolve().parent
RUTA_LOG = cuentas.DATOS / "registro.log"


def ruta_config():
    return cuentas.carpeta_cuenta() / "config.json"


def ruta_estado():
    return cuentas.carpeta_cuenta() / "estado.json"


def ruta_gestion():
    """Historial de gestión de la cuenta actual (una línea JSON por acción)."""
    return cuentas.carpeta_cuenta() / "gestion.jsonl"

NUKI_API = "https://api.nuki.io"
TIPO_CODIGO_TECLADO = 13  # tipo de autorización "Keypad code" en la API de Nuki
CLAVE_INVITADOS = "_invitados"  # dentro de estado.json
CLAVE_RENOVACIONES = "_renovaciones"  # renovaciones aún sin entregar, por persona (dentro de estado.json)
REINTENTOS_AUTOMATICOS = 6

# Quién lanza las acciones (se anota en el historial): "terminal", "panel" o "semanal"
ORIGEN = "terminal"

MENSAJE_FIJO = ("Hola {nombre}, tu código de acceso a la puerta es {codigo}. "
                "Es personal: por favor, no lo compartas.")

MENSAJE_INVITADO = ("Código de acceso temporal: {codigo}. Válido hasta el {hasta}. "
                    "Es solo para esta visita: por favor, no lo compartas.")


# ---------------------------------------------------------------- utilidades

OYENTES_REGISTRO = []  # funciones que se enteran de cada entrada del historial (p. ej. Home Assistant)

# Lo que nunca debe quedar en el historial ni en el log: códigos de puerta (6 cifras del 1 al 9),
# enlaces con el mensaje o claves en la dirección (wa.me?text=…, callmebot…?apikey=…) y tokens de bots.
_RE_CODIGO = re.compile(r"(?<!\d)[1-9]{6}(?!\d)")
_RE_URL_CON_DATOS = re.compile(r"(https?://[^\s?#]+)[?#][^\s]*")
_RE_CLAVE = re.compile(r"(?i)\b(apikey|api_key|token|auth_token|key)=[^\s&]+")
_RE_TOKEN_BOT = re.compile(r"\b\d{6,}:[\w-]{30,}\b")


def sanear(texto):
    """Quita de un texto códigos de puerta, enlaces con datos y claves. Se puede aplicar varias veces."""
    if not isinstance(texto, str) or not texto:
        return texto
    texto = _RE_URL_CON_DATOS.sub(r"\1?…", texto)
    texto = _RE_CLAVE.sub(r"\1=…", texto)
    texto = _RE_TOKEN_BOT.sub("…", texto)
    return _RE_CODIGO.sub("••••••", texto)


def registrar(accion, usuario="", detalle="", simulado=False, ok=True):
    """Añade una entrada al historial de gestión: quién, qué, cuándo y si salió bien.
    Nunca el código de la puerta ni el texto o el enlace del mensaje (se sanea por si acaso)."""
    entrada = {
        "fecha": datetime.now().astimezone().isoformat(timespec="seconds"),
        "accion": sanear(accion), "usuario": sanear(usuario), "detalle": sanear(detalle),
        "origen": getattr(cuentas._ctx, "origen", None) or ORIGEN,
        "gestor": cuentas.usuario_actual(), "simulado": simulado, "ok": ok,
    }
    try:
        with open(ruta_gestion(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entrada, ensure_ascii=False) + "\n")
    except OSError:
        pass
    for oyente in OYENTES_REGISTRO:
        try:
            oyente(entrada)
        except Exception:
            pass


_ACCIONES_MENSAJE = ("Telegram enviado", "WhatsApp enviado", "WhatsApp preparado")
VERSION_SANEADO = 1


def _reescribir(ruta, lineas):
    tmp = ruta.with_name(ruta.name + ".saneando")
    tmp.write_text("".join(lineas), encoding="utf-8")
    try:
        os.chmod(tmp, ruta.stat().st_mode & 0o777)
    except OSError:
        pass
    tmp.replace(ruta)


def sanear_registros():
    """Quita códigos de puerta, textos de mensajes, enlaces y claves del historial de cada casa y del log.
    Conserva fecha, acción, quién y resultado. Repetirlo no cambia nada. Devuelve cuántas líneas cambió."""
    cambiadas = 0
    for c in cuentas.listar_cuentas():
        ruta = cuentas.carpeta_cuenta(c["id"]) / "gestion.jsonl"
        if not ruta.exists():
            continue
        nuevas, cambio = [], False
        for linea in ruta.read_text(encoding="utf-8").splitlines(keepends=True):
            try:
                e = json.loads(linea)
            except ValueError:
                nuevas.append(sanear(linea)); cambio |= nuevas[-1] != linea
                continue
            antes = dict(e)
            for k in ("accion", "usuario", "detalle"):
                e[k] = sanear(e.get(k, ""))
            if str(e.get("accion", "")).startswith(_ACCIONES_MENSAJE):
                e["detalle"] = ""  # era el texto del mensaje
            if e != antes:
                cambio = True; cambiadas += 1
                linea = json.dumps(e, ensure_ascii=False) + "\n"
            nuevas.append(linea)
        if cambio:
            _reescribir(ruta, nuevas)
    if RUTA_LOG.exists():
        lineas = RUTA_LOG.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        nuevas = [sanear(l) for l in lineas]
        n = sum(1 for x, y in zip(lineas, nuevas) if x != y)
        if n:
            _reescribir(RUTA_LOG, nuevas)
            cambiadas += n
    return cambiadas


def sanear_registros_si_hace_falta():
    """Al arrancar: sanea una vez los registros guardados por versiones anteriores."""
    s = cuentas.leer_sistema()
    if s.get("registros_saneados", 0) >= VERSION_SANEADO:
        return
    n = sanear_registros()
    s = cuentas.leer_sistema()
    s["registros_saneados"] = VERSION_SANEADO
    cuentas.guardar_sistema(s)
    if n:
        log(f"Registros: retirados códigos y mensajes de {n} líneas antiguas")


def _lineas_al_reves(ruta, antes=None, bloque=65536):
    """Líneas de un fichero de la última a la primera, como (posición donde empieza, bytes).
    Lee por bloques desde el final: no carga el fichero entero. Con «antes», empieza por la línea
    que termina justo antes de esa posición."""
    with open(ruta, "rb") as f:
        f.seek(0, os.SEEK_END)
        fin = f.tell() if antes is None else max(0, min(int(antes), f.tell()))
        resto, pos = b"", fin
        while pos > 0:
            leer = min(bloque, pos)
            pos -= leer
            f.seek(pos)
            trozo = f.read(leer) + resto
            partes = trozo.split(b"\n")
            resto = partes[0]               # puede ser una línea a medias: se completa con el bloque anterior
            inicio = pos + len(trozo)
            for linea in reversed(partes[1:]):
                inicio -= len(linea) + 1
                if linea.strip():
                    yield inicio + 1, linea
        if resto.strip():
            yield 0, resto


def leer_gestion_pagina(limite=50, antes=None, aceptar=None):
    """Una página del historial de gestión, de la entrada más nueva a la más antigua.
    «antes» es la marca que devolvió la página anterior (posición en el fichero): como las entradas
    nuevas se añaden al final, pasar de página no salta ni repite nada aunque entren entradas nuevas.
    «aceptar(entrada)» puede filtrar (devuelve None) o transformar cada entrada. Si además tiene un
    atributo «previo(texto de la línea)», las líneas que este rechace ni se llegan a leer como JSON.
    Devuelve (entradas, marca de la siguiente página o None si no hay más)."""
    ruta = ruta_gestion()
    if not ruta.exists():
        return [], None
    salida, previo = [], getattr(aceptar, "previo", None)
    for inicio, linea in _lineas_al_reves(ruta, antes):
        if previo is not None and not previo(linea.decode("utf-8", "replace")):
            continue
        try:
            e = json.loads(linea)
        except ValueError:
            continue
        if aceptar is not None:
            e = aceptar(e)
            if e is None:
                continue
        salida.append(e)
        if len(salida) >= limite:
            return salida, (inicio if inicio > 0 else None)
    return salida, None


def leer_gestion(limite=500):
    """Últimas entradas del historial de gestión, de la más nueva a la más antigua."""
    return leer_gestion_pagina(limite)[0]


def log(mensaje):
    linea = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {sanear(str(mensaje))}"
    print(linea)
    try:
        RUTA_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(RUTA_LOG, "a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except OSError:
        pass


def cargar_json(ruta, por_defecto):
    if not ruta.exists():
        return por_defecto
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)


def guardar_json(ruta, datos):
    cuentas._escribir(ruta, datos)


CONFIG_INICIAL = {
    "nuki": {"token": "", "smartlock_id": 0},
    "dias_validez": 7,
    "prefijo_nombre": "Auto",
    "mensaje": "Hola {nombre}, tu nuevo código de acceso a la puerta es {codigo}. "
               "Es válido hasta el {hasta}.",
    "whatsapp": {"proveedor": "enlace",
                 "twilio": {"account_sid": "", "auth_token": "", "numero_origen": ""}},
    "rotacion": {"activa": True, "dia": 0, "hora": 9, "minuto": 0},  # lunes 09:00
    "destinatarios": [],
}


def cargar_config():
    """Configuración de la cuenta actual, con los secretos sacados de la bóveda."""
    cfg = cargar_json(ruta_config(), None) or json.loads(json.dumps(CONFIG_INICIAL))
    for clave, valor in CONFIG_INICIAL.items():
        cfg.setdefault(clave, json.loads(json.dumps(valor)))
    cfg["whatsapp"].setdefault("twilio", dict(CONFIG_INICIAL["whatsapp"]["twilio"]))
    cfg["nuki"]["token"] = cuentas.boveda_leer("nuki_token")
    cfg["whatsapp"]["twilio"]["auth_token"] = cuentas.boveda_leer("twilio_token")
    for d in cfg["destinatarios"]:
        d["callmebot_apikey"] = cuentas.boveda_leer(f"callmebot:{d['id']}")
    return cfg


def guardar_config(cfg):
    """Guarda la configuración: los secretos van a la bóveda, el resto a config.json."""
    copia = json.loads(json.dumps(cfg))
    secretos = {
        "nuki_token": copia["nuki"].pop("token", ""),
        "twilio_token": copia.get("whatsapp", {}).get("twilio", {}).pop("auth_token", ""),
    }
    for d in copia.get("destinatarios", []):
        secretos[f"callmebot:{d['id']}"] = d.pop("callmebot_apikey", "")
    copia.get("whatsapp", {}).pop("waha", None)
    for clave, valor in secretos.items():
        if valor:
            if cuentas.boveda_leer(clave) != valor:
                cuentas.boveda_guardar(clave, valor)
    guardar_json(ruta_config(), copia)


_CONTEXTO_SSL = None


def contexto_ssl():
    """Contexto SSL que funciona aunque el Python de python.org no traiga certificados:
    si hace falta, usa los certificados raíz del propio macOS."""
    global _CONTEXTO_SSL
    if _CONTEXTO_SSL is not None:
        return _CONTEXTO_SSL
    import ssl
    import subprocess
    import time
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs():
        pem = CARPETA / "certificados_macos.pem"
        try:
            if not pem.exists() or time.time() - pem.stat().st_mtime > 30 * 86400:
                r = subprocess.run(
                    ["/usr/bin/security", "find-certificate", "-a", "-p",
                     "/System/Library/Keychains/SystemRootCertificates.keychain"],
                    capture_output=True, text=True, timeout=30)
                if r.returncode == 0 and "BEGIN CERTIFICATE" in r.stdout:
                    pem.write_text(r.stdout)
            if pem.exists():
                ctx.load_verify_locations(cafile=str(pem))
        except Exception as e:
            log(f"No se pudieron cargar los certificados de macOS: {e}")
    _CONTEXTO_SSL = ctx
    return ctx


def peticion(metodo, url, cuerpo=None, cabeceras=None, formulario=False):
    """Hace una petición HTTP y devuelve (código_estado, respuesta_decodificada)."""
    cabeceras = dict(cabeceras or {})
    datos = None
    if cuerpo is not None:
        if formulario:
            datos = urllib.parse.urlencode(cuerpo).encode()
            cabeceras["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            datos = json.dumps(cuerpo).encode()
            cabeceras["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=datos, method=metodo, headers=cabeceras)
    try:
        with urllib.request.urlopen(req, timeout=30, context=contexto_ssl()) as r:
            texto = r.read().decode() or ""
            return r.status, (json.loads(texto) if texto.strip().startswith(("{", "[")) else texto)
    except urllib.error.HTTPError as e:
        detalle = e.read().decode(errors="replace")
        raise RuntimeError(f"Error {e.code} en {metodo} {sanear(url)}: {sanear(detalle)[:500]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"No se pudo conectar con {sanear(url)}: {e.reason}") from None


DIAS_CORTOS = ["L", "M", "X", "J", "V", "S", "D"]


def _min(hhmm):
    h, m = (str(hhmm or "0:0") + ":0").split(":")[:2]
    return (int(h) % 24) * 60 + int(m) % 60


def validar_franja(f):
    """Horario en que un código funciona: {"dias": [0..6] (0 = lunes), "desde": "HH:MM", "hasta": "HH:MM"}.
    Devuelve la franja limpia o None (= siempre)."""
    if not f or not f.get("activa"):
        return None
    dias = sorted({int(x) % 7 for x in (f.get("dias") or [])})
    if not dias:
        raise RuntimeError("Horario: marca al menos un día.")
    desde, hasta = _min(f.get("desde") or "00:00"), _min(f.get("hasta") or "00:00")
    if hasta == 0 and desde == 0:
        pass  # todo el día, solo esos días
    elif desde >= hasta:
        raise RuntimeError("Horario: la hora de fin debe ser posterior a la de inicio (la cerradura no admite franjas que crucen la medianoche).")
    return {"activa": True, "dias": dias, "desde": f"{desde // 60:02d}:{desde % 60:02d}",
            "hasta": f"{hasta // 60:02d}:{hasta % 60:02d}"}


def franja_nuki(f):
    """(allowedWeekDays, allowedFromTime, allowedUntilTime) para la API de Nuki.
    Días: lunes = 64, martes = 32 … domingo = 1. Horas en minutos desde las 00:00 (0 y 0 = todo el día)."""
    if not f or not f.get("activa"):
        return 127, 0, 0
    mascara = sum(64 >> d for d in f["dias"])
    return mascara, _min(f["desde"]), _min(f["hasta"])


def texto_dias(d):
    d = sorted(d)
    if d == list(range(7)):
        return "todos los días"
    if d == list(range(5)):
        return "de lunes a viernes"
    if d == [5, 6]:
        return "sábados y domingos"
    return " ".join(DIAS_CORTOS[x] for x in d)


def texto_franja(f):
    if not f or not f.get("activa"):
        return ""
    dias = texto_dias(f["dias"])
    horas = "a cualquier hora" if f["desde"] == f["hasta"] == "00:00" else f"de {f['desde']} a {f['hasta']}"
    return f"{dias}, {horas}"


def aplicar_franja(cfg, persona):
    """Si la persona ya tiene código en la cerradura, le cambia el horario sin cambiar el número."""
    if not (cfg["nuki"].get("token") and cfg["nuki"].get("smartlock_id")):
        return
    nuki = Nuki(cfg["nuki"]["token"])
    sid = cfg["nuki"]["smartlock_id"]
    nombre_auth = nombre_auth_de(cfg, persona)
    dias, de, a = franja_nuki(persona.get("franja"))
    for auth in nuki.autorizaciones(sid):
        if auth.get("type") == TIPO_CODIGO_TECLADO and auth.get("name") == nombre_auth and auth.get("enabled", True):
            nuki.actualizar_auth(sid, auth, {"allowedWeekDays": dias, "allowedFromTime": de, "allowedUntilTime": a})


def fecha_iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ---------------------------------------------------------------- Nuki

class Nuki:
    def __init__(self, token, simular=False):
        self.token = token
        self.simular = simular

    def _cab(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def cerraduras(self):
        _, r = peticion("GET", f"{NUKI_API}/smartlock", cabeceras=self._cab())
        return r

    def autorizaciones(self, id_cerradura):
        _, r = peticion("GET", f"{NUKI_API}/smartlock/{id_cerradura}/auth", cabeceras=self._cab())
        return r if isinstance(r, list) else []

    def crear_codigo(self, id_cerradura, nombre, codigo, desde, hasta, franja=None):
        dias, de, a = franja_nuki(franja)
        cuerpo = {
            "name": nombre[:20],
            "type": TIPO_CODIGO_TECLADO,
            "code": int(codigo),
            "allowedWeekDays": dias,  # 127 = todos los días
            "allowedFromTime": de,
            "allowedUntilTime": a,
        }
        if hasta is not None:  # sin fecha de fin = código fijo
            cuerpo["allowedFromDate"] = fecha_iso(desde)
            cuerpo["allowedUntilDate"] = fecha_iso(hasta)
        if self.simular:
            log(f"[SIMULACIÓN] Crearía un código en Nuki para «{nombre[:20]}»")
            return
        peticion("PUT", f"{NUKI_API}/smartlock/{id_cerradura}/auth", cuerpo, self._cab())

    def borrar(self, id_cerradura, id_auth):
        if self.simular:
            log(f"[SIMULACIÓN] Borraría la autorización {id_auth}")
            return
        peticion("DELETE", f"{NUKI_API}/smartlock/{id_cerradura}/auth/{id_auth}",
                 cabeceras=self._cab())

    def desactivar(self, id_cerradura, auth, nuevo_nombre):
        """Desactiva un código sin borrarlo y le cambia el nombre (para vigilar si alguien lo usa)."""
        cuerpo = {"name": nuevo_nombre[:20], "enabled": False,
                  "allowedWeekDays": auth.get("allowedWeekDays", 127),
                  "allowedFromTime": auth.get("allowedFromTime", 0),
                  "allowedUntilTime": auth.get("allowedUntilTime", 0)}
        for k in ("allowedFromDate", "allowedUntilDate"):
            if auth.get(k):
                cuerpo[k] = auth[k]
        if self.simular:
            log(f"[SIMULACIÓN] Desactivaría la autorización {auth.get('id')} como «{nuevo_nombre}»")
            return
        peticion("POST", f"{NUKI_API}/smartlock/{id_cerradura}/auth/{auth['id']}", cuerpo, self._cab())

    def estado(self, id_cerradura):
        """Datos completos de la cerradura (incluye state: cerrojo, sensor de puerta, batería)."""
        _, r = peticion("GET", f"{NUKI_API}/smartlock/{id_cerradura}", cabeceras=self._cab())
        return r if isinstance(r, dict) else {}

    def actualizar_auth(self, id_cerradura, auth, cambios):
        """Modifica una autorización conservando lo que no se cambia (nombre, horario, fechas…)."""
        cuerpo = {"name": auth.get("name", "")[:20], "enabled": auth.get("enabled", True),
                  "allowedWeekDays": auth.get("allowedWeekDays", 127),
                  "allowedFromTime": auth.get("allowedFromTime", 0),
                  "allowedUntilTime": auth.get("allowedUntilTime", 0)}
        if "remoteAllowed" in auth:
            cuerpo["remoteAllowed"] = auth["remoteAllowed"]
        for k in ("allowedFromDate", "allowedUntilDate"):
            if auth.get(k):
                cuerpo[k] = auth[k]
        cuerpo.update(cambios)
        if self.simular:
            log(f"[SIMULACIÓN] Modificaría la autorización {auth.get('id')}: {cambios}")
            return
        peticion("POST", f"{NUKI_API}/smartlock/{id_cerradura}/auth/{auth['id']}", cuerpo, self._cab())

    def guardar_ajustes(self, id_cerradura, seccion, datos):
        """seccion: «config» o «advancedConfig». Se manda el bloque completo (leído antes y modificado)."""
        ruta = {"config": "config", "advancedConfig": "advanced/config"}[seccion]
        if self.simular:
            log(f"[SIMULACIÓN] Guardaría {seccion}: {datos}")
            return
        peticion("POST", f"{NUKI_API}/smartlock/{id_cerradura}/{ruta}", datos, self._cab())

    def accion(self, id_cerradura, accion):
        """Cerradura: 1 desbloquear · 2 bloquear · 3 abrir (pestillo) · 4 lock'n'go · 5 lock'n'go con pestillo.
        Opener: 3 abrir el portal."""
        if accion not in (1, 2, 3, 4, 5):
            raise RuntimeError("Acción no permitida.")
        if self.simular:
            log(f"[SIMULACIÓN] Enviaría la acción {accion} a la cerradura")
            return
        peticion("POST", f"{NUKI_API}/smartlock/{id_cerradura}/action", {"action": accion}, self._cab())

    def registro(self, id_cerradura, limite=200):
        """Registro de actividad de la cerradura (aperturas, cierres, códigos...)."""
        _, r = peticion("GET", f"{NUKI_API}/smartlock/{id_cerradura}/log?limit={int(limite)}",
                        cabeceras=self._cab())
        return r if isinstance(r, list) else []


def validar_codigo(codigo, codigos_existentes=()):
    """Comprueba un código elegido a mano. Devuelve el código o lanza un error explicado."""
    codigo = str(codigo or "").strip()
    if len(codigo) != 6 or not codigo.isdigit():
        raise RuntimeError("El código debe tener exactamente 6 cifras.")
    if "0" in codigo:
        raise RuntimeError("El teclado Nuki no admite el 0: usa solo cifras del 1 al 9.")
    if codigo.startswith("12"):
        raise RuntimeError("Nuki no permite códigos que empiecen por 12.")
    if codigo in codigos_existentes:
        raise RuntimeError("Ese código ya lo tiene otra persona en la cerradura.")
    if codigo in codigos_usados():
        raise RuntimeError("Ese código ya se usó antes: por seguridad no se repiten códigos.")
    return codigo


# ---------------------------------------------------------------- historial de códigos
# estado.json["_codigos"]: todos los códigos emitidos en esta cuenta, para no repetirlos
# nunca y saber de quién era un código desactivado.

CLAVE_CODIGOS = "_codigos"
PREFIJO_BAJA = "Baja "


def historial_codigos():
    return cargar_json(ruta_estado(), {}).get(CLAVE_CODIGOS, [])


def codigos_usados():
    """Todos los códigos que ha tenido alguien alguna vez en esta cuenta."""
    estado = cargar_json(ruta_estado(), {})
    usados = {str(c["codigo"]) for c in estado.get(CLAVE_CODIGOS, [])}
    usados |= {str(v["codigo"]) for k, v in estado.items()
               if not k.startswith("_") and isinstance(v, dict) and v.get("codigo")}
    usados |= {str(i["codigo"]) for i in estado.get(CLAVE_INVITADOS, []) if i.get("codigo")}
    return usados


def sembrar_historial(config):
    """Si el historial de códigos está vacío, lo rellena con los códigos que ya existían."""
    estado = cargar_json(ruta_estado(), {})
    if estado.get(CLAVE_CODIGOS):
        return
    lista = []
    for d in config.get("destinatarios", []):
        info = estado.get(d["id"], {})
        if info.get("codigo"):
            lista.append({"codigo": str(info["codigo"]), "persona_id": d["id"], "persona": d["nombre"],
                          "tipo": "fijo" if d.get("fijo") else ("semanal" if d.get("automatico", True) else "temporal"),
                          "nombre_auth": nombre_auth_de(config, d), "desde": info.get("creado"),
                          "hasta": info.get("caduca"), "motivo": "", "baja": None, "nombre_baja": None, "retirado": None})
    for i in estado.get(CLAVE_INVITADOS, []):
        nombre = next((d["nombre"] for d in config.get("destinatarios", []) if d["id"] == i["inquilino"]), i["inquilino"])
        lista.append({"codigo": str(i["codigo"]), "persona_id": i["inquilino"], "persona": f"Invitado de {nombre}",
                      "tipo": "invitado", "nombre_auth": i["nombre_auth"], "desde": i.get("creado"), "hasta": i.get("hasta"),
                      "motivo": i.get("motivo", ""), "baja": None, "nombre_baja": None, "retirado": None})
    if lista:
        estado[CLAVE_CODIGOS] = lista
        guardar_json(ruta_estado(), estado)


def apuntar_codigo(codigo, persona, tipo, nombre_auth, desde, hasta, motivo=""):
    estado = cargar_json(ruta_estado(), {})
    estado.setdefault(CLAVE_CODIGOS, []).append({
        "codigo": str(codigo), "persona_id": persona["id"], "persona": persona["nombre"], "tipo": tipo,
        "nombre_auth": nombre_auth, "desde": desde.isoformat(), "hasta": hasta.isoformat() if hasta else None,
        "motivo": motivo, "baja": None, "nombre_baja": None, "retirado": None})
    guardar_json(ruta_estado(), estado)


def marcar_baja(codigo=None, nombre_auth=None, nombre_baja=None, motivo="", retirado=False):
    """Apunta que un código se ha desactivado (y con qué nombre queda en la cerradura)."""
    estado = cargar_json(ruta_estado(), {})
    ahora = datetime.now().astimezone().isoformat(timespec="seconds")
    cambiado = False
    for c in reversed(estado.get(CLAVE_CODIGOS, [])):
        if c.get("baja") and not nombre_baja:
            continue
        if (codigo and c["codigo"] == str(codigo)) or (nombre_auth and c["nombre_auth"] == nombre_auth and not c.get("baja")):
            c["baja"] = c.get("baja") or ahora
            c["motivo_baja"] = motivo
            if nombre_baja:
                c["nombre_baja"] = nombre_baja
            if retirado:
                c["retirado"] = ahora
            cambiado = True
            break
    if cambiado:
        guardar_json(ruta_estado(), estado)


def nombre_baja_de(persona_id):
    return f"{PREFIJO_BAJA}{persona_id[:10]} {secrets.token_hex(1)}"[:20]


def retirar_o_vigilar(config, nuki, auth, persona, motivo, forzar_borrado=False):
    """Quita un código de la cerradura. Si la vigilancia está activa, en vez de
    borrarlo lo desactiva y lo renombra «Baja …»: así, si alguien lo teclea,
    el registro de Nuki dirá de quién era."""
    id_cerradura = config["nuki"]["smartlock_id"]
    dias = int(config.get("vigilar_bajas_dias", 30))
    if dias > 0 and not forzar_borrado:
        nombre = nombre_baja_de(persona["id"])
        try:
            nuki.desactivar(id_cerradura, auth, nombre)
            if not nuki.simular:
                marcar_baja(codigo=auth.get("code"), nombre_auth=auth.get("name"), nombre_baja=nombre, motivo=motivo)
            return "desactivado"
        except Exception as e:
            log(f"No se pudo desactivar (se borrará): {e}")
    nuki.borrar(id_cerradura, auth["id"])
    if not nuki.simular:
        marcar_baja(codigo=auth.get("code"), nombre_auth=auth.get("name"), motivo=motivo, retirado=True)
    return "borrado"


def limpiar_bajas(config, nuki):
    """Borra de verdad los códigos desactivados que ya llevan más días de los configurados."""
    dias = int(config.get("vigilar_bajas_dias", 30))
    limite = datetime.now().astimezone() - timedelta(days=max(dias, 0))
    id_cerradura = config["nuki"]["smartlock_id"]
    viejos = {c["nombre_baja"]: c for c in historial_codigos()
              if c.get("nombre_baja") and not c.get("retirado") and c.get("baja")
              and datetime.fromisoformat(c["baja"]) < limite}
    if not viejos:
        return
    for a in nuki.autorizaciones(id_cerradura):
        c = viejos.get(a.get("name"))
        if c:
            nuki.borrar(id_cerradura, a["id"])
            marcar_baja(nombre_baja=c["nombre_baja"], codigo=c["codigo"], retirado=True)
            registrar("Código desactivado retirado", c["persona"],
                      "Ya no se vigila")


def generar_codigo(codigos_existentes):
    """Código de 6 cifras válido para el Keypad de Nuki:
    solo dígitos del 1 al 9 (sin ceros) y sin empezar por '12'."""
    while True:
        codigo = "".join(str(secrets.randbelow(9) + 1) for _ in range(6))
        if codigo.startswith("12") or codigo in codigos_existentes:
            continue
        if len(set(codigo)) < 3:  # evita códigos demasiado obvios (111111, 121212...)
            continue
        return codigo


# ---------------------------------------------------------------- WhatsApp

CANALES_ENVIO = ("whatsapp", "telegram", "ambos")


def enviar_whatsapp(config, destinatario, texto, simular=False, abrir=True):
    """Envía el mensaje a la persona por su canal (WhatsApp, Telegram o ambos) y lo anota.
    Con «ambos» basta con que llegue por uno; el fallo del otro queda en el historial.
    Con el proveedor de WhatsApp «enlace» devuelve el enlace wa.me."""
    return enviar_mensaje(config, destinatario, texto, simular, abrir)[0]


def enviar_mensaje(config, destinatario, texto, simular=False, abrir=True):
    """Como enviar_whatsapp, pero devuelve (enlace, entregado). «entregado» es False cuando
    el mensaje solo quedó preparado (proveedor «enlace»: falta que alguien pulse Enviar)."""
    canal = destinatario.get("canal_envio") or "whatsapp"
    automatico = config["whatsapp"].get("proveedor", "enlace") != "enlace"
    if canal not in ("telegram", "ambos"):
        return _enviar_solo_whatsapp(config, destinatario, texto, simular, abrir), automatico
    error_tg = None
    try:
        if not destinatario.get("telegram_chat"):
            raise RuntimeError(f"{destinatario['nombre']} todavía no ha vinculado Telegram (pulsa «Telegram» en su ficha).")
        if simular:
            log(f"[SIMULACIÓN] Telegram a {destinatario['nombre']}")
        else:
            import telegram
            telegram.enviar(destinatario["telegram_chat"], texto)
        registrar("Telegram enviado", destinatario["nombre"], "", simular)
    except Exception as e:
        error_tg = e
        registrar("Error al enviar Telegram", destinatario["nombre"], str(e), simular, ok=False)
    if canal == "telegram":
        if error_tg:
            raise error_tg
        return None, True
    try:
        enlace = _enviar_solo_whatsapp(config, destinatario, texto, simular, abrir)
    except Exception:
        if error_tg:
            raise
        return None, True  # llegó por Telegram
    return enlace, automatico or not error_tg


def _enviar_solo_whatsapp(config, destinatario, texto, simular=False, abrir=True):
    proveedor = config["whatsapp"].get("proveedor", "enlace")
    quien = f"{destinatario['nombre']} ({destinatario['telefono']})"
    try:
        enlace = _enviar_whatsapp(config, destinatario, texto, simular, abrir)
    except Exception as e:
        registrar("Error al enviar WhatsApp", destinatario["nombre"], str(e), simular, ok=False)
        raise
    if proveedor == "enlace":
        registrar("WhatsApp preparado (pendiente de pulsar Enviar)", quien, "", simular)
    else:
        registrar(f"WhatsApp enviado ({proveedor})", quien, "", simular)
    return enlace


def _enviar_whatsapp(config, destinatario, texto, simular=False, abrir=True):
    wa = config["whatsapp"]
    proveedor = wa.get("proveedor", "enlace")
    telefono = destinatario["telefono"].replace(" ", "")
    enlace = f"https://wa.me/{telefono.lstrip('+')}?text={urllib.parse.quote(texto)}"

    if simular:
        log(f"[SIMULACIÓN] WhatsApp vía {proveedor} a {destinatario['nombre']}")
        return enlace if proveedor == "enlace" else None

    if proveedor == "twilio":
        t = wa["twilio"]
        url = f"https://api.twilio.com/2010-04-01/Accounts/{t['account_sid']}/Messages.json"
        credenciales = base64.b64encode(f"{t['account_sid']}:{t['auth_token']}".encode()).decode()
        peticion("POST", url,
                 {"From": f"whatsapp:{t['numero_origen']}",
                  "To": f"whatsapp:{telefono}",
                  "Body": texto},
                 {"Authorization": f"Basic {credenciales}"}, formulario=True)

    elif proveedor == "waha":
        import waha
        waha.enviar(config, telefono, texto)

    elif proveedor == "callmebot":
        apikey = destinatario.get("callmebot_apikey")
        if not apikey:
            raise RuntimeError(f"Falta 'callmebot_apikey' para {destinatario['nombre']}")
        url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
            {"phone": telefono.lstrip("+"), "text": texto, "apikey": apikey})
        peticion("GET", url)

    elif proveedor == "enlace":
        # Sin servicio externo: abre WhatsApp en el Mac con el mensaje ya escrito.
        log(f"Enlace de WhatsApp preparado para {destinatario['nombre']}")
        if abrir and sys.platform == "darwin":
            import subprocess
            subprocess.run(["open", enlace], check=False)
        return enlace
    else:
        raise RuntimeError(f"Proveedor de WhatsApp desconocido: {proveedor}")

    log(f"WhatsApp enviado a {destinatario['nombre']} ({telefono})")
    return None


# ---------------------------------------------------------------- lógica

def buscar_destinatario(config, clave):
    for d in config["destinatarios"]:
        if d["id"] == clave:
            return d
    disponibles = ", ".join(d["id"] for d in config["destinatarios"])
    sys.exit(f"No existe el destinatario '{clave}'. Disponibles: {disponibles}")


def nombre_auth_de(config, destinatario):
    return f"{config.get('prefijo_nombre', 'Auto')} {destinatario['id']}"[:20]


def borrar_codigos_de(config, nuki, destinatario, existentes=None, motivo="Anulado", forzar_borrado=False):
    """Quita de la cerradura los códigos de este destinatario (los desactiva y vigila,
    o los borra si la vigilancia está apagada)."""
    id_cerradura = config["nuki"]["smartlock_id"]
    if existentes is None:
        existentes = nuki.autorizaciones(id_cerradura)
    nombre_auth = nombre_auth_de(config, destinatario)
    borrados = 0
    for a in existentes:
        if a.get("type") == TIPO_CODIGO_TECLADO and a.get("name") == nombre_auth:
            log(f"Retirando código de {destinatario['nombre']} (id {a['id']})")
            como = retirar_o_vigilar(config, nuki, a, destinatario, motivo, forzar_borrado)
            registrar("Código desactivado (vigilado)" if como == "desactivado" else "Código anulado",
                      destinatario["nombre"], motivo, nuki.simular)
            borrados += 1
    if not nuki.simular:
        estado = cargar_json(ruta_estado(), {})
        quitado = estado.pop(destinatario["id"], None) is not None
        if destinatario["id"] in estado.get(CLAVE_RENOVACIONES, {}):
            estado[CLAVE_RENOVACIONES].pop(destinatario["id"])
            quitado = True
        if quitado:
            guardar_json(ruta_estado(), estado)
    return borrados


def renovar_codigo(config, nuki, destinatario, simular=False, abrir=True, codigo_elegido=None):
    """Crea un código nuevo para el destinatario, lo envía por WhatsApp y después
    borra el anterior. Si el destinatario es «fijo», el código no caduca.
    Devuelve un resumen."""
    id_cerradura = config["nuki"]["smartlock_id"]
    dias = config.get("dias_validez", 7)
    fijo = bool(destinatario.get("fijo"))
    nombre_auth = nombre_auth_de(config, destinatario)

    existentes = [] if simular and not config["nuki"].get("token") else nuki.autorizaciones(id_cerradura)
    propios = [a for a in existentes if a.get("type") == TIPO_CODIGO_TECLADO and a.get("name") == nombre_auth]

    # Orden seguro: 1) crear el nuevo, 2) enviarlo, 3) solo entonces borrar el viejo.
    # Si el envío falla, el inquilino conserva su código anterior y no se queda en la calle.

    # 1) Crear el nuevo
    codigos_ajenos = {str(a.get("code")) for a in existentes if a.get("code") and a not in propios}
    if codigo_elegido:
        codigo = str(codigo_elegido).strip()
        if codigo in {str(a.get("code")) for a in propios}:
            # quiere conservar su mismo código: hay que borrar el viejo antes (Nuki no admite duplicados)
            borrar_codigos_de(config, nuki, destinatario, existentes, "Sustituido", forzar_borrado=True)
            existentes = []
        else:
            validar_codigo(codigo, codigos_ajenos)  # incluye: no repetir códigos ya usados
    else:
        codigo = generar_codigo(codigos_ajenos | {str(a.get("code")) for a in propios} | codigos_usados())
    desde = datetime.now(timezone.utc)
    hasta = None if fijo else desde + timedelta(days=dias)
    franja = destinatario.get("franja")
    anteriores = [a["id"] for a in existentes
                  if a.get("type") == TIPO_CODIGO_TECLADO and a.get("name") == nombre_auth and a.get("id")]
    nuki.crear_codigo(id_cerradura, nombre_auth, codigo, desde, hasta, franja)
    validez = "sin caducidad (fijo)" if fijo else f"válido hasta el {hasta.astimezone():%d/%m/%Y %H:%M}"
    log(f"Código nuevo para {destinatario['nombre']}: {validez}")
    registrar("Código fijo creado" if fijo else "Código creado", destinatario["nombre"],
              validez.capitalize(), simular)

    # 2) Enviar por WhatsApp
    if fijo:
        plantilla = config.get("mensaje_fijo", MENSAJE_FIJO)
    else:
        plantilla = config.get(
            "mensaje",
            "Hola {nombre}, tu nuevo código de acceso es {codigo}. Válido hasta el {hasta}.",
        )
    texto = plantilla.format(
        nombre=destinatario["nombre"],
        codigo=codigo,
        hasta=hasta.astimezone().strftime("%d/%m/%Y a las %H:%M") if hasta else "sin fecha de fin",
        dias=dias,
    )
    if texto_franja(franja):
        texto += f"\nSolo funciona {texto_franja(franja)}."

    if simular:
        enlace = enviar_whatsapp(config, destinatario, texto, simular, abrir)
        borrar_codigos_de(config, nuki, destinatario, existentes, "Renovado")
        return {"codigo": codigo, "hasta": hasta.isoformat() if hasta else None, "fijo": fijo,
                "enlace": enlace, "mensaje": texto, "estado": "enviado"}

    # Se apunta antes de enviar: si el panel se reinicia o el envío falla, la renovación sigue pendiente
    # y el reintento manda ESTE mismo código. Los anteriores no se tocan hasta confirmar la entrega.
    rec = {"estado": "pendiente", "codigo": codigo, "nombre_auth": nombre_auth, "desde": desde.isoformat(),
           "hasta": hasta.isoformat() if hasta else None, "fijo": fijo, "texto": texto,
           "anteriores": anteriores, "intentos": 0}
    _guardar_renovacion(destinatario["id"], rec)
    return _entregar(config, nuki, destinatario, rec, abrir)


# ---------------------------------------------------------------- renovaciones pendientes de entregar

def renovaciones():
    """{id de persona: renovación sin entregar} de la cuenta actual."""
    return cargar_json(ruta_estado(), {}).get(CLAVE_RENOVACIONES, {})


def _guardar_renovacion(persona_id, rec):
    estado = cargar_json(ruta_estado(), {})
    lista = estado.setdefault(CLAVE_RENOVACIONES, {})
    if rec is None:
        lista.pop(persona_id, None)
    else:
        lista[persona_id] = rec
    guardar_json(ruta_estado(), estado)


def _resumen(rec, enlace=None):
    return {"codigo": rec["codigo"], "hasta": rec["hasta"], "fijo": rec["fijo"], "enlace": enlace,
            "mensaje": rec["texto"], "estado": rec["estado"]}


def _entregar(config, nuki, destinatario, rec, abrir=False):
    """Envía el código de una renovación. Si consta que ha llegado, la completa (retira los anteriores).
    Si solo quedó preparado (enlace de WhatsApp), queda «por confirmar». Si falla, queda «fallido»."""
    rec["intentos"] = rec.get("intentos", 0) + 1
    rec["ultimo_intento"] = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        enlace, entregado = enviar_mensaje(config, destinatario, rec["texto"], False, abrir)
    except Exception as e:
        rec.update(estado="fallido", error=sanear(str(e))[:300])
        _guardar_renovacion(destinatario["id"], rec)
        registrar("Renovación incompleta", destinatario["nombre"],
                  "No se pudo enviar el código nuevo: se mantiene el anterior y se reintentará", ok=False)
        raise
    if not entregado:
        rec.update(estado="por_confirmar", error="")
        _guardar_renovacion(destinatario["id"], rec)
        registrar("Renovación por confirmar", destinatario["nombre"],
                  "Código nuevo preparado; el anterior sigue valiendo hasta que confirmes que lo has enviado")
        return _resumen(rec, enlace)
    _completar(config, nuki, destinatario, rec)
    return _resumen(rec, enlace)


def _completar(config, nuki, destinatario, rec):
    """El código nuevo ya ha llegado: se retiran los anteriores y se apunta como el vigente."""
    id_cerradura = config["nuki"]["smartlock_id"]
    viejos = set(rec.get("anteriores") or [])
    if viejos:
        borrar_codigos_de(config, nuki, destinatario,
                          [a for a in nuki.autorizaciones(id_cerradura) if a.get("id") in viejos], "Renovado")
    desde = datetime.fromisoformat(rec["desde"])
    hasta = datetime.fromisoformat(rec["hasta"]) if rec["hasta"] else None
    apuntar_codigo(rec["codigo"], destinatario,
                   "fijo" if rec["fijo"] else ("semanal" if destinatario.get("automatico", True) else "temporal"),
                   rec["nombre_auth"], desde, hasta)
    estado = cargar_json(ruta_estado(), {})
    estado[destinatario["id"]] = {"creado": rec["desde"], "caduca": rec["hasta"], "fijo": rec["fijo"],
                                  "codigo": rec["codigo"]}
    estado.get(CLAVE_RENOVACIONES, {}).pop(destinatario["id"], None)
    guardar_json(ruta_estado(), estado)
    rec["estado"] = "enviado"


def reintentar_renovacion(config, nuki, destinatario, abrir=False):
    """Vuelve a enviar el MISMO código de la renovación pendiente de esta persona."""
    rec = renovaciones().get(destinatario["id"])
    if not rec:
        raise RuntimeError(f"{destinatario['nombre']} no tiene ninguna renovación pendiente.")
    return _entregar(config, nuki, destinatario, rec, abrir)


def confirmar_renovacion(config, nuki, destinatario):
    """El gestor confirma que ha enviado el código (modo enlace): se retiran los anteriores."""
    rec = renovaciones().get(destinatario["id"])
    if not rec:
        raise RuntimeError(f"{destinatario['nombre']} no tiene ninguna renovación pendiente.")
    _completar(config, nuki, destinatario, rec)
    registrar("Renovación confirmada", destinatario["nombre"], "Código nuevo enviado; se retira el anterior")
    return _resumen(rec)


def reintentar_fallidas(config):
    """Reintenta solas las renovaciones fallidas de la cuenta actual (cada persona por separado).
    Las «por confirmar» esperan al gestor. Devuelve {nombre: resultado o {"error": ...}}."""
    pendientes = renovaciones()
    if not pendientes:
        return {}
    nuki = Nuki(config["nuki"].get("token", ""))
    resultados = {}
    for d in config["destinatarios"]:
        rec = pendientes.get(d["id"])
        if not rec or rec.get("estado") not in ("fallido", "pendiente") or rec.get("intentos", 0) >= REINTENTOS_AUTOMATICOS:
            continue
        try:
            resultados[d["nombre"]] = _entregar(config, nuki, d, rec)
        except Exception as e:
            log(f"Reintento de renovación para {d['nombre']} fallido: {e}")
            resultados[d["nombre"]] = {"error": str(e)}
    return resultados


# ---------------------------------------------------------------- invitados

def prefijo_invitado(inquilino):
    return f"Inv {inquilino['id'][:10]} "


def crear_invitado(config, nuki, inquilino, horas, motivo="", telefono_invitado=None,
                   simular=False, abrir=True):
    """Código temporal para un invitado de un inquilino. Se envía al inquilino
    o directamente al invitado si se da su teléfono."""
    horas = float(horas)
    if not 0 < horas <= 24 * 30:
        raise RuntimeError("La duración debe estar entre unos minutos y 30 días.")
    id_cerradura = config["nuki"]["smartlock_id"]
    existentes = [] if simular and not config["nuki"].get("token") else nuki.autorizaciones(id_cerradura)
    codigo = generar_codigo({str(a.get("code")) for a in existentes if a.get("code")} | codigos_usados())
    nombre_auth = (prefijo_invitado(inquilino) + secrets.token_hex(2))[:20]
    desde = datetime.now(timezone.utc)
    hasta = desde + timedelta(hours=horas)
    nuki.crear_codigo(id_cerradura, nombre_auth, codigo, desde, hasta)
    hasta_txt = hasta.astimezone().strftime("%d/%m/%Y a las %H:%M")
    registrar("Código de invitado creado", inquilino["nombre"],
              f"Hasta el {hasta_txt}" + (f" · Motivo: {motivo}" if motivo else ""),
              simular)

    texto = config.get("mensaje_invitado", MENSAJE_INVITADO).format(
        nombre=inquilino["nombre"], codigo=codigo, hasta=hasta_txt, motivo=motivo)
    if telefono_invitado:
        destino = {"nombre": f"Invitado de {inquilino['nombre']}", "telefono": telefono_invitado}
    else:
        destino = inquilino
    enlace = enviar_whatsapp(config, destino, texto, simular, abrir)

    if not simular:
        apuntar_codigo(codigo, {"id": inquilino["id"], "nombre": f"Invitado de {inquilino['nombre']}"},
                       "invitado", nombre_auth, desde, hasta, motivo)
        estado = cargar_json(ruta_estado(), {})
        estado.setdefault(CLAVE_INVITADOS, []).append({
            "nombre_auth": nombre_auth, "inquilino": inquilino["id"], "codigo": codigo,
            "motivo": motivo, "telefono": telefono_invitado or "",
            "creado": desde.isoformat(), "hasta": hasta.isoformat()})
        guardar_json(ruta_estado(), estado)
    return {"codigo": codigo, "hasta": hasta.isoformat(), "enlace": enlace, "mensaje": texto}


def anular_invitado(config, nuki, nombre_auth, simular=False, motivo_registro="Código de invitado anulado"):
    id_cerradura = config["nuki"]["smartlock_id"]
    estado = cargar_json(ruta_estado(), {})
    lista = estado.get(CLAVE_INVITADOS, [])
    inv = next((i for i in lista if i["nombre_auth"] == nombre_auth), None)
    inquilino = next((d["nombre"] for d in config["destinatarios"]
                      if inv and d["id"] == inv["inquilino"]), "")
    if config["nuki"].get("token"):
        persona = {"id": inv["inquilino"] if inv else "invitado", "nombre": f"Invitado de {inquilino}"}
        for a in nuki.autorizaciones(id_cerradura):
            if a.get("name") == nombre_auth:
                retirar_o_vigilar(config, nuki, a, persona, motivo_registro)
    registrar(motivo_registro, inquilino, f"Hasta el {datetime.fromisoformat(inv['hasta']).astimezone():%d/%m/%Y %H:%M}" if inv else "", simular)
    if not simular and inv:
        estado[CLAVE_INVITADOS] = [i for i in lista if i["nombre_auth"] != nombre_auth]
        guardar_json(ruta_estado(), estado)


def limpiar_invitados_caducados(config, nuki):
    """Quita de la cerradura y de la lista los códigos de invitado ya caducados."""
    ahora = datetime.now(timezone.utc)
    for inv in cargar_json(ruta_estado(), {}).get(CLAVE_INVITADOS, []):
        if datetime.fromisoformat(inv["hasta"]) <= ahora:
            try:
                anular_invitado(config, nuki, inv["nombre_auth"],
                                motivo_registro="Código de invitado caducado y retirado")
            except Exception as e:
                log(f"No se pudo retirar el código de invitado {inv['nombre_auth']}: {e}")


def invitados_activos(inquilino_id=None):
    ahora = datetime.now(timezone.utc)
    return [i for i in cargar_json(ruta_estado(), {}).get(CLAVE_INVITADOS, [])
            if datetime.fromisoformat(i["hasta"]) > ahora
            and (inquilino_id is None or i["inquilino"] == inquilino_id)]


# ---------------------------------------------------------------- registro de la puerta

ACCIONES_NUKI = {
    1: "Desbloqueo", 2: "Bloqueo", 3: "Apertura (pestillo)", 4: "Lock'n'Go",
    5: "Lock'n'Go con apertura", 208: "Aviso de puerta", 209: "Aviso de puerta",
    224: "Timbre reconocido", 240: "Puerta abierta", 241: "Puerta cerrada",
    242: "Sensor de puerta atascado", 243: "Actualización de firmware",
}
ORIGENES_NUKI = {
    0: "Sistema", 1: "A mano (llave o pomo)", 2: "Botón de la cerradura", 3: "Automático",
    4: "Nuki Web", 5: "App", 6: "Cierre automático", 7: "Accesorio", 255: "Teclado",
}
ESTADOS_NUKI = {
    0: "Correcto", 1: "Motor bloqueado", 2: "Cancelado", 3: "Demasiado seguido", 4: "Ocupado",
    5: "Batería baja", 6: "Fallo de embrague", 7: "Fallo de motor", 8: "Incompleto",
    9: "Rechazado", 10: "Rechazado (modo noche)", 224: "Código incorrecto",
}


def quien_es(config, nombre_auth):
    """Traduce el nombre de una autorización de Nuki a una persona de la lista.
    En el registro, Nuki añade el dispositivo al final: «Auto santi (Keypad)»."""
    nombre_auth = re.sub(r"\s*\((Keypad|App|Web|Fob|Bridge|Nuki Web|Smartphone)\)\s*$", "", nombre_auth or "", flags=re.I).strip()
    if nombre_auth.startswith(PREFIJO_BAJA):
        c = next((c for c in historial_codigos() if c.get("nombre_baja") == nombre_auth), None)
        if c:
            return f"{c['persona']} · código dado de baja", "desactivado", c["persona_id"]
        pid = nombre_auth[len(PREFIJO_BAJA):].rsplit(" ", 1)[0]
        d = next((d for d in config.get("destinatarios", []) if d["id"][:10] == pid), None)
        if d:
            return f"{d['nombre']} · código dado de baja", "desactivado", d["id"]
        return f"Código dado de baja ({nombre_auth})", "desactivado", None
    for d in config.get("destinatarios", []):
        if nombre_auth == nombre_auth_de(config, d):
            return d["nombre"], "inquilino", d["id"]
        if nombre_auth.startswith(prefijo_invitado(d)):
            return f"Invitado de {d['nombre']}", "invitado", d["id"]
    return nombre_auth or "Desconocido", "otro", None


def traducir_registro(config, eventos):
    salida = []
    for e in eventos:
        accion, origen, estado = e.get("action"), e.get("trigger"), e.get("state", 0)
        quien, tipo_persona, id_persona = quien_es(config, e.get("name"))
        teclado = origen == 255 or e.get("source") == 1
        if tipo_persona == "desactivado":
            sentido = "🚨 Intento con código desactivado"
            estado = estado or 9  # se marca como rechazado aunque Nuki no lo diga
        elif estado == 224:
            sentido = "⚠️ Intento con código incorrecto"
            quien = "Desconocido"
        elif accion in (240, 241):
            sentido = ACCIONES_NUKI[accion]
        elif teclado and accion in (1, 3, 5):
            sentido = "Entrada con código"
        elif origen in (1, 2) and accion in (1, 3):
            sentido = "Apertura desde dentro (probable salida)"
        elif accion in (2, 4, 6):
            sentido = "Cierre"
        else:
            sentido = ACCIONES_NUKI.get(accion, f"Acción {accion}")
        salida.append({
            "id": e.get("id"), "teclado": teclado, "fecha": e.get("date"), "quien": quien, "tipo_persona": tipo_persona,
            "persona_id": id_persona, "sentido": sentido,
            "accion": ACCIONES_NUKI.get(accion, f"Acción {accion}"),
            "origen": ORIGENES_NUKI.get(origen, f"Origen {origen}"),
            "estado": ESTADOS_NUKI.get(estado, f"Estado {estado}"), "ok": estado == 0,
        })
    return salida


# ---------------------------------------------------------------- órdenes

def cmd_cerraduras(config, nuki, args):
    for c in nuki.cerraduras():
        print(f"ID: {c.get('smartlockId')}  Nombre: {c.get('name')}")


def cmd_listar(config, nuki, args):
    auths = nuki.autorizaciones(config["nuki"]["smartlock_id"])
    codigos = [a for a in auths if a.get("type") == TIPO_CODIGO_TECLADO]
    if not codigos:
        print("No hay códigos de teclado.")
    for a in codigos:
        print(f"- {a.get('name'):20}  hasta: {a.get('allowedUntilDate', 'sin límite')}  (id {a.get('id')})")


def cmd_nuevo(config, nuki, args):
    renovar_codigo(config, nuki, buscar_destinatario(config, args.para), args.simular,
                   codigo_elegido=args.codigo)


def rotar_cuenta(config, simular=False):
    """Renueva los códigos semanales de la cuenta actual y retira invitados caducados.
    Devuelve {nombre: resultado o {"error": ...}}."""
    nuki = Nuki(config["nuki"].get("token", ""), simular=simular)
    resultados = {}
    for d in config["destinatarios"]:
        if not d.get("automatico", True) or d.get("fijo"):
            continue
        try:
            resultados[d["nombre"]] = renovar_codigo(config, nuki, d, simular, abrir=False)
        except Exception as e:  # que un fallo no impida renovar a los demás
            log(f"ERROR con {d['nombre']}: {e}")
            registrar("Error en la renovación", d["nombre"], str(e), simular, ok=False)
            resultados[d["nombre"]] = {"error": str(e)}
    if not simular and config["nuki"].get("token"):
        limpiar_invitados_caducados(config, nuki)
        try:
            limpiar_bajas(config, nuki)
        except Exception as e:
            log(f"No se pudieron retirar códigos desactivados antiguos: {e}")
    return resultados


def cmd_rotar(config, nuki, args):
    cuentas._ctx.origen = "semanal"
    lista = [args.cuenta] if args.cuenta else [c["id"] for c in cuentas.listar_cuentas()]
    errores = 0
    for c in lista:
        with cuentas.en_cuenta(c, "sistema"):
            cfg = cargar_config()
            if not (cfg["nuki"].get("token") and cfg["nuki"].get("smartlock_id")):
                continue
            res = rotar_cuenta(cfg, args.simular)
            errores += sum(1 for r in res.values() if "error" in r)
    if errores:
        sys.exit(1)


def cmd_invitado(config, nuki, args):
    crear_invitado(config, nuki, buscar_destinatario(config, args.para), args.horas,
                   args.motivo or "", args.telefono, args.simular)


def cmd_entradas(config, nuki, args):
    eventos = traducir_registro(config, nuki.registro(config["nuki"]["smartlock_id"], args.limite))
    for e in eventos:
        fecha = datetime.fromisoformat(e["fecha"].replace("Z", "+00:00")).astimezone()
        print(f"{fecha:%d/%m %H:%M}  {e['quien']:28} {e['sentido']}  [{e['origen']}]")


def cmd_historial(config, nuki, args):
    for e in leer_gestion(args.limite):
        marca = " (prueba)" if e.get("simulado") else ""
        print(f"{e['fecha'][:16].replace('T', ' ')}  {e['accion']}{marca}  {e.get('usuario', '')}  "
              f"{e.get('detalle', '')}  [{e.get('origen')}]")


def cmd_probar_whatsapp(config, nuki, args):
    d = buscar_destinatario(config, args.para)
    enviar_whatsapp(config, d, f"Mensaje de prueba para {d['nombre']} desde el gestor de la cerradura.",
                    args.simular)


def main():
    p = argparse.ArgumentParser(description="Códigos semanales Nuki + WhatsApp")
    p.add_argument("--simular", action="store_true",
                   help="no cambia nada en Nuki ni envía mensajes; solo muestra qué haría")
    sub = p.add_subparsers(dest="orden", required=True)
    sub.add_parser("cerraduras", help="lista tus cerraduras")
    sub.add_parser("listar", help="lista los códigos del teclado")
    n = sub.add_parser("nuevo", help="genera un código nuevo para un destinatario")
    n.add_argument("--para", required=True, help="id del destinatario (ver config.json)")
    n.add_argument("--codigo", help="código de 6 cifras elegido a mano (opcional)")
    sub.add_parser("rotar", help="renueva los códigos de todos los destinatarios automáticos")
    i = sub.add_parser("invitado", help="código temporal para el invitado de un inquilino")
    i.add_argument("--para", required=True, help="id del inquilino que lo pide")
    i.add_argument("--horas", type=float, default=24, help="duración en horas (por defecto 24)")
    i.add_argument("--motivo", help="motivo (queda en el historial)")
    i.add_argument("--telefono", help="enviar directamente al invitado en vez de al inquilino")
    e = sub.add_parser("entradas", help="registro de entradas y salidas de la puerta")
    e.add_argument("--limite", type=int, default=50)
    h = sub.add_parser("historial", help="historial de gestión (códigos y mensajes)")
    h.add_argument("--limite", type=int, default=50)
    w = sub.add_parser("probar-whatsapp", help="envía un mensaje de prueba")
    w.add_argument("--para", required=True)
    p.add_argument("--cuenta", help="cuenta sobre la que actuar (por defecto «principal»)")
    args = p.parse_args()

    cuentas.migrar_si_hace_falta(log)
    with cuentas.en_cuenta(args.cuenta or cuentas.CUENTA_PRINCIPAL, "terminal"):
        config = cargar_config()
        nuki = Nuki(config["nuki"].get("token", ""), simular=args.simular)
        _ejecutar_orden(args, config, nuki)


def _ejecutar_orden(args, config, nuki):
    ordenes = {
        "cerraduras": cmd_cerraduras,
        "listar": cmd_listar,
        "nuevo": cmd_nuevo,
        "rotar": cmd_rotar,
        "invitado": cmd_invitado,
        "entradas": cmd_entradas,
        "historial": cmd_historial,
        "probar-whatsapp": cmd_probar_whatsapp,
    }
    try:
        ordenes[args.orden](config, nuki, args)
    except RuntimeError as e:
        log(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
