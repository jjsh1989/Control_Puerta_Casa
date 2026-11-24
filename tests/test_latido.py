"""Latido: si se cae el panel, el servidor o internet de casa, el vigilante del hosting tiene con qué avisar."""
import unittest

from instalacion import Instalacion, comprobar_requisitos


def setUpModule():
    comprobar_requisitos()


class Latido(unittest.TestCase):
    def test_latido_y_vigilante_del_hosting(self):
        inst = Instalacion()
        self.addCleanup(inst.borrar)
        o = inst.ejecutar("latido")
        self.assertIsNone(o["sin_configurar"], "sin página del dominio no se manda nada")
        l = o["latido"]
        self.assertEqual(l["accion"], "latido")
        self.assertEqual(l["token"], "123456:token-de-bot-ficticio")
        self.assertEqual(l["chats"], [111], "solo los administradores reciben el aviso de caída")
        self.assertEqual(len(l["problemas"]), 2, l["problemas"])
        self.assertIn("bot de Telegram", l["problemas"][0], "un proceso interno parado se notifica")
        self.assertIn("no responde", l["problemas"][1], "el servidor web sin responder se notifica")
        self.assertIsNone(o["recien_empezado"], "se da tiempo a que la tarea del hosting se ejecute")
        self.assertIn("no se está ejecutando", o["parado"])
        self.assertIsNone(o["parado_otra_vez"], "el aviso de vigilante parado se manda una sola vez")
        self.assertIn("vuelve a ejecutarse", o["vuelve"])
        self.assertEqual([c for c, _ in o["enviados"]], [111, 111])


if __name__ == "__main__":
    unittest.main()
