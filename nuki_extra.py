"""Funciones de Nuki que dependen de lo que tenga cada casa.

- capacidades(): qué tiene la cerradura (teclado, sensor, llavero, Opener…) para que el panel
  muestre solo lo que sirve.
- Ajustes de la cerradura: solo los que la API devuelve para ese modelo.
- Accesos: apps, llaveros, teclados… (los códigos del teclado se gestionan aparte).
- Programaciones: bloquear / desbloquear / abrir a una hora y unos días.
"""
import secrets
import time
from datetime import datetime, timedelta

import nuki_codigos as nk

TIPOS_DISPOSITIVO = {0: "Smart Lock (1.ª/2.ª gen.)", 1: "Box", 2: "Opener", 3: "Smart Door",
                     4: "Smart Lock 3.0/4.0", 5: "Smart Lock Ultra"}
TIPOS_AUTH = {0: "App", 1: "Bridge", 2: "Llavero (Fob)", 3: "Teclado", 13: "Código del teclado"}
SIN_SENSOR = (0, 1, 240)  # doorState: sin sensor, desactivado, quitado

# Ajustes que el panel sabe manejar. Solo se muestran los que existan en la cerradura.
AJUSTES = [
    # (sección, clave, tipo, etiqueta, ayuda, opciones)
    ("advancedConfig", "autoLock", "bool", "Bloqueo automático",
     "Se bloquea sola un rato después de desbloquearse.", None),
    ("advancedConfig", "autoLockTimeout", "sel", "Bloquear automáticamente tras",
     "", [(30, "30 s"), (60, "1 min"), (120, "2 min"), (180, "3 min"), (300, "5 min"), (600, "10 min"),
          (900, "15 min"), (1800, "30 min")]),
    ("advancedConfig", "immediateAutoLockEnabled", "bool", "Bloquear en cuanto se cierre la puerta",
     "Necesita el sensor de puerta.", None),
    ("advancedConfig", "nightModeEnabled", "bool", "Modo noche", "Comportamiento distinto por la noche.", None),
    ("advancedConfig", "nightModeStartTime", "hora", "Modo noche: desde", "", None),
    ("advancedConfig", "nightModeEndTime", "hora", "Modo noche: hasta", "", None),
    ("advancedConfig", "nightModeAutoLockEnabled", "bool", "Modo noche: bloqueo automático", "", None),
    ("advancedConfig", "nightModeAutoUnlockDisabled", "bool", "Modo noche: sin desbloqueo automático", "", None),
    ("advancedConfig", "nightModeImmediateLockOnStart", "bool", "Modo noche: bloquear al empezar", "", None),
    ("config", "autoUnlatch", "bool", "Abrir el pestillo al desbloquear",
     "Al desbloquear también suelta el pestillo, y la puerta se puede empujar.", None),
    ("config", "singleLock", "bool", "Bloqueo de una sola vuelta", "Si no, da dos vueltas.", None),
    ("advancedConfig", "unlatchDuration", "sel", "Tiempo con el pestillo abierto", "",
     [(1, "1 s"), (3, "3 s"), (5, "5 s"), (10, "10 s"), (15, "15 s"), (20, "20 s"), (30, "30 s")]),
    ("advancedConfig", "lngTimeout", "sel", "Espera del Lock'n'Go", "Tiempo para salir antes de que se bloquee.",
     [(5, "5 s"), (10, "10 s"), (15, "15 s"), (20, "20 s"), (30, "30 s"), (45, "45 s"), (60, "60 s")]),
    ("config", "buttonEnabled", "bool", "Botón de la cerradura", "Permite abrir y cerrar pulsando la cerradura.", None),
    ("config", "ledEnabled", "bool", "Luz LED", "", None),
    ("config", "ledBrightness", "sel", "Brillo del LED", "",
     [(0, "Mínimo"), (1, "1"), (2, "2"), (3, "3"), (4, "4"), (5, "Máximo")]),
    ("advancedConfig", "autoUpdateEnabled", "bool", "Actualizar el firmware automáticamente", "", None),
]

def _dispositivo(nuki, sid, fresco=False):
    """Capacidades y ajustes cambian poco: vale una lectura de hace 2 minutos (salvo «fresco»)."""
    return nuki.estado(sid, max_edad=0 if fresco else 120)


