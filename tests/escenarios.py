"""Escenarios que se ejecutan DENTRO de una instalación de prueba (ver instalacion.py).
Cada uno simula los servicios externos, hace lo que haría el panel y devuelve lo observado;
las comprobaciones están en los test_*.py. Todos los datos son ficticios."""
import itertools
import os

import cuentas
import nuki_codigos as nk

PASSWORD_COPIAS = "contraseña-de-copias-ficticia"


# ---------------------------------------------------------------- Nuki y WhatsApp simulados

class NukiSimulado:
    """Sustituye las llamadas a la API de Nuki por una lista de autorizaciones en memoria."""

    def __init__(self):
        self.auths, self.ids = [], itertools.count(100)
        n = self

        def crear_codigo(self, sid, nombre, codigo, desde, hasta, franja=None):
            n.auths.append({"id": next(n.ids), "name": nombre, "type": nk.TIPO_CODIGO_TECLADO,
                            "code": int(codigo), "enabled": True})

        def desactivar(self, sid, auth, nuevo_nombre):
            for a in n.auths:
                if a["id"] == auth["id"]:
                    a.update(enabled=False, name=nuevo_nombre[:20])

        nk.Nuki.crear_codigo = crear_codigo
        nk.Nuki.desactivar = desactivar
        nk.Nuki.autorizaciones = lambda self, sid: [dict(a) for a in n.auths]
        nk.Nuki.borrar = lambda self, sid, i: n.auths.__setitem__(slice(None), [a for a in n.auths if a["id"] != i])

    def codigo_anterior(self, cfg, persona, codigo):
        self.auths.append({"id": next(self.ids), "name": nk.nombre_auth_de(cfg, persona),
                           "type": nk.TIPO_CODIGO_TECLADO, "code": int(codigo), "enabled": True})
        return self.auths[-1]["id"]

    def activos(self):
        """Códigos que abren la puerta ahora mismo."""
        return sorted(str(a["code"]) for a in self.auths if a["enabled"])


def _config(proveedor):
    cfg = nk.cargar_config()
    cfg["nuki"].update(token="token-ficticio", smartlock_id=1)
    cfg["whatsapp"]["proveedor"] = proveedor
    cfg["destinatarios"] = [
        {"id": "ana", "nombre": "Ana", "telefono": "+34600000001", "automatico": True},
        {"id": "bea", "nombre": "Bea", "telefono": "+34600000002", "automatico": True},
    ]
    return cfg


def _estado_renovacion(persona_id):
    # las versiones anteriores a «66e41df» no apuntaban las renovaciones sin entregar
    rec = getattr(nk, "renovaciones", dict)().get(persona_id)
    return rec and {"estado": rec.get("estado"), "codigo": rec.get("codigo")}


# ---------------------------------------------------------------- renovaciones

def renovacion_envio_manual():
    """Proveedor «enlace»: el código nuevo queda preparado, pero hasta que el gestor confirma que lo ha
    enviado nadie lo tiene. El anterior tiene que seguir abriendo la puerta hasta entonces."""
    nuki = NukiSimulado()
    obs = {}
    with cuentas.en_cuenta(cuentas.CUENTA_PRINCIPAL, "gestor"):
        cfg = _config("enlace")
        ana = cfg["destinatarios"][0]
        nuki.codigo_anterior(cfg, ana, "111111")
        r = nk.renovar_codigo(cfg, nk.Nuki(cfg["nuki"]["token"]), ana, abrir=False)
        obs["nuevo"] = r["codigo"]
        obs["preparado"] = {"estado": r.get("estado"), "con_enlace": bool(r.get("enlace")),
                            "activos": nuki.activos(), "pendiente": _estado_renovacion("ana")}
        try:
            nk.confirmar_renovacion(cfg, nk.Nuki(cfg["nuki"]["token"]), ana)
            obs["confirmado"] = {"activos": nuki.activos(), "pendiente": _estado_renovacion("ana")}
        except Exception as e:
            obs["confirmado"] = {"error": str(e)}
    return obs


