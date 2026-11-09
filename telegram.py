"""Telegram: un bot común a todo el sistema para enviar códigos y avisos.

- El token del bot lo pega el administrador en el panel y va a la bóveda (_sistema).
- Un bot no puede escribir a nadie que no le haya hablado antes. Por eso cada persona
  se vincula pulsando un enlace t.me/<bot>?start=<clave>: el hilo Escucha recibe ese
  /start, apunta su chat y le confirma por Telegram que ya está conectada.
- Las claves de vinculación son de un solo uso y caducan a los 7 días.
"""
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta

import cuentas
import nuki_codigos as nk

CLAVE_TOKEN = "telegram_token"  # en la bóveda de _sistema
CADUCIDAD_ENLACE = timedelta(days=7)
_bot_cache = {}


class ErrorTelegram(RuntimeError):
    pass


def token():
    return cuentas.boveda_leer(CLAVE_TOKEN, cuentas.CUENTA_SISTEMA)


def configurado():
    return bool(token())


def _api(metodo, datos=None, archivo=None, timeout=20, tok=None):
    tok = tok or token()
    if not tok:
        raise ErrorTelegram("Telegram no está configurado (Ajustes → Telegram, solo administrador).")
    url = f"https://api.telegram.org/bot{tok}/{metodo}"
    if archivo:  # multipart/form-data
        limite = uuid.uuid4().hex
        partes = []
        for k, v in (datos or {}).items():
            partes.append(f'--{limite}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        campo, nombre, contenido, tipo = archivo
        partes.append(f'--{limite}\r\nContent-Disposition: form-data; name="{campo}"; filename="{nombre}"\r\n'
                      f"Content-Type: {tipo}\r\n\r\n".encode() + contenido + b"\r\n")
        partes.append(f"--{limite}--\r\n".encode())
        cuerpo = b"".join(partes)
        cab = {"Content-Type": f"multipart/form-data; boundary={limite}"}
    else:
        cuerpo = json.dumps(datos or {}).encode()
        cab = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=cuerpo, headers=cab, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=nk.contexto_ssl()) as r:
            res = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            res = json.loads(e.read().decode())
        except Exception:
            raise ErrorTelegram(f"Telegram respondió {e.code}") from None
    except urllib.error.URLError as e:
        raise ErrorTelegram(f"No se pudo conectar con Telegram: {e.reason}") from None
    if not res.get("ok"):
        desc = res.get("description", "error desconocido")
        if "chat not found" in desc or "bot was blocked" in desc:
            desc += " (la persona tiene que volver a pulsar el enlace de Telegram)"
        if res.get("error_code") == 401:
            desc = "El token del bot no es válido."
        raise ErrorTelegram(f"Telegram: {desc}")
    return res.get("result")


def info_bot(forzar=False):
    tok = token()
    if not tok:
        return None
    if forzar or _bot_cache.get("tok") != tok:
        yo = _api("getMe", tok=tok)
        _bot_cache.update(tok=tok, usuario=yo.get("username"), nombre=yo.get("first_name"))
    return {"usuario": _bot_cache["usuario"], "nombre": _bot_cache["nombre"]}


def guardar_token(nuevo):
    nuevo = (nuevo or "").strip()
    yo = _api("getMe", tok=nuevo)  # comprueba que funciona antes de guardarlo
    cuentas.boveda_guardar(CLAVE_TOKEN, nuevo, cuentas.CUENTA_SISTEMA)
    _bot_cache.clear()
    try:
        _api("deleteWebhook", tok=nuevo)  # usamos getUpdates
    except ErrorTelegram:
        pass
    return {"usuario": yo.get("username"), "nombre": yo.get("first_name")}


def borrar_token():
    cuentas.boveda_borrar(CLAVE_TOKEN, cuentas.CUENTA_SISTEMA)
    _bot_cache.clear()


# ---------------------------------------------------------------- avisos: un Telegram por usuario del panel

def avisos_de(cfg):
    """{usuario_del_panel: {"chat", "nombre"}} de los que reciben los avisos de esta cuenta.
    (Antes había un solo chat por cuenta: se asigna al administrador.)"""
    v = cfg.get("telegram_avisos")
    if not v:
        return {}
    if not isinstance(v, dict):
        admin = next((u["usuario"] for u in cuentas.leer_usuarios() if u.get("rol") == "admin"), "admin")
        v = {admin: {"chat": v, "nombre": ""}}
        cfg["telegram_avisos"] = v
    return v


def chats_avisos(cfg):
    return [x["chat"] for x in avisos_de(cfg).values() if x.get("chat")]


def enviar_a_todos(cfg, funcion, *args):
    """Envía a todos los Telegram de avisos. Devuelve cuántos llegaron; lanza error si ninguno."""
    chats, errores, ok = chats_avisos(cfg), [], 0
    if not chats:
        raise ErrorTelegram("nadie ha vinculado su Telegram para los avisos (botón «Vincular mi Telegram»).")
    for chat in chats:
        try:
            funcion(chat, *args)
            ok += 1
        except Exception as e:
            errores.append(str(e))
    if not ok:
        raise ErrorTelegram("; ".join(errores))
    return ok


# ---------------------------------------------------------------- envío

