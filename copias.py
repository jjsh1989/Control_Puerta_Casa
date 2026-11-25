"""Copia de seguridad cifrada de los ajustes del panel.

Qué guarda: la carpeta datos/ (cuentas, usuarios, bóveda, historial…) menos las fotos y clips
de las cámaras, y la clave maestra de la bóveda. Todo va en un único archivo cifrado con una
contraseña de copia que pone el administrador (scrypt + Fernet/AES). Sin esa contraseña la copia
no sirve de nada; con ella se recupera todo en un servidor nuevo.

Cuándo: cada día o un día de la semana, a la hora elegida.

Dónde va: se guardan las últimas (1 a 5, se elige; por defecto 4) en datos/copias/. Para tenerlas fuera de casa:
  - un ordenador las recoge por SSH, sin descifrarlas (copias-fuera/: llave que solo sirve para eso);
    cada recogida deja la marca datos/copias/.recogida y, si pasan DIAS_SIN_RECOGER sin ella, se avisa;
  - y/o se manda cada una por Telegram a los administradores que reciben avisos (se puede desactivar).
Si una copia falla, avisa por Telegram.

Comprobar: tras cada copia (y con el botón «Comprobar») se descifra y se valida en memoria,
sin tocar nada: que se pueda abrir, que estén la clave maestra, los usuarios y la configuración
de cada casa, y que la bóveda se pueda leer con esa clave.

Recuperar (en el servidor nuevo, con el panel parado y desde la carpeta del panel):
    python3 copias.py restaurar ruta/al/archivo.lslab
Antes de cambiar nada comprueba la contraseña, la copia y los permisos; prepara todo aparte y
solo entonces lo sustituye. Si algo falla a mitad, deja la instalación como estaba.
"""
import base64
import io
import json
import os
import secrets
import shutil
import sys
import tarfile
import time
from datetime import datetime, timedelta
from pathlib import Path

CARPETA = Path(__file__).resolve().parent
DATOS = CARPETA / "datos"
CARPETA_COPIAS = DATOS / "copias"
CABECERA = b"LSLAB-COPIA-1\n"
GUARDAR = 4  # cuántas se conservan si no se elige otra cosa
OPCIONES_GUARDAR = (1, 2, 3, 4, 5)
EXCLUIR = {"camara", "copias", "importar"}
CLAVE_BOVEDA = "copia_clave"  # en la bóveda de _sistema: la clave derivada (no la contraseña)
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
TODOS_LOS_DIAS = 7  # valor de «dia» para la copia diaria
MARCA_RECOGIDA = ".recogida"  # la deja el ordenador que recoge las copias (copias-fuera/servidor-puerta-copias.sh)
DIAS_SIN_RECOGER = 2


def _derivar(password, sal):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    k = Scrypt(salt=sal, length=32, n=2 ** 15, r=8, p=1).derive(password.encode())
    return base64.urlsafe_b64encode(k)


# ---------------------------------------------------------------- ajustes (en datos/sistema)

def ajustes():
    import cuentas
    a = cuentas.leer_sistema().get("copias") or {}
    return {"activo": bool(a.get("activo")), "dia": int(a.get("dia", 6)), "hora": a.get("hora") or "03:30",
            "guardar": int(a.get("guardar") or GUARDAR), "telegram": a.get("telegram", True) is not False,
            "recogida": ultima_recogida(),
            "ultima": a.get("ultima"), "error": a.get("error"), "tamano": a.get("tamano"),
            # por separado: se creó (ultima), se pudo abrir y validar (comprobada), llegó fuera (envio)
            "comprobada": a.get("comprobada"), "envio": a.get("envio"),
            "clave": bool(cuentas.boveda_leer(CLAVE_BOVEDA, cuentas.CUENTA_SISTEMA)),
            "lista": listar()}


