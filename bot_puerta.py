"""La puerta desde Telegram: las órdenes /puerta y /camara, los botones de los avisos y el PIN para abrir.

Quién puede: solo los usuarios del panel que han vinculado su Telegram para los avisos de esa casa
(los invitados que reciben códigos por Telegram no). Ver el estado y cerrar con llave no piden nada
más; abrir la puerta o el portal pide además el PIN que cada usuario pone en el panel
(Ajustes → Comunicación → «La puerta desde Telegram»). El PIN se escribe en el chat, el bot borra ese mensaje enseguida,
la petición caduca a los 2 minutos y 3 fallos seguidos bloquean 15 minutos.
"""
import threading
import time
from datetime import datetime

import alertas
import cuentas
import nuki_codigos as nk
import telegram

CADUCA_PIN = 120  # segundos para escribir el PIN
PENDIENTES = {}   # chat -> {"cuenta", "usuario", "accion", "hasta", "msg"}
_cerrojo = threading.Lock()

ACCIONES = {  # accion: (orden de Nuki, texto para el historial, texto para el chat)
    "abrir": (3, "Puerta abierta desde Telegram", "🔓 Abriendo la puerta…"),
    "portal": (3, "Portal abierto desde Telegram", "🚪 Abriendo el portal…"),
    "cerrar": (2, "Cerradura bloqueada desde Telegram", "🔒 Cerrando con llave…"),
}
CON_PIN = ("abrir", "portal")


# ---------------------------------------------------------------- permisos

def casas_de(chat):
    """[(cuenta, usuario, nombre de la casa)] donde este chat recibe los avisos como usuario del panel."""
    salida = []
    for c in cuentas.listar_cuentas():
        with cuentas.en_cuenta(c["id"], "telegram"):
            try:
                cfg = nk.cargar_config()
            except Exception:
                continue
            for usuario, v in telegram.avisos_de(cfg).items():
                if str(v.get("chat")) != str(chat):
                    continue
                u = cuentas.buscar_usuario(usuario)
                if u and (u["rol"] == "admin" or u.get("cuenta") == c["id"]):
                    salida.append((c["id"], u, c["nombre"]))
    return salida


def _permiso(chat, cuenta):
    return next(((u, nombre) for c, u, nombre in casas_de(chat) if c == cuenta), (None, None))


# ---------------------------------------------------------------- menú de la puerta

def _texto_estado(cuenta, nombre_casa):
    with cuentas.en_cuenta(cuenta, "telegram"):
        cfg = nk.cargar_config()
        n = cfg.get("nuki") or {}
        if not (n.get("token") and n.get("smartlock_id")):
            return f"🏠 {nombre_casa}\nLa cerradura no está configurada en el panel.", False
        r = nk.Nuki(n["token"]).estado(n["smartlock_id"])
        st = r.get("state") or {}
        lineas = [f"🏠 {nombre_casa} · {(r.get('name') or 'Puerta').strip()}",
                  f"Cerradura: {alertas.ESTADOS_CERROJO.get(st.get('state'), 'desconocido')}"]
        if st.get("doorState") in (2, 3):
            lineas.append(f"Puerta: {alertas.ESTADOS_PUERTA.get(st.get('doorState'))}")
        if st.get("batteryCharge") is not None:
            lineas.append(f"Batería: {st['batteryCharge']} %" + (" ⚠️" if st.get("batteryCritical") else ""))
        if r.get("serverState", 0) != 0:
            lineas.append("⚠️ La cerradura no está conectada ahora mismo.")
        lineas.append(f"Actualizado a las {datetime.now():%H:%M:%S}")
        return "\n".join(lineas), bool(n.get("opener_id"))


def _botones_menu(cuenta, hay_portal):
    filas = [[{"text": "🔒 Cerrar con llave", "callback_data": f"c|{cuenta}"},
              {"text": "🔓 Abrir", "callback_data": f"a|{cuenta}"}]]
    if hay_portal:
        filas.append([{"text": "🚪 Abrir portal", "callback_data": f"o|{cuenta}"}])
    fila = [{"text": "🔄 Actualizar", "callback_data": f"p|{cuenta}"}]
    try:
        import camara
        if camara.camaras_de(cuenta):
            fila.append({"text": "📷 Cámaras", "callback_data": f"k|{cuenta}"})
    except Exception:
        pass
    filas.append(fila)
    return filas


def _menu(chat, cuenta, nombre_casa, editar=None, cabecera=""):
    try:
        texto, portal = _texto_estado(cuenta, nombre_casa)
    except Exception as e:
        texto, portal = f"🏠 {nombre_casa}\nNo se pudo leer la cerradura: {e}", False
    texto = (cabecera + "\n\n" if cabecera else "") + texto
    if editar:
        try:
            return telegram.editar_texto(chat, editar, texto, _botones_menu(cuenta, portal))
        except telegram.ErrorTelegram:
            pass
    return telegram.enviar(chat, texto, _botones_menu(cuenta, portal))