def olvidar(nuki, sid):
    nk.LECTURAS.olvidar(nuki.token, sid)


def capacidades(cfg, admin=False):
    """Lo que tiene esta casa. Si algo falla, se devuelve lo mínimo y el panel oculta lo demás."""
    n = cfg.get("nuki") or {}
    c = {"nuki": bool(n.get("token") and n.get("smartlock_id")), "opener": bool(n.get("opener_id")),
         "opener_nombre": n.get("opener_nombre", "")}
    if not c["nuki"]:
        return c
    nuki = nk.Nuki(n["token"])
    try:
        d = _dispositivo(nuki, n["smartlock_id"])
    except Exception as e:
        c["error"] = str(e)
        return c
    conf, st = d.get("config") or {}, d.get("state") or {}
    tipo = d.get("type")
    c.update({
        "modelo": TIPOS_DISPOSITIVO.get(tipo, f"Nuki (tipo {tipo})"),
        "es_opener": tipo == 2,
        "lockngo": tipo != 2,
        "teclado": bool(conf.get("keypadPaired") or conf.get("keypad2Paired")),
        "teclado2": bool(conf.get("keypad2Paired")),
        "llavero": bool(conf.get("fobPaired")),
        "sensor": st.get("doorState") is not None and st.get("doorState") not in SIN_SENSOR,
        "online": d.get("serverState", 0) == 0,
        "ajustes": [a[1] for a in AJUSTES if a[1] in (d.get(a[0]) or {})],
    })
    if admin:
        try:
            c["openers"] = [{"id": x.get("smartlockId"), "nombre": (x.get("name") or "").strip()}
                            for x in nuki.cerraduras() if x.get("type") == 2]
        except Exception:
            c["openers"] = []
    return c


# ---------------------------------------------------------------- ajustes de la cerradura

def leer_ajustes(cfg):
    n = cfg["nuki"]
    d = _dispositivo(nk.Nuki(n["token"]), n["smartlock_id"], fresco=True)
    salida = []
    for sec, clave, tipo, etq, ayuda, ops in AJUSTES:
        bloque = d.get(sec) or {}
        if clave not in bloque:
            continue
        salida.append({"clave": clave, "tipo": tipo, "etiqueta": etq, "ayuda": ayuda, "valor": bloque[clave],
                       "opciones": [{"v": v, "t": t} for v, t in ops] if ops else None})
    return salida


def guardar_ajustes(cfg, cambios):
    """cambios: {clave: valor}. Lee la configuración actual, cambia solo lo pedido y la manda entera."""
    n = cfg["nuki"]
    nuki = nk.Nuki(n["token"])
    d = _dispositivo(nuki, n["smartlock_id"], fresco=True)
    por_seccion, hecho = {}, []
    for sec, clave, tipo, etq, _, ops in AJUSTES:
        if clave not in cambios or clave not in (d.get(sec) or {}):
            continue
        v = cambios[clave]
        if tipo == "bool":
            v = bool(v)
        elif tipo == "sel":
            v = int(v)
            if v not in [o[0] for o in ops]:
                raise RuntimeError(f"Valor no válido para «{etq}».")
        elif tipo == "hora":
            m = nk._min(v)
            v = f"{m // 60:02d}:{m % 60:02d}"
        if d[sec][clave] == v:
            continue
        por_seccion.setdefault(sec, dict(d[sec]))[clave] = v
        hecho.append(f"{etq}: {_texto(v, ops)}")
    for sec, datos in por_seccion.items():
        nuki.guardar_ajustes(n["smartlock_id"], sec, datos)
    olvidar(nuki, n["smartlock_id"])
    return hecho


def _texto(v, ops):
    if isinstance(v, bool):
        return "sí" if v else "no"
    return dict(ops or []).get(v, v)


# ---------------------------------------------------------------- accesos (apps, llaveros…)

def es_del_panel(a):
    """La autorización «Nuki Web» es la que usa la API (y por tanto este panel): no se toca."""
    return (a.get("name") or "").strip().lower().startswith("nuki web")