def listar():
    """Copias guardadas en el servidor, de la más nueva a la más vieja."""
    if not CARPETA_COPIAS.exists():
        return []
    import cuentas
    comprobaciones = (cuentas.leer_sistema().get("copias") or {}).get("comprobaciones") or {}
    salida = []
    for p in sorted(CARPETA_COPIAS.glob("copia-panel-*.lslab"), reverse=True):
        try:
            fecha = datetime.strptime(p.stem[len("copia-panel-"):], "%Y%m%d-%H%M").astimezone().isoformat()
        except ValueError:
            fecha = None
        salida.append({"nombre": p.name, "fecha": fecha, "tamano": f"{p.stat().st_size / 1024:.0f} KB",
                       "comprobacion": comprobaciones.get(p.name)})
    return salida


def ultima_recogida():
    """Cuándo recogió las copias el ordenador por última vez (ISO) o None si nunca."""
    p = CARPETA_COPIAS / MARCA_RECOGIDA
    try:
        texto = p.read_text().strip()
        return datetime.fromisoformat(texto).astimezone().isoformat(timespec="seconds")
    except (OSError, ValueError):
        try:
            return datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds")
        except OSError:
            return None


def _ruta(nombre):
    p = CARPETA_COPIAS / str(nombre or "")
    if p.parent != CARPETA_COPIAS or not p.name.startswith("copia-panel-") or p.suffix != ".lslab" or not p.exists():
        raise RuntimeError("Esa copia no existe.")
    return p


def borrar(nombre):
    _ruta(nombre).unlink()


def reenviar(nombre):
    p = _ruta(nombre)
    n, error = _mandar_por_telegram(p.name, p.read_bytes())
    _apuntar_envio(n, error)
    if not n:
        raise RuntimeError(f"No se pudo enviar: {error}")
    return n


def _apuntar_envio(enviados, error):
    _guardar_ajustes(envio={"hora": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "enviados": enviados, "error": error})


def _recortar():
    """Borra las copias que sobran según cuántas se quieren conservar."""
    guardar = ajustes()["guardar"]
    for viejo in sorted(CARPETA_COPIAS.glob("copia-panel-*.lslab"))[:-guardar] if CARPETA_COPIAS.exists() else []:
        viejo.unlink()
    import cuentas
    a = cuentas.leer_sistema().get("copias") or {}
    if a.get("comprobaciones"):  # olvidar las comprobaciones de copias que ya no están
        quedan = {p.name for p in CARPETA_COPIAS.glob("copia-panel-*.lslab")}
        _guardar_ajustes(comprobaciones={k: v for k, v in a["comprobaciones"].items() if k in quedan})


def _guardar_ajustes(**cambios):
    import cuentas
    s = cuentas.leer_sistema()
    a = s.get("copias") or {}
    a.update(cambios)
    s["copias"] = a
    cuentas.guardar_sistema(s)


def poner_ajustes(activo=None, dia=None, hora=None, guardar=None, telegram=None):
    cambios = {}
    if telegram is not None:
        cambios["telegram"] = bool(telegram)
    if guardar is not None:
        if int(guardar) not in OPCIONES_GUARDAR:
            raise RuntimeError("Elige cuántas copias conservar: de 1 a 5.")
        cambios["guardar"] = int(guardar)
    if activo is not None:
        if activo and not ajustes()["clave"]:
            raise RuntimeError("Pon primero la contraseña de las copias.")
        cambios["activo"] = bool(activo)
    if dia is not None:
        if int(dia) not in range(TODOS_LOS_DIAS + 1):
            raise RuntimeError("Día no válido.")
        cambios["dia"] = int(dia)
    if hora is not None:
        datetime.strptime(hora, "%H:%M")
        cambios["hora"] = hora
    _guardar_ajustes(**cambios)
    if "guardar" in cambios:
        _recortar()


def poner_clave(password):
    import cuentas
    if len(password or "") < 10:
        raise RuntimeError("La contraseña de las copias debe tener al menos 10 caracteres.")
    sal = secrets.token_bytes(16)
    cuentas.boveda_guardar(CLAVE_BOVEDA, json.dumps({"sal": sal.hex(), "clave": _derivar(password, sal).decode()}),
                           cuentas.CUENTA_SISTEMA)


