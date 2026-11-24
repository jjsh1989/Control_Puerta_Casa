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
    if archivo:  # multipart/form-data; `archivo` es una tupla o una lista de tuplas
        limite = uuid.uuid4().hex
        partes = []
        for k, v in (datos or {}).items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v)
            partes.append(f'--{limite}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        for campo, nombre, contenido, tipo in (archivo if isinstance(archivo, list) else [archivo]):
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


def vinculo_valido(usuario, v, cuenta):
    """Un vínculo de avisos vale si el usuario sigue existiendo, sigue teniendo acceso a esa cuenta
    y es el mismo usuario que lo vinculó (no otro creado después con el mismo nombre)."""
    u = cuentas.buscar_usuario(usuario)
    if not (v and v.get("chat") and cuentas.pertenece(u, cuenta)):
        return False
    return not v.get("uid") or v["uid"] == u.get("uid")


def avisos_vigentes(cfg, cuenta=None):
    """Como avisos_de, pero solo con quienes siguen autorizados en esa cuenta (por defecto, la actual)."""
    cuenta = cuenta or cuentas.cuenta_actual()
    return {k: v for k, v in avisos_de(cfg).items() if vinculo_valido(k, v, cuenta)}


def chats_avisos(cfg):
    return [x["chat"] for x in avisos_vigentes(cfg).values()]


def depurar_vinculos(log=None):
    """Quita los vínculos de avisos y los enlaces pendientes de quien ya no tiene acceso a esa cuenta
    (usuario borrado, trasladado a otra vivienda o que ha dejado de ser administrador).
    A los vínculos antiguos sin «uid» les apunta el del usuario actual. Se puede repetir sin problema."""
    quitados = []
    for c in cuentas.listar_cuentas():
        with cuentas.en_cuenta(c["id"], "telegram"):
            try:
                cfg = nk.cargar_config()
            except Exception:
                continue
            av, cambiado = avisos_de(cfg), False
            for usuario, v in list(av.items()):
                if not vinculo_valido(usuario, v, c["id"]):
                    av.pop(usuario)
                    quitados.append((c["id"], usuario))
                    nk.registrar("Telegram desvinculado", "Avisos de " + usuario, "Ya no tiene acceso a esta casa")
                    cambiado = True
                elif not v.get("uid"):
                    v["uid"] = cuentas.buscar_usuario(usuario).get("uid", "")
                    cambiado = True
            if cambiado:
                if av:
                    cfg["telegram_avisos"] = av
                else:
                    cfg.pop("telegram_avisos", None)
                nk.guardar_config(cfg)
    s, vivos = _enlaces()
    antes = len(s.get("telegram_enlaces") or {})
    for k, e in list(vivos.items()):
        if e["destino"].startswith("_avisos"):
            u = cuentas.buscar_usuario(e["destino"].partition(":")[2])
            if not cuentas.pertenece(u, e["cuenta"]) or (e.get("uid") and e["uid"] != u.get("uid")):
                vivos.pop(k)
    if len(vivos) != antes:
        s["telegram_enlaces"] = vivos
        cuentas.guardar_sistema(s)
    if quitados and log:
        log(f"Telegram: retirados {len(quitados)} vínculos de avisos de usuarios sin acceso")
    return quitados


def enviar_a_todos(cfg, funcion, *args, recibos=None):
    """Envía a todos los Telegram de avisos. Devuelve cuántos llegaron; lanza error si ninguno.
    Si se pasa `recibos` (dict), guarda ahí {chat: id_del_mensaje} de los que llegaron."""
    chats, errores, ok = chats_avisos(cfg), [], 0
    if not chats:
        raise ErrorTelegram("nadie ha vinculado su Telegram para los avisos (botón «Vincular mi Telegram»).")
    for chat in chats:
        try:
            r = funcion(chat, *args)
            if recibos is not None and isinstance(r, dict) and r.get("message_id"):
                recibos[chat] = r["message_id"]
            ok += 1
        except Exception as e:
            errores.append(str(e))
    if not ok:
        raise ErrorTelegram("; ".join(errores))
    return ok


# ---------------------------------------------------------------- envío

def enviar(chat, texto, botones=None, sin_sonido=False):
    d = {"chat_id": chat, "text": texto, "disable_web_page_preview": True, "disable_notification": bool(sin_sonido)}
    if botones:
        d["reply_markup"] = {"inline_keyboard": botones}
    return _api("sendMessage", d)


def enviar_foto(chat, contenido, texto="", botones=None, sin_sonido=False):
    d = {"chat_id": chat, "caption": texto[:1000], "disable_notification": "true" if sin_sonido else "false"}
    if botones:
        d["reply_markup"] = {"inline_keyboard": botones}
    return _api("sendPhoto", d, ("photo", "foto.jpg", contenido, "image/jpeg"), timeout=60)


def editar_texto(chat, mensaje, texto, botones=None):
    d = {"chat_id": chat, "message_id": mensaje, "text": texto, "disable_web_page_preview": True,
         "reply_markup": {"inline_keyboard": botones or []}}
    try:
        return _api("editMessageText", d)
    except ErrorTelegram as e:
        if "not modified" not in str(e):
            raise


def editar_pie(chat, mensaje, texto, botones=None):
    """Cambia el texto bajo una foto o vídeo ya enviado (sin volver a sonar)."""
    d = {"chat_id": chat, "message_id": mensaje, "caption": texto[:1000]}
    if botones:
        d["reply_markup"] = {"inline_keyboard": botones}
    try:
        return _api("editMessageCaption", d)
    except ErrorTelegram as e:
        if "not modified" not in str(e):
            raise


def editar_botones(chat, mensaje, botones):
    try:
        return _api("editMessageReplyMarkup", {"chat_id": chat, "message_id": mensaje,
                                               "reply_markup": {"inline_keyboard": botones or []}})
    except ErrorTelegram as e:
        if "not modified" not in str(e):
            raise


def responder_boton(ident, texto="", alerta=False):
    try:
        _api("answerCallbackQuery", {"callback_query_id": ident, "text": texto[:190], "show_alert": alerta})
    except ErrorTelegram:
        pass


def borrar_mensaje(chat, mensaje):
    try:
        _api("deleteMessage", {"chat_id": chat, "message_id": mensaje})
        return True
    except ErrorTelegram:
        return False


def enviar_video(chat, contenido, texto=""):
    return _api("sendVideo", {"chat_id": chat, "caption": texto[:1000], "supports_streaming": "true"},
         ("video", "clip.mp4", contenido, "video/mp4"), timeout=120)


def cambiar_a_video(chat, mensaje, contenido, texto="", portada=None, botones=None):
    """Convierte un mensaje ya enviado (la foto del aviso) en el vídeo del clip, en el mismo mensaje.
    `portada`: JPEG pequeño (≤320 px) que Telegram enseña antes de reproducir."""
    medio = {"type": "video", "media": "attach://clip", "caption": texto[:1000], "supports_streaming": True}
    archivos = [("clip", "clip.mp4", contenido, "video/mp4")]
    if portada:
        medio["thumbnail"] = "attach://portada"
        archivos.append(("portada", "portada.jpg", portada, "image/jpeg"))
    d = {"chat_id": chat, "message_id": mensaje, "media": json.dumps(medio)}
    if botones:
        d["reply_markup"] = {"inline_keyboard": botones}
    return _api("editMessageMedia", d, archivos, timeout=120)


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
        if v["cuenta"] == cuenta and v["destino"] == destino and (
                not v.get("uid") or v["uid"] == (cuentas.buscar_usuario(destino.partition(":")[2]) or {}).get("uid")):
            return f"https://t.me/{bot['usuario']}?start={k}"
    clave = secrets.token_urlsafe(18)
    vivos[clave] = {"cuenta": cuenta, "destino": destino, "creado": datetime.now().astimezone().isoformat()}
    if destino.startswith("_avisos"):  # el enlace es de ESTE usuario, no de otro que se llame igual más tarde
        vivos[clave]["uid"] = (cuentas.buscar_usuario(destino.partition(":")[2]) or {}).get("uid", "")
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
            u = cuentas.buscar_usuario(usuario)
            if not cuentas.pertenece(u, e["cuenta"]) or (e.get("uid") and e["uid"] != u.get("uid")):
                return "Este enlace ya no es válido: ese usuario ya no tiene acceso a esta casa."
            av = avisos_de(cfg)
            av[usuario] = {"chat": chat, "nombre": quien, "uid": u.get("uid", "")}
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


def _bot_atiende(m, texto, chat):
    """Órdenes de la puerta (/puerta, el PIN…). Devuelve True si ya se respondió."""
    try:
        import bot_puerta
        return bot_puerta.mensaje(m, texto, chat)
    except Exception as e:
        nk.log(f"Telegram: orden: {e}")
        return False


class Escucha(threading.Thread):
    """Recibe los mensajes y botones que llegan al bot: vincular personas y manejar la puerta (bot_puerta)."""

    def __init__(self):
        super().__init__(daemon=True)

    def run(self):
        time.sleep(10)
        menu_puesto = None
        while True:
            if not configurado():
                time.sleep(30)
                continue
            if menu_puesto != token():
                menu_puesto = token()
                try:
                    import bot_puerta
                    bot_puerta.preparar()
                except Exception as e:
                    nk.log(f"Telegram: {e}")
            try:
                s = cuentas.leer_sistema()
                desplazamiento = int(s.get("telegram_offset") or 0)
                cambios = _api("getUpdates", {"offset": desplazamiento, "timeout": 25,
                                              "allowed_updates": ["message", "callback_query"]}, timeout=40) or []
                for u in cambios:
                    desplazamiento = max(desplazamiento, u["update_id"] + 1)
                    if u.get("callback_query"):
                        try:
                            import bot_puerta
                            bot_puerta.boton(u["callback_query"])
                        except Exception as e:
                            nk.log(f"Telegram: botón: {e}")
                        continue
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
                    elif _bot_atiende(m, texto, chat):
                        continue
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
