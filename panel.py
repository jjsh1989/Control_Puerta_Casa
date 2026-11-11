#!/usr/bin/env python3
"""
Panel web para gestionar los códigos Nuki de varias cuentas.

  python3 panel.py

Variables de entorno (en Docker las pone docker-compose.yml):
  PANEL_ESCUCHAR   dirección donde escuchar (127.0.0.1 por defecto; 0.0.0.0 en Docker)
  PANEL_PUERTO     puerto (8765)
  PANEL_HOSTS      nombres extra permitidos, separados por comas (además de localhost y *.ts.net)
"""

import hashlib
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import alertas
import bateria
import camara
import copias
import extras
import telegram
import cuentas
import nuki_codigos as nk
import nuki_extra
import waha

nk.ORIGEN = "panel"

ESCUCHAR = os.environ.get("PANEL_ESCUCHAR", "127.0.0.1")
PUERTO = int(os.environ.get("PANEL_PUERTO", "8765"))
HOSTS_EXTRA = {h.strip().lower() for h in os.environ.get("PANEL_HOSTS", "").split(",") if h.strip()}
HTML = (Path(__file__).resolve().parent / "panel.html").read_text(encoding="utf-8")

INACTIVIDAD_MAX = 12 * 3600          # tope absoluto de una sesión sin uso
INACTIVIDAD_OPCIONES = (5, 15, 30, 60, 240)   # minutos que puede elegir cada usuario
INACTIVIDAD_DEFECTO = 15


def minutos_inactividad(u):
    m = int((u or {}).get("inactividad_min") or INACTIVIDAD_DEFECTO)
    return m if m in INACTIVIDAD_OPCIONES else INACTIVIDAD_DEFECTO
INTENTOS_MAX, BLOQUEO_SEG = 5, 15 * 60
RECUPERACION = {}                     # usuario -> {"codigo", "caduca", "intentos", "enviado"}
RECUP_VALIDEZ, RECUP_ESPERA, RECUP_INTENTOS = 10 * 60, 120, 5
SESIONES = {}                         # token -> datos de la sesión
FALLOS = {}                           # usuario -> [fallos, bloqueado_hasta]
CODIGO_INSTALACION = None             # solo existe mientras no hay ningún usuario
_cerrojo = threading.Lock()
RUTA_SESIONES = cuentas.DATOS / "sesiones.json"   # guarda el hash del token, nunca el token


def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()


def cargar_sesiones():
    try:
        datos = json.loads(RUTA_SESIONES.read_text())
    except Exception:
        return
    ahora = time.time()
    SESIONES.update({k: v for k, v in datos.items() if ahora - v.get("ultimo", 0) < INACTIVIDAD_MAX})


def guardar_sesiones():
    with _cerrojo:
        datos = json.dumps({k: {x: y for x, y in v.items() if x != "token"} for k, v in SESIONES.items()})
    tmp = RUTA_SESIONES.with_suffix(".tmp")
    tmp.write_text(datos)
    os.chmod(tmp, 0o600)
    tmp.replace(RUTA_SESIONES)