def accesos(cfg):
    n = cfg["nuki"]
    salida = []
    for a in nk.Nuki(n["token"]).autorizaciones(n["smartlock_id"]):
        if a.get("type") == nk.TIPO_CODIGO_TECLADO:
            continue  # los códigos van en Personas y «Otros códigos del teclado»
        salida.append({
            "id": a.get("id"), "nombre": (a.get("name") or "").strip() or "(sin nombre)",
            "tipo": a.get("type"), "tipo_texto": TIPOS_AUTH.get(a.get("type"), f"Otro (tipo {a.get('type')})"),
            "activo": bool(a.get("enabled", True)), "remoto": bool(a.get("remoteAllowed")),
            "usos": a.get("lockCount"), "ultimo": a.get("lastActiveDate"),
            "horario": _horario_auth(a), "del_panel": es_del_panel(a),
            "gestionable": a.get("type") in (0, 2) and not es_del_panel(a),
        })
    salida.sort(key=lambda x: (x["tipo"], not x["activo"], x["nombre"].lower()))
    return salida


def _horario_auth(a):
    dias, de, hasta = a.get("allowedWeekDays", 127), a.get("allowedFromTime", 0), a.get("allowedUntilTime", 0)
    if dias in (127, None) and not de and not hasta and not a.get("allowedUntilDate"):
        return ""
    partes = []
    if dias not in (127, None):
        partes.append(", ".join(nk.DIAS_CORTOS[i] for i in range(7) if dias & (64 >> i)))
    if de or hasta:
        partes.append(f"{de // 60:02d}:{de % 60:02d}–{hasta // 60:02d}:{hasta % 60:02d}")
    if a.get("allowedUntilDate"):
        partes.append("hasta " + str(a["allowedUntilDate"])[:10])
    return " · ".join(partes)


def cambiar_acceso(cfg, ident, accion):
    n = cfg["nuki"]
    nuki = nk.Nuki(n["token"])
    a = next((x for x in nuki.autorizaciones(n["smartlock_id"]) if str(x.get("id")) == str(ident)), None)
    if not a:
        raise RuntimeError("Ese acceso ya no existe.")
    if es_del_panel(a):
        raise RuntimeError("Este acceso es el que usa el panel para hablar con la cerradura: no se puede tocar desde aquí.")
    if a.get("type") not in (0, 2):
        raise RuntimeError("Este tipo de acceso se gestiona desde la app de Nuki.")
    nombre = (a.get("name") or "").strip()
    if accion == "eliminar":
        nuki.borrar(n["smartlock_id"], a["id"])
        texto = "Acceso eliminado"
    elif accion in ("activar", "desactivar"):
        nuki.actualizar_auth(n["smartlock_id"], a, {"enabled": accion == "activar"})
        texto = "Acceso activado" if accion == "activar" else "Acceso desactivado"
    else:
        raise RuntimeError("Acción no válida.")
    nk.registrar(texto, nombre, TIPOS_AUTH.get(a.get("type"), ""))
    return texto


# ---------------------------------------------------------------- programaciones

ACCIONES_PROG = {
    "bloquear": (2, "Bloquear"),
    "desbloquear": (1, "Desbloquear"),
    "abrir": (3, "Abrir la puerta"),
    "lockngo": (4, "Lock'n'Go"),
    "portal": (3, "Abrir el portal (Opener)"),
    "camaras_apagar": (None, "Apagar las cámaras"),
    "camaras_encender": (None, "Encender las cámaras"),
    "camaras_fuera": (0, "Cámaras en modo «Fuera de casa»"),
    "camaras_casa": (1, "Cámaras en modo «En casa»"),
    "camaras_desactivar": (63, "Cámaras en modo «Desactivado»"),
}
ACCIONES_NUKI = ("bloquear", "desbloquear", "abrir", "lockngo", "portal")
MARGEN = timedelta(minutes=5)  # si el servidor estaba apagado, no se hace una acción con más retraso