def renovacion_falla_un_destinatario():
    """Renovación semanal de dos personas en la que el WhatsApp de una falla: la otra se renueva normal,
    la que falla conserva su código anterior y el reintento le manda el MISMO código nuevo."""
    import waha
    nuki = NukiSimulado()
    enviados = []
    caido = {"+34600000002"}

    def enviar(cfg, telefono, texto):
        if telefono in caido:
            raise RuntimeError("WhatsApp no responde (simulado)")
        enviados.append(telefono)
    waha.enviar = enviar
    obs = {}
    with cuentas.en_cuenta(cuentas.CUENTA_PRINCIPAL, "semanal"):
        cfg = _config("waha")
        ana, bea = cfg["destinatarios"]
        nuki.codigo_anterior(cfg, ana, "111111")
        nuki.codigo_anterior(cfg, bea, "222222")
        res = nk.rotar_cuenta(cfg)
        obs["resultado"] = {k: ("error" if "error" in v else v.get("estado")) for k, v in res.items()}
        obs["tras_fallo"] = {"activos": nuki.activos(), "pendiente_bea": _estado_renovacion("bea"),
                             "pendiente_ana": _estado_renovacion("ana"), "enviados": list(enviados)}
        caido.clear()  # WhatsApp vuelve
        try:
            obs["reintento"] = {k: ("error" if "error" in v else v.get("estado"))
                                for k, v in nk.reintentar_fallidas(cfg).items()}
        except Exception as e:
            obs["reintento"] = {"error": str(e)}
        obs["tras_reintento"] = {"activos": nuki.activos(), "pendiente_bea": _estado_renovacion("bea"),
                                 "enviados": list(enviados)}
    return obs


# ---------------------------------------------------------------- copias de seguridad

def _sin_telegram():
    """Ningún administrador con Telegram: la copia se queda en el servidor y no se manda a nadie."""
    import copias
    copias.chats_admin = lambda: []


def preparar_instalacion_con_datos():
    """Una instalación en uso: una casa, un administrador, un secreto en la bóveda y la contraseña de copias."""
    import copias
    casa = cuentas.crear_cuenta("Casa de prueba")
    cuentas.crear_usuario("admin1", "Administradora", "contraseña-admin-ficticia", "admin", casa)
    cuentas.crear_usuario("gestor1", "Gestor", "contraseña-gestor-ficticia", "gestor", casa)
    cuentas.boveda_guardar("nuki_token", "token-nuki-ficticio", casa)
    copias.poner_clave(PASSWORD_COPIAS)
    return {"casa": casa}


def hacer_copia():
    import copias
    _sin_telegram()
    copias.hacer_copia("prueba")
    lista = copias.listar()
    return {"nombre": lista[0]["nombre"], "ruta": str(copias.CARPETA_COPIAS / lista[0]["nombre"])}


