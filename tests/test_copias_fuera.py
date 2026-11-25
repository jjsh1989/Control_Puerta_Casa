"""Copias fuera de casa: la copia diaria, la recogida desde un ordenador con una llave que solo sirve para
eso, y que una descarga dañada o un fallo nunca dejen al ordenador sin sus copias."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from instalacion import CODIGO, Instalacion, comprobar_requisitos

SERVIDOR = CODIGO / "copias-fuera" / "servidor-puerta-copias.sh"
TRAER = CODIGO / "copias-fuera" / "traer-copias.sh"


def setUpModule():
    comprobar_requisitos()


class CopiaDiaria(unittest.TestCase):
    def test_copia_diaria_sin_telegram_y_aviso_si_el_ordenador_no_las_recoge(self):
        inst = Instalacion()
        self.addCleanup(inst.borrar)
        o = inst.ejecutar("copia_diaria_sin_telegram")
        self.assertFalse(o["toca_antes_de_la_hora"])
        self.assertTrue(o["toca_despues_de_la_hora"], "cada día a su hora")
        self.assertFalse(o["toca_ya_hecha"], "una sola copia al día")
        self.assertEqual(o["mandadas"], [], "con Telegram desactivado no se manda el archivo")
        self.assertIn("la recoge el ordenador", o["texto"])
        self.assertIsNone(o["sin_ordenador"], "si ningún ordenador las recoge, no hay nada que avisar")
        self.assertIsNotNone(o["recogida"])
        self.assertIn("no recoge las copias", o["retrasada"])
        self.assertIsNone(o["retrasada_otra_vez"], "se avisa una sola vez")
        self.assertIn("vuelve a recoger", o["vuelve"])
        self.assertEqual(o["avisos"], 2)
        self.assertEqual(o["mandadas_con_telegram"], 1, "con Telegram activado se sigue mandando")


class RecogidaDesdeElOrdenador(unittest.TestCase):
    """traer-copias.sh contra servidor-puerta-copias.sh, con un ssh de mentira que hace de orden forzada."""

    def setUp(self):
        self.raiz = Path(tempfile.mkdtemp(prefix="puerta-fuera-"))
        self.addCleanup(shutil.rmtree, self.raiz, True)
        self.copias, self.mac, self.bin = self.raiz / "servidor", self.raiz / "mac", self.raiz / "bin"
        for d in (self.copias, self.bin):
            d.mkdir()
        (self.raiz / "conf").write_text(f"CARPETA={self.copias}\nLXC=\n")
        # como sshd con la llave restringida: lo que pide el cliente llega en SSH_ORIGINAL_COMMAND
        (self.bin / "ssh").write_text(
            '#!/bin/bash\nwhile [ "${1:-}" != puerta-copias ]; do shift; done; shift\n'
            f'PUERTA_COPIAS_CONF="{self.raiz}/conf" SSH_ORIGINAL_COMMAND="$*" exec bash "{SERVIDOR}" '
            '| if [ -n "${DANAR:-}" ] && [ "$1" = traer ]; then sed "s/a/b/"; else cat; fi\n')
        (self.bin / "ssh").chmod(0o755)
        (self.bin / "osascript").write_text("#!/bin/sh\nexit 0\n")
        (self.bin / "osascript").chmod(0o755)
        if not os.access(self.bin / "ssh", os.X_OK):
            self.skipTest("la carpeta temporal no permite ejecutar programas")
        for dia in ("20261121", "20261122", "20261123", "20261124"):
            (self.copias / f"copia-panel-{dia}-0330.lslab").write_bytes(f"LSLAB-COPIA-1\naaaa{dia}".encode())

    def traer(self, **extra):
        entorno = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                       PUERTA_COPIAS_DESTINO=str(self.mac), PUERTA_COPIAS_GUARDAR="3", **extra)
        return subprocess.run(["bash", str(TRAER)], env=entorno, capture_output=True, text=True, timeout=60)

    def en_el_mac(self):
        return sorted(p.name for p in self.mac.iterdir() if p.name.startswith("copia-panel-"))

    def test_trae_las_que_faltan_conserva_las_3_mas_recientes_y_marca_la_recogida(self):
        p = self.traer()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.en_el_mac(), [f"copia-panel-{d}-0330.lslab" for d in ("20261122", "20261123", "20261124")])
        for n in self.en_el_mac():
            self.assertEqual((self.mac / n).read_bytes(), (self.copias / n).read_bytes(), "llega tal cual, cifrada")
        self.assertEqual(self.mac.stat().st_mode & 0o777, 0o700)
        self.assertTrue((self.copias / ".recogida").exists(), "el servidor sabe que se han recogido")
        self.assertEqual([p.name for p in self.mac.iterdir() if p.name.endswith(".tmp")], [])

        (self.copias / "copia-panel-20261125-0330.lslab").write_bytes(b"LSLAB-COPIA-1\nnueva")
        self.assertEqual(self.traer().returncode, 0)
        self.assertEqual(self.en_el_mac()[0], "copia-panel-20261123-0330.lslab", "al día siguiente sale la más vieja")
        self.assertEqual(len(self.en_el_mac()), 3)

    def test_una_copia_danada_no_se_guarda_ni_se_borra_ninguna(self):
        self.assertEqual(self.traer().returncode, 0)
        antes = self.en_el_mac()
        (self.copias / ".recogida").unlink()
        (self.copias / "copia-panel-20261125-0330.lslab").write_bytes(b"LSLAB-COPIA-1\naaaa")
        p = self.traer(DANAR="1")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("dañada", p.stdout)
        self.assertEqual(self.en_el_mac(), antes, "ni se guarda la dañada ni se borra la más antigua")
        self.assertEqual([p.name for p in self.mac.iterdir() if p.name.endswith(".tmp")], [])
        self.assertFalse((self.copias / ".recogida").exists(), "una recogida fallida no cuenta")

    def test_la_llave_no_sirve_para_otra_cosa(self):
        conf = {"PUERTA_COPIAS_CONF": str(self.raiz / "conf")}
        for orden in ("cat /etc/passwd", "traer ../../etc/passwd", "traer copia-panel-*.lslab", "listar; id", ""):
            p = subprocess.run(["bash", str(SERVIDOR)], env=dict(os.environ, SSH_ORIGINAL_COMMAND=orden, **conf),
                               capture_output=True, text=True, timeout=30)
            self.assertNotEqual(p.returncode, 0, orden)
            self.assertEqual(p.stdout, "", orden)


if __name__ == "__main__":
    unittest.main()