def validar_programaciones(lista, cfg):
    if not isinstance(lista, list) or len(lista) > 30:
        raise RuntimeError("Lista de programaciones no válida.")
    salida = []
    for p in lista:
        accion = p.get("accion")
        if accion not in ACCIONES_PROG:
            raise RuntimeError("Acción programada no válida.")
        if accion == "portal" and not cfg["nuki"].get("opener_id"):
            raise RuntimeError("Esta casa no tiene Opener.")
        if accion in ACCIONES_NUKI and not cfg["nuki"].get("token"):
            raise RuntimeError("Esta casa no tiene cerradura Nuki.")
        if accion.startswith("camaras_"):
            import camara
            import cuentas
            if not camara.camaras_de(cuentas.cuenta_actual()):
                raise RuntimeError("Esta casa no tiene cámaras conectadas.")
        dias = sorted({int(x) % 7 for x in (p.get("dias") or [])})
        if not dias:
            raise RuntimeError("Cada programación necesita al menos un día.")
        m = nk._min(p.get("hora") or "00:00")
        salida.append({
            "id": str(p.get("id") or secrets.token_hex(4))[:12], "accion": accion, "dias": dias,
            "hora": f"{m // 60:02d}:{m % 60:02d}", "activa": bool(p.get("activa", True)),
            "solo_si_cerrada": bool(p.get("solo_si_cerrada", True)), "avisar": bool(p.get("avisar", True)),
            "nota": str(p.get("nota") or "")[:40],
        })
    return salida


def texto_prog(p):
    return f"{ACCIONES_PROG[p['accion']][1]} · {nk.texto_dias(p['dias'])} a las {p['hora']}"


