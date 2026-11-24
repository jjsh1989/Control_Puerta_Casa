"""Latido: aviso por Telegram si se cae el panel, el servidor o la conexión de casa.

Dentro de casa nadie puede avisar de que la casa se ha quedado sin luz, sin internet o sin panel.
Por eso el panel manda cada pocos minutos un «latido» a la página del dominio (la misma api.php
firmada de dispositivos.py), y allí una tarea programada (privado/vigia.php) comprueba cada pocos
minutos cuándo llegó el último:

  - si pasan más de MINUTOS_CAIDA sin latido, avisa una vez a los administradores con Telegram;
  - cuando vuelve a llegar, avisa de que el panel ha vuelto y cuánto tiempo estuvo sin señal;
  - el latido lleva también lo que falla dentro del panel (un proceso interno parado o el servidor
    web sin responder), para avisar aunque el panel siga mandando latidos.

Para poder avisar con el panel caído, el hosting guarda (en su carpeta privada) el token del bot
y los chats de los administradores; el latido los mantiene al día.

El panel comprueba a su vez que la tarea del hosting se está ejecutando: si no, avisa él mismo,
porque entonces una caída pasaría sin aviso.
"""
import threading
import time
import urllib.error
import urllib.request

import cuentas
import nuki_codigos as nk

CADA_SEGUNDOS = 300
MINUTOS_CAIDA = 10
VIGIA_PARADO = 30 * 60  # segundos sin que se ejecute la tarea del hosting para dar la alarma
CLAVE_SISTEMA = "latido_vigia_parado"

NOMBRES_HILOS = {
    "avisos": "los avisos de la cerradura",
    "telegram": "el bot de Telegram",
    "programador": "las acciones programadas",
    "whatsapp": "la vigilancia de WhatsApp",
}


def problemas(hilos, url_local=None):
    """Lo que falla dentro del panel: procesos internos parados y el servidor web sin responder."""
    salida = [f"Se ha parado {NOMBRES_HILOS.get(n, n)}." for n, h in hilos.items() if not h.is_alive()]
    if url_local:
        try:
            urllib.request.urlopen(url_local, timeout=10).close()
        except urllib.error.HTTPError:
            pass  # responde, aunque sea con un error: está vivo
        except Exception:
            salida.append("El panel no responde a las páginas (servidor web atascado).")
    return salida


def latir(hilos, url_local=None):
    """Manda un latido. Devuelve la respuesta del hosting, o None si no está configurado."""
    import copias
    import dispositivos
    import telegram
    if not (dispositivos.disponible() and telegram.configurado()):
        return None
    return dispositivos._llamar("latido", token=telegram.token(), chats=copias.chats_admin()[:10],
                                minutos=MINUTOS_CAIDA, problemas=problemas(hilos, url_local)[:10])


def revisar_vigia(respuesta, latiendo_desde, ahora=None):
    """Avisa (una vez) si la tarea programada del hosting no se ejecuta, y otra vez cuando vuelva.
    Devuelve el mensaje enviado o None."""
    import copias
    import telegram
    ahora = ahora or time.time()
    if ahora - latiendo_desde < VIGIA_PARADO:
        return None  # primero hay que darle tiempo a ejecutarse
    hora = int(respuesta.get("ahora") or ahora)
    parado = hora - int(respuesta.get("vigia") or 0) > VIGIA_PARADO
    s = cuentas.leer_sistema()
    if parado == bool(s.get(CLAVE_SISTEMA)):
        return None
    if parado:
        texto = ("⚠️ El vigilante de la página del dominio no se está ejecutando: si el panel o el servidor "
                 "se caen, no te llegará ningún aviso. Revisa la tarea programada (cron) del hosting "
                 "que ejecuta privado/vigia.php.")
    else:
        texto = "✅ El vigilante de la página del dominio vuelve a ejecutarse: si el panel se cae, te avisará."
    enviados = 0
    for chat in copias.chats_admin():
        try:
            telegram.enviar(chat, texto)
            enviados += 1
        except Exception:
            pass
    if enviados:
        s = cuentas.leer_sistema()
        s[CLAVE_SISTEMA] = parado
        cuentas.guardar_sistema(s)
        nk.log(texto)
        return texto
    return None


class Latido(threading.Thread):
    def __init__(self, hilos, url_local=None):
        super().__init__(daemon=True)
        self.hilos, self.url_local = hilos, url_local

    def run(self):
        time.sleep(60)  # que arranque todo antes del primer latido
        latiendo_desde, ultimo_error = None, None
        while True:
            try:
                r = latir(self.hilos, self.url_local)
                if r is None:
                    latiendo_desde = None
                else:
                    latiendo_desde = latiendo_desde or time.time()
                    if ultimo_error:
                        nk.log("Latido: vuelve a llegar a la página del dominio.")
                    ultimo_error = None
                    revisar_vigia(r, latiendo_desde)
            except Exception as e:
                latiendo_desde = None
                if str(e) != ultimo_error:  # solo los cambios, para no llenar el registro
                    nk.log(f"Latido: {e}")
                ultimo_error = str(e)
            time.sleep(CADA_SEGUNDOS)
