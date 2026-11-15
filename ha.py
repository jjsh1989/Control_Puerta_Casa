"""Pasarela con Home Assistant por MQTT (con descubrimiento automático).

Publica los códigos del teclado y su renovación para que Home Assistant cree solo sus entidades:

  Por cada casa («Códigos · <casa>»):
    - interruptor «Renovación semanal», selector del día, hora y días de validez
    - sensores «Próxima renovación» y «Última renovación»
    - botón «Renovar todos ahora»
    - evento «Códigos» (renovado, error, invitado creado o caducado) para automatizaciones
  Por cada persona («<nombre> · <casa>»):
    - «Código activo» (sí/no) y «Caduca» (fecha)
    - interruptor «Renovación automática» (no en los códigos fijos)
    - botones «Renovar código» e «Invitado 24 h» (se envían como desde el panel)

Los códigos en sí nunca se publican. Ajustes en sistema.json («mqtt») y la contraseña en la bóveda
(«mqtt_password», cuenta _sistema).
"""
import json
import re
import threading
import time
from datetime import datetime, timedelta

import cuentas
import nuki_codigos as nk

CLAVE_BOVEDA = "mqtt_password"
BASE = "puerta"
DISPONIBLE = f"{BASE}/estado"
DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
EVENTOS = {
    "Código creado": "codigo_renovado", "Código fijo creado": "codigo_renovado",
    "Error en la renovación": "error_renovacion", "Renovación incompleta": "error_renovacion",
    "Código de invitado creado": "invitado_creado",
    "Código de invitado caducado y retirado": "invitado_caducado",
}
ORIGEN = "Home Assistant"
HORAS = [f"{m // 60:02d}:{m % 60:02d}" for m in range(0, 24 * 60, 15)]   # cada 15 minutos
HORAS_FIN = HORAS[1:] + ["23:59"]
TIPOS = {"semanal": "Semanal", "manual": "Temporal", "fijo": "Fijo"}
CANALES = {"whatsapp": "WhatsApp", "telegram": "Telegram", "ambos": "WhatsApp y Telegram"}
FRANJA_INICIAL = {"activa": True, "dias": [0, 1, 2, 3, 4], "desde": "09:00", "hasta": "14:00"}


def _con_actual(opciones, valor):
    """Lista de opciones que incluye el valor actual aunque no sea múltiplo de 15 minutos."""
    return opciones if valor in opciones else sorted(opciones + [valor])


def _leer_dias(texto):
    """«L M X J V» (o «lunes a viernes», «todos», «fines de semana») -> [0..6]."""
    t = texto.strip().lower()
    atajos = {"todos": range(7), "todos los dias": range(7), "todos los días": range(7),
              "laborables": range(5), "lunes a viernes": range(5), "de lunes a viernes": range(5),
              "fines de semana": [5, 6], "sabados y domingos": [5, 6], "sábados y domingos": [5, 6]}
    if t in atajos:
        return list(atajos[t])
    letras = re.findall(r"[lmxjvsd]", t)
    if not letras or re.sub(r"[lmxjvsd,\s]", "", t):
        raise RuntimeError("Días no válidos: usa letras L M X J V S D separadas por espacios.")
    return sorted({"lmxjvsd".index(x) for x in letras})


# ---------------------------------------------------------------- ajustes

def ajustes():
    a = cuentas.leer_sistema().get("mqtt") or {}
    return {"activo": bool(a.get("activo")), "servidor": a.get("servidor", ""), "puerto": int(a.get("puerto") or 1883),
            "usuario": a.get("usuario", ""), "prefijo": a.get("prefijo") or "homeassistant",
            "password_puesta": bool(cuentas.boveda_leer(CLAVE_BOVEDA, cuentas.CUENTA_SISTEMA))}


