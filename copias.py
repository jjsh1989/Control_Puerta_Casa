"""Copia de seguridad cifrada de los ajustes del panel.

Qué guarda: la carpeta datos/ (cuentas, usuarios, bóveda, historial…) menos las fotos y clips
de las cámaras, y la clave maestra de la bóveda. Todo va en un único archivo cifrado con una
contraseña de copia que pone el administrador (scrypt + Fernet/AES). Sin esa contraseña la copia
no sirve de nada; con ella se recupera todo en un servidor nuevo.

Dónde va: se guardan las 4 últimas en datos/copias/ y cada una se manda por Telegram a los
administradores que reciben avisos (así queda fuera del servidor). Si una copia falla, avisa.

Recuperar (en el servidor nuevo, con el panel parado y desde la carpeta del panel):
    python3 copias.py restaurar ruta/al/archivo.lslab
"""
import base64
import io
import json
import os
import secrets
import sys
import tarfile
import time
from datetime import datetime, timedelta
from pathlib import Path

CARPETA = Path(__file__).resolve().parent
DATOS = CARPETA / "datos"
CARPETA_COPIAS = DATOS / "copias"
CABECERA = b"LSLAB-COPIA-1\n"
GUARDAR = 4
EXCLUIR = {"camara", "copias", "importar"}
CLAVE_BOVEDA = "copia_clave"  # en la bóveda de _sistema: la clave derivada (no la contraseña)
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def _derivar(password, sal):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    k = Scrypt(salt=sal, length=32, n=2 ** 15, r=8, p=1).derive(password.encode())
    return base64.urlsafe_b64encode(k)


# ---------------------------------------------------------------- ajustes (en datos/sistema)

def ajustes():
    import cuentas
    a = cuentas.leer_sistema().get("copias") or {}
    return {"activo": bool(a.get("activo")), "dia": int(a.get("dia", 6)), "hora": a.get("hora") or "03:30",
            "ultima": a.get("ultima"), "error": a.get("error"), "tamano": a.get("tamano"),
            "clave": bool(cuentas.boveda_leer(CLAVE_BOVEDA, cuentas.CUENTA_SISTEMA))}


def _guardar_ajustes(**cambios):
    import cuentas
    s = cuentas.leer_sistema()
    a = s.get("copias") or {}
    a.update(cambios)
    s["copias"] = a
    cuentas.guardar_sistema(s)


def poner_ajustes(activo=None, dia=None, hora=None):
    cambios = {}
    if activo is not None:
        if activo and not ajustes()["clave"]:
            raise RuntimeError("Pon primero la contraseña de las copias.")
        cambios["activo"] = bool(activo)
    if dia is not None:
        if int(dia) not in range(7):
            raise RuntimeError("Día no válido.")
        cambios["dia"] = int(dia)
    if hora is not None:
        datetime.strptime(hora, "%H:%M")
        cambios["hora"] = hora
    _guardar_ajustes(**cambios)


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
            if p.name in EXCLUIR or p.name.endswith(".tmp"):
                continue
            tar.add(p, arcname=f"datos/{p.name}")
        clave = cuentas._clave_maestra()
        if clave:
            info = tarfile.TarInfo("secretos/clave_maestra")
            info.size, info.mtime, info.mode = len(clave), int(time.time()), 0o600
            tar.addfile(info, io.BytesIO(clave))
    return buf.getvalue()


def hacer_copia(quien="automático"):
    """Hace la copia, la guarda y la manda por Telegram. Devuelve un texto con el resultado."""
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
    (CARPETA_COPIAS / nombre).write_bytes(contenido)
    os.chmod(CARPETA_COPIAS / nombre, 0o600)
    for viejo in sorted(CARPETA_COPIAS.glob("copia-panel-*.lslab"))[:-GUARDAR]:
        viejo.unlink()
    enviados = _mandar_por_telegram(nombre, contenido)
    tam = f"{len(contenido) / 1024:.0f} KB"
    _guardar_ajustes(ultima=datetime.now().astimezone().isoformat(timespec="seconds"), error=None, tamano=tam)
    texto = f"{nombre} ({tam}) · guardada en el servidor" + (f" y enviada a {enviados} Telegram" if enviados else
                                                            " (no se pudo enviar por Telegram: nadie administrador vinculado)")
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
                av = telegram.avisos_de(nk.cargar_config())
            except Exception:
                continue
        for usuario, v in av.items():
            if usuario in admins and v.get("chat") and v["chat"] not in chats:
                chats.append(v["chat"])
    return chats


