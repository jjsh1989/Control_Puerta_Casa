"""Avisos de la puerta: código desactivado, código incorrecto, aperturas y (con cámara) movimiento.

- Cada tipo de aviso tiene su propia franja horaria y sus días de la semana.
- Un hilo revisa el registro de Nuki cada pocos segundos y envía los avisos nuevos.
- El envío reutiliza los canales del aviso de batería (correo, WhatsApp, número principal).
"""
import threading
import time
from datetime import datetime, timedelta, timezone

import bateria
import cuentas
import nuki_codigos as nk

CLAVE_ESTADO = "_alertas"  # dentro de estado.json de cada cuenta
CADA_SEGUNDOS = 30
VENTANA_INCORRECTOS = timedelta(minutes=10)

TIPOS = {
    "desactivado": "🚨 Intento con un código desactivado",
    "incorrecto": "⚠️ Código incorrecto en el teclado",
    "ataque": "🚨 POSIBLE INTENTO DE ENTRAR: muchos códigos incorrectos",
    "apertura": "🚪 Se ha abierto la puerta",
    "movimiento": "📷 Movimiento o timbre en la cámara",
    "abierta": "🚪 La puerta lleva abierta demasiado tiempo",
    "atascada": "⚠️ La cerradura está atascada",
    "bloqueo": "🔒 Se ha bloqueado la cerradura",
}
ESTADOS_CERROJO = {0: "sin calibrar", 1: "bloqueada", 2: "desbloqueando…", 3: "desbloqueada", 4: "bloqueando…",
                   5: "pestillo abierto", 6: "desbloqueada (lock'n'go)", 7: "abriendo…", 254: "atascada", 255: "desconocido"}
ESTADOS_PUERTA = {0: "sin sensor", 1: "sensor desactivado", 2: "cerrada", 3: "abierta", 4: "desconocido",
                  5: "calibrando", 16: "sensor sin calibrar", 240: "sensor quitado", 255: "desconocido"}
REGLA_INICIAL = {"activo": False, "siempre": True, "desde": "00:00", "hasta": "07:00",
                 "dias": [0, 1, 2, 3, 4, 5, 6], "minimo": 1}
INICIAL = {
    "destino": {"canal": "whatsapp", "wa_principal": True, "telefono": "", "correo": ""},
    "reglas": {
        "desactivado": {**REGLA_INICIAL},
        "incorrecto": {**REGLA_INICIAL, "minimo": 2},
        "ataque": {**REGLA_INICIAL, "activo": True, "minimo": 5},
        "apertura": {**REGLA_INICIAL, "siempre": False},
        "movimiento": {**REGLA_INICIAL, "siempre": False},
        "abierta": {**REGLA_INICIAL, "siempre": True, "minutos": 5},
        "atascada": {**REGLA_INICIAL, "activo": True, "siempre": True},
        "bloqueo": {**REGLA_INICIAL, "siempre": False},
    },
}


def ajustes(cfg):
    a = cfg.get("alertas") or {}
    salida = {"destino": {**INICIAL["destino"], **(a.get("destino") or {})}, "reglas": {}}
    for tipo, base in INICIAL["reglas"].items():
        salida["reglas"][tipo] = {**base, **((a.get("reglas") or {}).get(tipo) or {})}
    return salida


def _minutos(hhmm):
    h, m = (str(hhmm or "0:0") + ":0").split(":")[:2]
    return (int(h) % 24) * 60 + int(m) % 60


def en_franja(regla, momento):
    """¿Cae el momento (hora local) dentro de la franja y los días de la regla?
    Las franjas pueden cruzar la medianoche (p. ej. 23:00 → 07:00): en ese caso el día que
    cuenta es el día en que EMPIEZA la franja."""
    if regla.get("siempre"):
        return True
    m = momento.hour * 60 + momento.minute
    desde, hasta = _minutos(regla.get("desde")), _minutos(regla.get("hasta"))
    dias = set(regla.get("dias") or range(7))
    if desde == hasta:  # todo el día
        return momento.weekday() in dias
    if desde < hasta:
        return desde <= m < hasta and momento.weekday() in dias
    if m >= desde:
        return momento.weekday() in dias
    if m < hasta:
        return (momento.weekday() - 1) % 7 in dias
    return False


