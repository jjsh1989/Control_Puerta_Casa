"""Batería de la cerradura, del teclado y del sensor de puerta, y avisos por correo.

- La batería se lee de la API de Nuki (GET /smartlock/{id} → state).
- El servidor de correo (SMTP) es común a todo el sistema y lo configura el administrador;
  su contraseña va a la bóveda.
- Cada cuenta elige el porcentaje de aviso y a qué correo(s) se envía.
- Se avisa una sola vez por cada bajada; el aviso se "rearma" cuando la batería
  vuelve a subir claramente (pilas cambiadas o recargada).
"""
import re
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import cuentas
import nuki_codigos as nk

CLAVE_ESTADO = "_bateria"            # dentro de estado.json de cada cuenta
CLAVE_SMTP_PASSWORD = "smtp_password"  # en la bóveda de _sistema
AVISO_INICIAL = {"activo": False, "umbral": 25, "canal": "correo", "correo": "", "telefono": "", "wa_principal": False}
CANALES = ("correo", "whatsapp", "ambos")
MARGEN_REARME = 10                   # % por encima del umbral para volver a avisar
CACHE_SEGUNDOS = 300
_cache = {}                          # cuenta -> (momento, datos)

RE_CORREO = re.compile(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+")


# ---------------------------------------------------------------- lectura

def ajustes_aviso(cfg):
    av = dict(AVISO_INICIAL)
    av.update(cfg.get("aviso_bateria") or {})
    return av


def leer(cfg, forzar=False):
    """Estado de las baterías de la cerradura de la cuenta actual (o None si no está configurada)."""
    cuenta = cuentas.cuenta_actual()
    momento, datos = _cache.get(cuenta, (0, None))
    if datos and not forzar and time.time() - momento < CACHE_SEGUNDOS:
        return datos
    token, sid = cfg["nuki"].get("token"), cfg["nuki"].get("smartlock_id")
    if not (token and sid):
        return None
    _, r = nk.peticion("GET", f"{nk.NUKI_API}/smartlock/{sid}", cabeceras=nk.Nuki(token)._cab())
    r = r if isinstance(r, dict) else {}
    st = r.get("state") or {}
    carga = st.get("batteryCharge")
    datos = {
        "nombre": r.get("name", ""),
        "carga": int(carga) if isinstance(carga, (int, float)) else None,
        "critica": bool(st.get("batteryCritical")),
        "cargando": bool(st.get("batteryCharging")),
        "teclado_critica": bool(st.get("keypadBatteryCritical")),
        "sensor_critica": bool(st.get("doorsensorBatteryCritical")),
        "leido": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _cache[cuenta] = (time.time(), datos)
    return datos


# ---------------------------------------------------------------- correo

def smtp_config():
    s = dict(cuentas.leer_sistema().get("smtp") or {})
    s.setdefault("servidor", "")
    s.setdefault("puerto", 587)
    s.setdefault("seguridad", "starttls")
    s.setdefault("usuario", "")
    s.setdefault("remitente", "")
    return s


def smtp_publico():
    s = smtp_config()
    s["password_puesta"] = bool(cuentas.boveda_leer(CLAVE_SMTP_PASSWORD, cuentas.CUENTA_SISTEMA))
    return s


def guardar_smtp(datos):
    s = smtp_config()
    s["servidor"] = (datos.get("servidor") or "").strip()[:200]
    s["puerto"] = max(1, min(65535, int(datos.get("puerto") or 587)))
    s["seguridad"] = datos.get("seguridad") if datos.get("seguridad") in ("ssl", "starttls", "ninguna") else "starttls"
    s["usuario"] = (datos.get("usuario") or "").strip()[:200]
    s["remitente"] = (datos.get("remitente") or "").strip()[:200]
    sistema = cuentas.leer_sistema()
    sistema["smtp"] = s
    cuentas.guardar_sistema(sistema)
    if datos.get("password"):
        cuentas.boveda_guardar(CLAVE_SMTP_PASSWORD, datos["password"], cuentas.CUENTA_SISTEMA)
    return smtp_publico()


def correos_validos(texto):
    """Lista de direcciones a partir de un texto separado por comas; error si alguna no es válida."""
    partes = [p.strip() for p in re.split(r"[,;\s]+", texto or "") if p.strip()]
    malas = [p for p in partes if not RE_CORREO.fullmatch(p)]
    if malas:
        raise RuntimeError(f"Correo no válido: {', '.join(malas)}")
    return partes[:5]


def enviar_correo(destinos, asunto, texto):
    s = smtp_config()
    if not s["servidor"]:
        raise RuntimeError("Falta configurar el servidor de correo (Ajustes → Servidor de correo, solo administrador).")
    if not destinos:
        raise RuntimeError("No hay ningún correo de destino.")
    password = cuentas.boveda_leer(CLAVE_SMTP_PASSWORD, cuentas.CUENTA_SISTEMA)
    m = EmailMessage()
    m["From"] = s["remitente"] or s["usuario"]
    m["To"] = ", ".join(destinos)
    m["Subject"] = asunto
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain="puerta.local")
    m.set_content(texto)
    ctx = nk.contexto_ssl()
    if s["seguridad"] == "ssl":
        cliente = smtplib.SMTP_SSL(s["servidor"], s["puerto"], context=ctx, timeout=20)
    else:
        cliente = smtplib.SMTP(s["servidor"], s["puerto"], timeout=20)
    with cliente:
        if s["seguridad"] == "starttls":
            cliente.starttls(context=ctx)
        if s["usuario"] and password:
            cliente.login(s["usuario"], password)
        cliente.send_message(m)


def numero_principal():
    """Número vinculado al WhatsApp del servidor (WAHA), con prefijo +."""
    import waha
    st = waha.estado_sesion()
    if st.get("estado") != "WORKING" or not st.get("numero"):
        raise RuntimeError("El WhatsApp del servidor no está vinculado (pestaña WhatsApp → escanear QR).")
    return "+" + st["numero"]


def enviar_whatsapp_aviso(cfg, telefono, texto):
    """WhatsApp automático. Si la cuenta usa «enlace» o CallMeBot (que no sirven para avisos
    automáticos a un número cualquiera), se usa el WhatsApp del servidor (WAHA)."""
    if not telefono:
        raise RuntimeError("No hay teléfono para el aviso por WhatsApp.")
    c = dict(cfg)
    c["whatsapp"] = dict(cfg["whatsapp"])
    if c["whatsapp"].get("proveedor") not in ("waha", "twilio"):
        c["whatsapp"]["proveedor"] = "waha"
    nk.enviar_whatsapp(c, {"nombre": "Aviso de batería", "telefono": telefono}, texto, abrir=False)


def canales_de(av):
    """Canales elegidos: lista «por» (correo/whatsapp/telegram), o el formato antiguo «canal»."""
    if isinstance(av.get("por"), list):
        return {c for c in av["por"] if c in ("correo", "whatsapp", "telegram")}
    c = av.get("canal") if av.get("canal") in CANALES else "correo"
    return {"correo", "whatsapp"} if c == "ambos" else {c}


def enviar_aviso(cfg, av, asunto, texto, foto=None, video=None, recibos=None, botones=None, sin_sonido=False):
    """Envía por los canales elegidos. Devuelve la lista de destinos a los que llegó;
    lanza error solo si no llegó por ninguno. Foto y vídeo solo se adjuntan por Telegram.
    `recibos` (dict) recoge {chat: id_del_mensaje} de las fotos enviadas por Telegram."""
    por = canales_de(av)
    enviados, errores = [], []
    if not por:
        raise RuntimeError("No hay ningún canal elegido para el aviso.")
    if "telegram" in por:
        try:
            import telegram
            mensaje = f"{asunto}\n\n{texto}"
            if video:
                n = telegram.enviar_a_todos(cfg, telegram.enviar_video, video, mensaje)
            elif foto:
                n = telegram.enviar_a_todos(cfg, telegram.enviar_foto, foto, mensaje, botones, sin_sonido,
                                            recibos=recibos)
            else:
                n = telegram.enviar_a_todos(cfg, telegram.enviar, mensaje, botones, sin_sonido)
            enviados.append("Telegram" + (f" ({n})" if n > 1 else ""))
        except Exception as e:
            errores.append(f"Telegram: {e}")
    if "correo" in por:
        try:
            destinos = correos_validos(av.get("correo"))
            enviar_correo(destinos, asunto, texto)
            enviados += destinos
        except Exception as e:
            errores.append(f"correo: {e}")
    if "whatsapp" in por:
        try:
            if av.get("wa_principal"):
                c = dict(cfg)
                c["whatsapp"] = {**cfg["whatsapp"], "proveedor": "waha"}
                enviar_whatsapp_aviso(c, numero_principal(), f"*{asunto}*\n\n{texto}")
                enviados.append("WhatsApp principal")
            else:
                enviar_whatsapp_aviso(cfg, av.get("telefono"), f"*{asunto}*\n\n{texto}")
                enviados.append("WhatsApp " + (av.get("telefono") or "")[:-4] + "••••")
        except Exception as e:
            errores.append(f"WhatsApp: {e}")
    if errores and not enviados:
        raise RuntimeError(" · ".join(errores))
    if errores:
        nk.registrar("Aviso parcialmente fallido", asunto, " · ".join(errores), ok=False)
    return enviados


def nombre_cuenta():
    for c in cuentas.listar_cuentas():
        if c["id"] == cuentas.cuenta_actual():
            return c.get("nombre") or c["id"]
    return cuentas.cuenta_actual()


def enviar_prueba(cfg):
    av = ajustes_aviso(cfg)
    datos = None
    try:
        datos = leer(cfg, forzar=True)
    except Exception:
        pass
    carga = f"{datos['carga']} %" if datos and datos["carga"] is not None else "desconocida"
    destinos = enviar_aviso(cfg, av, f"Prueba de aviso · {nombre_cuenta()}",
                            f"Este es un aviso de prueba del gestor de la puerta ({nombre_cuenta()}).\n\n"
                            f"Batería actual de la cerradura: {carga}.\n"
                            f"Recibirás un aviso cuando baje del {av['umbral']} %.\n")
    nk.registrar("Aviso de batería de prueba enviado", ", ".join(destinos), "")
    return destinos


# ---------------------------------------------------------------- vigilancia

def revisar(cfg):
    """Lee la batería y, si hace falta, envía el aviso. Se llama periódicamente para cada cuenta."""
    datos = leer(cfg, forzar=True)
    if not datos:
        return None
    av = ajustes_aviso(cfg)
    umbral = int(av["umbral"])
    estado = nk.cargar_json(nk.ruta_estado(), {})
    e = estado.setdefault(CLAVE_ESTADO, {})
    e["ultimo"] = datos

    carga = datos["carga"]
    piezas = {
        "cerradura": ((carga is not None and carga <= umbral) or datos["critica"],
                      carga is not None and carga >= min(100, umbral + MARGEN_REARME) and not datos["critica"],
                      f"La batería de la cerradura está al {carga} %" if carga is not None
                      else "La batería de la cerradura está en nivel crítico"),
        "teclado": (datos["teclado_critica"], not datos["teclado_critica"],
                    "Las pilas del teclado Nuki están casi agotadas"),
        "sensor": (datos["sensor_critica"], not datos["sensor_critica"],
                   "La pila del sensor de puerta está casi agotada"),
    }
    pendientes = []
    for pieza, (baja, recuperada, texto) in piezas.items():
        if baja:
            if not e.get(f"registrado_{pieza}"):
                nk.registrar("Batería baja", pieza, texto)
                e[f"registrado_{pieza}"] = True
            if not e.get(f"avisado_{pieza}"):
                pendientes.append((pieza, texto))
        elif recuperada:
            if e.get(f"registrado_{pieza}"):
                nk.registrar("Batería recuperada", pieza,
                             f"{carga} %" if pieza == "cerradura" and carga is not None else "Normal")
            e[f"registrado_{pieza}"] = False
            e[f"avisado_{pieza}"] = False

    if pendientes and av["activo"] and canales_de(av):
        try:
            lineas = "\n".join(f"• {t}." for _, t in pendientes)
            destinos = enviar_aviso(cfg, av, f"🔋 Batería baja · {nombre_cuenta()}",
                                    f"Aviso del gestor de la puerta ({nombre_cuenta()}):\n\n{lineas}\n\n"
                                    f"Cámbialas o cárgalas pronto para que la cerradura no se quede sin energía.\n"
                                    f"(Aviso configurado al {umbral} %.)\n")
            for pieza, _ in pendientes:
                e[f"avisado_{pieza}"] = True
            e.pop("ultimo_error", None)
            nk.registrar("Aviso de batería enviado", ", ".join(destinos), "; ".join(t for _, t in pendientes))
        except Exception as err:
            # se reintenta en la siguiente revisión; el error solo se anota una vez
            if e.get("ultimo_error") != str(err):
                nk.registrar("Aviso de batería no enviado", "", str(err), ok=False)
            e["ultimo_error"] = str(err)

    estado = {**nk.cargar_json(nk.ruta_estado(), {}), CLAVE_ESTADO: e}  # releer por si cambió mientras
    nk.guardar_json(nk.ruta_estado(), estado)
    return datos


def estado_aviso():
    return nk.cargar_json(nk.ruta_estado(), {}).get(CLAVE_ESTADO, {})