class GuardaSesiones(threading.Thread):
    """Guarda las sesiones cada poco para que un reinicio del panel no eche a nadie."""
    def __init__(self):
        super().__init__(daemon=True)

    def run(self):
        anterior = None
        while True:
            time.sleep(20)
            try:
                with _cerrojo:
                    firma = json.dumps(sorted((k, v.get("cuenta"), int(v["ultimo"] // 300)) for k, v in SESIONES.items()))
                if firma != anterior:
                    guardar_sesiones()
                    anterior = firma
            except Exception as e:
                nk.log(f"Sesiones: {e}")


# ---------------------------------------------------------------- utilidades

def ocultar_telefono(t):
    t = (t or "").replace(" ", "")
    if len(t) < 6:
        return "•••"
    prefijo = t[:3] if t.startswith("+") else ""
    return f"{prefijo} ••• ••• •{t[-2:]}"


_RE_TEL = re.compile(r"\+?\d[\d ]{7,}\d")
_RE_COD = re.compile(r"(?<!\d)[1-9]{6}(?!\d)")


def ocultar_texto(texto):
    texto = _RE_TEL.sub(lambda m: ocultar_telefono(m.group(0)), texto or "")
    return _RE_COD.sub("••••••", texto)


def estado_usuarios(cfg):
    """Inquilinos con su estado, sin teléfonos ni códigos (se piden con «Mostrar»)."""
    local = nk.cargar_json(nk.ruta_estado(), {})
    aviso, auths, otros = None, None, []
    if cfg["nuki"].get("token") and cfg["nuki"].get("smartlock_id"):
        try:
            auths = [a for a in nk.Nuki(cfg["nuki"]["token"]).autorizaciones(cfg["nuki"]["smartlock_id"])
                     if a.get("type") == nk.TIPO_CODIGO_TECLADO]
        except Exception as e:
            aviso = f"No se pudo leer la cerradura: {e}"
    else:
        aviso = "Falta el token o la cerradura en Ajustes: los datos son solo locales."

    ahora = datetime.now().astimezone()
    nombres_programa, filas = set(), []
    for d in cfg["destinatarios"]:
        nombre_auth = nk.nombre_auth_de(cfg, d)
        nombres_programa.add(nombre_auth)
        info = local.get(d["id"], {})
        caduca, en_cerradura = info.get("caduca"), None
        if auths is not None:
            match = [a for a in auths if a.get("name") == nombre_auth]
            en_cerradura = bool(match)
            caduca = match[0].get("allowedUntilDate") if match else None
        activo = False
        if d.get("fijo") and info.get("codigo") and en_cerradura is not False:
            activo = True
        elif caduca:
            try:
                activo = datetime.fromisoformat(caduca.replace("Z", "+00:00")) > ahora
            except ValueError:
                pass
        filas.append({
            "id": d["id"], "nombre": d["nombre"], "telefono": ocultar_telefono(d["telefono"]),
            "automatico": d.get("automatico", True), "fijo": d.get("fijo", False),
            "seccion": d.get("seccion", ""),
            "franja": d.get("franja"), "franja_texto": nk.texto_franja(d.get("franja")),
            "canal_envio": d.get("canal_envio", "whatsapp"), "telegram": bool(d.get("telegram_chat")),
            "tiene_apikey": bool(d.get("callmebot_apikey")),
            "activo": activo, "caduca": caduca if activo else None,
            "invitados": [{k: i[k] for k in ("nombre_auth", "hasta", "motivo")}
                          for i in nk.invitados_activos(d["id"])],
        })
    if auths is not None:
        otros = [{"id": a.get("id"), "nombre": a.get("name"), "hasta": a.get("allowedUntilDate")}
                 for a in auths if a.get("name") not in nombres_programa
                 and not str(a.get("name", "")).startswith(("Inv ", nk.PREFIJO_BAJA))]
    return {"usuarios": filas, "otros": otros, "aviso": aviso}


def lista_secciones(cfg):
    lista = [x for x in cfg.get("secciones", []) if x]
    for d in cfg.get("destinatarios", []):
        if d.get("seccion") and d["seccion"] not in lista:
            lista.append(d["seccion"])
    return lista


def limpiar_nombre_seccion(nombre):
    nombre = " ".join(str(nombre or "").split())[:40]
    if not nombre:
        raise RuntimeError("Escribe un nombre para la sección.")
    return nombre


def validar_destino(cfg, a, anterior, activo, quien):
    """Canales (correo / WhatsApp / Telegram) y destinos de un aviso."""
    por = a.get("por")
    por = sorted({c for c in por if c in ("correo", "whatsapp", "telegram")}) if isinstance(por, list) \
        else sorted(bateria.canales_de(a))
    correo = ", ".join(bateria.correos_validos(a.get("correo") or ""))
    tel = re.sub(r"[\s-]", "", a.get("telefono") or "")
    if tel and not re.fullmatch(r"\+\d{8,15}", tel):
        raise RuntimeError(f"{quien}: el teléfono debe llevar prefijo, p. ej. +34600111222.")
    tel = tel or anterior.get("telefono", "")  # vacío = no cambiar
    principal = bool(a.get("wa_principal"))
    if activo:
        if not por:
            raise RuntimeError(f"{quien}: elige al menos un canal (correo, WhatsApp o Telegram).")
        if "correo" in por and not correo:
            raise RuntimeError(f"{quien}: escribe al menos un correo.")
        if "whatsapp" in por and not (tel or principal):
            raise RuntimeError(f"{quien}: elige el número principal o escribe un teléfono.")
        if "telegram" in por and not telegram.chats_avisos(cfg):
            raise RuntimeError(f"{quien}: primero vincula tu Telegram (botón «Vincular mi Telegram»).")
    return {"por": por, "correo": correo, "telefono": tel, "wa_principal": principal}


def config_publica(cfg, usuario=None):
    av = telegram.avisos_de(cfg)
    c = json.loads(json.dumps(cfg))
    c["secciones"] = lista_secciones(cfg)
    c["alertas"] = alertas.ajustes(cfg)
    c["alertas"]["destino"]["por"] = sorted(bateria.canales_de(c["alertas"]["destino"]))
    c["telegram_avisos"] = bool(av)
    c["telegram_avisos_lista"] = [{"usuario": k, "nombre": v.get("nombre", ""), "yo": k == usuario} for k, v in av.items()]
    c["telegram_avisos_yo"] = usuario in av
    c["telegram_pin_yo"] = cuentas.tiene_pin(usuario) if usuario else False
    c["extras"] = extras.ajustes(cfg)
    c["extras_camaras"] = [{"sn": sn, "nombre": camara.CLIENTE.dispositivos.get(sn, {}).get("nombre", sn)}
                           for sn in camara.camaras_de(cuentas.cuenta_actual())]
    for d in c["destinatarios"]:
        d["telegram_chat"] = bool(d.get("telegram_chat"))
    try:
        bot = telegram.info_bot()
    except Exception:
        bot = None
    c["telegram"] = {"configurado": telegram.configurado(), "bot": bot["usuario"] if bot else ""}
    if c["alertas"]["destino"].get("telefono"):
        c["alertas"]["destino"]["telefono"] = ocultar_telefono(c["alertas"]["destino"]["telefono"])
    if c.get("aviso_bateria"):
        c["aviso_bateria"]["por"] = sorted(bateria.canales_de(c["aviso_bateria"]))
    if c.get("aviso_bateria", {}).get("telefono"):
        c["aviso_bateria"]["telefono"] = ocultar_telefono(c["aviso_bateria"]["telefono"])
    c["nuki"]["token_puesto"] = bool(c["nuki"].pop("token", ""))
    tw = c["whatsapp"]["twilio"]
    tw["auth_token_puesto"] = bool(tw.pop("auth_token", ""))
    for d in c["destinatarios"]:
        d.pop("callmebot_apikey", None)
        d["telefono"] = ocultar_telefono(d["telefono"])
    return c


# ---------------------------------------------------------------- renovación programada

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


class Programador(threading.Thread):
    """Cada minuto mira si a alguna cuenta le toca la renovación semanal.
    Si el servidor estuvo apagado a la hora prevista, la hace en cuanto vuelve."""

    BATERIA_CADA = 30 * 60  # segundos entre lecturas de batería

    def __init__(self):
        super().__init__(daemon=True)
        self._bateria = {}  # cuenta -> última revisión

    def run(self):
        time.sleep(20)
        while True:
            for c in cuentas.listar_cuentas():
                try:
                    self._revisar(c["id"])
                except Exception as e:
                    nk.log(f"Programador ({c['id']}): {e}")
                try:
                    with cuentas.en_cuenta(c["id"], "programado"):
                        cuentas._ctx.origen = "programado"
                        try:
                            cfg = nk.cargar_config()
                            nuki_extra.revisar_programaciones(
                                cfg, lambda t, cfg=cfg: alertas.enviar(cfg, "programada", t))
                        finally:
                            cuentas._ctx.origen = None
                except Exception as e:
                    nk.log(f"Programaciones ({c['id']}): {e}")
                if time.time() - self._bateria.get(c["id"], 0) >= self.BATERIA_CADA:
                    self._bateria[c["id"]] = time.time()
                    try:
                        with cuentas.en_cuenta(c["id"], "automático"):
                            cfg = nk.cargar_config()
                            if cfg["nuki"].get("token") and cfg["nuki"].get("smartlock_id"):
                                bateria.revisar(cfg)
                    except Exception as e:
                        nk.log(f"Batería ({c['id']}): {e}")
            time.sleep(60)

    @staticmethod
    def _revisar(cuenta):
        with cuentas.en_cuenta(cuenta, "automático"):
            cuentas._ctx.origen = "semanal"
            try:
                cfg = nk.cargar_config()
                rot = cfg.get("rotacion", {})
                if not rot.get("activa", True):
                    return
                if not (cfg["nuki"].get("token") and cfg["nuki"].get("smartlock_id")):
                    return
                ahora = datetime.now().astimezone()
                prevista = (ahora - timedelta(days=(ahora.weekday() - int(rot.get("dia", 0))) % 7)).replace(
                    hour=int(rot.get("hora", 9)), minute=int(rot.get("minuto", 0)), second=0, microsecond=0)
                if prevista > ahora:
                    prevista -= timedelta(days=7)
                estado = nk.cargar_json(nk.ruta_estado(), {})
                ultima = estado.get("_ultima_rotacion")
                if ultima and datetime.fromisoformat(ultima) >= prevista:
                    return
                if not ultima:  # primera vez: no renovar de golpe al instalar, esperar a la próxima
                    estado["_ultima_rotacion"] = prevista.isoformat()
                    nk.guardar_json(nk.ruta_estado(), estado)
                    return
                nk.registrar("Renovación semanal iniciada", "", f"Prevista: {prevista:%d/%m/%Y %H:%M}")
                nk.rotar_cuenta(cfg)
                estado = nk.cargar_json(nk.ruta_estado(), {})
                estado["_ultima_rotacion"] = ahora.isoformat()
                nk.guardar_json(nk.ruta_estado(), estado)
            finally:
                cuentas._ctx.origen = None


# ---------------------------------------------------------------- servidor

class Manejador(BaseHTTPRequestHandler):
    server_version = "Puerta"
    sys_version = ""

    def log_message(self, *a):
        pass

    # ---- respuesta
    def _responder(self, codigo, datos, tipo="application/json", cabeceras=None):
        cuerpo = datos if isinstance(datos, bytes) else (
            datos.encode() if isinstance(datos, str) else json.dumps(datos).encode())
        self.send_response(codigo)
        self.send_header("Content-Type", tipo if tipo.startswith(("image", "video")) else f"{tipo}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (cabeceras or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(cuerpo)

    def _archivo(self, ruta):
        """Sirve una foto o un clip (con soporte de Range, que Safari necesita para el vídeo)."""
        tamano = ruta.stat().st_size
        tipo = "video/mp4" if ruta.suffix == ".mp4" else "image/jpeg"
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        inicio, fin = 0, tamano - 1
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                inicio = int(m.group(1))
                fin = min(int(m.group(2)), tamano - 1) if m.group(2) else tamano - 1
            else:
                inicio = max(0, tamano - int(m.group(2)))
        with open(ruta, "rb") as f:
            f.seek(inicio)
            datos = f.read(fin - inicio + 1)
        self.send_response(206 if m else 200)
        self.send_header("Content-Type", tipo)
        self.send_header("Accept-Ranges", "bytes")
        if m:
            self.send_header("Content-Range", f"bytes {inicio}-{fin}/{tamano}")
        self.send_header("Content-Length", str(len(datos)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(datos)

    def _directo(self, sn):
        """Vídeo en directo como MJPEG (multipart/x-mixed-replace): el navegador lo pinta en un <img>."""
        frontera = "fotograma"
        imagenes = camara.CLIENTE.directo_mjpeg(sn)
        try:
            primera = next(imagenes)
        except StopIteration:
            return self._error(504, "La cámara no ha enviado vídeo.")
        except Exception as e:
            return self._error(502, f"No se pudo abrir el directo: {e}")
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={frontera}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.close_connection = True
        try:
            jpg = primera
            while True:
                self.wfile.write(f"--{frontera}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n\r\n".encode()
                                 + jpg + b"\r\n")
                self.wfile.flush()
                jpg = next(imagenes)
        except (StopIteration, BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            imagenes.close()

    def _error(self, codigo, mensaje):
        self._responder(codigo, {"ok": False, "error": mensaje})

    def _ok(self, resultado=None):
        self._responder(200, {"ok": True, "resultado": resultado})

    # ---- seguridad
    def _host(self):
        return (self.headers.get("Host") or "").split(":")[0].lower()

    def _host_valido(self):
        h = self._host()
        return h in ("127.0.0.1", "localhost") or h.endswith(".ts.net") or h in HOSTS_EXTRA

    def _es_https(self):
        return self._host().endswith(".ts.net") or self.headers.get("X-Forwarded-Proto") == "https"

    def _sesion(self):
        galleta = self.headers.get("Cookie", "")
        m = re.search(r"(?:^|;\s*)puerta_sesion=([\w-]+)", galleta)
        if not m:
            return None
        clave = _h(m.group(1))
        with _cerrojo:
            s = SESIONES.get(clave)
            if not s:
                return None
            limite = min(INACTIVIDAD_MAX, s.get("limite") or INACTIVIDAD_MAX)
            if time.time() - s["ultimo"] > limite or not cuentas.buscar_usuario(s["usuario"]):
                SESIONES.pop(clave, None)
                return None
            s["ultimo"] = time.time()
            s["token"] = clave
            return s

    def _csrf_valido(self, s, valor=None):
        valor = valor or self.headers.get("X-CSRF", "")
        return bool(valor) and secrets.compare_digest(valor, s["csrf"])

    def _cuerpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
            raise RuntimeError("Petición demasiado grande.")
        return json.loads(self.rfile.read(n) or b"{}")

    # ---- entrada
    def do_GET(self):
        self._atender("GET")

    def do_POST(self):
        self._atender("POST")

    def _atender(self, metodo):
        if not self._host_valido():
            return self._error(403, "Host no permitido")
        url = urlparse(self.path)
        ruta, params = url.path, {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            datos = self._cuerpo() if metodo == "POST" else {}
        except Exception:
            return self._error(400, "Petición no válida")

        if metodo == "GET" and ruta in ("/", "/index.html"):
            return self._responder(200, HTML, "text/html", {
                "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; "
                                           "media-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                                           "connect-src 'self'; frame-ancestors 'none'"})
        if ruta == "/api/yo":
            return self._yo()
        if metodo == "POST" and ruta == "/api/entrar":
            return self._entrar(datos)
        if metodo == "POST" and ruta == "/api/configurar":
            return self._configurar(datos)
        if metodo == "POST" and ruta == "/api/recuperar":
            return self._recuperar(datos)
        if metodo == "POST" and ruta == "/api/recuperar/confirmar":
            return self._recuperar_confirmar(datos)

        s = self._sesion()
        if not s:
            return self._error(401, "Tu sesión ha caducado. Vuelve a entrar.")
        if not self._csrf_valido(s, params.get("csrf")):
            return self._error(403, "Petición no autorizada")

        with cuentas.en_cuenta(s["cuenta"], s["usuario"]):
            try:
                self._api(metodo, ruta, params, datos, s)
            except Exception as e:
                nk.log(f"ERROR (panel, {s['usuario']}): {e}")
                if metodo == "POST":
                    nk.registrar("Error", "", f"{ruta}: {e}", ok=False)
                self._error(400, str(e))

    # ---- acceso
    def _yo(self):
        s = self._sesion()
        if not cuentas.hay_usuarios():
            return self._ok({"configurar": True})
        if not s:
            return self._ok({"autenticado": False})
        u = cuentas.buscar_usuario(s["usuario"])
        lista = cuentas.listar_cuentas()
        nombre_cuenta = next((c["nombre"] for c in lista if c["id"] == s["cuenta"]), s["cuenta"])
        return self._ok({
            "autenticado": True, "usuario": u["usuario"], "nombre": u["nombre"], "rol": u["rol"],
            "cuenta": s["cuenta"], "nombre_cuenta": nombre_cuenta, "csrf": s["csrf"],
            "cuentas": lista if u["rol"] == "admin" else [],
            "inactividad_min": minutos_inactividad(u),
        })

    def _crear_sesion(self, u):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with _cerrojo:
            SESIONES[_h(token)] = {"usuario": u["usuario"], "rol": u["rol"], "cuenta": u["cuenta"],
                                   "csrf": csrf, "ultimo": time.time(),
                                   "limite": minutos_inactividad(u) * 60 + 90}  # + margen; el aviso lo da la página
        threading.Thread(target=guardar_sesiones, daemon=True).start()
        segura = "; Secure" if self._es_https() else ""
        return {"Set-Cookie": f"puerta_sesion={token}; HttpOnly; SameSite=Strict; Path=/{segura}"}

    def _entrar(self, datos):
        usuario = (datos.get("usuario") or "").strip().lower()
        ahora = time.time()
        fallos, hasta = FALLOS.get(usuario, [0, 0])
        if hasta > ahora:
            return self._error(429, f"Demasiados intentos. Espera {int((hasta - ahora) // 60) + 1} minutos.")
        u = cuentas.verificar_password(usuario, datos.get("password") or "")
        if not u:
            fallos += 1
            FALLOS[usuario] = [fallos, ahora + BLOQUEO_SEG if fallos >= INTENTOS_MAX else 0]
            existente = cuentas.buscar_usuario(usuario)
            if existente:
                with cuentas.en_cuenta(existente["cuenta"], usuario):
                    nk.registrar("Intento de acceso fallido", usuario,
                                 f"Contraseña incorrecta ({fallos}/{INTENTOS_MAX})", ok=False)
            time.sleep(1)
            return self._error(401, "Usuario o contraseña incorrectos.")
        FALLOS.pop(usuario, None)
        with cuentas.en_cuenta(u["cuenta"], usuario):
            nk.registrar("Inicio de sesión", u["nombre"], "")
        self._responder(200, {"ok": True}, cabeceras=self._crear_sesion(u))

    # ---- recuperar el acceso
    def _recuperar(self, datos):
        """Envía un código de un solo uso al Telegram vinculado de la cuenta (y, si es administrador,
        también al WhatsApp principal). La respuesta es siempre la misma, exista o no el usuario."""
        usuario = (datos.get("usuario") or "").strip().lower()
        generico = {"ok": True, "resultado": "Si el usuario existe y tiene Telegram o WhatsApp vinculado, "
                                              "te hemos enviado un código. Caduca en 10 minutos."}
        u = cuentas.buscar_usuario(usuario)
        ahora = time.time()
        previo = RECUPERACION.get(usuario)
        if not u or (previo and ahora - previo["enviado"] < RECUP_ESPERA):
            time.sleep(1)
            return self._responder(200, generico)
        codigo = "".join(secrets.choice("0123456789") for _ in range(8))
        texto = (f"🔑 Código para recuperar el acceso al panel de la puerta (usuario «{usuario}»): {codigo}\n"
                 f"Caduca en 10 minutos. Si no lo has pedido tú, ignóralo y avisa al administrador.")
        enviado = []
        with cuentas.en_cuenta(u["cuenta"], usuario):
            cfg = nk.cargar_config()
            chat = (telegram.avisos_de(cfg).get(usuario) or {}).get("chat")
            if chat and telegram.configurado():
                try:
                    telegram.enviar(chat, texto)  # solo a SU Telegram
                    enviado.append("Telegram")
                except Exception as e:
                    nk.log(f"Recuperación: Telegram falló ({e})")
            if u["rol"] == "admin":
                try:
                    c = dict(cfg)
                    c["whatsapp"] = {**cfg["whatsapp"], "proveedor": "waha"}
                    bateria.enviar_whatsapp_aviso(c, bateria.numero_principal(), texto)
                    enviado.append("WhatsApp principal")
                except Exception as e:
                    nk.log(f"Recuperación: WhatsApp falló ({e})")
            nk.registrar("Recuperación de acceso pedida", usuario,
                         ("Código enviado por " + ", ".join(enviado)) if enviado else "Sin Telegram ni WhatsApp para enviarlo",
                         ok=bool(enviado))
        if enviado:
            RECUPERACION[usuario] = {"codigo": codigo, "caduca": ahora + RECUP_VALIDEZ, "intentos": 0, "enviado": ahora}
        return self._responder(200, generico)

    def _recuperar_confirmar(self, datos):
        usuario = (datos.get("usuario") or "").strip().lower()
        r = RECUPERACION.get(usuario)
        if not r or r["caduca"] < time.time():
            RECUPERACION.pop(usuario, None)
            return self._error(400, "El código ha caducado o no es válido. Pide otro.")
        r["intentos"] += 1
        if not secrets.compare_digest((datos.get("codigo") or "").strip(), r["codigo"]):
            if r["intentos"] >= RECUP_INTENTOS:
                RECUPERACION.pop(usuario, None)
            time.sleep(1)
            return self._error(400, "Código incorrecto.")
        cuentas.cambiar_password(usuario, datos.get("nueva") or "")  # valida la contraseña nueva
        RECUPERACION.pop(usuario, None)
        FALLOS.pop(usuario, None)
        with _cerrojo:  # cierra todas las sesiones de ese usuario
            for t in [t for t, x in SESIONES.items() if x["usuario"] == usuario]:
                SESIONES.pop(t)
        u = cuentas.buscar_usuario(usuario)
        with cuentas.en_cuenta(u["cuenta"], usuario):
            nk.registrar("Contraseña recuperada", usuario, "Con código de recuperación")
        return self._ok()

    def _configurar(self, datos):
        """Primer arranque: crea el administrador con el código de instalación."""
        global CODIGO_INSTALACION
        if cuentas.hay_usuarios() or not CODIGO_INSTALACION:
            return self._error(403, "El panel ya está configurado.")
        if not secrets.compare_digest((datos.get("codigo") or "").strip(), CODIGO_INSTALACION):
            time.sleep(1)
            return self._error(403, "El código de instalación no es correcto.")
        cuenta = cuentas.CUENTA_PRINCIPAL
        if not cuentas.existe_cuenta(cuenta):
            cuentas.carpeta_cuenta(cuenta)
        cuentas.renombrar_cuenta(cuenta, (datos.get("nombre_cuenta") or "Mi casa").strip())
        try:
            cuentas.crear_usuario(datos.get("usuario"), datos.get("nombre"), datos.get("password") or "",
                                  "admin", cuenta)
        except RuntimeError as e:
            return self._error(400, str(e))
        CODIGO_INSTALACION = None
        cuentas.RUTA_USUARIOS.parent.joinpath("codigo_instalacion.txt").unlink(missing_ok=True)
        u = cuentas.buscar_usuario(datos.get("usuario"))
        with cuentas.en_cuenta(cuenta, u["usuario"]):
            nk.registrar("Administrador creado", u["nombre"], "Primer arranque del panel")
        self._responder(200, {"ok": True}, cabeceras=self._crear_sesion(u))

    # ---- API con sesión
    def _api(self, metodo, ruta, params, datos, s):
        admin = s["rol"] == "admin"
        cfg = nk.cargar_config()
        simular = bool(datos.get("simular"))
        nuki = nk.Nuki(cfg["nuki"].get("token", ""), simular=simular)

        def destinatario(ident):
            d = next((x for x in cfg["destinatarios"] if x["id"] == ident), None)
            if not d:
                raise RuntimeError("Persona no encontrada.")
            return d

        # ---------- sesión y cuenta
        if metodo == "POST" and ruta == "/api/salir":
            with _cerrojo:
                SESIONES.pop(s["token"], None)
            guardar_sesiones()
            return self._responder(200, {"ok": True}, cabeceras={
                "Set-Cookie": "puerta_sesion=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/"})
        if metodo == "POST" and ruta == "/api/preferencias":
            m = int(datos.get("inactividad_min") or 0)
            if m not in INACTIVIDAD_OPCIONES:
                raise RuntimeError("Tiempo no válido.")
            cuentas.modificar_usuario(s["usuario"], inactividad_min=m)
            with _cerrojo:
                for x in SESIONES.values():
                    if x["usuario"] == s["usuario"]:
                        x["limite"] = m * 60 + 90
            nk.registrar("Cierre por inactividad cambiado", s["usuario"], f"{m} min")
            return self._ok({"inactividad_min": m})
        if metodo == "POST" and ruta == "/api/cambiar-password":
            if not cuentas.verificar_password(s["usuario"], datos.get("actual") or ""):
                raise RuntimeError("La contraseña actual no es correcta.")
            cuentas.cambiar_password(s["usuario"], datos.get("nueva") or "")
            with _cerrojo:  # cierra las demás sesiones de este usuario
                for t in [t for t, x in SESIONES.items() if x["usuario"] == s["usuario"] and t != s["token"]]:
                    SESIONES.pop(t)
            nk.registrar("Contraseña cambiada", s["usuario"])
            return self._ok()
        if metodo == "POST" and ruta == "/api/cambiar-cuenta":
            if not admin:
                raise RuntimeError("Solo el administrador puede cambiar de cuenta.")
            if not cuentas.existe_cuenta(datos.get("cuenta", "")):
                raise RuntimeError("La cuenta no existe.")
            s["cuenta"] = datos["cuenta"]
            return self._ok()

        # ---------- lectura
        if metodo == "GET":
            if ruta == "/api/config":
                return self._ok(config_publica(cfg, s["usuario"]))
            if ruta == "/api/estado":
                return self._ok(estado_usuarios(cfg))
            if ruta == "/api/cerraduras":
                return self._ok([{"id": c.get("smartlockId"), "nombre": c.get("name")}
                                 for c in nk.Nuki(cfg["nuki"]["token"]).cerraduras()])
            if ruta == "/api/registro":
                if not (cfg["nuki"].get("token") and cfg["nuki"].get("smartlock_id")):
                    raise RuntimeError("Configura primero el token y la cerradura en Ajustes.")
                eventos = nuki.registro(cfg["nuki"]["smartlock_id"], min(int(params.get("limite", 200)), 1000))
                filas = nk.traducir_registro(cfg, eventos)
                cuenta = cuentas.cuenta_actual()
                for f in filas:  # foto de la cámara de la puerta en cada uso del teclado
                    if f.get("teclado") and f.get("id"):
                        f["foto"] = camara.foto_de_entrada(cuenta, f["id"])
                return self._ok(filas)
            if ruta == "/api/bateria":
                datos_bat = None
                error = ""
                try:
                    datos_bat = bateria.leer(cfg, forzar=params.get("actualizar") == "1")
                except Exception as e:
                    error = str(e)
                aviso_estado = bateria.estado_aviso()
                aviso_pub = bateria.ajustes_aviso(cfg)
                aviso_pub["telefono"] = ocultar_telefono(aviso_pub["telefono"]) if aviso_pub["telefono"] else ""
                return self._ok({"datos": datos_bat, "error": error, "aviso": aviso_pub,
                                 "smtp_listo": bool(bateria.smtp_config()["servidor"]),
                                 "ultimo_error_correo": aviso_estado.get("ultimo_error", "")})
            if ruta == "/api/codigos":
                nk.sembrar_historial(cfg)
                lista = list(reversed(nk.historial_codigos()))
                if params.get("revelar") == "1":
                    nk.registrar("Datos sensibles mostrados", s["usuario"], "Historial de códigos")
                else:
                    for c in lista:
                        c["codigo"] = "••••••"
                return self._ok(lista)
            if ruta == "/api/historial":
                entradas = nk.leer_gestion(min(int(params.get("limite", 1000)), 5000))
                if params.get("revelar") == "1":
                    nk.registrar("Datos sensibles mostrados", s["usuario"], "Historial completo")
                else:
                    for e in entradas:
                        e["usuario"] = ocultar_texto(e.get("usuario", ""))
                        e["detalle"] = ocultar_texto(e.get("detalle", ""))
                return self._ok(entradas)
            if ruta == "/api/cerradura":
                if not (cfg["nuki"].get("token") and cfg["nuki"].get("smartlock_id")):
                    raise RuntimeError("Configura primero la cerradura en Ajustes.")
                r = nuki.estado(cfg["nuki"]["smartlock_id"])
                st = r.get("state") or {}
                return self._ok({"nombre": (r.get("name") or "").strip(), "cerrojo": st.get("state"),
                                 "cerrojo_texto": alertas.ESTADOS_CERROJO.get(st.get("state"), "desconocido"),
                                 "puerta": st.get("doorState"),
                                 "puerta_texto": alertas.ESTADOS_PUERTA.get(st.get("doorState"), "desconocido"),
                                 "sensor": st.get("doorState") in (2, 3), "online": r.get("serverState", 0) == 0})
            if ruta == "/api/nuki/capacidades":
                return self._ok(nuki_extra.capacidades(cfg, admin))
            if ruta == "/api/programaciones":
                l = cfg.get("programaciones") or []
                return self._ok([{**p, "texto": nuki_extra.texto_prog(p)} for p in l])
            if ruta == "/api/programaciones/nuki":
                try:
                    return self._ok(nuki_extra.programaciones_nuki(cfg, fresco=params.get("fresco") == "1"))
                except Exception as e:
                    raise RuntimeError(f"No se pudo leer el registro de la cerradura: {e}")
            if ruta == "/api/admin/nuki/ajustes" and admin:
                return self._ok(nuki_extra.leer_ajustes(cfg))
            if ruta == "/api/admin/nuki/accesos" and admin:
                return self._ok(nuki_extra.accesos(cfg))
            m = re.fullmatch(r"/api/camara/ajustes/(\w+)", ruta)
            if m and admin:
                return self._ok(camara.leer_ajustes(m.group(1)))
            if ruta == "/api/admin/camara/disco" and admin:
                d = camara.uso_disco()
                d["sd"] = [{"nombre": x["nombre"], **(x.get("sd") or {})} for x in camara.estado()["dispositivos"] if x.get("sd")]
                d["nombres"] = {c["id"]: c["nombre"] for c in cuentas.listar_cuentas()}
                return self._ok(d)
            if ruta == "/api/camara":
                e = camara.estado()
                cuenta = cuentas.cuenta_actual()
                if not admin:
                    e = {k: v for k, v in e.items() if k in ("conectado", "sesion")}
                    e["dispositivos"] = [d for d in camara.estado()["dispositivos"] if d["cuenta"] == cuenta]
                e["eventos"] = camara.leer_eventos(cuenta, min(int(params.get("limite", 40)), 300))
                e["borrado_texto"] = camara.texto_borrado()
                e["admin"] = admin
                return self._ok(e)
            m = re.fullmatch(r"/api/admin/casa/([\w-]+)", ruta)
            if m and admin:
                if not cuentas.existe_cuenta(m.group(1)):
                    raise RuntimeError("La casa no existe.")
                with cuentas.en_cuenta(m.group(1), s["usuario"]):
                    c = nk.cargar_config()
                    return self._ok({"id": m.group(1), "solo_camara": bool(c.get("solo_camara")),
                                     "nuki": bool(c["nuki"].get("token")), "smartlock_id": c["nuki"].get("smartlock_id") or ""})
            m = re.fullmatch(r"/api/camara/(foto|directo)/(\w+)", ruta)
            if m:
                sn = m.group(2)
                if sn not in camara.CLIENTE.dispositivos or (not admin and camara.cuenta_de(sn) != cuentas.cuenta_actual()):
                    nk.log(f"Cámara {sn}: no encontrada ({len(camara.CLIENTE.dispositivos)} conocidas)")
                    return self._error(404, "Cámara no encontrada.")
                if m.group(1) == "foto":
                    _, foto = camara.CLIENTE.fotos.get(sn, (0, None))
                    if not foto:
                        return self._error(404, "Sin foto todavía.")
                    return self._responder(200, foto, "image/jpeg")
                if not camara.CLIENTE.driver:
                    return self._error(503, "La cámara no está conectada.")
                nk.registrar("Directo de cámara abierto", camara.CLIENTE.dispositivos[sn].get("nombre", sn), "")
                return self._directo(sn)
            m = re.fullmatch(r"/api/camara/archivo/([\w.-]+)", ruta)
            if m:
                return self._archivo(camara.ruta_archivo(cuentas.cuenta_actual(), m.group(1)))
            if ruta.startswith("/api/docker/") or ruta.startswith("/api/admin/"):
                if not admin:
                    raise RuntimeError("Solo el administrador puede ver esto.")
            if ruta == "/api/docker/estado":
                return self._ok(waha.estado_completo())
            if ruta == "/api/docker/logs":
                return self._ok(ocultar_texto(waha.logs_contenedor(200)))
            if ruta == "/api/docker/qr":
                return self._responder(200, waha.qr_png(), "image/png")
            if ruta == "/api/admin/correo":
                return self._ok(bateria.smtp_publico())
            if ruta == "/api/admin/cuentas":
                return self._ok({"cuentas": cuentas.listar_cuentas(), "usuarios": cuentas.usuarios_publicos()})
            return self._error(404, "No encontrado")

        # ---------- revelar datos sensibles
        m = re.fullmatch(r"/api/revelar/([\w-]+)", ruta)
        if m:
            d = destinatario(m.group(1))
            local = nk.cargar_json(nk.ruta_estado(), {}).get(d["id"], {})
            nk.registrar("Datos sensibles mostrados", d["nombre"], "Teléfono y código")
            return self._ok({"telefono": d["telefono"], "codigo": local.get("codigo"),
                             "invitados": {i["nombre_auth"]: i["codigo"] for i in nk.invitados_activos(d["id"])}})

        # ---------- ajustes e inquilinos
        if ruta == "/api/config":
            return self._ok(self._guardar_config(cfg, datos))
        if ruta == "/api/destinatario":
            return self._ok(self._guardar_destinatario(cfg, datos))
        if ruta == "/api/secciones":
            return self._ok(self._secciones(cfg, datos))
        if ruta == "/api/telegram/enlace":
            dest = datos.get("destino") or ""
            if dest == "_avisos":
                dest = "_avisos:" + s["usuario"]  # cada usuario vincula SU Telegram
            else:
                dest = destinatario(dest)["id"]
            enlace = telegram.crear_enlace(dest)
            nk.registrar("Enlace de Telegram creado", "Avisos de " + s["usuario"] if dest.startswith("_avisos") else dest, "")
            return self._ok({"enlace": enlace})
        if ruta == "/api/telegram/pin":  # PIN para abrir la puerta desde Telegram (cada usuario el suyo)
            if datos.get("quitar"):
                cuentas.quitar_pin(s["usuario"])
                nk.registrar("PIN de Telegram quitado", s.get("nombre") or s["usuario"], "")
                return self._ok()
            if not cuentas.verificar_password(s["usuario"], datos.get("password") or ""):
                raise RuntimeError("La contraseña del panel no es correcta.")
            cuentas.poner_pin(s["usuario"], datos.get("pin"))
            nk.registrar("PIN de Telegram puesto", s.get("nombre") or s["usuario"], "")
            return self._ok()
        if ruta == "/api/telegram/desvincular":
            dest = datos.get("destino") or ""
            if dest == "_avisos":
                quien = datos.get("usuario") or s["usuario"]
                if quien != s["usuario"] and not admin:
                    raise RuntimeError("Solo puedes desvincular tu propio Telegram.")
                av = telegram.avisos_de(cfg)
                av.pop(quien, None)
                cfg["telegram_avisos"] = av
                if not av:
                    cfg.pop("telegram_avisos", None)
                dest = "Avisos de " + quien
            else:
                destinatario(dest).pop("telegram_chat", None)
            nk.guardar_config(cfg)
            nk.registrar("Telegram desvinculado", dest, "")
            return self._ok()
        if ruta == "/api/alertas/probar":
            tipo = datos.get("tipo") if datos.get("tipo") in alertas.TIPOS else "apertura"
            return self._ok({"enviado_a": alertas.enviar(cfg, tipo, "Esto es una prueba del aviso")})
        if ruta == "/api/bateria/probar":
            return self._ok({"enviado_a": bateria.enviar_prueba(cfg)})

        m = re.fullmatch(r"/api/(renovar|borrar-codigo|eliminar|probar|invitado)/([\w-]+)", ruta)
        if m:
            accion, d = m.group(1), destinatario(m.group(2))
            if accion == "renovar":
                return self._ok(nk.renovar_codigo(cfg, nuki, d, simular, abrir=False,
                                                  codigo_elegido=(datos.get("codigo") or "").strip() or None))
            if accion == "probar":
                return self._ok({"enlace": nk.enviar_whatsapp(
                    cfg, d, f"Mensaje de prueba para {d['nombre']} desde el gestor de la cerradura.",
                    simular, abrir=False)})
            if accion == "invitado":
                tel = re.sub(r"[\s-]", "", datos.get("telefono") or "")
                if tel and not re.fullmatch(r"\+\d{8,15}", tel):
                    raise RuntimeError("El teléfono del invitado debe llevar prefijo, p. ej. +34600111222.")
                return self._ok(nk.crear_invitado(cfg, nuki, d, datos.get("horas", 24),
                                                  (datos.get("motivo") or "").strip(), tel or None,
                                                  simular, abrir=False))
            if accion == "borrar-codigo" and not cfg["nuki"].get("token") and not simular:
                raise RuntimeError("Falta el token de Nuki en Ajustes.")
            motivo = "Persona eliminada" if accion == "eliminar" else "Anulado a mano"
            borrados = nk.borrar_codigos_de(cfg, nuki, d, motivo=motivo) if cfg["nuki"].get("token") else 0
            if accion == "eliminar" and not simular:
                cfg["destinatarios"] = [x for x in cfg["destinatarios"] if x["id"] != d["id"]]
                nk.guardar_config(cfg)
                cuentas.boveda_borrar(f"callmebot:{d['id']}")
                nk.registrar("Persona eliminada", d["nombre"], "")
            return self._ok({"borrados": borrados})

        m = re.fullmatch(r"/api/anular-invitado/(.+)", ruta)
        if m:
            nk.anular_invitado(cfg, nuki, unquote(m.group(1)), simular)
            return self._ok()
        if ruta == "/api/rotar":
            return self._ok(nk.rotar_cuenta(cfg, simular))
        m = re.fullmatch(r"/api/borrar-auth/(\w+)", ruta)
        if m:
            nuki.borrar(cfg["nuki"]["smartlock_id"], m.group(1))
            nk.registrar("Código ajeno borrado", datos.get("nombre", ""), f"Autorización {m.group(1)}", simular)
            return self._ok()

        if ruta == "/api/cerradura/accion":
            acciones = {"abrir": (3, "Puerta abierta desde el panel"), "desbloquear": (1, "Cerradura desbloqueada desde el panel"),
                        "bloquear": (2, "Cerradura bloqueada desde el panel"),
                        "lockngo": (4, "Lock'n'Go desde el panel"), "portal": (3, "Portal abierto desde el panel")}
            if datos.get("accion") not in acciones:
                raise RuntimeError("Acción no permitida.")
            n, texto = acciones[datos["accion"]]
            if datos["accion"] == "portal":
                if not cfg["nuki"].get("opener_id"):
                    raise RuntimeError("Esta casa no tiene Opener.")
                nuki.accion(cfg["nuki"]["opener_id"], n)
            else:
                nuki.accion(cfg["nuki"]["smartlock_id"], n)
            nk.registrar(texto, s.get("nombre") or s["usuario"], "")
            return self._ok()
        if ruta == "/api/camara/accion":
            sn = datos.get("serial")
            if sn not in camara.CLIENTE.dispositivos or (not admin and camara.cuenta_de(sn) != cuentas.cuenta_actual()):
                raise RuntimeError("Cámara no encontrada en esta casa.")
            texto = camara.accion(sn, datos.get("que"), datos.get("valor"))
            if texto:
                nk.registrar("Cámara", s.get("nombre") or s["usuario"], texto)
            return self._ok({"texto": texto})
        if ruta == "/api/camara/evento":
            cuenta, ident = cuentas.cuenta_actual(), str(datos.get("id") or "")
            que = datos.get("accion")
            if que == "borrar":
                e = camara.borrar_evento(cuenta, ident)
                nk.registrar("Clip borrado" if e.get("clip") else "Foto borrada", s.get("nombre") or s["usuario"], f"{e.get('tipo', '')} · {e.get('hora', '')[:16].replace('T', ' ')}")
                return self._ok()
            if que in ("guardar", "no_guardar"):
                e = camara.marcar_guardar(cuenta, ident, que == "guardar")
                nk.registrar(("Clip" if e.get("clip") else "Foto") + (" marcado «no borrar»" if e.get("clip") else " marcada «no borrar»") if que == "guardar" else "Quitada la marca «no borrar»",
                             s.get("nombre") or s["usuario"], f"{e.get('tipo', '')} · {e.get('hora', '')[:16].replace('T', ' ')}")
                return self._ok({"guardar": bool(e.get("guardar"))})
            raise RuntimeError("Acción no válida.")
        if ruta == "/api/camaras/auto":
            def limpia(r):
                r = r or {}
                modo = r.get("modo")
                return {"modo": int(modo) if str(modo).lstrip("-").isdigit() else "",
                        "privacidad": r.get("privacidad") if r.get("privacidad") in ("apagar", "encender") else "",
                        "secciones": [str(x)[:40] for x in (r.get("secciones") or [])][:20]}
            propias = set(camara.camaras_de(cuentas.cuenta_actual()))
            cfg["camaras_auto"] = {"entrada": limpia(datos.get("entrada")), "salida": limpia(datos.get("salida")),
                                   "camaras": [x for x in (datos.get("camaras") or []) if x in propias]}
            nk.guardar_config(cfg)
            nk.registrar("Cámaras y cerradura", s.get("nombre") or s["usuario"], json.dumps(cfg["camaras_auto"], ensure_ascii=False))
            return self._ok()
        if ruta == "/api/programaciones":
            cfg["programaciones"] = nuki_extra.validar_programaciones(datos.get("lista"), cfg)
            nk.guardar_config(cfg)
            nk.registrar("Programaciones guardadas", s.get("nombre") or s["usuario"],
                         "; ".join(nuki_extra.texto_prog(p) + ("" if p["activa"] else " (pausada)")
                                   for p in cfg["programaciones"]) or "ninguna")
            return self._ok()
        if ruta == "/api/camara/probar":
            cuenta = cuentas.cuenta_actual()
            camaras = camara.camaras_de(cuenta)
            if datos.get("serial") in camaras:
                camaras = [datos["serial"]]
            elif camara.camara_puerta(cfg):
                camaras = [camara.camara_puerta(cfg)]
            if not camaras:
                raise RuntimeError("Esta cuenta no tiene ninguna cámara asignada (Ajustes → Cámara Eufy).")
            if not camara.CLIENTE.driver:
                raise RuntimeError("La cámara no está conectada ahora mismo.")
            momento = datetime.now().astimezone()
            destinos = camara.enviar_con_camara(cfg, "movimiento", "🧪 Prueba del aviso con cámara", momento,
                                                sn=camaras[0], con_clip=True)
            return self._ok({"destinos": destinos})

        if ruta == "/api/extras":  # recordatorio de la llave, resumen diario y avisos sin sonido (por casa)
            if datos.get("resumen_ahora"):
                n = extras._a_telegram(cfg, extras.resumen(cuentas.cuenta_actual(), cfg, datetime.now().date()))
                if not n:
                    raise RuntimeError("No hay nadie con Telegram vinculado para los avisos de esta casa.")
                return self._ok({"enviados": n})
            cfg["extras"] = extras.validar(datos, set(camara.camaras_de(cuentas.cuenta_actual())))
            nk.guardar_config(cfg)
            e = cfg["extras"]
            nk.registrar("Avisos extra", s.get("nombre") or s["usuario"],
                         f"Llave {'a las ' + e['llave']['hora'] if e['llave']['activo'] else 'no'} · "
                         f"resumen {'a las ' + e['resumen']['hora'] if e['resumen']['activo'] else 'no'} · "
                         f"sin sonido {e['silencio']['desde'] + '–' + e['silencio']['hasta'] if e['silencio']['activo'] else 'no'}")
            return self._ok(extras.ajustes(cfg))

        # ---------- solo administrador
        if not admin:
            return self._error(403, "Solo el administrador puede hacer esto.")
        if ruta == "/api/admin/copias":
            if datos.get("password"):
                cuentas_ok = cuentas.verificar_password(s["usuario"], datos.get("mi_password") or "")
                if not cuentas_ok:
                    raise RuntimeError("Tu contraseña del panel no es correcta.")
                copias.poner_clave(datos["password"])
                nk.registrar("Contraseña de las copias puesta", s.get("nombre") or s["usuario"], "")
            if any(k in datos for k in ("activo", "dia", "hora", "guardar")):
                antes = len(copias.listar())
                copias.poner_ajustes(datos.get("activo"), datos.get("dia"), datos.get("hora"), datos.get("guardar"))
                borradas = antes - len(copias.listar())
                if borradas > 0:
                    nk.registrar("Copias de seguridad borradas", s.get("nombre") or s["usuario"],
                                 f"{borradas} (se conservan {datos.get('guardar')})")
            if datos.get("borrar"):
                copias.borrar(datos["borrar"])
                nk.registrar("Copia de seguridad borrada", s.get("nombre") or s["usuario"], str(datos["borrar"]))
            if datos.get("reenviar"):
                n = copias.reenviar(datos["reenviar"])
                return self._ok({"enviados": n, **copias.ajustes()})
            if datos.get("hacer"):
                return self._ok({"texto": copias.hacer_copia(s.get("nombre") or s["usuario"]), **copias.ajustes()})
            return self._ok(copias.ajustes())
        if ruta == "/api/admin/nuki/ajustes":
            hecho = nuki_extra.guardar_ajustes(cfg, datos.get("cambios") or {})
            if hecho:
                nk.registrar("Ajustes de la cerradura", s.get("nombre") or s["usuario"], "; ".join(hecho))
            return self._ok({"cambios": hecho})
        if ruta == "/api/admin/nuki/acceso":
            return self._ok({"texto": nuki_extra.cambiar_acceso(cfg, datos.get("id"), datos.get("accion"))})
        if ruta == "/api/admin/nuki/opener":
            ident = datos.get("id")
            if ident:
                op = next((o for o in nuki_extra.capacidades(cfg, True).get("openers", []) if str(o["id"]) == str(ident)), None)
                if not op:
                    raise RuntimeError("No encuentro ese Opener en tu cuenta de Nuki.")
                cfg["nuki"]["opener_id"], cfg["nuki"]["opener_nombre"] = op["id"], op["nombre"]
                nk.registrar("Opener asignado", op["nombre"], "")
            else:
                cfg["nuki"].pop("opener_id", None)
                cfg["nuki"].pop("opener_nombre", None)
                cfg["programaciones"] = [p for p in cfg.get("programaciones") or [] if p["accion"] != "portal"]
                nk.registrar("Opener quitado", "", "")
            nk.guardar_config(cfg)
            return self._ok()
        if ruta == "/api/admin/camara":
            if datos.get("borrar"):
                camara.quitar_credenciales()
                nk.registrar("Cámara Eufy desconectada", "", "")
            else:
                camara.guardar_credenciales(datos.get("usuario"), datos.get("password"))
                nk.registrar("Cuenta de Eufy guardada", ocultar_texto(datos.get("usuario", "")), "")
            try:
                waha.accion_contenedor("reiniciar", camara.CONTENEDOR)
            except Exception as e:
                nk.log(f"Eufy: no se pudo reiniciar el contenedor ({e})")
            return self._ok(camara.estado())
        if ruta == "/api/admin/camara/captcha":
            c = camara.CLIENTE.captcha or {}
            camara.CLIENTE.comando("driver.set_captcha", captchaId=c.get("id"), captcha=(datos.get("codigo") or "").strip())
            camara.CLIENTE.captcha = None
            return self._ok()
        if ruta == "/api/admin/camara/codigo":
            camara.CLIENTE.comando("driver.set_verify_code", verifyCode=(datos.get("codigo") or "").strip())
            camara.CLIENTE.pide_codigo = False
            return self._ok()
        if ruta == "/api/admin/camara/asignar":
            if datos.get("cuenta") and not cuentas.existe_cuenta(datos["cuenta"]):
                raise RuntimeError("La cuenta no existe.")
            if datos.get("serial") not in camara.CLIENTE.dispositivos:
                raise RuntimeError("Cámara no encontrada.")
            camara.asignar(datos["serial"], datos.get("cuenta"), datos.get("predeterminada"), datos.get("movimiento"),
                           datos.get("tipos"))
            nk.registrar("Cámara configurada", camara.CLIENTE.dispositivos[datos["serial"]]["nombre"],
                         ", ".join(f"{k}: {datos[k]}" for k in ("cuenta", "predeterminada", "movimiento", "tipos") if k in datos))
            return self._ok()
        if ruta == "/api/admin/camara/ajuste":
            sn = datos.get("serial")
            if sn not in camara.CLIENTE.dispositivos:
                raise RuntimeError("Cámara no encontrada.")
            v = camara.guardar_ajuste(sn, datos.get("clave"), datos.get("valor"))
            etq = next((e for k, e, _ in camara.AJUSTES_CAM if k == datos.get("clave")), datos.get("clave"))
            nk.registrar("Ajuste de cámara", camara.CLIENTE.dispositivos[sn]["nombre"], f"{etq}: {v}")
            return self._ok()
        if ruta == "/api/admin/camara/dias":
            if datos.get("dias") is not None:
                camara.poner_dias_guardar(datos.get("dias"))
            if datos.get("modo"):
                camara.poner_borrado(datos["modo"], datos.get("dia"), datos.get("hora"))
            nk.registrar("Fotos y clips", "", camara.texto_borrado())
            return self._ok()
        if ruta == "/api/admin/camara/vaciar":
            c = datos.get("cuenta") or None
            if c and not cuentas.existe_cuenta(c):
                raise RuntimeError("La casa no existe.")
            n = camara.vaciar(c)
            nk.registrar("Fotos y clips borrados", "", f"{n} avisos (se han conservado los marcados con 📌)")
            return self._ok({"borrados": n})
        if ruta == "/api/admin/telegram":
            if datos.get("borrar"):
                telegram.borrar_token()
                nk.registrar("Bot de Telegram quitado", "", "")
                return self._ok({"configurado": False})
            bot = telegram.guardar_token(datos.get("token"))
            nk.registrar("Bot de Telegram configurado", "@" + (bot["usuario"] or ""), "")
            return self._ok({"configurado": True, "bot": bot["usuario"]})
        if ruta == "/api/admin/correo":
            r = bateria.guardar_smtp(datos)
            nk.registrar("Servidor de correo guardado", "", f"{r['servidor']}:{r['puerto']} ({r['seguridad']})")
            return self._ok(r)
        if ruta == "/api/admin/correo/probar":
            destinos = bateria.correos_validos(datos.get("correo") or "")
            bateria.enviar_correo(destinos, "Prueba del servidor de correo · Puerta",
                                  "Si lees esto, el gestor de la puerta ya puede enviar avisos por correo.\n")
            nk.registrar("Correo de prueba enviado", ", ".join(destinos), "Servidor de correo")
            return self._ok({"enviado_a": destinos})
        m = re.fullmatch(r"/api/docker/(arrancar|parar|reiniciar|iniciar-sesion|cerrar-sesion)", ruta)
        if m:
            a = m.group(1)
            {"iniciar-sesion": waha.iniciar_sesion, "cerrar-sesion": waha.cerrar_sesion}.get(
                a, lambda: waha.accion_contenedor(a))()
            return self._ok()
        if ruta == "/api/admin/nuki/cerraduras":  # para el asistente «Nueva casa» (no guarda nada)
            tok = (datos.get("token") or "").strip()
            if not tok:
                raise RuntimeError("Pega el token de Nuki Web.")
            return self._ok([{"id": c.get("smartlockId"), "nombre": (c.get("name") or "").strip()}
                             for c in nk.Nuki(tok).cerraduras()])
        m = re.fullmatch(r"/api/admin/casa/([\w-]+)", ruta)
        if m:  # editar una casa: nombre, cerradura Nuki (sí/no) y sus cámaras
            casa = m.group(1)
            if not cuentas.existe_cuenta(casa):
                raise RuntimeError("La casa no existe.")
            cambios = []
            if (datos.get("nombre") or "").strip():
                cuentas.renombrar_cuenta(casa, datos["nombre"].strip())
            with cuentas.en_cuenta(casa, s["usuario"]):
                c = nk.cargar_config()
                if datos.get("nuki") == "no":
                    tenia = bool(c["nuki"].get("token"))
                    c["solo_camara"] = True
                    c["nuki"]["token"], c["nuki"]["smartlock_id"] = "", 0
                    cuentas.boveda_borrar("nuki_token")
                    if tenia:
                        cambios.append("Nuki quitado")
                elif datos.get("nuki") == "si":
                    tok = (datos.get("token") or "").strip()
                    if tok:
                        c["nuki"]["token"] = tok
                    if not c["nuki"].get("token"):
                        raise RuntimeError("Pega el token de Nuki Web.")
                    sid = str(datos.get("smartlock_id") or "").strip()
                    if not sid.isdigit():
                        raise RuntimeError("Pulsa «Buscar» y elige la cerradura.")
                    c["nuki"]["smartlock_id"] = int(sid)
                    c["solo_camara"] = False
                    cambios.append("Nuki configurado")
                nk.guardar_config(c)
                if isinstance(datos.get("camaras"), dict):
                    for sn in camara.CLIENTE.dispositivos:
                        e = datos["camaras"].get(sn) or {}
                        if e.get("si"):
                            camara.asignar(sn, casa, bool(e.get("pred")), bool(e.get("mov")))
                        elif camara.cuenta_de(sn) == casa:
                            camara.asignar(sn, camara.SIN_CASA, False, False)
                    cambios.append("cámaras")
                nk.registrar("Casa editada", datos.get("nombre") or casa, ", ".join(cambios))
            return self._ok()
        if ruta == "/api/admin/cuenta":
            nombre = (datos.get("nombre") or "").strip()
            if not nombre:
                raise RuntimeError("Pon un nombre a la cuenta.")
            ident = cuentas.crear_cuenta(nombre)
            nk.registrar("Cuenta creada", nombre, ident)
            return self._ok(ident)
        m = re.fullmatch(r"/api/admin/cuenta/([\w-]+)/renombrar", ruta)
        if m:
            cuentas.renombrar_cuenta(m.group(1), datos.get("nombre") or m.group(1))
            return self._ok()
        if ruta == "/api/admin/usuario":
            cuentas.crear_usuario(datos.get("usuario"), datos.get("nombre"), datos.get("password") or "",
                                  datos.get("rol", "gestor"), datos.get("cuenta"))
            nk.registrar("Usuario del panel creado", datos.get("usuario"), f"Cuenta: {datos.get('cuenta')}")
            return self._ok()
        m = re.fullmatch(r"/api/admin/usuario/([\w.-]+)/(password|borrar|editar)", ruta)
        if m:
            usuario, accion = m.groups()
            if accion == "password":
                cuentas.cambiar_password(usuario, datos.get("password") or "")
                with _cerrojo:
                    for t in [t for t, x in SESIONES.items() if x["usuario"] == usuario]:
                        SESIONES.pop(t)
                nk.registrar("Contraseña restablecida", usuario)
            elif accion == "borrar":
                if usuario == s["usuario"]:
                    raise RuntimeError("No puedes borrarte a ti mismo.")
                cuentas.borrar_usuario(usuario)
                nk.registrar("Usuario del panel borrado", usuario)
            else:
                cuentas.modificar_usuario(usuario, nombre=datos.get("nombre"), rol=datos.get("rol"),
                                          cuenta=datos.get("cuenta"))
                nk.registrar("Usuario del panel modificado", usuario)
            return self._ok()
        return self._error(404, "No encontrado")

    # ---- guardados
    @staticmethod
    def _guardar_config(cfg, datos):
        if "solo_camara" in datos:
            cfg["solo_camara"] = bool(datos["solo_camara"])
        if datos.get("token"):
            cfg["nuki"]["token"] = datos["token"].strip()
        if "smartlock_id" in datos:
            sid = str(datos["smartlock_id"] or "").strip()
            if sid and not sid.isdigit():
                # ID de la app de Nuki (hexadecimal, p. ej. 3538C0F4): traducirlo al de la API
                nk.guardar_config(cfg)  # que no se pierda el token
                if not cfg["nuki"].get("token"):
                    raise RuntimeError("Pega primero el token para poder traducir el ID de la cerradura.")
                hexid = sid.upper().replace(" ", "")
                encontradas = [c["smartlockId"] for c in nk.Nuki(cfg["nuki"]["token"]).cerraduras()
                               if format(int(c["smartlockId"]), "X").endswith(hexid)]
                if not encontradas:
                    raise RuntimeError(f"No encuentro ninguna cerradura con el ID {sid}. Pulsa «Buscar».")
                sid = str(encontradas[0])
            cfg["nuki"]["smartlock_id"] = int(sid or 0)
        cfg["dias_validez"] = max(1, min(60, int(datos.get("dias_validez") or 7)))
        if datos.get("vigilar_bajas_dias") not in (None, ""):
            cfg["vigilar_bajas_dias"] = max(0, min(365, int(datos["vigilar_bajas_dias"])))
        for clave in ("mensaje", "mensaje_fijo", "mensaje_invitado"):
            if datos.get(clave):
                cfg[clave] = datos[clave][:1000]
        if datos.get("proveedor") in ("enlace", "waha", "callmebot", "twilio"):
            cfg["whatsapp"]["proveedor"] = datos["proveedor"]
        tw = cfg["whatsapp"]["twilio"]
        tw["account_sid"] = (datos.get("twilio_sid") or tw.get("account_sid", "")).strip()
        tw["numero_origen"] = (datos.get("twilio_numero") or tw.get("numero_origen", "")).strip()
        if datos.get("twilio_token"):
            tw["auth_token"] = datos["twilio_token"].strip()
        if isinstance(datos.get("rotacion"), dict):
            r = datos["rotacion"]
            cfg["rotacion"] = {"activa": bool(r.get("activa")), "dia": int(r.get("dia", 0)) % 7,
                               "hora": max(0, min(23, int(r.get("hora", 9)))),
                               "minuto": max(0, min(59, int(r.get("minuto", 0))))}
        if isinstance(datos.get("aviso_bateria"), dict):
            a = datos["aviso_bateria"]
            anterior = bateria.ajustes_aviso(cfg)
            destino = validar_destino(cfg, a, anterior, bool(a.get("activo")), "Aviso de batería")
            cfg["aviso_bateria"] = {"activo": bool(a.get("activo")), **destino,
                                    "umbral": max(5, min(90, int(a.get("umbral") or 25)))}
        if isinstance(datos.get("alertas"), dict):
            nuevo = alertas.validar(datos["alertas"])
            activos = any(r["activo"] for r in nuevo["reglas"].values())
            nuevo["destino"] = validar_destino(cfg, (datos["alertas"].get("destino") or {}),
                                               alertas.ajustes(cfg)["destino"], activos, "Avisos de la puerta")
            cfg["alertas"] = nuevo
        nk.guardar_config(cfg)
        rot = cfg["rotacion"]
        nk.registrar("Ajustes guardados", "", f"WhatsApp: {cfg['whatsapp']['proveedor']} · validez "
                     f"{cfg['dias_validez']} días · renovación: "
                     + (f"{DIAS[rot['dia']]} {rot['hora']:02d}:{rot['minuto']:02d}" if rot["activa"] else "desactivada"))
        return config_publica(cfg)

    @staticmethod
    def _secciones(cfg, datos):
        """Crear, renombrar, borrar y ordenar secciones. Renombrar o borrar actualiza a las personas."""
        lista = lista_secciones(cfg)
        accion = datos.get("accion")
        if accion == "crear":
            nombre = limpiar_nombre_seccion(datos.get("nombre"))
            if nombre.lower() in (x.lower() for x in lista):
                raise RuntimeError(f"Ya existe la sección «{nombre}».")
            lista.append(nombre)
            nk.registrar("Sección creada", nombre, "")
        elif accion == "renombrar":
            viejo, nuevo = datos.get("nombre"), limpiar_nombre_seccion(datos.get("nuevo"))
            if viejo not in lista:
                raise RuntimeError("Esa sección no existe.")
            if nuevo != viejo and nuevo.lower() in (x.lower() for x in lista if x != viejo):
                raise RuntimeError(f"Ya existe la sección «{nuevo}».")
            lista[lista.index(viejo)] = nuevo
            for d in cfg["destinatarios"]:
                if d.get("seccion") == viejo:
                    d["seccion"] = nuevo
            nk.registrar("Sección renombrada", nuevo, f"Antes: {viejo}")
        elif accion == "borrar":
            nombre = datos.get("nombre")
            if nombre not in lista:
                raise RuntimeError("Esa sección no existe.")
            lista.remove(nombre)
            n = 0
            for d in cfg["destinatarios"]:
                if d.get("seccion") == nombre:
                    d.pop("seccion", None)
                    n += 1
            nk.registrar("Sección borrada", nombre, f"{n} persona(s) pasan a «Sin sección»")
        elif accion == "mover":
            nombre, paso = datos.get("nombre"), int(datos.get("paso") or 0)
            if nombre not in lista:
                raise RuntimeError("Esa sección no existe.")
            i = lista.index(nombre)
            j = max(0, min(len(lista) - 1, i + paso))
            lista.insert(j, lista.pop(i))
        else:
            raise RuntimeError("Acción no válida.")
        cfg["secciones"] = lista
        nk.guardar_config(cfg)
        return lista

    @staticmethod
    def _guardar_destinatario(cfg, datos):
        nombre = (datos.get("nombre") or "").strip()[:60]
        if not nombre:
            raise RuntimeError("El nombre es obligatorio.")
        ident = datos.get("id")
        existente = next((x for x in cfg["destinatarios"] if x["id"] == ident), None) if ident else None
        telefono = re.sub(r"[\s-]", "", datos.get("telefono") or "")
        if not telefono and existente:
            telefono = existente["telefono"]  # al editar, vacío = no cambiar
        if not re.fullmatch(r"\+\d{8,15}", telefono):
            raise RuntimeError("El teléfono debe ir con prefijo internacional, p. ej. +34600111222.")
        if existente is None:
            base = cuentas.a_slug(nombre, 12)
            ident, n = base, 2
            usados = {x["id"] for x in cfg["destinatarios"]}
            while ident in usados:
                ident, n = f"{base}{n}", n + 1
            existente = {"id": ident}
            cfg["destinatarios"].append(existente)
            nk.registrar("Persona añadida", nombre, telefono)
        else:
            nk.registrar("Persona modificada", nombre, telefono)
        tipo = datos.get("tipo") or "semanal"
        if tipo not in ("semanal", "manual", "fijo"):
            raise RuntimeError("Tipo de código no válido.")
        existente.update({"nombre": nombre, "telefono": telefono,
                          "automatico": tipo == "semanal", "fijo": tipo == "fijo"})
        if datos.get("canal_envio") in nk.CANALES_ENVIO:
            existente["canal_envio"] = datos["canal_envio"]
        if "seccion" in datos:
            seccion = " ".join(str(datos.get("seccion") or "").split())[:40]
            if seccion:
                existente["seccion"] = seccion
                if seccion not in cfg.setdefault("secciones", []):
                    cfg["secciones"].append(seccion)
            else:
                existente.pop("seccion", None)
        if datos.get("callmebot_apikey"):
            existente["callmebot_apikey"] = datos["callmebot_apikey"].strip()
        if "franja" in datos:
            franja = nk.validar_franja(datos.get("franja"))
            if franja != existente.get("franja"):
                if franja:
                    existente["franja"] = franja
                else:
                    existente.pop("franja", None)
                nk.registrar("Horario del código", nombre, nk.texto_franja(franja) or "a cualquier hora")
                Manejador._aplicar_franja(cfg, existente)
        nk.guardar_config(cfg)
        return existente["id"]

    @staticmethod
    def _aplicar_franja(cfg, persona):
        """Si ya tiene código en la cerradura, le cambia el horario sin cambiar el número."""
        if not (cfg["nuki"].get("token") and cfg["nuki"].get("smartlock_id")):
            return
        nuki = nk.Nuki(cfg["nuki"]["token"])
        sid = cfg["nuki"]["smartlock_id"]
        nombre_auth = nk.nombre_auth_de(cfg, persona)
        dias, de, a = nk.franja_nuki(persona.get("franja"))
        for auth in nuki.autorizaciones(sid):
            if auth.get("type") == nk.TIPO_CODIGO_TECLADO and auth.get("name") == nombre_auth and auth.get("enabled", True):
                nuki.actualizar_auth(sid, auth, {"allowedWeekDays": dias, "allowedFromTime": de, "allowedUntilTime": a})


def main():
    global CODIGO_INSTALACION
    cuentas.migrar_si_hace_falta(nk.log)
    if not cuentas.hay_usuarios():
        CODIGO_INSTALACION = "-".join(secrets.token_hex(2).upper() for _ in range(3))
        aviso = cuentas.DATOS / "codigo_instalacion.txt"
        aviso.parent.mkdir(parents=True, exist_ok=True)
        aviso.write_text(CODIGO_INSTALACION + "\n")
        aviso.chmod(0o600)
        print("=" * 60)
        print(f"  PRIMER ARRANQUE · Código de instalación: {CODIGO_INSTALACION}")
        print("  Escríbelo en el panel para crear el administrador.")
        print("=" * 60, flush=True)
    if not cuentas.existe_cuenta(cuentas.CUENTA_PRINCIPAL):
        cuentas.carpeta_cuenta(cuentas.CUENTA_PRINCIPAL)
        cuentas.renombrar_cuenta(cuentas.CUENTA_PRINCIPAL, "Mi casa")
    cargar_sesiones()
    GuardaSesiones().start()
    waha.Vigilante().start()
    Programador().start()
    alertas.Vigilante().start()
    extras.Extras().start()
    telegram.Escucha().start()
    camara.CLIENTE.start()
    camara.Limpieza().start()
    camara.VigilaCamaras().start()
    servidor = ThreadingHTTPServer((ESCUCHAR, PUERTO), Manejador)
    print(f"Panel escuchando en http://{ESCUCHAR}:{PUERTO}", flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nPanel cerrado.")


def recuperar_por_consola(usuario):
    """Último recurso desde el servidor:  docker exec -it puerta-panel python panel.py --recuperar USUARIO"""
    usuario = (usuario or "").strip().lower()
    u = cuentas.buscar_usuario(usuario)
    if not u:
        print("No existe ese usuario. Usuarios:", ", ".join(x["usuario"] for x in cuentas.usuarios_publicos()))
        return 1
    temporal = "-".join(secrets.token_hex(3) for _ in range(3))
    cuentas.cambiar_password(usuario, temporal)
    with cuentas.en_cuenta(u["cuenta"], usuario):
        nk.registrar("Contraseña restablecida", usuario, "Desde la consola del servidor")
    print(f"Contraseña temporal de «{usuario}»: {temporal}")
    print("Entra con ella y cámbiala en el menú de usuario → Cambiar contraseña.")
    print("Si te sale «Demasiados intentos», reinicia el panel:  docker restart puerta-panel")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--recuperar":
        sys.exit(recuperar_por_consola(sys.argv[2]))
    main()