# ---------------------------------------------------------------- acciones

def _despues(segundos, funcion, *args, **kw):
    """Ejecuta algo dentro de unos segundos sin parar la escucha del bot (p. ej. el estado tras abrir)."""
    def correr():
        time.sleep(segundos)
        try:
            funcion(*args, **kw)
        except Exception as e:
            nk.log(f"Telegram: {e}")
    threading.Thread(target=correr, daemon=True).start()


def _ejecutar(chat, cuenta, u, accion):
    orden, historial, _ = ACCIONES[accion]
    with cuentas.en_cuenta(cuenta, u["usuario"]):
        cfg = nk.cargar_config()
        n = cfg["nuki"]
        dispositivo = n.get("opener_id") if accion == "portal" else n.get("smartlock_id")
        if not (n.get("token") and dispositivo):
            raise RuntimeError("Esta casa no tiene ese dispositivo configurado.")
        nk.Nuki(n["token"]).accion(dispositivo, orden)
        nk.registrar(historial, u.get("nombre") or u["usuario"], "Telegram")
        # que lo sepan los demás que reciben los avisos de esta casa
        aviso = {"abrir": "🔓 {q} ha abierto la puerta desde Telegram.",
                 "portal": "🚪 {q} ha abierto el portal desde Telegram.",
                 "cerrar": "🔒 {q} ha cerrado con llave desde Telegram."}[accion]
        for otro, v in telegram.avisos_de(cfg).items():
            if v.get("chat") and str(v["chat"]) != str(chat):
                try:
                    telegram.enviar(v["chat"], aviso.format(q=u.get("nombre") or u["usuario"]))
                except Exception:
                    pass


def _cambiar_boton(teclado, dato, nuevo):
    """Devuelve el teclado con el botón `dato` sustituido por `nuevo` (o los botones de `nuevo` si es lista)."""
    return [[nuevo if b.get("callback_data") == dato else b for b in fila] for fila in teclado]