def enviar(chat, texto):
    _api("sendMessage", {"chat_id": chat, "text": texto, "disable_web_page_preview": True})


def enviar_foto(chat, contenido, texto=""):
    _api("sendPhoto", {"chat_id": chat, "caption": texto[:1000]}, ("photo", "foto.jpg", contenido, "image/jpeg"), timeout=60)


def enviar_video(chat, contenido, texto=""):
    _api("sendVideo", {"chat_id": chat, "caption": texto[:1000], "supports_streaming": "true"},
         ("video", "clip.mp4", contenido, "video/mp4"), timeout=120)


# ---------------------------------------------------------------- vinculación

def _enlaces():
    s = cuentas.leer_sistema()
    ahora = datetime.now().astimezone()
    vivos = {k: v for k, v in (s.get("telegram_enlaces") or {}).items()
             if datetime.fromisoformat(v["creado"]) + CADUCIDAD_ENLACE > ahora}
    return s, vivos


def crear_enlace(destino):
    """destino: id de persona, o "_avisos:<usuario del panel>" para los avisos de ese usuario."""
    bot = info_bot()
    if not bot:
        raise ErrorTelegram("Telegram no está configurado (Ajustes → Telegram, solo administrador).")
    s, vivos = _enlaces()
    cuenta = cuentas.cuenta_actual()
    # reutilizar uno vivo para el mismo destino
    for k, v in vivos.items():
        if v["cuenta"] == cuenta and v["destino"] == destino:
            return f"https://t.me/{bot['usuario']}?start={k}"
    clave = secrets.token_urlsafe(18)
    vivos[clave] = {"cuenta": cuenta, "destino": destino, "creado": datetime.now().astimezone().isoformat()}
    s["telegram_enlaces"] = vivos
    cuentas.guardar_sistema(s)
    return f"https://t.me/{bot['usuario']}?start={clave}"


def _vincular(clave, chat, quien):
    s, vivos = _enlaces()
    e = vivos.pop(clave, None)
    s["telegram_enlaces"] = vivos
    cuentas.guardar_sistema(s)
    if not e:
        return "Este enlace ya se usó o ha caducado. Pide uno nuevo."
    with cuentas.en_cuenta(e["cuenta"], "telegram"):
        cfg = nk.cargar_config()
        if e["destino"].startswith("_avisos"):
            usuario = e["destino"].partition(":")[2] or "admin"
            av = avisos_de(cfg)
            av[usuario] = {"chat": chat, "nombre": quien}
            cfg["telegram_avisos"] = av
            nk.guardar_config(cfg)
            nk.registrar("Telegram vinculado", f"Avisos de {usuario}", quien)
            return (f"✅ Listo, {usuario}. Aquí recibirás los avisos de «{bateria_nombre()}» "
                    f"(puerta, cámara y batería) y, si lo pides, el código para recuperar tu acceso al panel.")
        d = next((x for x in cfg["destinatarios"] if x["id"] == e["destino"]), None)
        if not d:
            return "Esa persona ya no existe en la lista."
        d["telegram_chat"] = chat
        nk.guardar_config(cfg)
        nk.registrar("Telegram vinculado", d["nombre"], quien)
        return f"✅ Hola {d['nombre']}. A partir de ahora tus códigos de acceso a la puerta te llegarán por aquí."


def bateria_nombre():
    try:
        import bateria
        return bateria.nombre_cuenta()
    except Exception:
        return "tu casa"


class Escucha(threading.Thread):
    """Recibe los mensajes que llegan al bot (solo para vincular a las personas)."""

    def __init__(self):
        super().__init__(daemon=True)

    def run(self):
        time.sleep(10)
        while True:
            if not configurado():
                time.sleep(30)
                continue
            try:
                s = cuentas.leer_sistema()
                desplazamiento = int(s.get("telegram_offset") or 0)
                cambios = _api("getUpdates", {"offset": desplazamiento, "timeout": 25,
                                              "allowed_updates": ["message"]}, timeout=40) or []
                for u in cambios:
                    desplazamiento = max(desplazamiento, u["update_id"] + 1)
                    m = u.get("message") or {}
                    texto, chat = (m.get("text") or "").strip(), (m.get("chat") or {}).get("id")
                    if not chat or (m.get("chat") or {}).get("type") != "private":
                        continue
                    de = m.get("from") or {}
                    quien = " ".join(x for x in (de.get("first_name"), de.get("last_name")) if x) or "?"
                    if de.get("username"):
                        quien += f" (@{de['username']})"
                    if texto.startswith("/start ") and len(texto) > 7:
                        respuesta = _vincular(texto[7:].strip(), chat, quien)
                    else:
                        respuesta = ("Hola. Este bot es privado y solo envía los códigos y avisos de una puerta. "
                                     "Para conectarte, usa el enlace que te haya pasado quien gestiona la puerta.")
                    try:
                        enviar(chat, respuesta)
                    except ErrorTelegram:
                        pass
                if cambios:
                    s = cuentas.leer_sistema()
                    s["telegram_offset"] = desplazamiento
                    cuentas.guardar_sistema(s)
            except Exception as e:
                nk.log(f"Telegram: {e}")
                time.sleep(20)