def poner_ajustes(activo=None, servidor=None, puerto=None, usuario=None, prefijo=None, password=None):
    s = cuentas.leer_sistema()
    a = s.setdefault("mqtt", {})
    if servidor is not None:
        servidor = servidor.strip()
        if servidor and not re.fullmatch(r"[\w.-]+", servidor):
            raise RuntimeError("El servidor MQTT debe ser un nombre o una IP, sin «mqtt://» ni barras.")
        a["servidor"] = servidor
    if puerto is not None:
        puerto = int(puerto)
        if not 1 <= puerto <= 65535:
            raise RuntimeError("Puerto no válido.")
        a["puerto"] = puerto
    if usuario is not None:
        a["usuario"] = usuario.strip()[:64]
    if prefijo is not None:
        prefijo = prefijo.strip() or "homeassistant"
        if not re.fullmatch(r"[\w-]+", prefijo):
            raise RuntimeError("Prefijo de descubrimiento no válido.")
        a["prefijo"] = prefijo
    if activo is not None:
        if activo and not a.get("servidor"):
            raise RuntimeError("Escribe primero el servidor MQTT.")
        a["activo"] = bool(activo)
    cuentas.guardar_sistema(s)
    if password is not None:
        if password:
            cuentas.boveda_guardar(CLAVE_BOVEDA, password, cuentas.CUENTA_SISTEMA)
        else:
            cuentas.boveda_borrar(CLAVE_BOVEDA, cuentas.CUENTA_SISTEMA)
    PASARELA.despertar.set()


def estado():
    return {**ajustes(), "conectado": PASARELA.conectado, "error": PASARELA.error}


# ---------------------------------------------------------------- datos que se publican

def _proxima(rot, ahora):
    p = (ahora - timedelta(days=(ahora.weekday() - int(rot.get("dia", 0))) % 7)).replace(
        hour=int(rot.get("hora", 9)), minute=int(rot.get("minuto", 0)), second=0, microsecond=0)
    return p if p > ahora else p + timedelta(days=7)


def _datos_casa(cuenta):
    """Estado de una casa sin llamar a Nuki (solo lo que el panel guarda): rápido y sin gastar API."""
    with cuentas.en_cuenta(cuenta, ORIGEN):
        cfg = nk.cargar_config()
        local = nk.cargar_json(nk.ruta_estado(), {})
        invitados = nk.invitados_activos()
    rot = cfg.get("rotacion", {})
    ahora = datetime.now().astimezone()
    activa = rot.get("activa", True)
    casa = {"rotacion": "ON" if activa else "OFF", "dia": DIAS[int(rot.get("dia", 0)) % 7],
            "hora": f"{int(rot.get('hora', 9)):02d}:{int(rot.get('minuto', 0)):02d}",
            "dias": int(cfg.get("dias_validez", 7)), "vigilar": int(cfg.get("vigilar_bajas_dias", 30)),
            "proxima": _proxima(rot, ahora).isoformat() if activa else None,
            "ultima": local.get("_ultima_rotacion")}
    personas = {}
    for d in cfg["destinatarios"]:
        info = local.get(d["id"], {})
        caduca, fijo = info.get("caduca"), bool(d.get("fijo"))
        f = d.get("franja") if (d.get("franja") or {}).get("activa") else None
        guardada = f or d.get("franja_anterior") or FRANJA_INICIAL
        activo = bool(info.get("codigo")) and (fijo or bool(caduca and datetime.fromisoformat(caduca) > ahora))
        personas[d["id"]] = {
            "nombre": d["nombre"], "fijo": fijo, "activo": activo,
            "caduca": caduca if activo and not fijo else None,
            "automatico": "ON" if d.get("automatico", True) else "OFF",
            "tipo": TIPOS["fijo" if fijo else ("semanal" if d.get("automatico", True) else "manual")],
            "canal": CANALES.get(d.get("canal_envio", "whatsapp"), "WhatsApp"),
            "horario": "ON" if f else "OFF",
            "h_dias": " ".join(nk.DIAS_CORTOS[x] for x in guardada["dias"]),
            "h_desde": guardada["desde"], "h_hasta": guardada["hasta"],
            "franja": nk.texto_franja(d.get("franja")) or "siempre",
            "seccion": d.get("seccion", ""),
            "invitados": sum(1 for i in invitados if i["inquilino"] == d["id"]),
        }
    return casa, personas


