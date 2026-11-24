"""Instalaciones de prueba: cada una es una carpeta temporal con una copia del código, su datos/ vacío
y su propia clave maestra. Los escenarios (escenarios.py) se ejecutan dentro, en un proceso aparte,
así cada instalación tiene sus rutas y su estado y no se mezclan entre sí ni con una instalación real.

Nada sale del equipo: Nuki, WhatsApp y Telegram se sustituyen por simulaciones dentro de cada escenario.

Por defecto se prueba el código de este repositorio. Con PUERTA_CODIGO=/otra/carpeta se prueba otra
versión (por ejemplo una anterior, para ver que las pruebas detectan el fallo que corrigen).
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PRUEBAS = Path(__file__).resolve().parent
CODIGO = Path(os.environ.get("PUERTA_CODIGO") or PRUEBAS.parent).resolve()


def comprobar_requisitos():
    """Sin «cryptography» el panel guardaría los secretos en el Llavero de macOS: las pruebas no deben
    tocarlo nunca, así que se paran antes."""
    try:
        import cryptography  # noqa: F401
    except ImportError:
        raise RuntimeError("Faltan dependencias. Instálalas con:  pip install cryptography==43.0.3 "
                           "websocket-client==1.8.0 paho-mqtt==2.1.0  (o ejecuta las pruebas en el contenedor)")


class Instalacion:
    def __init__(self, raiz=None):
        self._tmp = None
        if raiz is None:
            self._tmp = tempfile.mkdtemp(prefix="puerta-prueba-")
            raiz = self._tmp
        self.raiz = Path(raiz)
        self.raiz.mkdir(parents=True, exist_ok=True)
        for f in list(CODIGO.glob("*.py")) + [CODIGO / "panel.html", CODIGO / "instalar.sh"]:
            if f.exists():
                shutil.copy2(f, self.raiz / f.name)
        (self.raiz / "datos").mkdir(exist_ok=True)
        secretos = self.raiz / "secretos"
        secretos.mkdir(exist_ok=True)
        clave = secretos / "clave_maestra"
        clave.write_text(base64.urlsafe_b64encode(os.urandom(32)).decode() + "\n")
        clave.chmod(0o400)  # como la deja instalar.sh
        os.chmod(secretos, 0o700)
        (self.raiz / ".env").write_text("TZ=Europe/Madrid\nPANEL_PUERTO_HOST=8790\n")

    @property
    def clave_maestra(self):
        return (self.raiz / "secretos" / "clave_maestra").read_text().strip()

    def ejecutar(self, escenario, *args, timeout=120):
        """Ejecuta escenarios.<escenario>(*args) dentro de esta instalación y devuelve lo que observe."""
        entorno = dict(os.environ)
        entorno.update(CLAVE_MAESTRA_ARCHIVO=str(self.raiz / "secretos" / "clave_maestra"),
                       PYTHONPATH=os.pathsep.join([str(self.raiz), str(PRUEBAS)]),
                       PYTHONDONTWRITEBYTECODE="1", TZ="Europe/Madrid")
        orden = ("import json, sys, escenarios; "
                 "r = getattr(escenarios, sys.argv[1])(*json.loads(sys.argv[2])); "
                 "print('\\n@@RESULTADO@@' + json.dumps(r, ensure_ascii=False))")
        p = subprocess.run([sys.executable, "-c", orden, escenario, json.dumps(args)], cwd=self.raiz,
                           env=entorno, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0 or "@@RESULTADO@@" not in p.stdout:
            raise AssertionError(f"El escenario «{escenario}» terminó con error:\n{p.stdout[-2000:]}\n{p.stderr[-3000:]}")
        return json.loads(p.stdout.rsplit("@@RESULTADO@@", 1)[1])

    def borrar(self):
        if self._tmp:
            for d, _, archivos in os.walk(self._tmp):  # la clave es de solo lectura
                os.chmod(d, 0o700)
                for a in archivos:
                    try:
                        os.chmod(os.path.join(d, a), 0o600)
                    except OSError:
                        pass
            shutil.rmtree(self._tmp, ignore_errors=True)