# ---------------------------------------------------------------- hacer la copia

def _empaquetar():
    import cuentas
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(DATOS.iterdir()):
            if p.name in EXCLUIR or p.name.endswith(".tmp") or p.name.startswith("."):  # p. ej. .antes-de-restaurar-…
                continue
            tar.add(p, arcname=f"datos/{p.name}")
        clave = cuentas._clave_maestra()
        if clave:
            info = tarfile.TarInfo("secretos/clave_maestra")
            info.size, info.mtime, info.mode = len(clave), int(time.time()), 0o600
            tar.addfile(info, io.BytesIO(clave))
    return buf.getvalue()


def hacer_copia(quien="automático"):
    """Hace la copia, la guarda y (si está elegido) la manda por Telegram. Devuelve un texto con el resultado."""
    import cuentas
    import nuki_codigos as nk
    from cryptography.fernet import Fernet
    guardada = cuentas.boveda_leer(CLAVE_BOVEDA, cuentas.CUENTA_SISTEMA)
    if not guardada:
        raise RuntimeError("Falta la contraseña de las copias (Ajustes → Casas → Copia de seguridad).")
    g = json.loads(guardada)
    cifrado = Fernet(g["clave"].encode()).encrypt(_empaquetar())
    contenido = CABECERA + g["sal"].encode() + b"\n" + cifrado
    nombre = f"copia-panel-{datetime.now():%Y%m%d-%H%M}.lslab"
    CARPETA_COPIAS.mkdir(parents=True, exist_ok=True)
    ruta = CARPETA_COPIAS / nombre
    tmp = CARPETA_COPIAS / f".{nombre}.tmp"
    try:  # se escribe aparte (privado desde el principio) y solo al final recibe su nombre
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(contenido)
        tmp.replace(ruta)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tam = f"{len(contenido) / 1024:.0f} KB"
    _guardar_ajustes(ultima=datetime.now().astimezone().isoformat(timespec="seconds"), error=None, tamano=tam)
    try:
        comprobar(nombre)
        comprobada = "comprobada"
    except Exception as e:
        comprobada = f"⚠️ NO se ha podido comprobar: {e}"
    _recortar()
    texto = f"{nombre} ({tam}) · guardada en el servidor, {comprobada}"
    if ajustes()["telegram"]:
        enviados, error_envio = _mandar_por_telegram(nombre, contenido)
        _apuntar_envio(enviados, error_envio)
        texto += f" y enviada a {enviados} Telegram" if enviados else f" · ⚠️ no se pudo enviar por Telegram: {error_envio}"
    else:
        _guardar_ajustes(envio=None)
        texto += " · la recoge el ordenador"
    with cuentas.en_cuenta(cuentas.CUENTA_PRINCIPAL, quien):
        nk.registrar("Copia de seguridad", quien, texto)
    return texto


def chats_admin():
    """Chats de Telegram de los administradores (sin repetir)."""
    import cuentas
    import nuki_codigos as nk
    import telegram
    admins = {u["usuario"] for u in cuentas.leer_usuarios() if u["rol"] == "admin"}
    chats = []
    for c in cuentas.listar_cuentas():
        with cuentas.en_cuenta(c["id"], "copias"):
            try:
                av = telegram.avisos_vigentes(nk.cargar_config(), c["id"])
            except Exception:
                continue
        for usuario, v in av.items():
            if usuario in admins and v.get("chat") and v["chat"] not in chats:
                chats.append(v["chat"])
    return chats


def _mandar_por_telegram(nombre, contenido):
    """Devuelve (cuántos la recibieron, motivo si alguno falló o no había a quién mandarla)."""
    import telegram
    n, errores = 0, []
    chats = chats_admin()
    if not chats:
        return 0, "ningún administrador tiene su Telegram vinculado para los avisos"
    for chat in chats:
        try:
            telegram._api("sendDocument", {"chat_id": chat, "disable_notification": True,
                                           "caption": "💾 Copia de seguridad del panel (cifrada). Guárdala: "
                                                      "sirve para recuperar todo con tu contraseña de copias."},
                          ("document", nombre, contenido, "application/octet-stream"), timeout=120)
            n += 1
        except Exception as e:
            errores.append(str(e)[:200])
    return n, "; ".join(errores) or None