def _mandar_por_telegram(nombre, contenido):
    import telegram
    n = 0
    for chat in chats_admin():
        try:
            telegram._api("sendDocument", {"chat_id": chat, "disable_notification": True,
                                           "caption": "💾 Copia de seguridad del panel (cifrada). Guárdala: "
                                                      "sirve para recuperar todo con tu contraseña de copias."},
                          ("document", nombre, contenido, "application/octet-stream"), timeout=120)
            n += 1
        except Exception:
            pass
    return n


def avisar_admins(texto):
    import telegram
    for chat in chats_admin():
        try:
            telegram.enviar(chat, texto)
        except Exception:
            pass


def toca(ahora=None):
    """¿Toca la copia semanal? (o se pasó la hora con el servidor apagado)."""
    a = ajustes()
    if not (a["activo"] and a["clave"]):
        return False
    ahora = ahora or datetime.now().astimezone()
    h, m = map(int, a["hora"].split(":"))
    prevista = ahora.replace(hour=h, minute=m, second=0, microsecond=0) - timedelta(days=(ahora.weekday() - a["dia"]) % 7)
    if prevista > ahora:
        prevista -= timedelta(days=7)
    ultima = datetime.fromisoformat(a["ultima"]) if a["ultima"] else None
    ultimo_error = datetime.fromisoformat(a["error"]["hora"]) if a.get("error") else None
    if ultimo_error and ahora - ultimo_error < timedelta(hours=6):
        return False  # tras un fallo, reintenta cada 6 h
    return not ultima or ultima < prevista


def revisar():
    """Lo llama el hilo de extras cada minuto."""
    if not toca():
        return
    try:
        hacer_copia()
    except Exception as e:
        _guardar_ajustes(error={"hora": datetime.now().astimezone().isoformat(timespec="seconds"), "texto": str(e)})
        avisar_admins(f"⚠️ No se pudo hacer la copia de seguridad del panel: {e}\nLo vuelvo a intentar en 6 horas.")


# ---------------------------------------------------------------- recuperar

def restaurar(ruta, password, destino=CARPETA):
    from cryptography.fernet import Fernet, InvalidToken
    contenido = Path(ruta).read_bytes()
    if not contenido.startswith(CABECERA):
        raise RuntimeError("Ese archivo no es una copia del panel.")
    resto = contenido[len(CABECERA):]
    sal, _, cifrado = resto.partition(b"\n")
    try:
        datos = Fernet(_derivar(password, bytes.fromhex(sal.decode()))).decrypt(cifrado)
    except InvalidToken:
        raise RuntimeError("Contraseña incorrecta (o archivo dañado).") from None
    destino = Path(destino)
    with tarfile.open(fileobj=io.BytesIO(datos), mode="r:gz") as tar:
        for m in tar.getmembers():
            if m.name.startswith("/") or ".." in Path(m.name).parts or not m.name.split("/")[0] in ("datos", "secretos"):
                raise RuntimeError(f"Archivo sospechoso dentro de la copia: {m.name}")
        tar.extractall(destino, filter="data") if sys.version_info >= (3, 12) else tar.extractall(destino)
    return destino


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "restaurar":
        print("Uso: python3 copias.py restaurar ruta/al/archivo.lslab")
        sys.exit(1)
    import getpass
    destino = restaurar(sys.argv[2], getpass.getpass("Contraseña de las copias: "))
    print(f"✅ Copia recuperada en {destino}/datos y {destino}/secretos. Arranca el panel: docker compose up -d")
