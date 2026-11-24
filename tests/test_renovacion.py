"""Renovación de códigos: nadie se queda sin poder entrar porque el código nuevo no le haya llegado."""
import unittest

from instalacion import Instalacion, comprobar_requisitos


def setUpModule():
    comprobar_requisitos()


class Renovacion(unittest.TestCase):
    def setUp(self):
        self.inst = Instalacion()
        self.addCleanup(self.inst.borrar)

    def test_envio_manual_pendiente_conserva_el_codigo_anterior(self):
        o = self.inst.ejecutar("renovacion_envio_manual")
        nuevo = o["nuevo"]
        p = o["preparado"]
        self.assertIn("111111", p["activos"], "el código anterior debe seguir abriendo hasta confirmar el envío")
        self.assertTrue(p["con_enlace"], "con el proveedor «enlace» se devuelve el enlace de WhatsApp")
        self.assertEqual(p["estado"], "por_confirmar", "el mensaje solo está preparado, no enviado")
        self.assertIn(nuevo, p["activos"])
        self.assertEqual(p["pendiente"], {"estado": "por_confirmar", "codigo": nuevo})
        c = o["confirmado"]
        self.assertNotIn("error", c, c.get("error"))
        self.assertEqual(c["activos"], [nuevo], "al confirmar se retira el anterior y queda solo el nuevo")
        self.assertIsNone(c["pendiente"])

    def test_si_falla_un_destinatario_los_demas_se_renuevan_y_el_reintento_manda_el_mismo_codigo(self):
        o = self.inst.ejecutar("renovacion_falla_un_destinatario")
        t = o["tras_fallo"]
        self.assertIn("222222", t["activos"], "a Bea no le llegó: su anterior debe seguir abriendo")
        self.assertEqual(o["resultado"]["Ana"], "enviado")
        self.assertEqual(o["resultado"]["Bea"], "error")
        self.assertEqual(t["enviados"], ["+34600000001"])
        self.assertIsNone(t["pendiente_ana"])
        self.assertNotIn("111111", t["activos"], "a Ana le llegó el nuevo: su anterior se retira")
        self.assertEqual((t["pendiente_bea"] or {}).get("estado"), "fallido", "queda apuntada para reintentar")
        codigo_bea = t["pendiente_bea"]["codigo"]
        self.assertIn(codigo_bea, t["activos"])

        self.assertEqual(o["reintento"], {"Bea": "enviado"})
        r = o["tras_reintento"]
        self.assertEqual(r["enviados"], ["+34600000001", "+34600000002"])
        self.assertIsNone(r["pendiente_bea"])
        self.assertNotIn("222222", r["activos"], "con el nuevo ya entregado se retira el anterior")
        self.assertIn(codigo_bea, r["activos"], "el reintento manda el mismo código que se creó, no otro")
        self.assertEqual(len(r["activos"]), 2, "un código por persona")


if __name__ == "__main__":
    unittest.main()
