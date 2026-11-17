"""Cámaras Eufy a través de eufy-security-ws (contenedor «puerta-eufy»).

- Usuario y contraseña de la cuenta secundaria de Eufy: los escribe el administrador en el panel.
  Van a la bóveda y, para que el contenedor de Eufy pueda usarlos, a datos/eufy/ (permisos 600).
- Este módulo mantiene una conexión WebSocket con ese contenedor y recibe sus eventos:
  movimiento, persona, timbre... y la foto de cada evento.
- Para los clips se abre el vídeo en directo unos segundos y se convierte a MP4 con ffmpeg.
  En las cámaras con «Grabar antes del aviso» el directo se mantiene siempre abierto y los últimos
  segundos se guardan en memoria (clase Memoria), así el clip empieza unos segundos antes del aviso.
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
DURACION_VISITA = 15 * 60  # hasta cuánto se juntan las detecciones seguidas en el mismo mensaje
SEGUNDOS_CLIP = 15
PREVIOS_VALIDOS = (0, 5, 10, 15)  # segundos de antes del aviso que se pueden elegir por cámara
RE_ARCHIVO = re.compile(r"^[\w-]+\.(jpg|mp4)$")

EVENTOS = {
    "motion detected": "Movimiento", "radar motion detected": "Movimiento",
    "person detected": "Persona", "stranger person detected": "Persona desconocida",
    "someone loitering": "Alguien merodeando", "rings": "Timbre",
    "pet detected": "Mascota", "crying detected": "Llanto", "sound detected": "Sonido",
}
# qué propiedad indica que una cámara sabe detectar cada tipo (para ofrecer solo los que tiene)
TIPOS_DETECCION = [("Persona", "personDetected"), ("Persona desconocida", "strangerPersonDetected"),
                   ("Mascota", "petDetected"), ("Movimiento", "motionDetected"), ("Timbre", "ringing"),
                   ("Llanto", "cryingDetected"), ("Sonido", "soundDetected"), ("Alguien merodeando", "someoneLoitering")]

# ajustes que el panel sabe mostrar (solo salen los que tenga cada cámara y se puedan cambiar)
AJUSTES_CAM = [
    ("motionDetection", "Detección de movimiento", ""),
    ("motionDetectionType", "Qué detecta", ""),
    ("motionDetectionSensitivity", "Sensibilidad", "1 = poca, 5 = mucha"),
    ("motionTracking", "Seguir el movimiento", "La cámara gira sola para seguir lo que se mueve."),
    ("petDetection", "Detectar mascotas", ""),
    ("soundDetection", "Detectar sonido", ""),
    ("soundDetectionType", "Tipo de sonido", ""),
    ("soundDetectionSensitivity", "Sensibilidad al sonido", "1 = poca, 5 = mucha"),
    ("continuousRecording", "Grabación continua en la tarjeta", "Si no, solo graba cuando detecta algo."),
    ("recordingClipLength", "Duración de cada grabación", ""),
    ("videoRecordingQuality", "Calidad de grabación", ""),
    ("videoStreamingQuality", "Calidad del directo", ""),
    ("autoNightvision", "Visión nocturna automática", ""),
    ("statusLed", "Luz de estado", ""),
    ("rotationSpeed", "Velocidad de giro", ""),
    ("imageMirrored", "Imagen invertida", "Para cámaras colgadas del techo."),
    ("lightSettingsEnable", "Foco activado", ""),
    ("lightSettingsBrightnessManual", "Brillo del foco", ""),
    ("lightSettingsMotionTriggered", "Encender el foco con movimiento", ""),
    ("lightSettingsBrightnessMotion", "Brillo del foco con movimiento", ""),
    ("lightSettingsMotionTriggeredTimer", "Tiempo encendido tras el movimiento", ""),
    ("microphone", "Micrófono", ""),
    ("audioRecording", "Grabar sonido", ""),
    ("speakerVolume", "Volumen del altavoz", ""),
    ("watermark", "Marca en la imagen", ""),
]
TRADUCCIONES = {
    "Person": "Personas", "Pet": "Mascotas", "Person and Pet": "Personas y mascotas",
    "All other motions": "Otros movimientos", "Person and all other motions": "Personas y otros movimientos",
    "Pet and all other motions": "Mascotas y otros movimientos", "Person, Pet and all other motions": "Todo",
    "Humans only": "Solo personas", "All motions": "Todo movimiento", "Crying": "Llanto", "All Sounds": "Todos los sonidos",
    "Auto": "Automática", "Low": "Baja", "Medium": "Media", "High": "Alta", "Min": "Mínima", "Max": "Máxima",
    "Timestamp": "Fecha y hora", "Timestamp and Logo": "Fecha, hora y logo", "Off": "Nada",
    "30 sec.": "30 s", "1 min.": "1 min", "3 min.": "3 min", "5 min.": "5 min", "15 min.": "15 min",
    "Away": "Fuera de casa", "Home": "En casa", "Schedule": "Según horario de Eufy", "Disarmed": "Desactivado",
    "Custom 1": "Personalizado 1", "Custom 2": "Personalizado 2", "Custom 3": "Personalizado 3", "Geofencing": "Por ubicación",
}
MODOS_OFRECIDOS = ("1", "0", "63", "2")  # En casa, Fuera, Desactivado, Según horario
DIRECCIONES = {"izquierda": 1, "derecha": 2, "arriba": 3, "abajo": 4}


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
            "movimiento": bool(d.get("movimiento")), "tipos": d.get("tipos"),  # tipos None = todos
            "previo": int(d.get("previo") or 0)}


def cuenta_de(serial):
    return ajustes_camara(serial)["cuenta"]


def asignar(serial, cuenta=None, predeterminada=None, movimiento=None, tipos=None, previo=None):
    s = cuentas.leer_sistema()
    disp = s.setdefault("eufy", {}).setdefault("dispositivos", {})
    d = disp.setdefault(serial, {"cuenta": cuentas.CUENTA_PRINCIPAL})
    if cuenta and cuenta != d.get("cuenta"):
        d["cuenta"] = cuenta
        d["predeterminada"] = False  # al cambiar de casa deja de ser la predeterminada de la anterior
    if movimiento is not None:
        d["movimiento"] = bool(movimiento)
    if previo is not None:
        if int(previo) not in PREVIOS_VALIDOS:
            raise RuntimeError("Segundos antes del aviso no válidos.")
        d["previo"] = int(previo)
    if tipos is not None:
        validos = {t for t, _ in TIPOS_DETECCION}
        d["tipos"] = sorted({t for t in tipos if t in validos})
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
    with _CANDADO_EVENTOS, open(carpeta(cuenta) / "eventos.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(evento, ensure_ascii=False) + "\n")


def actualizar_evento(cuenta, ident, **cambios):
    with _CANDADO_EVENTOS:
        _actualizar_evento(cuenta, ident, **cambios)


def _actualizar_evento(cuenta, ident, **cambios):
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


_CANDADO_EVENTOS = threading.Lock()


def _todas_lineas(cuenta):
    ruta = carpeta(cuenta) / "eventos.jsonl"
    if not ruta.exists():
        return ruta, []
    salida = []
    for l in ruta.read_text(encoding="utf-8").splitlines():
        try:
            salida.append(json.loads(l))
        except ValueError:
            pass
    return ruta, salida


def _escribir_eventos(ruta, eventos):
    ruta.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in eventos), encoding="utf-8")


def _borrar_archivos(cuenta, e):
    for k in ("foto", "clip"):
        n = e.get(k)
        if n and RE_ARCHIVO.match(n):
            (carpeta(cuenta) / n).unlink(missing_ok=True)


def marcar_guardar(cuenta, ident, guardar):
    """«No borrar»: el evento se salta la limpieza automática."""
    with _CANDADO_EVENTOS:
        ruta, evs = _todas_lineas(cuenta)
        e = next((x for x in evs if x.get("id") == ident), None)
        if not e:
            raise RuntimeError("Ese evento ya no existe.")
        if guardar:
            e["guardar"] = True
        else:
            e.pop("guardar", None)
        _escribir_eventos(ruta, evs)
        return e


def borrar_evento(cuenta, ident):
    with _CANDADO_EVENTOS:
        ruta, evs = _todas_lineas(cuenta)
        e = next((x for x in evs if x.get("id") == ident), None)
        if not e:
            raise RuntimeError("Ese evento ya no existe.")
        _borrar_archivos(cuenta, e)
        _escribir_eventos(ruta, [x for x in evs if x is not e])
        return e


def vaciar(cuenta=None):
    """Borra todo lo que no esté marcado como «no borrar» (de una casa o de todas). Devuelve cuántos eventos."""
    n = 0
    casas = [cuenta] if cuenta else [c.name for c in CARPETA_MEDIA.iterdir() if c.is_dir()] if CARPETA_MEDIA.exists() else []
    for c in casas:
        with _CANDADO_EVENTOS:
            ruta, evs = _todas_lineas(c)
            quedan = []
            for e in evs:
                if e.get("guardar"):
                    quedan.append(e)
                else:
                    _borrar_archivos(c, e); n += 1
            if evs:
                _escribir_eventos(ruta, quedan)
            protegidos = {e.get(k) for e in quedan for k in ("foto", "clip")}
            for f in carpeta(c).iterdir():  # fotos de entradas y archivos sueltos
                if f.suffix in (".jpg", ".mp4") and f.name not in protegidos:
                    f.unlink(missing_ok=True)
    return n


DIAS_SEMANA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def borrado():
    """Cómo se borran solos: por antigüedad («dias») o todo lo no marcado una vez por semana («semanal»)."""
    b = ajustes_sistema().get("borrado") or {}
    try:
        dia = max(0, min(6, int(b.get("dia", 0))))
    except (TypeError, ValueError):
        dia = 0
    hora = b.get("hora") if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(b.get("hora") or "")) else "04:00"
    modo = "semanal" if b.get("modo") == "semanal" else "dias"
    return {"modo": modo, "dia": dia, "hora": hora, "ultimo": b.get("ultimo") or "", "dias": dias_guardar()}


def texto_borrado():
    b = borrado()
    if b["modo"] == "semanal":
        return f"Se borran solos cada {DIAS_SEMANA[b['dia']]} a las {b['hora']}, menos los marcados con 📌"
    return f"Se borran solos a los {b['dias']} días, menos los marcados con 📌"


def poner_borrado(modo, dia=None, hora=None):
    if modo not in ("dias", "semanal"):
        raise RuntimeError("Modo de borrado no válido.")
    s = cuentas.leer_sistema()
    b = s.setdefault("eufy", {}).setdefault("borrado", {})
    b["modo"] = modo
    if dia is not None:
        if not 0 <= int(dia) <= 6:
            raise RuntimeError("Día no válido.")
        b["dia"] = int(dia)
    if hora is not None:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(hora)):
            raise RuntimeError("Hora no válida (HH:MM).")
        b["hora"] = hora
    if modo == "semanal":  # empieza a contar desde ahora: la primera pasada es la próxima vez que toque
        b["ultimo"] = datetime.now().astimezone().isoformat(timespec="seconds")
    cuentas.guardar_sistema(s)


def _toca_semanal(ahora=None):
    b = borrado()
    if b["modo"] != "semanal":
        return False
    ahora = ahora or datetime.now().astimezone()
    h, m = map(int, b["hora"].split(":"))
    objetivo = ahora.replace(hour=h, minute=m, second=0, microsecond=0) - timedelta(days=(ahora.weekday() - b["dia"]) % 7)
    if objetivo > ahora:
        objetivo -= timedelta(days=7)
    # toca si la última pasada fue antes del momento programado más reciente (y este fue hace menos de un día)
    try:
        ultimo = datetime.fromisoformat(b["ultimo"]) if b["ultimo"] else None
    except ValueError:
        ultimo = None
    return (ultimo is None or ultimo < objetivo) and ahora - objetivo < timedelta(days=1)


def _marcar_semanal_hecho():
    s = cuentas.leer_sistema()
    s.setdefault("eufy", {}).setdefault("borrado", {})["ultimo"] = datetime.now().astimezone().isoformat(timespec="seconds")
    cuentas.guardar_sistema(s)


def evento(cuenta, ident):
    return next((e for e in leer_eventos(cuenta, 2000) if e.get("id") == ident), None)


def ruta_archivo(cuenta, nombre):
    if not RE_ARCHIVO.match(nombre or ""):
        raise RuntimeError("Archivo no válido.")
    ruta = carpeta(cuenta) / nombre
    if not ruta.is_file():
        raise RuntimeError("Ese archivo ya no existe (se ha borrado).")
    return ruta


def dias_guardar():
    try:
        return max(3, min(365, int(ajustes_sistema().get("dias_guardar") or DIAS_GUARDAR)))
    except (TypeError, ValueError):
        return DIAS_GUARDAR


def poner_dias_guardar(dias):
    dias = int(dias)
    if not 3 <= dias <= 365:
        raise RuntimeError("Entre 3 y 365 días.")
    s = cuentas.leer_sistema()
    s.setdefault("eufy", {})["dias_guardar"] = dias
    cuentas.guardar_sistema(s)


def uso_disco():
    """Lo que ocupan las fotos y clips guardados por el panel, por casa."""
    salida = {}
    if CARPETA_MEDIA.exists():
        for c in CARPETA_MEDIA.iterdir():
            if c.is_dir():
                fotos = clips = 0
                for f in c.iterdir():
                    if f.suffix == ".jpg":
                        fotos += f.stat().st_size
                    elif f.suffix == ".mp4":
                        clips += f.stat().st_size
                salida[c.name] = {"fotos": fotos, "clips": clips}
    libre = shutil.disk_usage(CARPETA_MEDIA if CARPETA_MEDIA.exists() else cuentas.DATOS)
    marcados = {c.name: sum(1 for e in _todas_lineas(c.name)[1] if e.get("guardar"))
                for c in CARPETA_MEDIA.iterdir() if c.is_dir()} if CARPETA_MEDIA.exists() else {}
    return {"casas": salida, "libre": libre.free, "total": libre.total, "dias": dias_guardar(),
            "borrado": borrado(), "marcados": marcados}


def limpiar_antiguos():
    DIAS = dias_guardar()
    limite = time.time() - DIAS * 86400
    if not CARPETA_MEDIA.exists():
        return
    corte = (datetime.now().astimezone() - timedelta(days=DIAS)).isoformat()
    for c in CARPETA_MEDIA.iterdir():
        if not c.is_dir():
            continue
        with _CANDADO_EVENTOS:
            ruta, evs = _todas_lineas(c.name)
            vivas = [e for e in evs if e.get("guardar") or e.get("hora", "") >= corte]
            if len(vivas) != len(evs):
                _escribir_eventos(ruta, vivas)
            protegidos = {e.get(k) for e in evs if e.get("guardar") for k in ("foto", "clip")}
            for f in c.iterdir():
                if f.suffix in (".jpg", ".mp4") and f.name not in protegidos and f.stat().st_mtime < limite:
                    f.unlink(missing_ok=True)


def url_panel():
    return (os.environ.get("PANEL_URL_PUBLICA") or cuentas.leer_sistema().get("url_publica") or "").rstrip("/")


def url_enlaces():
    """Dirección para los botones de Telegram (p. ej. un dominio propio que redirige al panel);
    si no hay, la del panel."""
    return (os.environ.get("PANEL_URL_ENLACES") or "").rstrip("/") or url_panel()


def url_evento(ident):
    base = url_enlaces()
    return f"{base}/#evento={ident}" if base else ""


# ---------------------------------------------------------------- silenciar una cámara (botón de Telegram)

def silenciada(sn):
    """Hasta cuándo (epoch) están silenciados los avisos de movimiento de esa cámara; 0 si no lo están."""
    hasta = (cuentas.leer_sistema().get("camara_silencio") or {}).get(sn, 0)
    return hasta if hasta > time.time() else 0


def silenciar(sn, segundos=3600):
    s = cuentas.leer_sistema()
    sil = {k: v for k, v in (s.get("camara_silencio") or {}).items() if v > time.time()}
    if segundos:
        sil[sn] = time.time() + segundos
    else:
        sil.pop(sn, None)
    s["camara_silencio"] = sil
    cuentas.guardar_sistema(s)
    return sil.get(sn, 0)


# ---------------------------------------------------------------- botones de los avisos en Telegram

def botones_aviso(cuenta, ident, sn, tipo):
    """Botones bajo el aviso, según cómo estén ahora (silenciada, marcado «no borrar»)."""
    def b(texto, dato):
        return {"text": texto, "callback_data": dato} if len(dato.encode()) <= 64 else None
    fila1, fila2 = [], []
    if tipo == "movimiento" and sn:
        hasta = silenciada(sn)
        fila1.append(b(f"🔔 Reactivar (silencio hasta {datetime.fromtimestamp(hasta):%H:%M})", f"r|{cuenta}|{sn}")
                     if hasta else b("🔕 Silenciar 1 h", f"s|{cuenta}|{sn}"))
    if ident:
        e = evento(cuenta, ident) or {}
        fila1.append(b("📌 Guardado · quitar", f"m|{cuenta}|{ident}") if e.get("guardar")
                     else b("📌 No borrar", f"n|{cuenta}|{ident}"))
    fila2.append(b("🚪 Puerta", f"p|{cuenta}"))
    base = url_enlaces()
    if sn and base.startswith("https://"):
        fila2.append({"text": "📹 En directo", "url": f"{base}/#directo={sn}"})
    filas = [[x for x in f if x] for f in (fila1, fila2)]
    return [f for f in filas if f]


def foto_ahora(sn, segundos=3):
    """Foto en este momento: graba unos segundos del directo y se queda con el último fotograma."""
    mp4 = CLIENTE.clip(sn, segundos)
    if not mp4:
        return None
    with tempfile.TemporaryDirectory() as d:
        entrada, salida = Path(d) / "c.mp4", Path(d) / "f.jpg"
        entrada.write_bytes(mp4)
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(entrada), "-update", "1", "-q:v", "3",
                            str(salida)], capture_output=True, timeout=30)
        return salida.read_bytes() if r.returncode == 0 and salida.exists() else None


def _portada(foto):
    """Miniatura JPEG (≤320 px) para el vídeo de Telegram."""
    if not foto:
        return None
    try:
        r = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "image2pipe", "-i", "-", "-vf",
                            "scale='min(320,iw)':-2", "-q:v", "5", "-f", "image2", "-c:v", "mjpeg", "-"],
                           input=foto, capture_output=True, timeout=20)
        return r.stdout if r.returncode == 0 and 0 < len(r.stdout) < 200_000 else None
    except Exception:
        return None


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
        self.props = {}                 # serial -> propiedades actuales (sin la foto)
        self.meta = {}                  # serial -> metadatos de propiedades (tipo, si se puede cambiar, opciones…)
        self.ordenes = {}               # serial -> órdenes que admite
        self.estacion_de = {}           # serial cámara -> serial de su estación (base)
        self.estaciones = {}            # serial estación -> propiedades
        self.meta_est = {}              # serial estación -> metadatos
        self.pendientes = {}            # messageId -> [Event, resultado]
        self.fotos = {}                 # serial -> (momento, bytes)
        self.foto_nueva = threading.Condition()
        self.oyentes = {}               # serial -> set de funciones que reciben (bytes, metadata) del directo
        self.cerrojo_directo = threading.Lock()
        self.bloqueo_clip = {}
        self.ultimo_aviso = {}
        self.visitas = {}  # sn -> el último aviso de movimiento, para juntar las detecciones seguidas
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
                self.props[sn] = {k: v for k, v in p.items() if k != "picture"}
                est = p.get("stationSerialNumber") or sn
                self.estacion_de[sn] = est
                self._enviar({"messageId": f"_meta:{sn}", "command": "device.get_properties_metadata", "serialNumber": sn})
                self._enviar({"messageId": f"_cmds:{sn}", "command": "device.get_commands", "serialNumber": sn})
                self._enviar({"messageId": f"_eprops:{est}", "command": "station.get_properties", "serialNumber": est})
                self._enviar({"messageId": f"_emeta:{est}", "command": "station.get_properties_metadata", "serialNumber": est})
                self.dispositivos[sn] = {"nombre": p.get("name") or sn, "modelo": p.get("model", ""),
                                         "camara": True, "bateria": p.get("battery")}
                if nuevo:
                    nk.log(f"Eufy: cámara «{self.dispositivos[sn]['nombre']}» lista")
                foto = _bytes((p.get("picture") or {}).get("data"))
                if foto and sn not in self.fotos:
                    self.fotos[sn] = (0, foto)
            elif ident and ident.split(":", 1)[0] in ("_meta", "_cmds", "_eprops", "_emeta") and m.get("success"):
                que, sn = ident.split(":", 1)
                r = m.get("result") or {}
                if que == "_meta":
                    self.meta[sn] = r.get("properties") or {}
                elif que == "_cmds":
                    self.ordenes[sn] = r.get("commands") or []
                elif que == "_eprops":
                    self.estaciones[sn] = {k: v for k, v in (r.get("properties") or {}).items() if k not in ("lanIpAddress", "macAddress")}
                else:
                    self.meta_est[sn] = r.get("properties") or {}
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
        if origen == "station":
            est = e.get("serialNumber")
            if nombre == "property changed" and est in self.estaciones:
                self.estaciones[est][e.get("name")] = e.get("value")
            elif nombre == "guard mode changed" and est in self.estaciones:
                self.estaciones[est]["guardMode"] = e.get("guardMode")
            elif nombre == "current mode changed" and est in self.estaciones:
                self.estaciones[est]["currentMode"] = e.get("currentMode")
            return
        if origen != "device":
            return
        sn = e.get("serialNumber")
        if nombre == "property changed" and sn in self.props and e.get("name") != "picture":
            self.props[sn][e.get("name")] = e.get("value")
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

    def clip(self, sn, segundos=SEGUNDOS_CLIP, desde=None):
        """Graba unos segundos del directo y devuelve el MP4 (bytes) o None.
        Con «desde» (momento del aviso) y la cámara guardando en memoria, el clip va de unos segundos
        antes de ese momento hasta «segundos» después."""
        cerrojo = self.bloqueo_clip.setdefault(sn, threading.Lock())
        if not cerrojo.acquire(timeout=1):
            return None  # ya se está grabando un clip de esta cámara
        try:
            if desde and MEMORIA.viva(sn):
                mp4 = MEMORIA.clip(sn, desde, segundos)
                if mp4:
                    return mp4
                nk.log("Eufy: la memoria del directo no sirvió para el clip; se graba desde ahora")
            return self._clip_directo(sn, segundos)
        finally:
            cerrojo.release()

    def _clip_directo(self, sn, segundos):
        c = {"datos": bytearray(), "meta": {}}

        def recibir(trozo, meta):
            if trozo:
                c["datos"] += trozo
                c["meta"] = meta or c["meta"]
                c.setdefault("primero", time.time())
        try:
            self.unirse(sn, recibir)
            fin = time.time() + segundos + 25  # margen: la cámara tarda unos segundos en empezar a emitir
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
        aj = ajustes_camara(sn)
        if not aj["movimiento"]:
            return  # esta cámara no avisa de movimiento (se elige en Ajustes → Cámara Eufy)
        if aj["tipos"] is not None and que not in aj["tipos"]:
            return  # ese tipo de detección no interesa (p. ej. solo personas)
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
        v = self.visitas.get(sn)
        if v and time.time() - v["ultima"] < ESPERA_ENTRE_AVISOS and time.time() - v["inicio"] < DURACION_VISITA:
            # misma visita: no se manda otro mensaje, se actualiza el que ya llegó
            v["n"] += 1
            v["ultima"], v["hora_ultima"] = time.time(), momento
            _actualizar_visita(v)
            return
        if time.time() - self.ultimo_aviso.get(sn, 0) < ESPERA_ENTRE_AVISOS:
            return
        if silenciada(sn):
            return  # silenciada desde Telegram («🔕 Silenciar 1 h»): se guarda el evento, pero sin aviso
        with cuentas.en_cuenta(cuenta, "cámara"):
            cfg = nk.cargar_config()
            regla = alertas.ajustes(cfg)["reglas"]["movimiento"]
            if not (regla["activo"] and alertas.en_franja(regla, momento)):
                return
            self.ultimo_aviso[sn] = time.time()
            visita = {"inicio": time.time(), "ultima": time.time(), "n": 1, "recibos": {}, "video": False}
            self.visitas[sn] = visita
            enviar_con_camara(cfg, "movimiento", f"{que} · {nombre_cam}", momento, ident_existente=ident,
                              foto=foto, sn=sn, con_clip=regla.get("clip", True), visita=visita)


CLIENTE = Cliente()


def _es_clave(trozo, hevc):
    """¿Empieza este trozo un fotograma clave (lleva SPS en H.264 o VPS en H.265)? Solo desde ahí se puede cortar."""
    i = 0
    while True:
        i = trozo.find(b"\x00\x00\x01", i, 400)
        if i < 0 or i + 3 >= len(trozo):
            return False
        b = trozo[i + 3]
        if (hevc and (b >> 1) & 0x3F == 32) or (not hevc and b & 0x1F == 7):
            return True
        i += 3


class Memoria(threading.Thread):
    """Mantiene abierto el directo de las cámaras con «Grabar antes del aviso» y guarda en memoria
    los últimos segundos (nada en disco). Si la emisión se corta, la vuelve a pedir."""
    REVISAR = 10
    CORTADA = 20  # segundos sin vídeo para darla por cortada

    def __init__(self):
        super().__init__(daemon=True)
        self.oyentes = {}   # sn -> función que recibe el directo
        self.trozos = {}    # sn -> deque de (momento, bytes, es_clave)
        self.meta = {}
        self.ultimo = {}    # sn -> momento del último trozo (o del último intento de reanudar)
        self.fallando = set()
        self.sin_video = {}  # sn -> veces seguidas que se reanudó sin que llegara vídeo
        self.cerrojo = threading.Lock()

    def viva(self, sn):
        return sn in self.oyentes and time.time() - self.ultimo.get(sn, 0) < 5 and bool(self.trozos.get(sn))

    def run(self):
        time.sleep(20)  # que el cliente conecte y cargue las cámaras
        while True:
            try:
                self._revisar()
            except Exception as e:
                nk.log(f"Eufy: memoria del directo ({e})")
            time.sleep(self.REVISAR)

    def _revisar(self):
        quiero = {sn for sn in CLIENTE.dispositivos if ajustes_camara(sn)["previo"]} if CLIENTE.driver else set()
        for sn in list(self.oyentes):
            if sn not in quiero:
                self._parar(sn)
        for sn in quiero:
            if sn not in self.oyentes:
                self._empezar(sn)
            elif time.time() - self.ultimo.get(sn, 0) > self.CORTADA:
                self._reanudar(sn)

    def _nombre(self, sn):
        return CLIENTE.dispositivos.get(sn, {}).get("nombre", sn)

    def _empezar(self, sn):
        hevc = {"v": False}
        cola = deque()

        def recibir(trozo, meta):
            if not trozo:
                return
            ahora = time.time()
            if meta:
                self.meta[sn] = meta
                hevc["v"] = str(meta.get("videoCodec", "H264")).upper() in ("H265", "HEVC", "1")
            with self.cerrojo:
                cola.append((ahora, trozo, _es_clave(trozo, hevc["v"])))
                self._recortar(cola, ahora - ajustes_camara(sn)["previo"] - SEGUNDOS_CLIP - 20)
            self.ultimo[sn] = ahora
            if self.sin_video.pop(sn, 0) >= 3:
                nk.log(f"Eufy: «{self._nombre(sn)}» vuelve a mandar vídeo")
        self.trozos[sn], self.ultimo[sn] = cola, time.time()
        try:
            CLIENTE.unirse(sn, recibir)
        except Exception as e:
            self.trozos.pop(sn, None)
            if sn not in self.fallando:
                self.fallando.add(sn)
                nk.log(f"Eufy: no se pudo abrir el directo de «{self._nombre(sn)}» para grabar antes del aviso ({e})")
            return
        self.oyentes[sn] = recibir
        self.fallando.discard(sn)
        nk.log(f"Eufy: «{self._nombre(sn)}» guarda en memoria los segundos antes de cada aviso")

    @staticmethod
    def _recortar(cola, limite):
        # quita lo antiguo, pero siempre empezando en un fotograma clave
        while len(cola) > 1 and cola[0][0] < limite:
            siguiente = next((i for i, t in enumerate(cola) if i and t[2]), None)
            if siguiente is None or cola[siguiente][0] > limite:
                break
            for _ in range(siguiente):
                cola.popleft()

    def _reanudar(self, sn):
        self.ultimo[sn] = time.time()  # el próximo intento, dentro de CORTADA segundos
        n = self.sin_video[sn] = self.sin_video.get(sn, 0) + 1
        if n == 3:
            nk.log(f"Eufy: «{self._nombre(sn)}» tiene el directo abierto pero no manda vídeo: los avisos irán sin clip. "
                   "Si sigue así, reinicia la cámara (Ajustes de la cámara o la app de Eufy).")
        for orden in ("device.stop_livestream", "device.start_livestream"):
            try:
                CLIENTE.comando(orden, timeout=40, serialNumber=sn)
            except Exception as e:
                if orden.endswith("start_livestream") and "already" not in str(e).lower() and sn not in self.fallando:
                    self.fallando.add(sn)
                    nk.log(f"Eufy: el directo de «{self._nombre(sn)}» se cortó y no se puede reanudar ({e})")
                    return
        if sn in self.fallando:
            self.fallando.discard(sn)
            nk.log(f"Eufy: el directo de «{self._nombre(sn)}» vuelve a guardarse en memoria")

    def _parar(self, sn):
        f = self.oyentes.pop(sn, None)
        self.trozos.pop(sn, None)
        self.fallando.discard(sn)
        if f:
            CLIENTE.salir(sn, f)
            nk.log(f"Eufy: «{self._nombre(sn)}» deja de guardar en memoria los segundos antes de cada aviso")

    def clip(self, sn, desde, segundos):
        """Clip desde «previo» segundos antes de «desde» hasta «segundos» después (espera a que pasen)."""
        previo = ajustes_camara(sn)["previo"]
        fin = desde + segundos
        while time.time() < fin + 1:
            if not self.viva(sn) and time.time() - self.ultimo.get(sn, 0) > 8:
                break  # se cortó a medias: se usa lo que haya
            time.sleep(0.3)
        cola = self.trozos.get(sn)
        if not cola:
            return None
        inicio = desde - previo
        with self.cerrojo:
            lista = list(cola)
        claves = [i for i, t in enumerate(lista) if t[2]]
        if not claves:
            return None
        antes = [i for i in claves if lista[i][0] <= inicio]
        primero = antes[-1] if antes else claves[0]
        datos = b"".join(t[1] for t in lista[primero:] if t[0] <= fin + 0.5)
        if len(datos) < 2000:
            return None
        return _a_mp4(datos, self.meta.get(sn) or {})


MEMORIA = Memoria()


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


def texto_visita(v):
    t = ("🎥 " if v.get("video") else "") + v["pie"]
    if v.get("n", 1) > 1:
        t += f"\n👀 Sigue habiendo movimiento · {v['n']} detecciones (última a las {v['hora_ultima']:%H:%M:%S})"
    return t


def _actualizar_visita(v):
    if not v.get("recibos"):
        return
    try:
        import telegram
        botones = botones_aviso(v["cuenta"], v["ident"], v["sn"], v["tipo"])
        for chat, mid in v["recibos"].items():
            try:
                telegram.editar_pie(chat, mid, texto_visita(v), botones)
            except Exception as e:
                nk.log(f"Telegram: no se pudo actualizar el aviso ({e})")
    except Exception:
        pass


def enviar_con_camara(cfg, tipo, detalle, momento, ident_existente=None, foto=None, sn=None, con_clip=True,
                      visita=None):
    """Envía el aviso con la foto (Telegram) o el enlace (WhatsApp/correo) y después, en segundo plano, el clip.
    En Telegram el clip sustituye a la foto en el mismo mensaje, para que cada aviso sea un solo mensaje."""
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
    recibos = {}  # {chat de Telegram: id del mensaje con la foto}
    import extras
    callado = tipo == "movimiento" and bool(sn) and extras.sin_sonido(cfg, sn)
    destinos = alertas.enviar(cfg, tipo, detalle + texto_extra, momento, foto=foto, recibos=recibos,
                              botones=botones_aviso(cuenta, ident, sn, tipo), sin_sonido=callado)
    titulo = f"{alertas.TIPOS.get(tipo, tipo)} · {bateria.nombre_cuenta()}"
    pie = f"{titulo}\n\n{detalle}{texto_extra}\n{momento:%d/%m/%Y %H:%M:%S}"
    if visita is not None:
        visita.update(recibos=recibos, pie=pie, cuenta=cuenta, ident=ident, tipo=tipo, sn=sn)
    if con_clip and sn and CLIENTE.driver:
        def grabar():
            mp4 = CLIENTE.clip(sn, desde=momento.timestamp())
            if not mp4:
                nk.log(f"Eufy: el aviso de las {momento:%H:%M:%S} va sin clip: no llegó vídeo de "
                       f"«{CLIENTE.dispositivos.get(sn, {}).get('nombre', sn)}»")
                return
            (carpeta(cuenta) / f"{ident}.mp4").write_bytes(mp4)
            actualizar_evento(cuenta, ident, clip=f"{ident}.mp4")
            with cuentas.en_cuenta(cuenta, "cámara"):
                c = nk.cargar_config()
                dest = alertas.ajustes(c)["destino"]
                if "telegram" in bateria.canales_de(dest) and c.get("telegram_avisos"):
                    import telegram
                    sueltos = []  # chats donde no había foto que cambiar: el clip va aparte
                    portada, botones = _portada(foto), botones_aviso(cuenta, ident, sn, tipo)
                    if visita is not None:
                        visita["video"] = True
                    pie_video = texto_visita(visita) if visita is not None else f"🎥 {pie}"
                    for chat in telegram.chats_avisos(c):
                        if chat in recibos:
                            try:
                                telegram.cambiar_a_video(chat, recibos[chat], mp4, pie_video, portada, botones)
                                continue
                            except Exception as e:
                                nk.log(f"Telegram: no se pudo cambiar la foto por el clip ({e}); va aparte")
                        sueltos.append(chat)
                    errores = []
                    for chat in sueltos:
                        try:
                            telegram.enviar_video(chat, mp4,
                                                  f"🎥 Clip · {alertas.TIPOS.get(tipo, tipo)} · {momento:%H:%M:%S}")
                        except Exception as e:
                            errores.append(str(e))
                    if errores:
                        nk.registrar("Clip no enviado", "Telegram", "; ".join(errores), ok=False)
        threading.Thread(target=grabar, daemon=True).start()
    return destinos


def estado():
    c = CLIENTE
    a = ajustes_sistema()
    return {
        "usuario": a.get("usuario", ""), "credenciales": credenciales_puestas(),
        "conectado": c.conectado, "sesion": c.driver, "error": c.error,
        "captcha": c.captcha, "pide_codigo": c.pide_codigo,
        "dispositivos": [{"serial": sn, **d, **ajustes_camara(sn), **capacidades(sn)} for sn, d in c.dispositivos.items()],
        "sin_casa": SIN_CASA,
    }


class Limpieza(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)

    def run(self):
        ultima = 0
        while True:
            try:
                if _toca_semanal():
                    n = vaciar()
                    _marcar_semanal_hecho()
                    nk.log(f"Cámara: borrado semanal ({n} avisos)")
                elif borrado()["modo"] == "dias" and time.time() - ultima > 6 * 3600:
                    limpiar_antiguos(); ultima = time.time()
            except Exception as e:
                nk.log(f"Cámara: limpieza ({e})")
            time.sleep(600)


# ---------------------------------------------------------------- lo que sabe hacer cada cámara

def _escribible(sn, prop):
    m = CLIENTE.meta.get(sn, {}).get(prop)
    return bool(m and m.get("writeable")) and prop in CLIENTE.props.get(sn, {})


def _tr(t):
    return TRADUCCIONES.get(str(t), str(t))


def capacidades(sn):
    c, p, o = CLIENTE, CLIENTE.props.get(sn, {}), CLIENTE.ordenes.get(sn, [])
    est = c.estacion_de.get(sn, sn)
    ep, em = c.estaciones.get(est, {}), c.meta_est.get(est, {}).get("guardMode") or {}
    modos = [{"v": int(k), "t": _tr(v)} for k, v in (em.get("states") or {}).items() if k in MODOS_OFRECIDOS] \
        if em.get("writeable") else []
    sd = None
    if ep.get("sdCapacity"):
        sd = {"total_mb": ep.get("sdCapacity"), "libre_mb": ep.get("sdCapacityAvailable")}
    return {
        "ptz": "pan_and_tilt" in o, "sirena": "trigger_alarm_sound" in o, "calibrar": "calibrate" in o,
        "privacidad": _escribible(sn, "enabled"), "encendida": p.get("enabled", True) is not False,
        "foco": _escribible(sn, "light"), "foco_on": bool(p.get("light")),
        "modos": modos, "modo": ep.get("guardMode"), "estacion": est, "sd": sd,
        "tipos_posibles": [t for t, prop in TIPOS_DETECCION if prop in p],
        "n_ajustes": sum(1 for k, _, _ in AJUSTES_CAM if _escribible(sn, k)),
    }


def leer_ajustes(sn):
    if sn not in CLIENTE.props:
        raise RuntimeError("Esa cámara no está conectada.")
    salida = []
    for clave, etq, ayuda in AJUSTES_CAM:
        if not _escribible(sn, clave):
            continue
        m, v = CLIENTE.meta[sn][clave], CLIENTE.props[sn][clave]
        a = {"clave": clave, "etiqueta": etq, "ayuda": ayuda, "valor": v}
        if m.get("type") == "boolean":
            a["tipo"] = "bool"
        elif m.get("states"):
            a["tipo"] = "sel"
            a["opciones"] = [{"v": int(k) if str(k).lstrip("-").isdigit() else k, "t": _tr(t)} for k, t in m["states"].items()]
        elif m.get("type") == "number":
            a.update(tipo="num", min=m.get("min", 0), max=m.get("max", 100), unidad=m.get("unit", ""))
        else:
            continue
        salida.append(a)
    return salida


def guardar_ajuste(sn, clave, valor):
    if clave not in {k for k, _, _ in AJUSTES_CAM} | {"enabled", "light"} or not _escribible(sn, clave):
        raise RuntimeError("Ese ajuste no existe en esta cámara.")
    m = CLIENTE.meta[sn][clave]
    if m.get("type") == "boolean":
        valor = bool(valor)
    else:
        valor = int(valor)
        if m.get("states") and str(valor) not in m["states"]:
            raise RuntimeError("Valor no válido.")
        if "min" in m and not m["min"] <= valor <= m.get("max", valor):
            raise RuntimeError("Valor fuera de rango.")
    CLIENTE.comando("device.set_property", serialNumber=sn, name=clave, value=valor)
    return valor


def accion(sn, que, valor=None):
    """Órdenes directas: mover, sirena, foco, apagar/encender, modo, calibrar."""
    if sn not in CLIENTE.dispositivos:
        raise RuntimeError("Esa cámara no está conectada.")
    cap = capacidades(sn)
    nombre = CLIENTE.dispositivos[sn]["nombre"]
    if que == "mover":
        if not cap["ptz"] or valor not in DIRECCIONES:
            raise RuntimeError("Esta cámara no se puede mover así.")
        CLIENTE.comando("device.pan_and_tilt", serialNumber=sn, direction=DIRECCIONES[valor])
        return ""
    if que == "calibrar" and cap["calibrar"]:
        CLIENTE.comando("device.calibrate", serialNumber=sn)
        return f"Cámara «{nombre}» recolocada"
    if que == "sirena" and cap["sirena"]:
        segundos = max(5, min(60, int(valor or 10)))
        CLIENTE.comando("device.trigger_alarm", serialNumber=sn, seconds=segundos)
        return f"Sirena de «{nombre}» ({segundos} s)"
    if que == "parar_sirena" and cap["sirena"]:
        CLIENTE.comando("device.reset_alarm", serialNumber=sn)
        return f"Sirena de «{nombre}» parada"
    if que == "foco" and cap["foco"]:
        guardar_ajuste(sn, "light", bool(valor))
        return f"Foco de «{nombre}» {'encendido' if valor else 'apagado'}"
    if que == "privacidad" and cap["privacidad"]:
        guardar_ajuste(sn, "enabled", not bool(valor))  # privacidad = cámara apagada
        return f"Cámara «{nombre}» {'apagada' if valor else 'encendida'}"
    if que == "modo" and cap["modos"]:
        modo = int(valor)
        if modo not in [m["v"] for m in cap["modos"]]:
            raise RuntimeError("Modo no válido.")
        CLIENTE.comando("station.set_guard_mode", serialNumber=cap["estacion"], mode=modo)
        return f"«{nombre}»: modo {next(m['t'] for m in cap['modos'] if m['v'] == modo)}"
    raise RuntimeError("Esta cámara no admite esa orden.")


def aplicar_casa(cuenta, privacidad=None, modo=None, solo=None, quien="automático"):
    """Apaga/enciende y cambia el modo de las cámaras de una casa. solo = lista de seriales (None = todas)."""
    hechos, fallos = [], []
    for sn in camaras_de(cuenta):
        if solo is not None and sn not in solo:
            continue
        cap = capacidades(sn)
        for que, v, puede in (("privacidad", privacidad, cap["privacidad"]), ("modo", modo, bool(cap["modos"]))):
            if v is None or v == "" or not puede:
                continue
            try:
                if que == "privacidad" and cap["encendida"] == (not v):
                    continue  # ya está como se quiere
                if que == "modo" and str(cap["modo"]) == str(v):
                    continue
                hechos.append(accion(sn, que, v))
            except Exception as e:
                fallos.append(f"{CLIENTE.dispositivos.get(sn, {}).get('nombre', sn)}: {e}")
    if hechos or fallos:
        with cuentas.en_cuenta(cuenta, quien):
            nk.registrar("Cámaras", quien, "; ".join(hechos + fallos), ok=not fallos)
    return hechos, fallos


# ---------------------------------------------------------------- foto de cada entrada por el teclado

def _nombre_entrada(ident):
    return "entrada-" + re.sub(r"[^\w-]", "", str(ident))[:40] + ".jpg"


def foto_entrada(cuenta, ident, cuando, sn):
    """Guarda la foto de la cámara de la puerta más cercana al momento de una entrada (en segundo plano)."""
    nombre = _nombre_entrada(ident)
    destino = carpeta(cuenta) / nombre
    if destino.exists() or not sn:
        return

    def buscar():
        t_ev = cuando.timestamp()
        for _ in range(4):  # hasta ~20 s: la foto llega cuando la cámara detecta movimiento
            t, foto = CLIENTE.fotos.get(sn, (0, None))
            if foto and abs(t - t_ev) <= 150:
                destino.write_bytes(foto)
                return
            with CLIENTE.foto_nueva:
                CLIENTE.foto_nueva.wait(5)
    threading.Thread(target=buscar, daemon=True).start()


def foto_de_entrada(cuenta, ident):
    nombre = _nombre_entrada(ident)
    return nombre if (carpeta(cuenta) / nombre).exists() else ""


# ---------------------------------------------------------------- vigilancia: cámaras sin conexión

class VigilaCamaras(threading.Thread):
    """Si el puente de Eufy pierde la conexión (o una cámara se queda sin batería), avisa a cada casa con cámaras."""

    def __init__(self):
        super().__init__(daemon=True)
        self.desde = None
        self.avisadas = set()     # casas avisadas de la desconexión
        self.bateria = set()      # seriales avisados de batería baja

    def run(self):
        time.sleep(120)  # margen al arrancar
        while True:
            try:
                self._revisar()
            except Exception as e:
                nk.log(f"Vigilancia de cámaras: {e}")
            time.sleep(60)

    def _casas(self):
        disp = ajustes_sistema().get("dispositivos") or {}
        return {d.get("cuenta", cuentas.CUENTA_PRINCIPAL) for d in disp.values()} - {SIN_CASA}

    def _revisar(self):
        import alertas
        if not credenciales_puestas():
            return
        ok = CLIENTE.conectado and CLIENTE.driver
        ahora = datetime.now().astimezone()
        if ok:
            self.desde = None
            for cuenta in list(self.avisadas):
                try:
                    with cuentas.en_cuenta(cuenta, "cámara"):
                        alertas.enviar(nk.cargar_config(), "camara", "✅ Las cámaras vuelven a estar conectadas.", ahora)
                except Exception:
                    pass
            self.avisadas.clear()
        else:
            self.desde = self.desde or ahora
        for cuenta in self._casas():
            if not cuentas.existe_cuenta(cuenta):
                continue
            with cuentas.en_cuenta(cuenta, "cámara"):
                cfg = nk.cargar_config()
                regla = alertas.ajustes(cfg)["reglas"].get("camara") or {}
                if not ok and cuenta not in self.avisadas and ahora - self.desde >= timedelta(minutes=regla.get("minutos", 15)):
                    if alertas.aviso_si_toca(cfg, "camara", f"Sin conexión con las cámaras desde las {self.desde:%H:%M}. "
                                             f"{CLIENTE.error or 'Revisa el wifi, la corriente o la sesión de Eufy.'}", ahora):
                        self.avisadas.add(cuenta)
                if ok:
                    for sn in camaras_de(cuenta):
                        b = CLIENTE.props.get(sn, {}).get("battery")
                        if b is None:
                            continue
                        if b <= 20 and sn not in self.bateria:
                            if alertas.aviso_si_toca(cfg, "camara", f"🔋 Batería baja en «{CLIENTE.dispositivos[sn]['nombre']}»: {b} %.", ahora):
                                self.bateria.add(sn)
                        elif b > 30:
                            self.bateria.discard(sn)