def copia_falla_al_escribir():
    """El disco se llena a mitad de escribir la copia: no debe quedar un archivo a medias con aspecto
    de copia buena, ni estropear la anterior, ni apuntarse como hecha."""
    import copias
    _sin_telegram()
    copias.hacer_copia("prueba")
    antes = {p.name: p.read_bytes() for p in copias.CARPETA_COPIAS.iterdir()}
    ultima_antes = copias.ajustes()["ultima"]
    os.fdopen_real = os.fdopen

    class Llena:
        def __init__(self, f):
            self.f = f

        def write(self, datos):
            self.f.write(datos[: len(datos) // 2])
            raise OSError(28, "No queda espacio en el dispositivo")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.f.close()

    copias.os.fdopen = lambda fd, modo="r", *a, **k: Llena(os.fdopen_real(fd, modo, *a, **k))
    obs = {}
    try:
        copias.hacer_copia("prueba")
        obs["error"] = None
    except Exception as e:
        obs["error"] = str(e)
    copias.os.fdopen = os.fdopen_real
    despues = {p.name: p.read_bytes() for p in copias.CARPETA_COPIAS.iterdir()}
    obs["archivos_antes"] = sorted(antes)
    obs["archivos_despues"] = sorted(despues)
    obs["anteriores_intactas"] = all(despues.get(n) == b for n, b in antes.items())
    obs["ultima_igual"] = copias.ajustes()["ultima"] == ultima_antes
    obs["listadas"] = [c["nombre"] for c in copias.listar()]
    return obs


def restaurar(ruta, password):
    import copias
    r = copias.restaurar(ruta, password, destino=copias.CARPETA)
    return {"casas": r["casas"], "usuarios": r["usuarios"]}


def leer_instalacion(casa):
    """Lo que ve el panel al arrancar en esta instalación."""
    return {"usuarios": sorted(u["usuario"] for u in cuentas.leer_usuarios()),
            "casas": [c["id"] for c in cuentas.listar_cuentas()],
            "token_nuki": cuentas.boveda_leer("nuki_token", casa),
            "entra_admin": bool(cuentas.verificar_password("admin1", "contraseña-admin-ficticia"))}


# ---------------------------------------------------------------- latido (aviso si se cae el panel)

def latido():
    """El latido lleva el token y los chats de los administradores (no los de otros usuarios), y lo que
    falla dentro del panel; si la tarea del hosting deja de ejecutarse, el panel avisa una sola vez."""
    import threading
    import dispositivos
    import latido as lt
    import telegram
    casa = cuentas.crear_cuenta("Casa de prueba")
    cuentas.crear_usuario("admin1", "Administradora", "contraseña-admin-ficticia", "admin", casa)
    cuentas.crear_usuario("gestor1", "Gestor", "contraseña-gestor-ficticia", "gestor", casa)
    with cuentas.en_cuenta(casa):
        cfg = nk.cargar_config()
        cfg["telegram_avisos"] = {"admin1": {"chat": 111, "nombre": "Admin"}, "gestor1": {"chat": 222, "nombre": "G"}}
        nk.guardar_config(cfg)
    obs = {"sin_configurar": lt.latir({})}
    os.environ["PANEL_URL_ENLACES"] = "https://puerta.ejemplo.invalid"
    cuentas.boveda_guardar(dispositivos.CLAVE_BOVEDA, "secreto-ficticio", cuentas.CUENTA_SISTEMA)
    cuentas.boveda_guardar(telegram.CLAVE_TOKEN, "123456:token-de-bot-ficticio", cuentas.CUENTA_SISTEMA)
    llamadas, enviados = [], []
    dispositivos._llamar = lambda accion, **d: llamadas.append({"accion": accion, **d}) or {"vigia": 0, "ahora": 0}
    telegram.enviar = lambda chat, texto, *a, **k: enviados.append((chat, texto))

    vivo = threading.Thread(target=threading.Event().wait, daemon=True)
    vivo.start()
    parado = threading.Thread(target=lambda: None)
    parado.start()
    parado.join()
    lt.latir({"avisos": vivo, "telegram": parado}, "http://127.0.0.1:9/no-responde")
    obs["latido"] = llamadas[-1]

    t0 = 1_000_000
    obs["recien_empezado"] = lt.revisar_vigia({"vigia": 0, "ahora": t0 + 60}, t0, ahora=t0 + 60)
    obs["parado"] = lt.revisar_vigia({"vigia": t0, "ahora": t0 + 3600}, t0, ahora=t0 + 3600)
    obs["parado_otra_vez"] = lt.revisar_vigia({"vigia": t0, "ahora": t0 + 3900}, t0, ahora=t0 + 3900)
    obs["vuelve"] = lt.revisar_vigia({"vigia": t0 + 4000, "ahora": t0 + 4100}, t0, ahora=t0 + 4100)
    obs["enviados"] = enviados
    return obs