# ---------------------------------------------------------------- pasarela

class Pasarela(threading.Thread):
    REFRESCAR = 60

    def __init__(self):
        super().__init__(daemon=True)
        self.cliente = None
        self.config_usada = None
        self.conectado = False
        self.error = ""
        self.despertar = threading.Event()
        self.sucio = threading.Event()
        self.publicados = set()     # temas de descubrimiento publicados por nosotros
        self.ultimo_estado = {}     # tema -> carga (para no repetir)
        self.firma = None           # qué casas/personas había al publicar el descubrimiento
        self.ocupado = {}           # cuenta -> Lock (una orden a la vez por casa)
        self.antiguos = set()       # descubrimientos que había en el servidor al conectar

    # ---------- bucle
    def run(self):
        nk.OYENTES_REGISTRO.append(self._registro)
        time.sleep(15)
        ultimo = 0
        while True:
            try:
                self._asegurar_conexion()
                if self.conectado and (self.sucio.is_set() or time.time() - ultimo >= self.REFRESCAR):
                    self.sucio.clear()
                    ultimo = time.time()
                    self._publicar_todo()
            except Exception as e:
                self.error = str(e)[:200]
                nk.log(f"Home Assistant: {e}")
            self.despertar.wait(5)
            self.despertar.clear()

    def _asegurar_conexion(self):
        a = ajustes()
        clave = cuentas.boveda_leer(CLAVE_BOVEDA, cuentas.CUENTA_SISTEMA) or ""
        usada = (a["activo"], a["servidor"], a["puerto"], a["usuario"], a["prefijo"], clave)
        if usada == self.config_usada:
            return
        self._cerrar()
        self.config_usada = usada
        if not (a["activo"] and a["servidor"]):
            self.error = ""
            return
        import paho.mqtt.client as mqtt
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="puerta-panel")
        if a["usuario"]:
            c.username_pw_set(a["usuario"], clave)
        c.will_set(DISPONIBLE, "offline", retain=True)
        c.on_connect = self._al_conectar
        c.on_disconnect = self._al_desconectar
        c.on_message = self._al_mensaje
        c.reconnect_delay_set(5, 120)
        self.cliente, self.prefijo = c, a["prefijo"]
        try:
            c.connect_async(a["servidor"], a["puerto"], keepalive=60)
            c.loop_start()
        except Exception as e:
            self.error = f"No se pudo conectar con {a['servidor']}:{a['puerto']} ({e})"

    def _cerrar(self):
        c, self.cliente = self.cliente, None
        if c:
            try:
                c.publish(DISPONIBLE, "offline", retain=True)
                c.disconnect()
                c.loop_stop()
            except Exception:
                pass
        self.conectado = False
        self.publicados.clear()
        self.ultimo_estado.clear()
        self.firma = None

    def _al_conectar(self, c, _datos, _banderas, codigo, _props):
        if codigo.is_failure:
            self.conectado = False
            self.error = {"Not authorized": "Usuario o contraseña de MQTT incorrectos.",
                          "Bad user name or password": "Usuario o contraseña de MQTT incorrectos."
                          }.get(str(codigo), f"El servidor MQTT rechazó la conexión ({codigo}).")
            return
        self.conectado, self.error = True, ""
        nk.log("Home Assistant: conectado al servidor MQTT")
        self.publicados.clear()
        self.ultimo_estado.clear()
        self.firma = None
        self.antiguos = set()
        c.subscribe([(f"{BASE}/+/cmd/+", 1), (f"{BASE}/+/p/+/cmd/+", 1), (f"{self.prefijo}/status", 1),
                     (f"{self.prefijo}/+/{BASE}/+/config", 0)])
        c.publish(DISPONIBLE, "online", retain=True)
        # se espera un momento a recibir los descubrimientos antiguos (retenidos) antes de publicar
        threading.Timer(3, self._marcar_sucio).start()

    def _al_desconectar(self, _c, _datos, _banderas, codigo, _props):
        if self.conectado:
            nk.log(f"Home Assistant: desconectado del servidor MQTT ({codigo})")
        self.conectado = False
        if not self.error:
            self.error = "Sin conexión con el servidor MQTT; se reintenta solo."

    def _marcar_sucio(self):
        self.sucio.set()
        self.despertar.set()

    def _registro(self, entrada):
        """Lo llama nk.registrar: cualquier cambio de códigos refresca el estado y algunos son eventos."""
        if not self.conectado or entrada.get("simulado"):
            return
        cuenta = cuentas.cuenta_actual()
        tipo = EVENTOS.get(entrada.get("accion"))
        if tipo:
            self._publicar(f"{BASE}/{cuenta}/evento",
                           json.dumps({"event_type": tipo, "persona": entrada.get("usuario", ""),
                                       "accion": entrada.get("accion")}, ensure_ascii=False), retain=False)
        self._marcar_sucio()

    # ---------- publicar
    def _publicar(self, tema, carga, retain=True):
        c = self.cliente
        if c and self.conectado:
            c.publish(tema, carga, qos=1, retain=retain)

    def _estado(self, tema, datos):
        carga = json.dumps(datos, ensure_ascii=False)
        if self.ultimo_estado.get(tema) != carga:
            self.ultimo_estado[tema] = carga
            self._publicar(tema, carga)

    def _publicar_todo(self):
        casas = {c["id"]: c["nombre"] for c in cuentas.listar_cuentas()}
        todo = {cuenta: _datos_casa(cuenta) for cuenta in casas}
        firma = json.dumps({k: [casas[k], todo[k][0]["hora"],
                                {p: (v["nombre"], v["h_desde"], v["h_hasta"]) for p, v in todo[k][1].items()}] for k in todo},
                           sort_keys=True, ensure_ascii=False)
        if firma != self.firma:
            self._descubrimiento(casas, todo)
            self.firma = firma
        for cuenta, (casa, personas) in todo.items():
            self._estado(f"{BASE}/{cuenta}/casa", casa)
            for pid, p in personas.items():
                self._estado(f"{BASE}/{cuenta}/p/{pid}/estado", p)

    def _descubrimiento(self, casas, todo):
        nuevos = {}
        disp = {"availability_topic": DISPONIBLE}
        for cuenta, nombre_casa in casas.items():
            casa, personas = todo[cuenta]
            dispositivo = {"identifiers": [f"{BASE}_{cuenta}"], "name": f"Códigos · {nombre_casa}",
                           "manufacturer": "Puerta", "model": "Códigos del teclado"}
            t = f"{BASE}/{cuenta}/casa"
            cmd = f"{BASE}/{cuenta}/cmd"

            def ent(tipo, obj, nombre, dev=dispositivo, **extra):
                uid = f"{BASE}_{cuenta}_{obj}"
                nuevos[f"{self.prefijo}/{tipo}/{BASE}/{uid}/config"] = {
                    "name": nombre, "unique_id": uid, "default_entity_id": f"{tipo}.{uid.replace('-', '_')}",
                    "device": dev, **disp, **extra}
            ent("switch", "rotacion", "Renovación semanal", state_topic=t, value_template="{{ value_json.rotacion }}",
                command_topic=f"{cmd}/rotacion", icon="mdi:calendar-sync")
            ent("select", "dia", "Día de renovación", state_topic=t, value_template="{{ value_json.dia }}",
                command_topic=f"{cmd}/dia", options=DIAS, icon="mdi:calendar-week")
            ent("select", "hora", "Hora de renovación", state_topic=t, value_template="{{ value_json.hora }}",
                command_topic=f"{cmd}/hora", options=_con_actual(HORAS, casa["hora"]), icon="mdi:clock-outline")
            ent("number", "dias", "Días que dura cada código", state_topic=t, value_template="{{ value_json.dias }}",
                command_topic=f"{cmd}/dias", min=1, max=60, step=1, mode="box", unit_of_measurement="d",
                icon="mdi:calendar-range")
            ent("number", "vigilar", "Vigilar códigos desactivados", state_topic=t,
                value_template="{{ value_json.vigilar }}", command_topic=f"{cmd}/vigilar", min=0, max=365, step=1,
                mode="box", unit_of_measurement="d", icon="mdi:shield-search", entity_category="config")
            ent("sensor", "proxima", "Próxima renovación", state_topic=t, device_class="timestamp",
                value_template="{{ value_json.proxima }}")
            ent("sensor", "ultima", "Última renovación", state_topic=t, device_class="timestamp",
                value_template="{{ value_json.ultima }}")
            ent("button", "renovar_todos", "Renovar todos ahora", command_topic=f"{cmd}/renovar_todos",
                payload_press="PRESS", icon="mdi:key-change")
            ent("event", "eventos", "Códigos", state_topic=f"{BASE}/{cuenta}/evento",
                event_types=sorted(set(EVENTOS.values())), icon="mdi:key-chain")
            for pid, p in personas.items():
                dev = {"identifiers": [f"{BASE}_{cuenta}_{pid}"], "name": f"{p['nombre']} · {nombre_casa}",
                       "manufacturer": "Puerta", "model": "Código fijo" if p["fijo"] else "Código que se renueva",
                       "via_device": f"{BASE}_{cuenta}"}
                tp, cp = f"{BASE}/{cuenta}/p/{pid}/estado", f"{BASE}/{cuenta}/p/{pid}/cmd"
                ent("binary_sensor", f"{pid}_activo", "Código activo", dev, state_topic=tp,
                    value_template="{{ 'ON' if value_json.activo else 'OFF' }}", icon="mdi:dialpad",
                    json_attributes_topic=tp,
                    json_attributes_template="{{ {'fijo': value_json.fijo, 'franja': value_json.franja, "
                                             "'seccion': value_json.seccion, 'invitados': value_json.invitados} | tojson }}")
                ent("sensor", f"{pid}_caduca", "Caduca", dev, state_topic=tp, device_class="timestamp",
                    value_template="{{ value_json.caduca }}")
                ent("select", f"{pid}_tipo", "Tipo de código", dev, state_topic=tp,
                    value_template="{{ value_json.tipo }}", command_topic=f"{cp}/tipo",
                    options=list(TIPOS.values()), icon="mdi:autorenew")
                ent("select", f"{pid}_canal", "Enviar el código por", dev, state_topic=tp,
                    value_template="{{ value_json.canal }}", command_topic=f"{cp}/canal",
                    options=list(CANALES.values()), icon="mdi:send", entity_category="config")
                ent("switch", f"{pid}_horario", "Solo en un horario", dev, state_topic=tp,
                    value_template="{{ value_json.horario }}", command_topic=f"{cp}/horario", icon="mdi:calendar-clock")
                ent("text", f"{pid}_h_dias", "Días del horario", dev, state_topic=tp,
                    value_template="{{ value_json.h_dias }}", command_topic=f"{cp}/h_dias", min=1, max=40,
                    icon="mdi:calendar-week")
                ent("select", f"{pid}_h_desde", "Horario desde", dev, state_topic=tp,
                    value_template="{{ value_json.h_desde }}", command_topic=f"{cp}/h_desde",
                    options=_con_actual(HORAS, p["h_desde"]), icon="mdi:clock-start")
                ent("select", f"{pid}_h_hasta", "Horario hasta", dev, state_topic=tp,
                    value_template="{{ value_json.h_hasta }}", command_topic=f"{cp}/h_hasta",
                    options=_con_actual(HORAS_FIN, p["h_hasta"]), icon="mdi:clock-end")
                ent("button", f"{pid}_renovar", "Renovar código", dev, command_topic=f"{cp}/renovar",
                    payload_press="PRESS", icon="mdi:key-change")
                ent("button", f"{pid}_invitado", "Invitado 24 h", dev, command_topic=f"{cp}/invitado",
                    payload_press="PRESS", icon="mdi:account-clock")
        for tema, conf in nuevos.items():
            self._publicar(tema, json.dumps(conf, ensure_ascii=False))
        # lo que ya no existe (personas o casas borradas, o de antes de reiniciar) se quita de Home Assistant
        for tema in (self.publicados | self.antiguos) - set(nuevos):
            self._publicar(tema, "")
        self.publicados, self.antiguos = set(nuevos), set()

    # ---------- órdenes desde Home Assistant
    def _al_mensaje(self, _c, _datos, msg):
        tema, carga = msg.topic, msg.payload.decode(errors="replace").strip()
        if tema.endswith("/config"):
            if carga and msg.retain and tema not in self.publicados:
                self.antiguos.add(tema)
            return
        if tema == f"{self.prefijo}/status":
            if carga == "online":  # Home Assistant se ha reiniciado: volver a anunciarse
                self.firma = None
                self.ultimo_estado.clear()
                self._marcar_sucio()
            return
        if msg.retain:
            return  # una orden retenida no se ejecuta (podría ser vieja)
        m = re.fullmatch(rf"{BASE}/([\w-]+)/(?:p/([\w-]+)/)?cmd/(\w+)", tema)
        if not m or not cuentas.existe_cuenta(m.group(1)):
            return
        threading.Thread(target=self._orden, args=(m.group(1), m.group(2), m.group(3), carga), daemon=True).start()

    def _orden(self, cuenta, pid, que, carga):
        cerrojo = self.ocupado.setdefault(cuenta, threading.Lock())
        if not cerrojo.acquire(timeout=1):
            nk.log(f"Home Assistant: orden «{que}» ignorada, ya hay otra en marcha en esa casa")
            return
        try:
            with cuentas.en_cuenta(cuenta, ORIGEN):
                cuentas._ctx.origen = "homeassistant"
                try:
                    self._ejecutar(pid, que, carga)
                finally:
                    cuentas._ctx.origen = None
        except Exception as e:
            nk.log(f"Home Assistant: la orden «{que}» falló: {e}")
            with cuentas.en_cuenta(cuenta, ORIGEN):
                nk.registrar("Orden de Home Assistant fallida", "", f"{que}: {e}", ok=False)
        finally:
            cerrojo.release()
            self._marcar_sucio()

    @staticmethod
    def _ejecutar(pid, que, carga):
        cfg = nk.cargar_config()
        antes = json.dumps(cfg, sort_keys=True, default=str)  # para no apuntar nada si no cambia
        if pid is None:
            rot = cfg.setdefault("rotacion", {})
            if que == "rotacion":
                rot["activa"] = carga == "ON"
                texto = "activada" if rot["activa"] else "desactivada"
            elif que == "dia":
                if carga not in DIAS:
                    raise RuntimeError("Día no válido")
                rot["dia"] = DIAS.index(carga)
                texto = f"día: {carga}"
            elif que == "vigilar":
                dias = int(float(carga))
                if not 0 <= dias <= 365:
                    raise RuntimeError("Días de vigilancia entre 0 y 365")
                cfg["vigilar_bajas_dias"] = dias
                texto = f"vigilar desactivados: {dias} días"
            elif que == "hora":
                if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", carga):
                    raise RuntimeError("Hora no válida")
                rot["hora"], rot["minuto"] = int(carga[:2]), int(carga[3:])
                texto = f"hora: {carga}"
            elif que == "dias":
                dias = int(float(carga))
                if not 1 <= dias <= 60:
                    raise RuntimeError("Días de validez entre 1 y 60")
                cfg["dias_validez"] = dias
                texto = f"validez: {dias} días"
            elif que == "renovar_todos":
                if not (cfg["nuki"].get("token") and cfg["nuki"].get("smartlock_id")):
                    raise RuntimeError("Falta el token o la cerradura de Nuki en esta casa.")
                nk.registrar("Renovación de todos pedida", "", "Desde Home Assistant")
                nk.rotar_cuenta(cfg)
                return
            else:
                return
            if json.dumps(cfg, sort_keys=True, default=str) == antes:
                return
            nk.guardar_config(cfg)
            nk.registrar("Renovación semanal cambiada", "", f"Desde Home Assistant · {texto}")
            return
        d = next((x for x in cfg["destinatarios"] if x["id"] == pid), None)
        if not d:
            raise RuntimeError("Esa persona ya no existe.")
        if que == "tipo":
            tipo = next((k for k, v in TIPOS.items() if v == carga), None)
            if not tipo:
                raise RuntimeError("Tipo no válido")
            d["automatico"], d["fijo"] = tipo == "semanal", tipo == "fijo"
            if json.dumps(cfg, sort_keys=True, default=str) == antes:
                return
            nk.guardar_config(cfg)
            nk.registrar("Persona modificada", d["nombre"], f"Desde Home Assistant · tipo: {carga}")
            return
        if que == "canal":
            canal = next((k for k, v in CANALES.items() if v == carga), None)
            if canal not in nk.CANALES_ENVIO:
                raise RuntimeError("Canal no válido")
            d["canal_envio"] = canal
            if json.dumps(cfg, sort_keys=True, default=str) == antes:
                return
            nk.guardar_config(cfg)
            nk.registrar("Persona modificada", d["nombre"], f"Desde Home Assistant · envío por {carga}")
            return
        if que in ("horario", "h_dias", "h_desde", "h_hasta"):
            actual = d.get("franja") if (d.get("franja") or {}).get("activa") else None
            f = dict(actual or d.get("franja_anterior") or FRANJA_INICIAL)
            if que == "horario":
                activa = carga == "ON"
            else:
                activa = bool(actual)
                if que == "h_dias":
                    f["dias"] = _leer_dias(carga)
                elif carga not in (HORAS + ["23:59"]) and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", carga):
                    raise RuntimeError("Hora no válida")
                else:
                    f["desde" if que == "h_desde" else "hasta"] = carga
            f["activa"] = True
            f = nk.validar_franja(f)  # comprueba días y que «hasta» sea después de «desde»
            d["franja_anterior"] = f
            nueva = f if activa else None
            if nueva != actual:
                if nueva:
                    d["franja"] = nueva
                else:
                    d.pop("franja", None)
                nk.aplicar_franja(cfg, d)
                nk.registrar("Horario del código", d["nombre"],
                             f"Desde Home Assistant · {nk.texto_franja(nueva) or 'a cualquier hora'}")
            nk.guardar_config(cfg)
            return
        if que not in ("renovar", "invitado"):
            return
        if not (cfg["nuki"].get("token") and cfg["nuki"].get("smartlock_id")):
            raise RuntimeError("Falta el token o la cerradura de Nuki en esta casa.")
        nuki = nk.Nuki(cfg["nuki"]["token"])
        if que == "renovar":
            nk.renovar_codigo(cfg, nuki, d, abrir=False)
        else:
            nk.crear_invitado(cfg, nuki, d, 24, "Desde Home Assistant", None, abrir=False)


PASARELA = Pasarela()