def validar(datos):
    """Limpia lo que llega del panel."""
    base = ajustes({"alertas": datos})
    d = base["destino"]
    d["canal"] = d["canal"] if d["canal"] in bateria.CANALES else "whatsapp"
    if isinstance(d.get("por"), list):
        d["por"] = sorted({c for c in d["por"] if c in ("correo", "whatsapp", "telegram")})
    d["correo"] = ", ".join(bateria.correos_validos(d.get("correo") or ""))
    d["wa_principal"] = bool(d.get("wa_principal"))
    for tipo, r in base["reglas"].items():
        r["activo"] = bool(r.get("activo"))
        r["siempre"] = bool(r.get("siempre"))
        for k in ("desde", "hasta"):
            m = _minutos(r.get(k))
            r[k] = f"{m // 60:02d}:{m % 60:02d}"
        r["dias"] = sorted({int(x) % 7 for x in (r.get("dias") or [])}) or list(range(7))
        r["minimo"] = max(1, min(10, int(r.get("minimo") or 1)))
        if "minutos" in r:
            r["minutos"] = max(1, min(240, int(r.get("minutos") or 5)))
        if "clip" in r:
            r["clip"] = bool(r["clip"])
    return base


# ---------------------------------------------------------------- envío

def enviar(cfg, tipo, detalle, momento=None, foto=None):
    """Envía un aviso del tipo dado. Devuelve los destinos o lanza error."""
    a = ajustes(cfg)
    momento = momento or datetime.now().astimezone()
    titulo = TIPOS.get(tipo, tipo)
    texto = f"{detalle}\n{momento:%d/%m/%Y %H:%M:%S}"
    destinos = bateria.enviar_aviso(cfg, a["destino"], f"{titulo} · {bateria.nombre_cuenta()}", texto, foto=foto)
    nk.registrar("Aviso enviado", titulo, f"{detalle} → {', '.join(destinos)}")
    return destinos


def aviso_si_toca(cfg, tipo, detalle, momento):
    regla = ajustes(cfg)["reglas"].get(tipo)
    if not regla or not regla["activo"] or not en_franja(regla, momento):
        return False
    try:
        try:
            import camara
            hay_camara = bool(camara.camara_puerta(cfg))
        except Exception:
            hay_camara = False
        if hay_camara and tipo != "movimiento":
            # con cámara: foto reciente y, en los avisos graves, un clip de unos segundos
            camara.enviar_con_camara(cfg, tipo, detalle, momento,
                                     con_clip=regla.get("clip", tipo != "incorrecto"))
        else:
            enviar(cfg, tipo, detalle, momento)
        return True
    except Exception as e:
        nk.registrar("Aviso no enviado", TIPOS.get(tipo, tipo), str(e), ok=False)
        return False


# ---------------------------------------------------------------- vigilancia del registro

