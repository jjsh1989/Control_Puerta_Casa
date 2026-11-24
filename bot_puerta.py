"""La puerta desde Telegram: las órdenes /puerta y /camara, los botones de los avisos y el PIN para abrir.

Quién puede: solo los usuarios del panel que han vinculado su Telegram para los avisos de esa casa
(los invitados que reciben códigos por Telegram no). Ver el estado y cerrar con llave no piden nada
más; abrir la puerta o el portal pide además el PIN que cada usuario pone en el panel
(Ajustes → Comunicación → «La puerta desde Telegram»). El PIN se escribe en el chat, el bot borra ese mensaje enseguida,
la petición caduca a los 2 minutos y 3 fallos seguidos bloquean 15 minutos.
"""
import threading
import time
from datetime import datetime, timedelta

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
            for usuario, v in telegram.avisos_vigentes(cfg, c["id"]).items():
                if str(v.get("chat")) == str(chat):
                    salida.append((c["id"], cuentas.buscar_usuario(usuario), c["nombre"]))
    return salida


def _permiso(chat, cuenta):
    return next(((u, nombre) for c, u, nombre in casas_de(chat) if c == cuenta), (None, None))


def _admin(chat):
    """(cuenta, usuario) si este chat es de un administrador del panel."""
    return next(((c, u) for c, u, _ in casas_de(chat) if u["rol"] == "admin"), (None, None))


# ---------------------------------------------------------------- dispositivos recordados (/dispositivo)

def _lista_dispositivos(chat, cuenta, editar=None, cabecera=""):
    import dispositivos
    lineas = [cabecera] if cabecera else []
    filas = []
    try:
        lista = dispositivos.listar().get("dispositivos") or []
    except Exception as e:
        lineas.append(f"⚠️ {e}")
    else:
        sitio = dispositivos.url_web().split("//", 1)[-1]
        lineas.append(f"📱 Dispositivos recordados en {sitio}:" if lista else "📱 Todavía no hay dispositivos recordados.")
        lineas += [f"• {d['nombre']} — {d.get('equipo') or '?'} · último uso {d.get('ultimo') or '—'}" for d in lista]
        filas = [[{"text": f"🗑 Olvidar «{d['nombre'][:24]}»", "callback_data": f"d|{cuenta}|{d['id']}"}] for d in lista]
    lineas.append("\nPara añadir uno, escribe /dispositivo y su nombre; por ejemplo: /dispositivo iPhone de Ana")
    filas.append([{"text": "🔄 Actualizar", "callback_data": f"y|{cuenta}"}])
    if editar:
        telegram.editar_texto(chat, editar, "\n".join(lineas), filas)
    else:
        telegram.enviar(chat, "\n".join(lineas), filas)


def _orden_dispositivo(chat, argumento):
    import dispositivos
    cuenta, u = _admin(chat)
    if not u:
        telegram.enviar(chat, "Solo un administrador del panel puede añadir u olvidar dispositivos.")
        return
    if not dispositivos.disponible():
        telegram.enviar(chat, "La gestión de dispositivos no está configurada en este servidor.")
        return
    nombre = " ".join(argumento.split())[:40]
    if not nombre:
        return _lista_dispositivos(chat, cuenta)
    quien = u.get("nombre") or u["usuario"]
    try:
        enlace, _ = dispositivos.alta(nombre, quien)
    except Exception as e:
        telegram.enviar(chat, f"⚠️ No se pudo crear el enlace: {e}")
        return
    with cuentas.en_cuenta(cuenta, u["usuario"]):
        nk.registrar("Enlace para recordar un dispositivo", quien, f"{nombre} (desde Telegram)")
    dispositivos.mandar_enlace(chat, enlace, nombre)


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
    filas.append([{"text": "🔑 Dar un código", "callback_data": f"q|{cuenta}"}])
    if _modos(cuenta):
        filas.append([{"text": "🏠 En casa", "callback_data": f"e|{cuenta}|entrada"},
                      {"text": "🚶 Fuera", "callback_data": f"e|{cuenta}|salida"}])
    fila = [{"text": "🔄 Actualizar", "callback_data": f"p|{cuenta}"}]
    try:
        import camara
        if camara.camaras_de(cuenta):
            fila.append({"text": "📷 Cámaras", "callback_data": f"k|{cuenta}"})
    except Exception:
        pass
    filas.append(fila)
    return filas


