"""Copias de seguridad: un fallo al hacerla no deja nada a medias, y una copia sirve para recuperar
todo en una instalación nueva."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from escenarios import PASSWORD_COPIAS
from instalacion import Instalacion, comprobar_requisitos


def setUpModule():
    comprobar_requisitos()


class ErrorAlArchivar(unittest.TestCase):
    def setUp(self):
        self.inst = Instalacion()
        self.addCleanup(self.inst.borrar)

    def test_copia_del_panel_que_falla_al_escribir_no_deja_nada_a_medias(self):
        self.inst.ejecutar("preparar_instalacion_con_datos")
        o = self.inst.ejecutar("copia_falla_al_escribir")
        self.assertIsNotNone(o["error"], "el fallo debe notarse, no darse la copia por hecha")
        self.assertEqual(o["archivos_despues"], o["archivos_antes"],
                         "no puede quedar un archivo nuevo (ni temporal) a medias en la carpeta de copias")
        self.assertTrue(o["anteriores_intactas"], "la copia anterior no se toca")
        self.assertTrue(o["ultima_igual"], "no se apunta como hecha una copia que falló")
        self.assertEqual(o["listadas"], o["archivos_antes"])

    def _instalar_sh_copia(self, tar_falso):
        """Ejecuta «./instalar.sh copia» con un docker de mentira (solo comprueba que existe) y,
        si se pide, un tar que escribe medio archivo y falla (como con un disco lleno o un archivo ilegible)."""
        bin_falso = Path(tempfile.mkdtemp(prefix="puerta-bin-"))
        self.addCleanup(shutil.rmtree, bin_falso, True)
        (bin_falso / "docker").write_text("#!/bin/sh\nexit 0\n")
        if tar_falso:
            (bin_falso / "tar").write_text('#!/bin/sh\nprintf "medio archivo" > "$2"\n'
                                           'echo "tar: datos/x: No se puede leer" >&2\nexit 2\n')
        for p in bin_falso.iterdir():
            p.chmod(0o755)
        if not os.access(bin_falso / "docker", os.X_OK):
            self.skipTest("la carpeta temporal no permite ejecutar programas (p. ej. /tmp del contenedor del panel)")
        (self.inst.raiz / "datos" / "usuarios.json").write_text('{"usuarios": []}')
        entorno = dict(os.environ, PATH=f"{bin_falso}{os.pathsep}{os.environ.get('PATH', '')}")
        return subprocess.run(["bash", "instalar.sh", "copia"], cwd=self.inst.raiz, env=entorno,
                              capture_output=True, text=True, timeout=60)

    def _restos(self):
        return sorted(p.name for p in self.inst.raiz.iterdir()
                      if p.name.startswith(("copia-puerta-", ".copia-puerta")))

    def test_copia_manual_que_falla_avisa_y_no_deja_un_archivo_que_parezca_bueno(self):
        p = self._instalar_sh_copia(tar_falso=True)
        self.assertNotEqual(p.returncode, 0, "la copia fallida no puede terminar como si hubiera ido bien")
        self.assertIn("ERROR", p.stderr)
        self.assertNotIn("Copia creada", p.stdout)
        self.assertEqual(self._restos(), [], "no debe quedar ni la copia a medias ni el temporal")

    def test_copia_manual_correcta(self):
        p = self._instalar_sh_copia(tar_falso=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        restos = self._restos()
        self.assertEqual(len(restos), 1, restos)
        archivo = self.inst.raiz / restos[0]
        self.assertTrue(restos[0].startswith("copia-puerta-") and restos[0].endswith(".tar.gz"))
        self.assertEqual(archivo.stat().st_mode & 0o777, 0o600, "lleva la clave maestra: solo para su dueño")
        contenido = subprocess.run(["tar", "tzf", str(archivo)], capture_output=True, text=True).stdout.split()
        self.assertIn("secretos/clave_maestra", contenido)
        self.assertIn("datos/usuarios.json", contenido)


class RestaurarEnInstalacionNueva(unittest.TestCase):
    def test_una_copia_recupera_todo_en_una_instalacion_recien_hecha(self):
        vieja, nueva = Instalacion(), Instalacion()
        self.addCleanup(vieja.borrar)
        self.addCleanup(nueva.borrar)
        casa = vieja.ejecutar("preparar_instalacion_con_datos")["casa"]
        esperado = vieja.ejecutar("leer_instalacion", casa)  # lo que hay al hacer la copia
        copia = vieja.ejecutar("hacer_copia")
        self.assertEqual(esperado["token_nuki"], "token-nuki-ficticio")

        # la nueva acaba de instalarse: otra clave maestra (de solo lectura) y nada en datos/
        self.assertNotEqual(nueva.clave_maestra, vieja.clave_maestra)
        self.assertEqual(list((nueva.raiz / "datos").iterdir()), [])

        r = nueva.ejecutar("restaurar", copia["ruta"], PASSWORD_COPIAS)
        self.assertEqual((r["casas"], r["usuarios"]), (1, 2))
        self.assertEqual(nueva.clave_maestra, vieja.clave_maestra, "se recupera la clave maestra de la copia")
        self.assertEqual((nueva.raiz / "secretos" / "clave_maestra").stat().st_mode & 0o777, 0o400)
        recuperado = nueva.ejecutar("leer_instalacion", casa)
        self.assertEqual(recuperado, esperado,
                         "usuarios, casas y secretos de la bóveda vuelven a estar disponibles")
        self.assertTrue(recuperado["entra_admin"], "el administrador entra con su contraseña de siempre")

    def test_con_la_contrasena_equivocada_no_se_toca_nada(self):
        vieja, nueva = Instalacion(), Instalacion()
        self.addCleanup(vieja.borrar)
        self.addCleanup(nueva.borrar)
        vieja.ejecutar("preparar_instalacion_con_datos")
        copia = vieja.ejecutar("hacer_copia")
        clave_antes = nueva.clave_maestra
        with self.assertRaises(AssertionError) as e:
            nueva.ejecutar("restaurar", copia["ruta"], "otra-contraseña-cualquiera")
        self.assertIn("Contraseña incorrecta", str(e.exception))
        self.assertEqual(nueva.clave_maestra, clave_antes)
        self.assertEqual(list((nueva.raiz / "datos").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