def _fecha(e):
    try:
        return datetime.fromisoformat(str(e.get("date", "")).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _clave(e):
    return str(e.get("id") or f"{e.get('date')}|{e.get('action')}|{e.get('name')}|{e.get('state')}")


def revisar(cfg):
    """Lee los últimos eventos de la cerradura y avisa de los nuevos. Devuelve cuántos avisos envió."""
    a = ajustes(cfg)
    if not any(r["activo"] for t, r in a["reglas"].items() if t != "movimiento"):
        return 0
    token, sid = cfg["nuki"].get("token"), cfg["nuki"].get("smartlock_id")
    if not (token and sid):
        return 0
    eventos = nk.Nuki(token).registro(sid, 30)
    estado = nk.cargar_json(nk.ruta_estado(), {})
    e = estado.get(CLAVE_ESTADO) or {}
    vistos = list(e.get("vistos") or [])
    primera_vez = not vistos and not e.get("iniciado")
    nuevos = [ev for ev in eventos if _clave(ev) not in vistos]
    nuevos.sort(key=_fecha)
    enviados = 0
    if not primera_vez:  # la primera vez solo se toma nota, para no avisar de cosas antiguas
        limite_antiguedad = datetime.now(timezone.utc) - timedelta(hours=2)
        incorrectos = [datetime.fromisoformat(x) for x in e.get("incorrectos", [])]
        for ev in nuevos:
            cuando = _fecha(ev)
            if cuando < limite_antiguedad:
                continue
            local = cuando.astimezone()
            t = nk.traducir_registro(cfg, [ev])[0]
            accion, estado_nuki = ev.get("action"), ev.get("state", 0)
            if t["tipo_persona"] == "desactivado":
                enviados += aviso_si_toca(cfg, "desactivado", f"{t['quien']} · {t['origen']}", local)
            elif estado_nuki == 224:
                incorrectos = [x for x in incorrectos if cuando - x < VENTANA_INCORRECTOS] + [cuando]
                minimo = a["reglas"]["incorrecto"]["minimo"]
                if len(incorrectos) == minimo:  # un solo aviso por racha
                    n = f"{len(incorrectos)} intentos seguidos" if minimo > 1 else "1 intento"
                    enviados += aviso_si_toca(cfg, "incorrecto", f"{n} con un código que no existe", local)
                ataque = a["reglas"]["ataque"]["minimo"]
                if len(incorrectos) == ataque:  # racha grande: aviso urgente, también una sola vez
                    primero = incorrectos[0].astimezone()
                    enviados += aviso_si_toca(
                        cfg, "ataque",
                        f"{len(incorrectos)} códigos incorrectos desde las {primero:%H:%M:%S}. "
                        f"Alguien puede estar probando códigos al azar en el teclado. "
                        f"Mira la cámara o acércate a comprobarlo.", local)
            elif estado_nuki == 0 and accion in (1, 3, 5):
                enviados += aviso_si_toca(cfg, "apertura", f"{t['quien']} · {t['origen']}", local)
            elif estado_nuki == 0 and accion in (2, 4):
                enviados += aviso_si_toca(cfg, "bloqueo", f"{t['quien']} · {t['origen']}", local)
        e["incorrectos"] = [x.isoformat() for x in incorrectos if datetime.now(timezone.utc) - x < VENTANA_INCORRECTOS]
    enviados += _revisar_estado(cfg, a, sid, token, e)
    e["vistos"] = ([_clave(ev) for ev in nuevos] + vistos)[:300]
    e["iniciado"] = True
    e["revisado"] = datetime.now().astimezone().isoformat(timespec="seconds")
    estado = {**nk.cargar_json(nk.ruta_estado(), {}), CLAVE_ESTADO: e}
    nk.guardar_json(nk.ruta_estado(), estado)
    return enviados


def _revisar_estado(cfg, a, sid, token, e):
    """Puerta abierta demasiado tiempo y cerradura atascada (lee el estado actual de la cerradura)."""
    r = a["reglas"]
    if not (r["abierta"]["activo"] or r["atascada"]["activo"]):
        e.pop("abierta_desde", None)
        return 0
    st = nk.Nuki(token).estado(sid).get("state") or {}
    ahora = datetime.now().astimezone()
    enviados = 0
    # puerta abierta
    if st.get("doorState") == 3:
        desde = datetime.fromisoformat(e["abierta_desde"]) if e.get("abierta_desde") else ahora
        e["abierta_desde"] = desde.isoformat(timespec="seconds")
        minutos = r["abierta"].get("minutos", 5)
        if not e.get("abierta_avisada") and ahora - desde >= timedelta(minutes=minutos):
            e["abierta_avisada"] = True
            enviados += aviso_si_toca(cfg, "abierta", f"Abierta desde las {desde:%H:%M} (más de {minutos} min). "
                                      f"Comprueba que no se haya quedado mal cerrada.", ahora)
    else:
        if e.get("abierta_avisada") and st.get("doorState") == 2 and r["abierta"]["activo"]:
            try:
                enviar(cfg, "abierta", "✅ Ya se ha cerrado.", ahora)
            except Exception:
                pass
        e.pop("abierta_desde", None)
        e.pop("abierta_avisada", None)
    # cerradura atascada
    if st.get("state") == 254:
        if not e.get("atascada_avisada"):
            e["atascada_avisada"] = True
            enviados += aviso_si_toca(cfg, "atascada", "El motor no ha podido terminar de girar. "
                                      "Puede que la puerta no esté bien cerrada o que algo bloquee el cerrojo.", ahora)
    else:
        e.pop("atascada_avisada", None)
    return enviados


class Vigilante(threading.Thread):
    """Revisa el registro de la cerradura de cada cuenta cada CADA_SEGUNDOS."""

    def __init__(self):
        super().__init__(daemon=True)

    def run(self):
        time.sleep(15)
        while True:
            for c in cuentas.listar_cuentas():
                try:
                    with cuentas.en_cuenta(c["id"], "avisos"):
                        cuentas._ctx.origen = "avisos"
                        try:
                            revisar(nk.cargar_config())
                        finally:
                            cuentas._ctx.origen = None
                except Exception as err:
                    nk.log(f"Avisos ({c['id']}): {err}")
            time.sleep(CADA_SEGUNDOS)