def _modos(cuenta):
    """Reglas de «Cámaras y cerradura» de esa casa ({"entrada": …, "salida": …}) si hay alguna."""
    with cuentas.en_cuenta(cuenta, "telegram"):
        auto = nk.cargar_config().get("camaras_auto") or {}
    return auto if any((auto.get(m) or {}).get("modo") not in (None, "") or (auto.get(m) or {}).get("privacidad")
                       for m in ("entrada", "salida")) else None


def _aplicar_modo(cuenta, u, momento):
    import camara
    auto = _modos(cuenta) or {}
    regla = auto.get(momento) or {}
    priv = {"apagar": True, "encender": False}.get(regla.get("privacidad"))
    modo = regla.get("modo")
    modo = int(modo) if str(modo).lstrip("-").isdigit() else None
    quien = f"{u.get('nombre') or u['usuario']} (Telegram, «{'En casa' if momento == 'entrada' else 'Fuera'}»)"
    hechos, fallos = [], []
    if priv is not None:
        h, f = camara.aplicar_casa(cuenta, privacidad=priv, solo=auto.get("camaras") or None, quien=quien)
        hechos += h; fallos += f
    if modo is not None:
        h, f = camara.aplicar_casa(cuenta, modo=modo, quien=quien)
        hechos += h; fallos += f
    return hechos, fallos


# ---------------------------------------------------------------- códigos temporales

DURACIONES = {"2": ("2 horas", 2), "hoy": ("hasta esta noche (23:59)", None), "24": ("24 horas", 24), "72": ("3 días", 72)}


def _horas(clave):
    texto, h = DURACIONES[clave]
    if h is None:
        ahora = datetime.now()
        h = max(0.5, (ahora.replace(hour=23, minute=59, second=0) - ahora).total_seconds() / 3600)
    return texto, h


def _crear_codigo(cuenta, u, clave):
    """Código temporal de un solo uso por visita, creado desde Telegram. Devuelve (código, hasta, nombre_auth)."""
    from datetime import timezone
    texto, horas = _horas(clave)
    with cuentas.en_cuenta(cuenta, u["usuario"]):
        cfg = nk.cargar_config()
        n = cfg["nuki"]
        nuki = nk.Nuki(n["token"])
        existentes = nuki.autorizaciones(n["smartlock_id"])
        codigo = nk.generar_codigo({str(a.get("code")) for a in existentes if a.get("code")} | nk.codigos_usados())
        persona = {"id": "_telegram", "nombre": u.get("nombre") or u["usuario"]}
        nombre_auth = (nk.prefijo_invitado(persona) + nk.secrets.token_hex(2))[:20]
        desde = datetime.now(timezone.utc)
        hasta = desde + timedelta(hours=horas)
        nuki.crear_codigo(n["smartlock_id"], nombre_auth, codigo, desde, hasta)
        hasta_txt = hasta.astimezone().strftime("%d/%m/%Y a las %H:%M")
        nk.registrar("Código de invitado creado", persona["nombre"], f"Desde Telegram · {texto} · hasta el {hasta_txt}")
        nk.apuntar_codigo(codigo, {"id": "_telegram", "nombre": f"Invitado de {persona['nombre']} (Telegram)"},
                          "invitado", nombre_auth, desde, hasta, "Telegram")
        estado = nk.cargar_json(nk.ruta_estado(), {})
        estado.setdefault(nk.CLAVE_INVITADOS, []).append({
            "nombre_auth": nombre_auth, "inquilino": "_telegram", "codigo": codigo, "motivo": "Telegram",
            "telefono": "", "creado": desde.isoformat(), "hasta": hasta.isoformat()})
        nk.guardar_json(nk.ruta_estado(), estado)
        mensaje = cfg.get("mensaje_invitado", nk.MENSAJE_INVITADO).format(
            nombre=persona["nombre"], codigo=codigo, hasta=hasta_txt, motivo="")
    return codigo, hasta_txt, nombre_auth, mensaje