def revisar_programaciones(cfg, avisar):
    """Se llama cada minuto. avisar(texto) manda el aviso por los canales de la casa."""
    progs = [p for p in cfg.get("programaciones") or [] if p.get("activa")]
    if not progs:
        return
    ahora = datetime.now().astimezone()
    estado = nk.cargar_json(nk.ruta_estado(), {})
    hechas = estado.get("_programaciones") or {}
    cambios = False
    for p in progs:
        prevista = ahora.replace(hour=nk._min(p["hora"]) // 60, minute=nk._min(p["hora"]) % 60, second=0, microsecond=0)
        if prevista > ahora:
            continue
        clave = f"{prevista:%Y-%m-%d}"
        if prevista.weekday() not in p["dias"] or hechas.get(p["id"]) == clave:
            continue
        hechas[p["id"]] = clave
        cambios = True
        if ahora - prevista > MARGEN:
            continue  # demasiado tarde (servidor apagado): no se hace con retraso
        try:
            texto = ejecutar(cfg, p)
        except Exception as e:
            texto = f"⚠️ No se ha podido hacer «{texto_prog(p)}»: {e}"
            nk.registrar("Programación fallida", ACCIONES_PROG[p["accion"]][1], str(e), ok=False)
        if p.get("avisar") and texto:
            try:
                avisar(texto)
            except Exception:
                pass
    if cambios:
        estado = nk.cargar_json(nk.ruta_estado(), {})
        estado["_programaciones"] = hechas
        nk.guardar_json(nk.ruta_estado(), estado)


def ejecutar(cfg, p):
    if p["accion"].startswith("camaras_"):
        import camara
        import cuentas
        num, nombre = ACCIONES_PROG[p["accion"]]
        auto = cfg.get("camaras_auto") or {}
        if p["accion"] in ("camaras_apagar", "camaras_encender"):
            hechos, fallos = camara.aplicar_casa(cuentas.cuenta_actual(), privacidad=p["accion"] == "camaras_apagar",
                                                 solo=auto.get("camaras") or None, quien="programado")
        else:
            hechos, fallos = camara.aplicar_casa(cuentas.cuenta_actual(), modo=num, quien="programado")
        if fallos:
            return f"⚠️ {nombre} (programado a las {p['hora']}): {'; '.join(fallos)}"
        return f"⏰ {nombre}: hecho (programado a las {p['hora']})." if hechos else ""
    n = cfg["nuki"]
    nuki = nk.Nuki(n["token"])
    num, nombre = ACCIONES_PROG[p["accion"]]
    if p["accion"] == "portal":
        nuki.accion(n["opener_id"], num)
        nk.registrar("Programación: portal abierto", "", p.get("nota", ""))
        return f"⏰ Portal abierto (programado a las {p['hora']})."
    st = (nuki.estado(n["smartlock_id"]).get("state") or {})
    if p["accion"] in ("bloquear", "lockngo") and p.get("solo_si_cerrada") and st.get("doorState") == 3:
        nk.registrar("Programación no hecha", nombre, "La puerta estaba abierta", ok=False)
        return f"⚠️ No he bloqueado la cerradura a las {p['hora']}: la puerta está abierta."
    if p["accion"] == "bloquear" and st.get("state") == 1:
        nk.registrar("Programación", nombre, "Ya estaba bloqueada")
        return ""  # ya estaba como se quería: no hace falta avisar
    if p["accion"] == "desbloquear" and st.get("state") == 3:
        nk.registrar("Programación", nombre, "Ya estaba desbloqueada")
        return ""
    nuki.accion(n["smartlock_id"], num)
    nk.registrar("Programación hecha", nombre, f"{p['hora']}" + (f" · {p['nota']}" if p.get("nota") else ""))
    return f"⏰ {nombre}: hecho (programado a las {p['hora']})."


# ---------------------------------------------------------------- control horario de la app de Nuki
# La API web de Nuki no permite leer el «control horario» que se configura en la app: vive dentro de la
# cerradura. Pero cada vez que se ejecuta queda en el registro como acción automática (trigger 3, sin
# usuario). Repasando los últimos 30 días se ven las que se repiten a la misma hora.

DIAS_REGISTRO = 30
_CACHE_HORARIO = {}  # (token, id) -> (momento, lista)


def _registro_paginado(nuki, sid, accion, desde, paginas=20):
    import urllib.parse
    ultimo, salida = None, []
    for _ in range(paginas):
        q = {"limit": 50, "action": accion, "fromDate": nk.fecha_iso(desde)}
        if ultimo:
            q["id"] = ultimo
        _, ev = nk.peticion("GET", f"{nk.NUKI_API}/smartlock/{sid}/log?" + urllib.parse.urlencode(q),
                            cabeceras=nuki._cab())
        if not isinstance(ev, list) or not ev:
            break
        salida += ev
        if len(ev) < 50:
            break
        ultimo = ev[-1].get("id")
    return salida


def programaciones_nuki(cfg, fresco=False):
    """Programaciones hechas en la app de Nuki, deducidas del registro (se guardan 1 hora en memoria)."""
    n = cfg.get("nuki") or {}
    if not (n.get("token") and n.get("smartlock_id")):
        return []
    clave = (n["token"], str(n["smartlock_id"]))
    m, l = _CACHE_HORARIO.get(clave, (0, None))
    if l is not None and not fresco and time.time() - m < 3600:
        return l
    nuki = nk.Nuki(n["token"])
    ahora = datetime.now().astimezone()
    desde = ahora - timedelta(days=DIAS_REGISTRO)
    nombres = {1: "desbloquear", 2: "bloquear", 3: "abrir", 4: "lockngo"}
    salida = []
    for num, accion in nombres.items():
        momentos = []
        for e in _registro_paginado(nuki, n["smartlock_id"], num, desde):
            if e.get("trigger") == 3 and not e.get("authId") and e.get("state", 0) == 0:
                try:
                    momentos.append(datetime.fromisoformat(str(e["date"]).replace("Z", "+00:00")).astimezone())
                except (KeyError, ValueError):
                    pass
        # agrupar por hora del día (±3 min: la cerradura a veces lo hace un minuto tarde)
        grupos = []
        for t in sorted(momentos, key=lambda t: t.hour * 60 + t.minute):
            mm = t.hour * 60 + t.minute
            if grupos and mm - grupos[-1]["base"] <= 3:
                grupos[-1]["veces"].append(t)
            else:
                grupos.append({"base": mm, "veces": [t]})
        for g in grupos:
            veces = g["veces"]
            if len({t.date() for t in veces}) < 3:
                continue  # algo puntual, no una programación
            horas = {}
            for t in veces:
                horas[f"{t:%H:%M}"] = horas.get(f"{t:%H:%M}", 0) + 1
            ultimo = max(veces)
            salida.append({
                "accion": accion, "hora": max(horas, key=horas.get),
                "dias": sorted({t.weekday() for t in veces}), "veces": len({t.date() for t in veces}),
                "ultimo": ultimo.isoformat(timespec="minutes"),
                "reciente": ahora - ultimo < timedelta(days=8),
            })
    salida.sort(key=lambda p: p["hora"])
    for p in salida:
        p["texto"] = f"{ACCIONES_PROG.get(p['accion'], (0, p['accion']))[1]} · {nk.texto_dias(p['dias'])} a las {p['hora']}"
    _CACHE_HORARIO[clave] = (time.time(), salida)
    return salida
