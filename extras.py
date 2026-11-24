"""Avisos extra por Telegram, revisados cada minuto por el hilo Extras:

- Recordatorio de noche si la puerta no está cerrada con llave (con botón «Cerrar ahora»).
- Resumen diario (entradas, avisos, cámaras y batería).
- Avisos de cámara sin sonido en una franja horaria o para ciertas cámaras (lo usa camara.py).
- Salud del servidor para los administradores: disco casi lleno o cámaras desconectadas.
- La copia de seguridad semanal (copias.py).

Ajustes de cada casa en cfg["extras"]; lo ya enviado hoy, en datos/sistema (extras_hecho).
"""
import shutil
import threading
import time
from datetime import datetime, timedelta

import cuentas
import nuki_codigos as nk

POR_DEFECTO = {"llave": {"activo": False, "hora": "23:30"},
               "resumen": {"activo": False, "hora": "22:00"},
               "silencio": {"activo": False, "desde": "23:00", "hasta": "07:00", "camaras": []}}
MARGEN = timedelta(hours=2)  # si el servidor estuvo apagado a la hora, se envía si no han pasado 2 h


def ajustes(cfg):
    e = cfg.get("extras") or {}
    return {k: {**v, **(e.get(k) or {})} for k, v in POR_DEFECTO.items()}


def validar(datos, camaras_validas):
    salida = {}
    for k, base in POR_DEFECTO.items():
        d = datos.get(k) or {}
        r = {"activo": bool(d.get("activo"))}
        for campo in ("hora", "desde", "hasta"):
            if campo in base:
                v = str(d.get(campo) or base[campo])
                datetime.strptime(v, "%H:%M")
                r[campo] = v
        if k == "silencio":
            r["camaras"] = [sn for sn in (d.get("camaras") or []) if sn in camaras_validas]
        salida[k] = r
    return salida


def en_franja(desde, hasta, ahora=None):
    ahora = (ahora or datetime.now()).strftime("%H:%M")
    return desde <= ahora < hasta if desde <= hasta else (ahora >= desde or ahora < hasta)


def sin_sonido(cfg, sn):
    """¿Este aviso de cámara debe llegar sin sonido?"""
    s = ajustes(cfg)["silencio"]
    return bool(s["activo"] and (not s["camaras"] or sn in s["camaras"]) and en_franja(s["desde"], s["hasta"]))


# ---------------------------------------------------------------- lo ya enviado hoy

def _hecho(cuenta, tipo):
    return ((cuentas.leer_sistema().get("extras_hecho") or {}).get(cuenta) or {}).get(tipo)


def _marcar(cuenta, tipo, valor):
    s = cuentas.leer_sistema()
    s.setdefault("extras_hecho", {}).setdefault(cuenta, {})[tipo] = valor
    cuentas.guardar_sistema(s)


def _toca(cuenta, tipo, hora, ahora):
    h, m = map(int, hora.split(":"))
    prevista = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
    if prevista > ahora:
        prevista -= timedelta(days=1)
    if ahora - prevista > MARGEN or _hecho(cuenta, tipo) == prevista.date().isoformat():
        return None
    return prevista


def _a_telegram(cfg, texto, botones=None):
    import telegram
    n = 0
    for chat in telegram.chats_avisos(cfg):
        try:
            telegram.enviar(chat, texto, botones)
            n += 1
        except Exception:
            pass
    return n


# ---------------------------------------------------------------- recordatorio de la llave

def revisar_llave(cuenta, cfg, ahora):
    a = ajustes(cfg)["llave"]
    if not a["activo"]:
        return
    prevista = _toca(cuenta, "llave", a["hora"], ahora)
    if not prevista:
        return
    _marcar(cuenta, "llave", prevista.date().isoformat())
    n = cfg.get("nuki") or {}
    if not (n.get("token") and n.get("smartlock_id")):
        return
    import alertas
    st = (nk.Nuki(n["token"]).estado(n["smartlock_id"], max_edad=60).get("state") or {})
    if st.get("state") == 1:
        return  # ya está cerrada con llave
    abierta = st.get("doorState") == 3
    texto = (f"🔓 {cuentas_nombre(cuenta)}: son las {ahora:%H:%M} y la cerradura está "
             f"«{alertas.ESTADOS_CERROJO.get(st.get('state'), 'desconocido')}».")
    if abierta:
        texto += "\n⚠️ Además, la puerta está ABIERTA: ciérrala antes de echar la llave."
    botones = None if abierta else [[{"text": "🔒 Cerrar ahora", "callback_data": f"c|{cuenta}"},
                                      {"text": "🚪 Ver puerta", "callback_data": f"p|{cuenta}"}]]
    if _a_telegram(cfg, texto, botones):
        nk.registrar("Recordatorio: puerta sin llave", "automático", alertas.ESTADOS_CERROJO.get(st.get("state"), ""))


def cuentas_nombre(cuenta):
    return next((c["nombre"] for c in cuentas.listar_cuentas() if c["id"] == cuenta), cuenta)


# ---------------------------------------------------------------- resumen diario