def avisar_admins(texto):
    import telegram
    for chat in chats_admin():
        try:
            telegram.enviar(chat, texto)
        except Exception:
            pass


def toca(ahora=None):
    """¿Toca la copia (diaria o semanal)? También si se pasó la hora con el servidor apagado."""
    a = ajustes()
    if not (a["activo"] and a["clave"]):
        return False
    ahora = ahora or datetime.now().astimezone()
    h, m = map(int, a["hora"].split(":"))
    cada = 1 if a["dia"] == TODOS_LOS_DIAS else 7
    prevista = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
    if cada == 7:
        prevista -= timedelta(days=(ahora.weekday() - a["dia"]) % 7)
    if prevista > ahora:
        prevista -= timedelta(days=cada)
    ultima = datetime.fromisoformat(a["ultima"]) if a["ultima"] else None
    ultimo_error = datetime.fromisoformat(a["error"]["hora"]) if a.get("error") else None
    if ultimo_error and ahora - ultimo_error < timedelta(hours=6):
        return False  # tras un fallo, reintenta cada 6 h
    return not ultima or ultima < prevista


def revisar():
    """Lo llama el hilo de extras cada minuto."""
    if toca():
        try:
            hacer_copia()
        except Exception as e:
            _guardar_ajustes(error={"hora": datetime.now().astimezone().isoformat(timespec="seconds"), "texto": str(e)})
            avisar_admins(f"⚠️ No se pudo hacer la copia de seguridad del panel: {e}\nLo vuelvo a intentar en 6 horas.")
    revisar_recogida()


def revisar_recogida(ahora=None):
    """Si un ordenador recoge las copias y deja de hacerlo, avisa una vez (y otra cuando vuelva).
    Devuelve el texto enviado o None."""
    a = ajustes()
    if not (a["activo"] and a["recogida"]):
        return None  # sin copias automáticas o sin ordenador que las recoja: no hay nada que vigilar
    ahora = ahora or datetime.now().astimezone()
    recogida = datetime.fromisoformat(a["recogida"])
    retrasada = ahora - recogida > timedelta(days=DIAS_SIN_RECOGER)
    import cuentas
    avisada = bool((cuentas.leer_sistema().get("copias") or {}).get("aviso_recogida"))
    if retrasada == avisada:
        return None
    if retrasada:
        texto = (f"⚠️ El ordenador no recoge las copias de seguridad desde el {recogida:%d/%m a las %H:%M}. "
                 f"En el servidor solo se conservan las {a['guardar']} últimas: si no las recoge, las más antiguas "
                 f"se perderán sin haber salido de casa. Comprueba que el ordenador esté encendido y conectado "
                 f"a Tailscale, o tráelas a mano.")
    else:
        texto = "✅ El ordenador vuelve a recoger las copias de seguridad."
    avisar_admins(texto)
    _guardar_ajustes(aviso_recogida=retrasada)
    return texto


# ---------------------------------------------------------------- comprobar

def _descifrar(contenido, password=None, clave=None):
    """Devuelve el .tar.gz de dentro. Con `password` (recuperar) o con la clave guardada en la bóveda."""
    from cryptography.fernet import Fernet, InvalidToken
    if not contenido.startswith(CABECERA):
        raise RuntimeError("Ese archivo no es una copia del panel.")
    sal, _, cifrado = contenido[len(CABECERA):].partition(b"\n")
    try:
        sal_b = bytes.fromhex(sal.decode())
    except ValueError:
        raise RuntimeError("La copia está dañada (cabecera ilegible).") from None
    if password is not None:
        clave = _derivar(password, sal_b)
    elif clave is None:
        import cuentas
        g = json.loads(cuentas.boveda_leer(CLAVE_BOVEDA, cuentas.CUENTA_SISTEMA) or "{}")
        if not g:
            raise RuntimeError("Falta la contraseña de las copias.")
        if g["sal"] != sal.decode():
            raise RuntimeError("Esta copia se hizo con otra contraseña de copias: escríbela para comprobarla.")
        clave = g["clave"].encode()
    try:
        return Fernet(clave).decrypt(cifrado)
    except InvalidToken:
        raise RuntimeError("Contraseña incorrecta o copia dañada: no se puede descifrar.") from None


