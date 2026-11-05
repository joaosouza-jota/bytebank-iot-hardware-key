#TESTES DA FILA OFFLINE DO ESP32
#A FILA NÃO DEPENDE DE HARDWARE, ENTÃO RODA NO PYTHON DO COMPUTADOR:
#  python3 -m unittest discover tests

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "esp32"))

from fila_offline import FilaOffline


class TestFilaOffline(unittest.TestCase):

    def test_fila_nova_esta_vazia(self):
        fila = FilaOffline(3)
        self.assertTrue(fila.esta_vazia())
        self.assertIsNone(fila.primeiro())
        self.assertIsNone(fila.desenfileirar())

    def test_sai_na_ordem_de_chegada(self):
        fila = FilaOffline(3)
        for evento in ["a", "b", "c"]:
            fila.enfileirar(evento)
        self.assertEqual([fila.desenfileirar() for _ in range(3)], ["a", "b", "c"])
        self.assertTrue(fila.esta_vazia())

    def test_primeiro_nao_remove(self):
        fila = FilaOffline(3)
        fila.enfileirar("a")
        self.assertEqual(fila.primeiro(), "a")
        self.assertEqual(len(fila), 1)

    def test_fila_cheia_descarta_o_mais_antigo(self):
        fila = FilaOffline(3)
        for evento in ["a", "b", "c", "d"]:
            fila.enfileirar(evento)
        self.assertTrue(fila.esta_cheia())
        self.assertEqual(fila.descartados, 1)
        self.assertEqual([fila.desenfileirar() for _ in range(3)], ["b", "c", "d"])

    def test_indices_dao_a_volta_no_vetor(self):
        fila = FilaOffline(3)
        for rodada in range(10):
            fila.enfileirar(rodada)
            fila.enfileirar(rodada + 100)
            self.assertEqual(fila.desenfileirar(), rodada)
            self.assertEqual(fila.desenfileirar(), rodada + 100)
        self.assertTrue(fila.esta_vazia())


if __name__ == "__main__":
    unittest.main()