def _anular_codigo(cuenta, u, nombre_auth):
    with cuentas.en_cuenta(cuenta, u["usuario"]):
        cfg = nk.cargar_config()
        nk.anular_invitado(cfg, nk.Nuki(cfg["nuki"]["token"]), nombre_auth,
                           motivo_registro="Código de invitado anulado desde Telegram")


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
        for otro, v in telegram.avisos_vigentes(cfg).items():
            if str(v["chat"]) != str(chat):
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

    if tipo in ("d", "w", "y"):  # dispositivos: olvidar (pregunta), olvidar (confirmado), actualizar
        if u["rol"] != "admin":
            return telegram.responder_boton(ident, "Solo un administrador puede hacer esto.", alerta=True)
        if tipo == "y":
            telegram.responder_boton(ident)
            return _lista_dispositivos(chat, cuenta, editar=mid)
        if tipo == "d":
            telegram.responder_boton(ident, "¿Seguro? Dejará de entrar por el dominio (por la VPN, sí).")
            return telegram.editar_botones(chat, mid, [[{"text": "✅ Sí, olvidarlo", "callback_data": f"w|{cuenta}|{extra}"},
                                                         {"text": "Cancelar", "callback_data": f"y|{cuenta}"}]])
        import dispositivos
        try:
            dispositivos.borrar(extra)
        except Exception as e:
            telegram.responder_boton(ident, f"No se pudo: {e}", alerta=True)
            return _lista_dispositivos(chat, cuenta, editar=mid)
        with cuentas.en_cuenta(cuenta, u["usuario"]):
            nk.registrar("Dispositivo olvidado", u.get("nombre") or u["usuario"], f"{extra} (desde Telegram)")
        telegram.responder_boton(ident, "Olvidado")
        return _lista_dispositivos(chat, cuenta, editar=mid, cabecera="✅ Dispositivo olvidado.")

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

    if tipo == "q" and not extra:  # dar un código: elegir duración
        telegram.responder_boton(ident)
        return _elegir_duracion(chat, cuenta, nombre_casa)

    if tipo == "q":  # duración elegida: pide PIN
        if extra not in DURACIONES:
            return telegram.responder_boton(ident, "Este botón ya no sirve.")
        if not cuentas.tiene_pin(u["usuario"]):
            return telegram.responder_boton(
                ident, "Para dar códigos desde Telegram, primero pon tu PIN en el panel: "
                       "Ajustes → Comunicación → «La puerta desde Telegram».", alerta=True)
        telegram.responder_boton(ident)
        texto, _ = _horas(extra)
        r = telegram.enviar(chat, f"🔐 Vas a crear un código de «{nombre_casa}» válido {texto}.\n"
                                  f"Escribe tu PIN para confirmar (caduca en 2 minutos).",
                            [[{"text": "Cancelar", "callback_data": f"x|{cuenta}"}]])
        with _cerrojo:
            PENDIENTES[chat] = {"cuenta": cuenta, "usuario": u["usuario"], "accion": f"codigo:{extra}",
                                "hasta": time.time() + CADUCA_PIN, "msg": (r or {}).get("message_id"),
                                "casa": nombre_casa}
        return

    if tipo == "z":  # anular un código creado desde Telegram
        try:
            _anular_codigo(cuenta, u, extra)
        except Exception as e:
            return telegram.responder_boton(ident, f"No se pudo: {e}", alerta=True)
        telegram.responder_boton(ident, "Código anulado")
        return telegram.editar_texto(chat, mid, (msg.get("text") or "").split("\n")[0] + "\n❌ Anulado: ya no abre.")

    if tipo == "e":  # modo En casa / Fuera
        if extra not in ("entrada", "salida"):
            return telegram.responder_boton(ident, "Este botón ya no sirve.")
        telegram.responder_boton(ident, "Aplicando…")

        def hacer():
            hechos, fallos = _aplicar_modo(cuenta, u, extra)
            nombre = "🏠 En casa" if extra == "entrada" else "🚶 Fuera"
            if not hechos and not fallos:
                texto = f"{nombre}: las cámaras ya estaban así."
            else:
                texto = f"{nombre}:\n" + "\n".join(f"✅ {h}" for h in hechos) + \
                        ("\n" if hechos and fallos else "") + "\n".join(f"⚠️ {f}" for f in fallos)
            telegram.enviar(chat, texto)
        threading.Thread(target=hacer, daemon=True).start()
        return

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