def boton(q):
    ident, dato = q.get("id"), q.get("data") or ""
    msg = q.get("message") or {}
    chat, mid = (msg.get("chat") or {}).get("id"), msg.get("message_id")
    if not chat or (msg.get("chat") or {}).get("type") != "private":
        return telegram.responder_boton(ident)
    tipo, _, resto = dato.partition("|")
    cuenta, _, extra = resto.partition("|")
    u, nombre_casa = _permiso(chat, cuenta)
    if not u:
        return telegram.responder_boton(ident, "No tienes permiso para esto en esa casa.", alerta=True)
    teclado = (msg.get("reply_markup") or {}).get("inline_keyboard") or []
    import camara

    if tipo == "p":  # menú / actualizar
        telegram.responder_boton(ident)
        return _menu(chat, cuenta, nombre_casa, editar=mid if msg.get("text") else None)

    if tipo == "c":  # cerrar con llave
        try:
            _ejecutar(chat, cuenta, u, "cerrar")
        except Exception as e:
            return telegram.responder_boton(ident, f"No se pudo: {e}", alerta=True)
        telegram.responder_boton(ident, "Orden enviada: cerrar con llave")
        _despues(4, _menu, chat, cuenta, nombre_casa, editar=mid if msg.get("text") else None,
                 cabecera=ACCIONES["cerrar"][2])
        return

    if tipo in ("a", "o"):  # abrir: pide PIN
        accion = "abrir" if tipo == "a" else "portal"
        if not cuentas.tiene_pin(u["usuario"]):
            return telegram.responder_boton(
                ident, "Para abrir desde Telegram, primero pon tu PIN en el panel: Ajustes → Comunicación → «La puerta desde Telegram».",
                alerta=True)
        telegram.responder_boton(ident)
        que = "la puerta" if accion == "abrir" else "el portal"
        r = telegram.enviar(chat, f"🔐 Vas a abrir {que} de «{nombre_casa}».\n"
                                  f"Escribe tu PIN para confirmar (caduca en 2 minutos).",
                            [[{"text": "Cancelar", "callback_data": f"x|{cuenta}"}]])
        with _cerrojo:
            PENDIENTES[chat] = {"cuenta": cuenta, "usuario": u["usuario"], "accion": accion,
                                "hasta": time.time() + CADUCA_PIN, "msg": (r or {}).get("message_id"),
                                "casa": nombre_casa}
        return

    if tipo == "x":  # cancelar el PIN
        with _cerrojo:
            PENDIENTES.pop(chat, None)
        telegram.responder_boton(ident, "Cancelado")
        return telegram.editar_texto(chat, mid, "Cancelado. No se ha abierto nada.")

    if tipo in ("s", "r"):  # silenciar / reactivar una cámara
        if camara.cuenta_de(extra) != cuenta:
            return telegram.responder_boton(ident, "Esa cámara no es de esta casa.", alerta=True)
        hasta = camara.silenciar(extra, 3600 if tipo == "s" else 0)
        nombre_cam = camara.CLIENTE.dispositivos.get(extra, {}).get("nombre", "la cámara")
        with cuentas.en_cuenta(cuenta, u["usuario"]):
            nk.registrar("Cámara silenciada 1 h" if tipo == "s" else "Avisos de cámara reactivados",
                         u.get("nombre") or u["usuario"], f"{nombre_cam} · Telegram")
        if tipo == "s":
            telegram.responder_boton(ident, f"Sin avisos de «{nombre_cam}» hasta las {datetime.fromtimestamp(hasta):%H:%M}")
            nuevo = {"text": f"🔔 Reactivar (silencio hasta {datetime.fromtimestamp(hasta):%H:%M})",
                     "callback_data": f"r|{cuenta}|{extra}"}
        else:
            telegram.responder_boton(ident, f"Avisos de «{nombre_cam}» reactivados")
            nuevo = {"text": "🔕 Silenciar 1 h", "callback_data": f"s|{cuenta}|{extra}"}
        return telegram.editar_botones(chat, mid, _cambiar_boton(teclado, dato, nuevo))

    if tipo in ("n", "m"):  # «no borrar» / quitar la marca
        try:
            e = camara.marcar_guardar(cuenta, extra, tipo == "n")
        except Exception as e:
            return telegram.responder_boton(ident, str(e), alerta=True)
        with cuentas.en_cuenta(cuenta, u["usuario"]):
            nk.registrar(("Clip" if e.get("clip") else "Foto") + " marcado «no borrar»" if tipo == "n"
                         else "Quitada la marca «no borrar»", u.get("nombre") or u["usuario"], "Telegram")
        telegram.responder_boton(ident, "No se borrará" if tipo == "n" else "Se borrará con la limpieza automática")
        nuevo = ({"text": "📌 Guardado · quitar", "callback_data": f"m|{cuenta}|{extra}"} if tipo == "n"
                 else {"text": "📌 No borrar", "callback_data": f"n|{cuenta}|{extra}"})
        return telegram.editar_botones(chat, mid, _cambiar_boton(teclado, dato, nuevo))

    if tipo == "k" and not extra:  # elegir cámara
        telegram.responder_boton(ident)
        return _elegir_camara(chat, cuenta, nombre_casa)

    if tipo in ("k", "v"):  # foto al momento / clip de una cámara
        if camara.cuenta_de(extra) != cuenta or extra not in camara.CLIENTE.dispositivos:
            return telegram.responder_boton(ident, "Esa cámara no es de esta casa.", alerta=True)
        nombre_cam = camara.CLIENTE.dispositivos[extra].get("nombre", "la cámara")
        telegram.responder_boton(ident, f"{'📷 Pidiendo una foto' if tipo == 'k' else '🎥 Grabando 15 s'} a «{nombre_cam}»…")
        threading.Thread(target=_mandar_camara, args=(chat, cuenta, u, extra, tipo == "v"), daemon=True).start()
        return

    telegram.responder_boton(ident, "Este botón ya no sirve.")


# ---------------------------------------------------------------- cámaras

def _elegir_camara(chat, cuenta, nombre_casa):
    import camara
    sns = camara.camaras_de(cuenta)
    if not sns:
        return telegram.enviar(chat, f"🏠 {nombre_casa}\nNo hay cámaras en esta casa.")
    botones = [{"text": "📷 " + camara.CLIENTE.dispositivos.get(sn, {}).get("nombre", sn), "callback_data": f"k|{cuenta}|{sn}"}
               for sn in sns]
    filas = [botones[i:i + 2] for i in range(0, len(botones), 2)]
    return telegram.enviar(chat, f"🏠 {nombre_casa}\n¿Qué cámara quieres ver?", filas)


def _botones_camara(cuenta, sn):
    import camara
    filas = [[{"text": "🔄 Otra foto", "callback_data": f"k|{cuenta}|{sn}"},
              {"text": "🎥 Clip 15 s", "callback_data": f"v|{cuenta}|{sn}"}]]
    fila = []
    base = camara.url_enlaces()
    if base.startswith("https://"):
        fila.append({"text": "📹 En directo", "url": f"{base}/#directo={sn}"})
    fila.append({"text": "📷 Otra cámara", "callback_data": f"k|{cuenta}"})
    filas.append(fila)
    return filas


