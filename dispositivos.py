"""Dispositivos recordados por la página de entrada del dominio (p. ej. puerta.midominio.es).

Esa página (web/puerta/index.php, en el hosting) recuerda cada móvil u ordenador con una cookie
que pone su servidor, y guarda la lista allí. El panel la gestiona llamando a su api.php con
peticiones firmadas (HMAC-SHA256) con un secreto que comparten los dos:

  - aquí, en la bóveda (clave «web_dispositivos», cuenta _sistema);
  - allí, en privado/secreto.php.

Dar de alta crea un enlace de un solo uso que caduca a los 15 minutos; al abrirlo en el móvil,
queda recordado. Olvidar lo quita de la lista y su cookie deja de valer.
"""
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.request

import cuentas

CLAVE_BOVEDA = "web_dispositivos"


def url_web():
    """Dirección de la página de entrada (la de los enlaces de Telegram)."""
    return (os.environ.get("PANEL_URL_ENLACES") or "").rstrip("/")


def disponible():
    return bool(url_web() and cuentas.boveda_leer(CLAVE_BOVEDA, cuentas.CUENTA_SISTEMA))


def _llamar(accion, **datos):
    secreto = cuentas.boveda_leer(CLAVE_BOVEDA, cuentas.CUENTA_SISTEMA)
    if not (url_web() and secreto):
        raise RuntimeError("La gestión de dispositivos no está configurada en este servidor.")
    cuerpo = json.dumps({"accion": accion, "t": int(time.time()), "n": secrets.token_hex(16), **datos},
                        ensure_ascii=False).encode()
    firma = hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()
    req = urllib.request.Request(url_web() + "/api.php", data=cuerpo, method="POST",
                                 headers={"Content-Type": "application/json", "X-Firma": firma,
                                          "User-Agent": "panel-puerta"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            error = json.loads(e.read()).get("error")
        except Exception:
            error = None
        raise RuntimeError(error or f"La página del dominio respondió con un error ({e.code}).") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"No se pudo conectar con la página del dominio: {getattr(e, 'reason', e)}") from None


def listar():
    return _llamar("listar")


def alta(nombre, quien=""):
    """Crea un enlace de un solo uso para recordar un dispositivo. Devuelve (enlace, datos)."""
    nombre = " ".join(str(nombre or "").split())[:40]
    if not nombre:
        raise RuntimeError("Ponle un nombre al dispositivo (por ejemplo «iPhone de Ana»).")
    r = _llamar("alta", nombre=nombre, quien=str(quien or "")[:40])
    return f"{url_web()}/?alta={r.pop('codigo')}", r


def borrar(ident):
    return _llamar("borrar", id=str(ident))


def cancelar_enlaces():
    return _llamar("cancelar")


def chat_de(usuario):
    """Chat de Telegram donde ese usuario del panel recibe los avisos (el primero que haya)."""
    import nuki_codigos as nk
    import telegram
    for c in cuentas.listar_cuentas():
        with cuentas.en_cuenta(c["id"], "dispositivos"):
            try:
                v = telegram.avisos_vigentes(nk.cargar_config(), c["id"]).get(usuario) or {}
            except Exception:
                continue
        if v.get("chat"):
            return v["chat"]
    return None


def mandar_enlace(chat, enlace, nombre):
    import telegram
    sitio = url_web().split("//", 1)[-1]
    telegram.enviar(chat, f"📱 Enlace para recordar «{nombre}» en {sitio}.\n"
                          f"Ábrelo en ese dispositivo, con Safari o su navegador. Sirve una sola vez y caduca en 15 minutos.",
                    [[{"text": "📱 Recordar este dispositivo", "url": enlace}]])