def _elegir_duracion(chat, cuenta, nombre_casa):
    botones = [{"text": t, "callback_data": f"q|{cuenta}|{k}"} for k, (t, _) in DURACIONES.items()]
    return telegram.enviar(chat, f"🔑 {nombre_casa}\n¿Para cuánto tiempo es el código?",
                           [botones[i:i + 2] for i in range(0, len(botones), 2)])


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
        if p["accion"].startswith("codigo:"):
            try:
                codigo, hasta, nombre_auth, texto_inv = _crear_codigo(p["cuenta"], u, p["accion"].split(":", 1)[1])
            except Exception as e:
                telegram.editar_texto(chat, p["msg"], f"⚠️ No se pudo crear el código: {e}")
                return True
            telegram.editar_texto(chat, p["msg"], f"🔑 Código creado: {codigo}\nVale hasta el {hasta}.\n"
                                                  f"Reenvía el mensaje de abajo a quien lo necesite.",
                                  [[{"text": "❌ Anular este código", "callback_data": f"z|{p['cuenta']}|{nombre_auth}"}]])
            telegram.enviar(chat, texto_inv)
            return True
        try:
            _ejecutar(chat, p["cuenta"], u, p["accion"])
            telegram.editar_texto(chat, p["msg"], ACCIONES[p["accion"]][2])
        except Exception as e:
            telegram.editar_texto(chat, p["msg"], f"⚠️ No se pudo: {e}")
            return True
        _despues(5, _menu, chat, p["cuenta"], p["casa"])
        return True

    orden, _, argumento = texto.partition(" ")
    if orden.split("@")[0] in ("/dispositivo", "/dispositivos"):
        if not casas_de(chat):
            return False
        _orden_dispositivo(chat, argumento)
        return True

    if texto.split("@")[0] in ("/codigo", "/código"):
        casas = casas_de(chat)
        if not casas:
            return False
        for cuenta, _, nombre_casa in casas:
            _elegir_duracion(chat, cuenta, nombre_casa)
        return True

    if texto.split("@")[0] in ("/camara", "/camaras", "/cámara"):
        casas = casas_de(chat)
        if not casas:
            return False
        for cuenta, _, nombre_casa in casas:
            _elegir_camara(chat, cuenta, nombre_casa)
        return True

    if texto.split("@")[0] in ("/reiniciar_camaras", "/reiniciar"):
        if not casas_de(chat):
            return False
        _reiniciar_camaras(chat)
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


def _reiniciar_camaras(chat):
    import camara
    import waha
    cuenta, u = _admin(chat)
    if not u:
        telegram.enviar(chat, "Solo un administrador del panel puede reiniciar el servicio de las cámaras.")
        return
    quien = u.get("nombre") or u["usuario"]
    with cuentas.en_cuenta(cuenta, u["usuario"]):
        nk.registrar("Reinicio del servicio de las cámaras (Telegram)", quien, "")
    telegram.enviar(chat, "🔄 Reiniciando el servicio de las cámaras… Durante un minuto no llegarán avisos.")

    def hacer():
        try:
            waha.accion_contenedor("reiniciar", camara.CONTENEDOR)
        except Exception as e:
            telegram.enviar(chat, f"⚠️ No se pudo reiniciar: {e}")
            return
        time.sleep(10)  # que el cliente note la caída antes de esperar a que vuelva
        fin = time.time() + 120
        while time.time() < fin and not (camara.CLIENTE.driver and camara.CLIENTE.dispositivos):
            time.sleep(2)
        if camara.CLIENTE.driver:
            telegram.enviar(chat, f"✅ Cámaras conectadas de nuevo ({len(camara.CLIENTE.dispositivos)}).")
        else:
            telegram.enviar(chat, "⚠️ El servicio de las cámaras no ha vuelto a conectar en 2 minutos. "
                                  "Míralo en el panel.")
    threading.Thread(target=hacer, daemon=True).start()


def preparar():
    """Pone la orden /puerta en el menú del bot."""
    try:
        telegram._api("setMyCommands", {"commands": [{"command": "puerta", "description": "Estado y control de la puerta"},
                                                     {"command": "camara", "description": "Ver una cámara ahora"},
                                                     {"command": "codigo", "description": "Dar un código temporal"},
                                                     {"command": "reiniciar_camaras", "description": "Reiniciar el servicio de las cámaras (admin)"},
                                                     {"command": "dispositivo", "description": "Recordar u olvidar un móvil (admin)"}]})
    except Exception as e:
        nk.log(f"Telegram: no se pudo poner el menú del bot: {e}")