def _mandar_camara(chat, cuenta, u, sn, con_video):
    import camara
    nombre_cam = camara.CLIENTE.dispositivos.get(sn, {}).get("nombre", sn)
    with cuentas.en_cuenta(cuenta, u["usuario"]):
        nk.registrar("Clip pedido desde Telegram" if con_video else "Foto pedida desde Telegram",
                     u.get("nombre") or u["usuario"], nombre_cam)
    try:
        if con_video:
            mp4 = camara.CLIENTE.clip(sn)
            if mp4:
                telegram._api("sendVideo", {"chat_id": chat, "caption": f"🎥 {nombre_cam} · {datetime.now():%H:%M:%S}",
                                            "supports_streaming": "true",
                                            "reply_markup": {"inline_keyboard": _botones_camara(cuenta, sn)}},
                              ("video", "clip.mp4", mp4, "video/mp4"), timeout=120)
                return
        else:
            foto = camara.foto_ahora(sn)
            if foto:
                telegram.enviar_foto(chat, foto, f"📷 {nombre_cam} · {datetime.now():%H:%M:%S}",
                                     _botones_camara(cuenta, sn))
                return
        telegram.enviar(chat, f"⚠️ «{nombre_cam}» no ha respondido. Puede que esté apagada, en modo privacidad, "
                              f"sin conexión o ya grabando otro clip. Prueba otra vez en un momento.",
                        _botones_camara(cuenta, sn))
    except Exception as e:
        nk.log(f"Telegram: cámara: {e}")


# ---------------------------------------------------------------- mensajes escritos

def mensaje(m, texto, chat):
    """Atiende /puerta y el PIN. Devuelve True si ya respondió."""
    with _cerrojo:
        p = PENDIENTES.get(chat)
    if p and not texto.startswith("/"):
        telegram.borrar_mensaje(chat, m.get("message_id"))  # que el PIN no se quede en el chat
        if time.time() > p["hasta"]:
            with _cerrojo:
                PENDIENTES.pop(chat, None)
            telegram.editar_texto(chat, p["msg"], "⌛ Ha caducado. Vuelve a pulsar «Abrir» si quieres abrir.")
            return True
        u, nombre_casa = _permiso(chat, p["cuenta"])
        if not u or u["usuario"] != p["usuario"]:
            with _cerrojo:
                PENDIENTES.pop(chat, None)
            return True
        bien, error = cuentas.comprobar_pin(u["usuario"], texto)
        if not bien:
            with cuentas.en_cuenta(p["cuenta"], u["usuario"]):
                nk.registrar("PIN de Telegram incorrecto", u.get("nombre") or u["usuario"], error, ok=False)
            if "Bloqueado" in error or "Demasiados" in error:
                with _cerrojo:
                    PENDIENTES.pop(chat, None)
                telegram.editar_texto(chat, p["msg"], f"⛔ {error}")
            else:
                telegram.editar_texto(chat, p["msg"], f"❌ {error}\nEscribe el PIN otra vez.",
                                      [[{"text": "Cancelar", "callback_data": f"x|{p['cuenta']}"}]])
            return True
        with _cerrojo:
            PENDIENTES.pop(chat, None)
        try:
            _ejecutar(chat, p["cuenta"], u, p["accion"])
            telegram.editar_texto(chat, p["msg"], ACCIONES[p["accion"]][2])
        except Exception as e:
            telegram.editar_texto(chat, p["msg"], f"⚠️ No se pudo: {e}")
            return True
        _despues(5, _menu, chat, p["cuenta"], p["casa"])
        return True

    if texto.split("@")[0] in ("/camara", "/camaras", "/cámara"):
        casas = casas_de(chat)
        if not casas:
            return False
        for cuenta, _, nombre_casa in casas:
            _elegir_camara(chat, cuenta, nombre_casa)
        return True

    if texto.split("@")[0] in ("/puerta", "/start", "/menu"):
        casas = casas_de(chat)
        if not casas:
            return False  # invitados o desconocidos: respuesta normal del bot
        with _cerrojo:
            PENDIENTES.pop(chat, None)
        for cuenta, _, nombre_casa in casas:
            _menu(chat, cuenta, nombre_casa)
        return True
    return False


def preparar():
    """Pone la orden /puerta en el menú del bot."""
    try:
        telegram._api("setMyCommands", {"commands": [{"command": "puerta", "description": "Estado y control de la puerta"},
                                                     {"command": "camara", "description": "Ver una cámara ahora"}]})
    except Exception as e:
        nk.log(f"Telegram: no se pudo poner el menú del bot: {e}")