def revisar_resumen(cuenta, cfg, ahora):
    a = ajustes(cfg)["resumen"]
    if not a["activo"]:
        return
    prevista = _toca(cuenta, "resumen", a["hora"], ahora)
    if not prevista:
        return
    _marcar(cuenta, "resumen", prevista.date().isoformat())
    _a_telegram(cfg, resumen(cuenta, cfg, prevista.date()))


def resumen(cuenta, cfg, dia):
    import json
    lineas = [f"📋 Resumen de hoy · {cuentas_nombre(cuenta)} · {dia:%d/%m}"]
    n = cfg.get("nuki") or {}
    if n.get("token") and n.get("smartlock_id"):
        try:
            import alertas
            nuki = nk.Nuki(n["token"])
            eventos = [e for e in nuki.registro(n["smartlock_id"], 200)
                       if alertas._fecha(e).astimezone().date() == dia]
            entradas = [t for t, e in zip(nk.traducir_registro(cfg, eventos), eventos) if e.get("action") in (1, 3)]
            quienes = {}
            for t in entradas:
                q = (t.get("quien") or "?").replace(" ()", "").strip() or "?"
                quienes[q] = quienes.get(q, 0) + 1
            lineas.append(f"🚪 Aperturas: {len(entradas)}" + (" · " + ", ".join(f"{q} ({c})" for q, c in
                                                                         sorted(quienes.items(), key=lambda x: -x[1])[:5])
                                                           if quienes else ""))
            st = (nuki.estado(n["smartlock_id"], max_edad=60).get("state") or {})
            lineas.append(f"🔒 Ahora: {alertas.ESTADOS_CERROJO.get(st.get('state'), 'desconocido')}"
                          + (f" · batería {st['batteryCharge']} %" if st.get("batteryCharge") is not None else ""))
        except Exception as e:
            lineas.append(f"🚪 No se pudo leer la cerradura: {e}")
    try:
        avisos = 0
        ruta = cuentas.carpeta_cuenta(cuenta) / "gestion.jsonl"
        if ruta.exists():
            for l in ruta.read_text(encoding="utf-8").splitlines()[-2000:]:
                try:
                    h = json.loads(l)
                except ValueError:
                    continue
                if h.get("accion") == "Aviso enviado" and str(h.get("fecha", "")).startswith(dia.isoformat()):
                    avisos += 1
        lineas.append(f"🔔 Avisos enviados: {avisos}")
    except Exception:
        pass
    try:
        import camara
        evs = [e for e in camara.leer_eventos(cuenta, 500) if str(e.get("hora", "")).startswith(dia.isoformat())]
        if evs:
            por = {}
            for e in evs:
                por[e.get("camara") or "?"] = por.get(e.get("camara") or "?", 0) + 1
            lineas.append(f"📷 Detecciones de cámara: {len(evs)} · " + ", ".join(f"{c} ({k})" for c, k in por.items()))
    except Exception:
        pass
    return "\n".join(lineas)


# ---------------------------------------------------------------- salud del servidor

class _Salud:
    def __init__(self):
        self.ultimo = {}       # clave -> epoch del último aviso
        self.eufy_caido = None

    def _avisar(self, clave, texto, cada=24 * 3600):
        if time.time() - self.ultimo.get(clave, 0) < cada:
            return
        self.ultimo[clave] = time.time()
        import copias
        copias.avisar_admins(texto)
        with cuentas.en_cuenta(cuentas.CUENTA_PRINCIPAL, "automático"):
            nk.registrar("Aviso del servidor", "automático", texto, ok=False)

    def revisar(self):
        d = shutil.disk_usage(cuentas.DATOS)
        usado = d.used / d.total * 100
        if usado >= 90:
            self._avisar("disco", f"💽 El disco del servidor está al {usado:.0f} % ({d.free / 2**30:.1f} GB libres). "
                                  f"Borra clips viejos (Ajustes → Cámaras → Espacio) o amplía el disco.")
        try:
            import camara
            hay_eufy = camara.credenciales_puestas()
            caido = hay_eufy and not (camara.CLIENTE.conectado and camara.CLIENTE.driver)
        except Exception:
            caido = False
        if caido:
            self.eufy_caido = self.eufy_caido or time.time()
            if time.time() - self.eufy_caido > 15 * 60:
                self._avisar("eufy", "📷 Las cámaras llevan más de 15 minutos sin conexión con el panel "
                                     "(puente de Eufy caído o sin internet). Revísalo en Ajustes → Cámaras.", cada=6 * 3600)
        else:
            self.eufy_caido = None


class Extras(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.salud = _Salud()
        self._salud_cada = 0

    def run(self):
        time.sleep(40)
        while True:
            ahora = datetime.now().astimezone()
            for c in cuentas.listar_cuentas():
                try:
                    with cuentas.en_cuenta(c["id"], "automático"):
                        cfg = nk.cargar_config()
                        revisar_llave(c["id"], cfg, ahora)
                        revisar_resumen(c["id"], cfg, ahora)
                except Exception as e:
                    nk.log(f"Extras ({c['id']}): {e}")
            if time.time() - self._salud_cada > 600:
                self._salud_cada = time.time()
                try:
                    self.salud.revisar()
                except Exception as e:
                    nk.log(f"Salud: {e}")
            try:
                import copias
                copias.revisar()
            except Exception as e:
                nk.log(f"Copias: {e}")
            time.sleep(60)
