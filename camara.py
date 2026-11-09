"""Cámaras Eufy a través de eufy-security-ws (contenedor «puerta-eufy»).

- Usuario y contraseña de la cuenta secundaria de Eufy: los escribe el administrador en el panel.
  Van a la bóveda y, para que el contenedor de Eufy pueda usarlos, a datos/eufy/ (permisos 600).
- Este módulo mantiene una conexión WebSocket con ese contenedor y recibe sus eventos:
  movimiento, persona, timbre... y la foto de cada evento.
- Para los clips se abre el vídeo en directo unos segundos y se convierte a MP4 con ffmpeg.
- Fotos y clips se guardan en datos/camara/<cuenta>/ y se borran a los 30 días.
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import cuentas
import nuki_codigos as nk

URL = os.environ.get("EUFY_URL", "ws://eufy:3000")
CONTENEDOR = os.environ.get("EUFY_CONTENEDOR", "puerta-eufy")
CARPETA_CRED = cuentas.DATOS / "eufy"
CARPETA_MEDIA = cuentas.DATOS / "camara"
DIAS_GUARDAR = 30
ESPERA_ENTRE_AVISOS = 120  # segundos entre avisos de movimiento de la misma cámara
SEGUNDOS_CLIP = 12
RE_ARCHIVO = re.compile(r"^[\w-]+\.(jpg|mp4)$")

EVENTOS = {
    "motion detected": "Movimiento", "radar motion detected": "Movimiento",
    "person detected": "Persona", "stranger person detected": "Persona desconocida",
    "someone loitering": "Alguien merodeando", "rings": "Timbre",
}


def _bytes(buf):
    if isinstance(buf, dict) and isinstance(buf.get("data"), list):
        return bytes(buf["data"])
    if isinstance(buf, list):
        return bytes(buf)
    return b""


# ---------------------------------------------------------------- credenciales

def credenciales_puestas():
    return (CARPETA_CRED / "usuario").is_file() and (CARPETA_CRED / "contrasena").is_file()


def guardar_credenciales(usuario, password):
    usuario = (usuario or "").strip()
    if not usuario or "@" not in usuario:
        raise RuntimeError("Escribe el correo de la cuenta secundaria de Eufy.")
    if not password and not cuentas.boveda_leer("eufy_password", cuentas.CUENTA_SISTEMA):
        raise RuntimeError("Escribe la contraseña de la cuenta de Eufy.")
    if password:
        cuentas.boveda_guardar("eufy_password", password, cuentas.CUENTA_SISTEMA)
    s = cuentas.leer_sistema()
    s.setdefault("eufy", {})["usuario"] = usuario
    cuentas.guardar_sistema(s)
    _escribir_archivos_cred()


def _escribir_archivos_cred():
    usuario = (cuentas.leer_sistema().get("eufy") or {}).get("usuario")
    password = cuentas.boveda_leer("eufy_password", cuentas.CUENTA_SISTEMA)
    if not (usuario and password):
        return False
    CARPETA_CRED.mkdir(parents=True, exist_ok=True)
    os.chmod(CARPETA_CRED, 0o700)
    for nombre, valor in (("usuario", usuario), ("contrasena", password)):
        ruta = CARPETA_CRED / nombre
        tmp = ruta.with_suffix(".tmp")
        tmp.write_text(valor)
        os.chmod(tmp, 0o600)
        tmp.replace(ruta)
    return True


def quitar_credenciales():
    cuentas.boveda_borrar("eufy_password", cuentas.CUENTA_SISTEMA)
    s = cuentas.leer_sistema()
    s.pop("eufy", None)
    cuentas.guardar_sistema(s)
    shutil.rmtree(CARPETA_CRED, ignore_errors=True)


def ajustes_sistema():
    return cuentas.leer_sistema().get("eufy") or {}


def ajustes_camara(serial):
    d = (ajustes_sistema().get("dispositivos") or {}).get(serial) or {}
    return {"cuenta": d.get("cuenta", cuentas.CUENTA_PRINCIPAL), "predeterminada": bool(d.get("predeterminada")),
            "movimiento": bool(d.get("movimiento"))}


def cuenta_de(serial):
    return ajustes_camara(serial)["cuenta"]


def asignar(serial, cuenta=None, predeterminada=None, movimiento=None):
    s = cuentas.leer_sistema()
    disp = s.setdefault("eufy", {}).setdefault("dispositivos", {})
    d = disp.setdefault(serial, {"cuenta": cuentas.CUENTA_PRINCIPAL})
    if cuenta and cuenta != d.get("cuenta"):
        d["cuenta"] = cuenta
        d["predeterminada"] = False  # al cambiar de casa deja de ser la predeterminada de la anterior
    if movimiento is not None:
        d["movimiento"] = bool(movimiento)
    if predeterminada is not None:
        if predeterminada:  # solo una predeterminada por casa
            for sn, otra in disp.items():
                if sn != serial and otra.get("cuenta", cuentas.CUENTA_PRINCIPAL) == d["cuenta"]:
                    otra["predeterminada"] = False
        d["predeterminada"] = bool(predeterminada)
    cuentas.guardar_sistema(s)


def camara_puerta(cfg):
    """Cámara elegida para acompañar los avisos de la puerta (o la única que haya)."""
    propias = camaras_de(cuentas.cuenta_actual())
    elegida = next((sn for sn in propias if ajustes_camara(sn)["predeterminada"]), None)
    return elegida or (propias[0] if len(propias) == 1 else None)


SIN_CASA = "_ninguna"  # cámara que no se muestra en ninguna casa


def camaras_de(cuenta):
    return [sn for sn, d in CLIENTE.dispositivos.items() if cuenta_de(sn) == cuenta and d.get("camara", True)]


# ---------------------------------------------------------------- archivos

def carpeta(cuenta):
    c = CARPETA_MEDIA / cuenta
    c.mkdir(parents=True, exist_ok=True)
    return c


def guardar_evento(cuenta, evento):
    with open(carpeta(cuenta) / "eventos.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(evento, ensure_ascii=False) + "\n")


def actualizar_evento(cuenta, ident, **cambios):
    ruta = carpeta(cuenta) / "eventos.jsonl"
    if not ruta.exists():
        return
    lineas = ruta.read_text(encoding="utf-8").splitlines()
    for i, l in enumerate(lineas):
        try:
            e = json.loads(l)
        except ValueError:
            continue
        if e.get("id") == ident:
            e.update(cambios)
            lineas[i] = json.dumps(e, ensure_ascii=False)
    ruta.write_text("\n".join(lineas) + "\n", encoding="utf-8")


def leer_eventos(cuenta, limite=50):
    ruta = carpeta(cuenta) / "eventos.jsonl"
    if not ruta.exists():
        return []
    salida = []
    for l in ruta.read_text(encoding="utf-8").splitlines()[-limite:]:
        try:
            salida.append(json.loads(l))
        except ValueError:
            pass
    return list(reversed(salida))


def evento(cuenta, ident):
    return next((e for e in leer_eventos(cuenta, 2000) if e.get("id") == ident), None)


def ruta_archivo(cuenta, nombre):
    if not RE_ARCHIVO.match(nombre or ""):
        raise RuntimeError("Archivo no válido.")
    ruta = carpeta(cuenta) / nombre
    if not ruta.is_file():
        raise RuntimeError("Ese archivo ya no existe (se borran a los 30 días).")
    return ruta


def limpiar_antiguos():
    limite = time.time() - DIAS_GUARDAR * 86400
    if not CARPETA_MEDIA.exists():
        return
    for c in CARPETA_MEDIA.iterdir():
        if not c.is_dir():
            continue
        for f in c.iterdir():
            if f.suffix in (".jpg", ".mp4") and f.stat().st_mtime < limite:
                f.unlink(missing_ok=True)
        ruta = c / "eventos.jsonl"
        if ruta.exists():
            corte = (datetime.now().astimezone() - timedelta(days=DIAS_GUARDAR)).isoformat()
            vivas = []
            for l in ruta.read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(l).get("hora", "") >= corte:
                        vivas.append(l)
                except ValueError:
                    pass
            ruta.write_text("\n".join(vivas) + ("\n" if vivas else ""), encoding="utf-8")


def url_evento(ident):
    base = (os.environ.get("PANEL_URL_PUBLICA") or cuentas.leer_sistema().get("url_publica") or "").rstrip("/")
    return f"{base}/#evento={ident}" if base else ""


# ---------------------------------------------------------------- cliente WebSocket

class Cliente(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.ws = None
        self.envio = threading.Lock()
        self.conectado = False          # WebSocket con el contenedor
        self.driver = False             # sesión con la nube de Eufy
        self.error = ""
        self.captcha = None             # {"id", "imagen"}
        self.pide_codigo = False
        self.dispositivos = {}          # serial -> {nombre, modelo, camara}
        self.pendientes = {}            # messageId -> [Event, resultado]
        self.fotos = {}                 # serial -> (momento, bytes)
        self.foto_nueva = threading.Condition()
        self.oyentes = {}               # serial -> set de funciones que reciben (bytes, metadata) del directo
        self.cerrojo_directo = threading.Lock()
        self.bloqueo_clip = {}
        self.ultimo_aviso = {}
        self.recientes = deque(maxlen=200)  # (momento, cuenta, ident)

    # ---------- conexión
    def run(self):
        import websocket  # websocket-client
        espera = 5
        while True:
            if not credenciales_puestas():
                if not _escribir_archivos_cred():
                    self.conectado = self.driver = False
                    time.sleep(15)
                    continue
            try:
                self.ws = websocket.create_connection(URL, timeout=15)
                self.conectado, self.error, espera = True, "", 5
                self.ws.settimeout(60)
                while True:
                    try:
                        crudo = self.ws.recv()
                    except websocket.WebSocketTimeoutException:
                        self.ws.ping()
                        continue
                    if not crudo:
                        break
                    self._mensaje(json.loads(crudo))
            except Exception as e:
                if self.conectado:
                    nk.log(f"Eufy: conexión perdida ({e})")
                self.error = "" if "Connection refused" in str(e) or "Name or service" in str(e) else str(e)[:200]
            self.conectado = self.driver = False
            try:
                self.ws and self.ws.close()
            except Exception:
                pass
            time.sleep(espera)
            espera = min(60, espera * 2)

    def _enviar(self, mensaje):
        with self.envio:
            if not self.ws:
                raise RuntimeError("La cámara no está conectada.")
            self.ws.send(json.dumps(mensaje))

    def comando(self, orden, timeout=30, **datos):
        ident = secrets.token_hex(6)
        ev = threading.Event()
        self.pendientes[ident] = [ev, None]
        self._enviar({"messageId": ident, "command": orden, **datos})
        if not ev.wait(timeout):
            self.pendientes.pop(ident, None)
            raise RuntimeError(f"Eufy no respondió a {orden}")
        r = self.pendientes.pop(ident)[1]
        if not r.get("success"):
            raise RuntimeError(f"Eufy: {r.get('errorCode') or r.get('message') or 'error'}")
        return r.get("result") or {}

    # ---------- mensajes
    def _mensaje(self, m):
        tipo = m.get("type")
        if tipo == "version":
            self._enviar({"messageId": "_schema", "command": "set_api_schema", "schemaVersion": min(21, m.get("maxSchemaVersion", 21))})
            self._enviar({"messageId": "_escuchar", "command": "start_listening"})
        elif tipo == "result":
            ident = m.get("messageId")
            if ident == "_escuchar" and m.get("success"):
                estado = (m.get("result") or {}).get("state") or {}
                self.driver = bool((estado.get("driver") or {}).get("connected"))
                nk.log(f"Eufy: conectado al puente (sesión {'activa' if self.driver else 'sin iniciar'}, "
                       f"{len(estado.get('devices') or [])} cámaras)")
                for sn in estado.get("devices") or []:
                    self._enviar({"messageId": f"_props:{sn}", "command": "device.get_properties", "serialNumber": sn})
            elif ident and ident.startswith("_props:") and m.get("success"):
                p = ((m.get("result") or {}).get("properties")) or {}
                sn = ident.split(":", 1)[1]
                nuevo = sn not in self.dispositivos
                self.dispositivos[sn] = {"nombre": p.get("name") or sn, "modelo": p.get("model", ""),
                                         "camara": True, "bateria": p.get("battery")}
                if nuevo:
                    nk.log(f"Eufy: cámara «{self.dispositivos[sn]['nombre']}» lista")
                foto = _bytes((p.get("picture") or {}).get("data"))
                if foto and sn not in self.fotos:
                    self.fotos[sn] = (0, foto)
            elif ident in self.pendientes:
                self.pendientes[ident][1] = m
                self.pendientes[ident][0].set()
        elif tipo == "event":
            self._evento(m.get("event") or {})

    def _evento(self, e):
        origen, nombre = e.get("source"), e.get("event")
        if origen == "driver":
            if nombre == "captcha request":
                self.captcha = {"id": e.get("captchaId"), "imagen": e.get("captcha")}
                nk.log("Eufy pide un captcha: resuélvelo en Ajustes → Cámara Eufy")
            elif nombre == "verify code":
                self.pide_codigo = True
                nk.log("Eufy pide un código de verificación: escríbelo en Ajustes → Cámara Eufy")
            elif nombre == "connected":
                self.driver, self.captcha, self.pide_codigo = True, None, False
                self._enviar({"messageId": "_escuchar", "command": "start_listening"})
            elif nombre == "disconnected":
                self.driver = False
            return
        if origen != "device":
            return
        sn = e.get("serialNumber")
        if nombre == "property changed" and e.get("name") == "picture":
            foto = _bytes((e.get("value") or {}).get("data"))
            if foto:
                with self.foto_nueva:
                    self.fotos[sn] = (time.time(), foto)
                    self.foto_nueva.notify_all()
        elif nombre == "livestream video data":
            trozo, meta = _bytes(e.get("buffer")), e.get("metadata") or {}
            for f in list(self.oyentes.get(sn, ())):
                try:
                    f(trozo, meta)
                except Exception:
                    pass
        elif nombre == "livestream stopped":
            for f in list(self.oyentes.get(sn, ())):
                try:
                    f(None, {})  # aviso de fin
                except Exception:
                    pass
        elif nombre == "device added":
            self._enviar({"messageId": f"_props:{sn}", "command": "device.get_properties", "serialNumber": sn})
        elif nombre in EVENTOS and e.get("state", True):
            threading.Thread(target=self._procesar_evento, args=(sn, EVENTOS[nombre]), daemon=True).start()

    # ---------- fotos y clips
    def esperar_foto(self, sn, desde, segundos=10):
        fin = time.time() + segundos
        with self.foto_nueva:
            while time.time() < fin:
                t, foto = self.fotos.get(sn, (0, None))
                if foto and t >= desde - 2:
                    return foto
                self.foto_nueva.wait(fin - time.time())
        return None

    # ---------- directo compartido (clips y vista en directo usan la misma emisión)
    def unirse(self, sn, funcion):
        with self.cerrojo_directo:
            primero = not self.oyentes.get(sn)
            self.oyentes.setdefault(sn, set()).add(funcion)
        if primero:
            try:
                self.comando("device.start_livestream", timeout=40, serialNumber=sn)
            except RuntimeError as e:
                if "already" not in str(e).lower():
                    self.salir(sn, funcion, parar=False)
                    raise

    def salir(self, sn, funcion, parar=True):
        with self.cerrojo_directo:
            grupo = self.oyentes.get(sn, set())
            grupo.discard(funcion)
            ultimo = not grupo
            if ultimo:
                self.oyentes.pop(sn, None)
        if ultimo and parar:
            try:
                self.comando("device.stop_livestream", timeout=15, serialNumber=sn)
            except Exception:
                pass

    def clip(self, sn, segundos=SEGUNDOS_CLIP):
        """Graba unos segundos del directo y devuelve el MP4 (bytes) o None."""
        cerrojo = self.bloqueo_clip.setdefault(sn, threading.Lock())
        if not cerrojo.acquire(timeout=1):
            return None  # ya se está grabando un clip de esta cámara
        c = {"datos": bytearray(), "meta": {}}

        def recibir(trozo, meta):
            if trozo:
                c["datos"] += trozo
                c["meta"] = meta or c["meta"]
                c.setdefault("primero", time.time())
        try:
            self.unirse(sn, recibir)
            fin = time.time() + 30
            while time.time() < fin:
                if c.get("primero") and time.time() - c["primero"] >= segundos:
                    break
                time.sleep(0.3)
            self.salir(sn, recibir)
            if len(c["datos"]) < 2000:
                return None
            return _a_mp4(bytes(c["datos"]), c["meta"])
        except Exception as e:
            nk.log(f"Eufy: no se pudo grabar el clip ({e})")
            self.salir(sn, recibir)
            return None
        finally:
            cerrojo.release()

    def directo_mjpeg(self, sn, segundos_max=300, ancho=960, fps=8):
        """Generador de imágenes JPEG del directo (para mostrarlo en el navegador como MJPEG).
        Termina al cerrarse el generador (el navegador se va) o a los segundos_max."""
        import queue
        cola = queue.Queue(maxsize=200)

        def recibir(trozo, meta):
            try:
                cola.put_nowait((trozo, meta))
            except queue.Full:
                pass  # si va con retraso, se descartan trozos
        self.unirse(sn, recibir)
        proceso = None
        try:
            trozo, meta = cola.get(timeout=30)
            if trozo is None:
                return
            codec = str(meta.get("videoCodec", "H264")).upper()
            formato = "hevc" if codec in ("H265", "HEVC", "1") else "h264"
            proceso = subprocess.Popen(
                ["ffmpeg", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay", "-threads", "1",
                 "-f", formato, "-i", "pipe:0", "-vf", f"fps={fps},scale={ancho}:-2", "-q:v", "7",
                 "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

            def alimentar(primero):
                try:
                    proceso.stdin.write(primero)
                    while True:
                        t, _ = cola.get(timeout=20)
                        if t is None:
                            break
                        proceso.stdin.write(t)
                except Exception:
                    pass
                finally:
                    try:
                        proceso.stdin.close()
                    except Exception:
                        pass
            threading.Thread(target=alimentar, args=(trozo,), daemon=True).start()
            fin, buf = time.time() + segundos_max, b""
            while time.time() < fin:
                datos = proceso.stdout.read1(65536) if hasattr(proceso.stdout, "read1") else proceso.stdout.read(4096)
                if not datos:
                    break
                buf += datos
                while True:
                    a = buf.find(b"\xff\xd8")
                    b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                    if b < 0:
                        break
                    yield buf[a:b + 2]
                    buf = buf[b + 2:]
        finally:
            self.salir(sn, recibir)
            try:
                cola.put_nowait((None, {}))
            except Exception:
                pass
            if proceso:
                proceso.kill()
                proceso.wait(timeout=5)

    # ---------- evento de la cámara
    def _procesar_evento(self, sn, que):
        import alertas
        momento = datetime.now().astimezone()
        cuenta = cuenta_de(sn)
        if not ajustes_camara(sn)["movimiento"]:
            return  # esta cámara no avisa de movimiento (se elige en Ajustes → Cámara Eufy)
        nombre_cam = self.dispositivos.get(sn, {}).get("nombre", sn)
        ident = momento.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        foto = self.esperar_foto(sn, time.time())
        ev = {"id": ident, "hora": momento.isoformat(timespec="seconds"), "camara": nombre_cam, "serial": sn,
              "tipo": que, "foto": "", "clip": ""}
        if foto:
            (carpeta(cuenta) / f"{ident}.jpg").write_bytes(foto)
            ev["foto"] = f"{ident}.jpg"
        guardar_evento(cuenta, ev)
        self.recientes.append((time.time(), cuenta, ident))
        if time.time() - self.ultimo_aviso.get(sn, 0) < ESPERA_ENTRE_AVISOS:
            return
        with cuentas.en_cuenta(cuenta, "cámara"):
            cfg = nk.cargar_config()
            regla = alertas.ajustes(cfg)["reglas"]["movimiento"]
            if not (regla["activo"] and alertas.en_franja(regla, momento)):
                return
            self.ultimo_aviso[sn] = time.time()
            enviar_con_camara(cfg, "movimiento", f"{que} · {nombre_cam}", momento, ident_existente=ident,
                              foto=foto, sn=sn, con_clip=regla.get("clip", True))


CLIENTE = Cliente()


def _a_mp4(crudo, meta):
    codec = str(meta.get("videoCodec", "H264")).upper()
    formato = "hevc" if codec in ("H265", "HEVC", "1") else "h264"
    fps = int(meta.get("videoFPS") or 15) or 15
    with tempfile.TemporaryDirectory() as d:
        entrada, salida = Path(d) / f"in.{formato}", Path(d) / "out.mp4"
        entrada.write_bytes(crudo)
        orden = ["ffmpeg", "-y", "-loglevel", "error", "-f", formato, "-framerate", str(fps), "-i", str(entrada),
                 "-c:v", "copy", "-movflags", "+faststart"]
        if formato == "hevc":
            orden += ["-tag:v", "hvc1"]
        r = subprocess.run(orden + [str(salida)], capture_output=True, timeout=60)
        if r.returncode != 0 or not salida.exists():
            nk.log(f"Eufy: ffmpeg falló: {r.stderr.decode(errors='replace')[:300]}")
            return None
        return salida.read_bytes()


# ---------------------------------------------------------------- avisos con foto y clip

def foto_cercana(sn, segundos=180):
    """Foto de esa cámara tomada en los últimos minutos (para los avisos de Nuki)."""
    t, foto = CLIENTE.fotos.get(sn, (0, None))
    return foto if foto and time.time() - t < segundos else None


def enviar_con_camara(cfg, tipo, detalle, momento, ident_existente=None, foto=None, sn=None, con_clip=True):
    """Envía el aviso con la foto (Telegram) o el enlace (WhatsApp/correo) y después, en segundo plano, el clip."""
    import alertas
    import bateria
    cuenta = cuentas.cuenta_actual()
    sn = sn or camara_puerta(cfg)
    if not sn and not foto:
        return alertas.enviar(cfg, tipo, detalle, momento)
    if foto is None and sn:
        foto = foto_cercana(sn)
    ident = ident_existente
    if not ident:
        ident = momento.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        ev = {"id": ident, "hora": momento.isoformat(timespec="seconds"),
              "camara": CLIENTE.dispositivos.get(sn, {}).get("nombre", sn or ""), "serial": sn or "",
              "tipo": alertas.TIPOS.get(tipo, tipo), "foto": "", "clip": ""}
        if foto:
            (carpeta(cuenta) / f"{ident}.jpg").write_bytes(foto)
            ev["foto"] = f"{ident}.jpg"
        guardar_evento(cuenta, ev)
    enlace = url_evento(ident)
    texto_extra = f"\n📷 Ver foto y clip: {enlace}" if enlace else ""
    destinos = alertas.enviar(cfg, tipo, detalle + texto_extra, momento, foto=foto)
    if con_clip and sn and CLIENTE.driver:
        def grabar():
            mp4 = CLIENTE.clip(sn)
            if not mp4:
                return
            (carpeta(cuenta) / f"{ident}.mp4").write_bytes(mp4)
            actualizar_evento(cuenta, ident, clip=f"{ident}.mp4")
            with cuentas.en_cuenta(cuenta, "cámara"):
                c = nk.cargar_config()
                dest = alertas.ajustes(c)["destino"]
                if "telegram" in bateria.canales_de(dest) and c.get("telegram_avisos"):
                    try:
                        import telegram
                        telegram.enviar_a_todos(c, telegram.enviar_video, mp4,
                                                f"🎥 Clip · {alertas.TIPOS.get(tipo, tipo)} · {momento:%H:%M:%S}")
                    except Exception as e:
                        nk.registrar("Clip no enviado", "Telegram", str(e), ok=False)
        threading.Thread(target=grabar, daemon=True).start()
    return destinos


def estado():
    c = CLIENTE
    a = ajustes_sistema()
    return {
        "usuario": a.get("usuario", ""), "credenciales": credenciales_puestas(),
        "conectado": c.conectado, "sesion": c.driver, "error": c.error,
        "captcha": c.captcha, "pide_codigo": c.pide_codigo,
        "dispositivos": [{"serial": sn, **d, **ajustes_camara(sn)} for sn, d in c.dispositivos.items()],
        "sin_casa": SIN_CASA,
    }


class Limpieza(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)

    def run(self):
        while True:
            try:
                limpiar_antiguos()
            except Exception as e:
                nk.log(f"Cámara: limpieza ({e})")
            time.sleep(6 * 3600)