def validar(tar_gz):
    """Comprueba que lo descifrado es una copia completa y coherente. Devuelve un resumen."""
    from cryptography.fernet import Fernet, InvalidToken
    try:
        tar = tarfile.open(fileobj=io.BytesIO(tar_gz), mode="r:gz")
        miembros = tar.getmembers()
    except (tarfile.TarError, EOFError, OSError):
        raise RuntimeError("La copia está dañada: no se puede abrir su contenido.") from None
    with tar:
        archivos = {}
        for m in miembros:
            if m.name.startswith("/") or ".." in Path(m.name).parts or m.name.split("/")[0] not in ("datos", "secretos"):
                raise RuntimeError(f"Archivo sospechoso dentro de la copia: {m.name}")
            if not (m.isfile() or m.isdir()):
                raise RuntimeError(f"Tipo de archivo no admitido dentro de la copia: {m.name}")
            if m.isfile():
                archivos[m.name] = tar.extractfile(m).read()
    clave = (archivos.get("secretos/clave_maestra") or b"").strip()
    if not clave:
        raise RuntimeError("A la copia le falta la clave maestra: no serviría para recuperar los secretos.")
    if "datos/usuarios.json" not in archivos:
        raise RuntimeError("A la copia le faltan los usuarios del panel.")
    for nombre, contenido in archivos.items():
        if nombre.endswith(".json"):
            try:
                json.loads(contenido.decode() or "null")
            except ValueError:
                raise RuntimeError(f"La copia tiene un archivo estropeado: {nombre}") from None
    if "datos/boveda.cifrada" in archivos:
        try:
            Fernet(clave).decrypt(archivos["datos/boveda.cifrada"])
        except (InvalidToken, ValueError):
            raise RuntimeError("La bóveda de la copia no se puede abrir con su clave maestra.") from None
    casas = {n.split("/")[2] for n in archivos if n.startswith("datos/cuentas/") and n.count("/") >= 3}
    usuarios = json.loads(archivos["datos/usuarios.json"]).get("usuarios", [])
    return {"archivos": len(archivos), "casas": len(casas), "usuarios": len(usuarios)}


def comprobar(nombre, password=None):
    """Descifra y valida una copia guardada (sin tocar nada) y apunta el resultado."""
    import cuentas
    p = _ruta(nombre)
    ahora = datetime.now().astimezone().isoformat(timespec="seconds")
    a = cuentas.leer_sistema().get("copias") or {}
    lista = dict(a.get("comprobaciones") or {})
    try:
        resumen = validar(_descifrar(p.read_bytes(), password))
    except Exception as e:
        lista[p.name] = {"hora": ahora, "ok": False, "error": str(e)}
        _guardar_ajustes(comprobaciones=lista)
        raise
    lista[p.name] = {"hora": ahora, "ok": True, **resumen}
    cambios = {"comprobaciones": lista}
    if listar() and listar()[0]["nombre"] == p.name:
        cambios["comprobada"] = {"hora": ahora, "nombre": p.name}
    _guardar_ajustes(**cambios)
    return resumen


# ---------------------------------------------------------------- recuperar

def _probar_escritura(carpeta):
    try:
        carpeta.mkdir(parents=True, exist_ok=True)
        prueba = carpeta / f".prueba-escritura-{secrets.token_hex(4)}"
        prueba.write_bytes(b"")
        prueba.unlink()
    except OSError as e:
        raise RuntimeError(f"No se puede escribir en {carpeta} ({e.strerror}). No se ha cambiado nada.") from None


def restaurar(ruta, password, destino=CARPETA):
    """Recupera una copia en `destino` (datos/ y secretos/) sin dejar nada a medias:
    1) descifra y valida, 2) comprueba permisos, 3) lo prepara aparte en cada carpeta,
    4) aparta lo que había y pone lo nuevo. Si algo falla, devuelve lo que había a su sitio.
    Lo anterior queda en «.antes-de-restaurar-…» dentro de cada carpeta, por si hay que volver.
    Las fotos, clips y copias del servidor (no van en la copia) se quedan donde están."""
    tar_gz = _descifrar(Path(ruta).read_bytes(), password)
    resumen = validar(tar_gz)
    destino = Path(destino)
    marca = datetime.now().strftime("%Y%m%d-%H%M%S")
    carpetas = {raiz: destino / raiz for raiz in ("datos", "secretos")}
    for c in carpetas.values():
        _probar_escritura(c)

    # 3) preparar aparte, dentro de cada carpeta (mismo disco: luego basta con renombrar)
    preparadas = {raiz: c / f".restaurando-{marca}" for raiz, c in carpetas.items()}
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_gz), mode="r:gz") as tar:
            for m in tar.getmembers():
                raiz, _, resto = m.name.partition("/")
                if not resto:
                    continue
                m.name = resto
                if sys.version_info >= (3, 12):
                    tar.extract(m, preparadas[raiz], filter="data")
                else:
                    tar.extract(m, preparadas[raiz])
        clave = preparadas["secretos"] / "clave_maestra"
        if clave.exists():
            clave.chmod(0o400)
    except Exception as e:
        for p in preparadas.values():
            shutil.rmtree(p, ignore_errors=True)
        raise RuntimeError(f"No se pudo preparar la copia ({e}). No se ha cambiado nada.") from None

    # 4) sustituir, apuntando cada paso para poder deshacerlo
    hechos = []  # (de, a) ya movidos
    apartados = {raiz: c / f".antes-de-restaurar-{marca}" for raiz, c in carpetas.items()}
    try:
        for raiz, c in carpetas.items():
            apartados[raiz].mkdir()
            for e in sorted(c.iterdir()):
                if e.name.startswith((".restaurando-", ".antes-de-restaurar-")) or (raiz == "datos" and e.name in EXCLUIR):
                    continue
                e.rename(apartados[raiz] / e.name)
                hechos.append((e, apartados[raiz] / e.name))
            for e in sorted(preparadas[raiz].iterdir()):
                if raiz == "datos" and e.name in EXCLUIR:
                    continue
                e.rename(c / e.name)
                hechos.append((e, c / e.name))
    except Exception as e:
        for origen, actual in reversed(hechos):  # deshacer en orden inverso
            try:
                actual.rename(origen)
            except OSError:
                pass
        for raiz in carpetas:
            shutil.rmtree(preparadas[raiz], ignore_errors=True)
            try:
                apartados[raiz].rmdir()  # solo si quedó vacía
            except OSError:
                pass
        raise RuntimeError(f"Falló al sustituir los datos ({e}). Se ha dejado todo como estaba.") from None
    for p in preparadas.values():
        shutil.rmtree(p, ignore_errors=True)
    return {"destino": str(destino), "anterior": [str(p) for p in apartados.values()], **resumen}


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "restaurar":
        print("Uso: python3 copias.py restaurar ruta/al/archivo.lslab")
        sys.exit(1)
    import getpass
    try:
        r = restaurar(sys.argv[2], getpass.getpass("Contraseña de las copias: "))
    except RuntimeError as e:
        print(f"❌ {e}")
        sys.exit(1)
    print(f"✅ Copia recuperada ({r['casas']} casas, {r['usuarios']} usuarios). Arranca el panel: docker compose up -d")
    print("Lo que había antes está en estas carpetas, por si hiciera falta volver atrás:")
    for p in r["anterior"]:
        print("   ", p)
    print("Cuando compruebes que todo funciona, puedes borrarlas.")
