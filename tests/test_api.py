#TESTES DE PONTA A PONTA DA API (SEM HARDWARE)
#CADA TESTE FAZ O PAPEL DO DASHBOARD E DO ESP32, CHAMANDO AS MESMAS ROTAS QUE ELES CHAMAM,
#NUM BANCO SQLITE TEMPORÁRIO. O RELÓGIO DA API É SUBSTITUÍDO PARA TESTAR A EXPIRAÇÃO SEM ESPERAR 60 SEGUNDOS.
#  .venv/bin/python -m unittest discover tests

import os
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from backend import app as api
from backend.estruturas import FilaTentativas, TabelaTokens

ANA, BRUNO, CARLA = 1, 2, 3
TOKEN = "token-01"


class RelogioFalso:

    def __init__(self):
        self.instante = 1_800_000_000.0

    def __call__(self):
        return self.instante

    def avancar(self, segundos):
        self.instante += segundos


class TestApi(unittest.TestCase):

    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory()
        self.banco = os.path.join(self.pasta.name, "teste.db")
        os.environ["BYTEBANK_DB"] = self.banco

        self.relogio = RelogioFalso()
        self.agora_original = api.agora
        api.agora = self.relogio
        api.tokens = TabelaTokens()
        api.tentativas = FilaTentativas(limite=3, janela_s=300)

        self.cliente = TestClient(api.app)
        self.cliente.__enter__()

    def tearDown(self):
        self.cliente.__exit__(None, None, None)
        api.agora = self.agora_original
        del os.environ["BYTEBANK_DB"]
        self.pasta.cleanup()

    #---- APOIO ----

    def consultar(self, sql, *parametros):
        conexao = sqlite3.connect(self.banco)
        try:
            return conexao.execute(sql, parametros).fetchall()
        finally:
            conexao.close()

    def saldo(self, conta_id):
        return self.consultar("SELECT saldo FROM contas WHERE id = ?", conta_id)[0][0]

    def status_pix(self, pix_id):
        return self.consultar("SELECT status FROM transferencias_pix WHERE id = ?", pix_id)[0][0]

    def pedir_pix(self, valor, origem=ANA, chave="bruno@bytebank.com"):
        return self.cliente.post("/pix", json={"conta_origem_id": origem, "chave_destino": chave, "valor": valor})

    def codigo_no_token(self):
        resposta = self.cliente.get("/token/pendente", params={"dispositivo": TOKEN}).json()
        return resposta["pendente"]

    def comandos_do_token(self):
        return self.cliente.get("/comandos", params={"dispositivo": TOKEN}).json()["comandos"]

    #---- PIX ATÉ R$ 10.000 ----

    def test_pix_ate_o_limite_transfere_na_hora(self):
        resposta = self.pedir_pix(10_000)
        self.assertEqual(resposta.json()["status"], "CONCLUIDO")
        self.assertEqual(self.saldo(ANA), 40_000)
        self.assertEqual(self.saldo(BRUNO), 13_200)
        self.assertIsNone(self.codigo_no_token())

    def test_validacoes_do_pedido(self):
        self.assertEqual(self.pedir_pix(100, chave="ninguem@x.com").status_code, 404)
        self.assertEqual(self.pedir_pix(100, chave="ana@bytebank.com").status_code, 400)
        self.assertEqual(self.pedir_pix(999_999).status_code, 400)
        self.assertEqual(self.pedir_pix(-5).status_code, 422)
        #A CARLA NÃO TEM TOKEN FÍSICO: NÃO PODE FAZER PIX ACIMA DE R$ 10.000
        self.assertEqual(self.pedir_pix(15_000, origem=CARLA).status_code, 400)

    #---- PIX ACIMA DE R$ 10.000 ----

    def test_fluxo_completo_codigo_e_botao_libera_o_pix(self):
        resposta = self.pedir_pix(15_000).json()
        pix_id = resposta["pix_id"]
        self.assertEqual(resposta["status"], "RETIDO")
        self.assertNotIn("otp", resposta)
        self.assertEqual(self.saldo(ANA), 50_000)

        pendente = self.codigo_no_token()
        self.assertEqual(pendente["pix_id"], pix_id)
        self.assertEqual(pendente["destino"], "Bruno")

        validacao = self.cliente.post(f"/pix/{pix_id}/codigo", json={"otp": pendente["otp"]}).json()
        self.assertEqual(validacao["status"], "AGUARDANDO_BOTAO")

        botao = self.cliente.post("/token/confirmar", json={"dispositivo": TOKEN, "pix_id": pix_id, "idade_ms": 150}).json()
        self.assertEqual(botao["status"], "LIBERADO")
        self.assertEqual(self.saldo(ANA), 35_000)
        self.assertEqual(self.saldo(BRUNO), 18_200)
        self.assertEqual(self.status_pix(pix_id), "LIBERADO")
        self.assertIsNone(self.codigo_no_token())

    def test_botao_antes_do_codigo_nao_libera(self):
        pix_id = self.pedir_pix(15_000).json()["pix_id"]
        pendente = self.codigo_no_token()

        botao = self.cliente.post("/token/confirmar", json={"dispositivo": TOKEN, "pix_id": pix_id}).json()
        self.assertEqual(botao["status"], "AGUARDANDO_CODIGO")
        self.assertEqual(self.saldo(ANA), 50_000)

        #O TOKEN CONTINUA MOSTRANDO O MESMO CÓDIGO
        self.assertEqual(self.codigo_no_token()["otp"], pendente["otp"])

    def test_reenvio_da_fila_offline_nao_debita_duas_vezes(self):
        pix_id = self.pedir_pix(15_000).json()["pix_id"]
        self.cliente.post(f"/pix/{pix_id}/codigo", json={"otp": self.codigo_no_token()["otp"]})
        confirmacao = {"dispositivo": TOKEN, "pix_id": pix_id, "idade_ms": 100}
        self.assertEqual(self.cliente.post("/token/confirmar", json=confirmacao).json()["status"], "LIBERADO")
        self.assertEqual(self.cliente.post("/token/confirmar", json=confirmacao).json()["status"], "LIBERADO")
        self.assertEqual(self.saldo(ANA), 35_000)

    def test_codigo_expira_em_60_segundos(self):
        pix_id = self.pedir_pix(15_000).json()["pix_id"]
        otp = self.codigo_no_token()["otp"]
        self.relogio.avancar(61)

        resposta = self.cliente.post(f"/pix/{pix_id}/codigo", json={"otp": otp}).json()
        self.assertEqual(resposta["status"], "EXPIRADO")
        self.assertEqual(self.status_pix(pix_id), "EXPIRADO")
        self.assertEqual(self.saldo(ANA), 50_000)

    def test_clique_dentro_do_prazo_que_chegou_atrasado_pela_fila_vale(self):
        pix_id = self.pedir_pix(15_000).json()["pix_id"]
        self.cliente.post(f"/pix/{pix_id}/codigo", json={"otp": self.codigo_no_token()["otp"]})
        self.relogio.avancar(65)

        #O BOTÃO FOI APERTADO HÁ 10 S (NO SEGUNDO 55), MAS A REDE CAIU E O PACOTE SÓ CHEGOU AGORA
        resposta = self.cliente.post("/token/confirmar", json={"dispositivo": TOKEN, "pix_id": pix_id, "idade_ms": 10_000}).json()
        self.assertEqual(resposta["status"], "LIBERADO")

    #---- RATE LIMITING E BLOQUEIO ----

    def test_tres_codigos_errados_bloqueiam_a_conta_e_disparam_o_alarme(self):
        pix_id = self.pedir_pix(15_000).json()["pix_id"]
        otp = self.codigo_no_token()["otp"]
        errado = "000000" if otp != "000000" else "111111"

        primeira = self.cliente.post(f"/pix/{pix_id}/codigo", json={"otp": errado}).json()
        self.assertEqual(primeira, {"status": "CODIGO_INCORRETO", "tentativas_restantes": 2})
        self.cliente.post(f"/pix/{pix_id}/codigo", json={"otp": errado})
        terceira = self.cliente.post(f"/pix/{pix_id}/codigo", json={"otp": errado}).json()
        self.assertEqual(terceira["status"], "BLOQUEADO")

        self.assertEqual(self.status_pix(pix_id), "BLOQUEADO")
        self.assertEqual(self.consultar("SELECT bloqueada FROM contas WHERE id = ?", ANA)[0][0], 1)
        self.assertEqual(self.pedir_pix(100).status_code, 423)

        comandos = self.comandos_do_token()
        self.assertEqual([(c["tipo"], c["parametro"]) for c in comandos], [("RESULTADO", "BLOQUEADO")])

        #O CÓDIGO CERTO DEPOIS DO BLOQUEIO NÃO SERVE MAIS
        self.assertEqual(self.cliente.post(f"/pix/{pix_id}/codigo", json={"otp": otp}).json()["status"], "BLOQUEADO")

    def test_falhas_antigas_saem_da_janela_deslizante(self):
        pix_id = self.pedir_pix(15_000).json()["pix_id"]
        otp = self.codigo_no_token()["otp"]
        errado = "000000" if otp != "000000" else "111111"
        self.cliente.post(f"/pix/{pix_id}/codigo", json={"otp": errado})
        self.cliente.post(f"/pix/{pix_id}/codigo", json={"otp": errado})

        #DEPOIS DE 5 MINUTOS AS DUAS FALHAS SAEM DA FILA; O PIX ANTIGO EXPIROU, ENTÃO PEDE OUTRO
        self.relogio.avancar(301)
        novo = self.pedir_pix(15_000).json()["pix_id"]
        resposta = self.cliente.post(f"/pix/{novo}/codigo", json={"otp": "000000" if self.codigo_no_token()["otp"] != "000000" else "111111"}).json()
        self.assertEqual(resposta["tentativas_restantes"], 2)

    def test_desbloqueio_pelo_gerente_avisa_o_token(self):
        api.tentativas.registrar_falha(ANA, self.relogio())
        conexao = sqlite3.connect(self.banco)
        conexao.execute("UPDATE contas SET bloqueada = 1 WHERE id = 1")
        conexao.commit()
        conexao.close()

        self.assertEqual(self.cliente.post(f"/contas/{ANA}/desbloquear").status_code, 200)
        self.assertEqual(self.consultar("SELECT bloqueada FROM contas WHERE id = ?", ANA)[0][0], 0)
        self.assertEqual(api.tentativas.falhas(ANA, self.relogio()), 0)
        self.assertEqual([c["tipo"] for c in self.comandos_do_token()], ["DESBLOQUEADO"])

    #---- TELEMETRIA E COMANDOS ----

    def test_telemetria_e_gravada_e_atualiza_o_ultimo_contato(self):
        leitura = {"dispositivo": TOKEN, "rssi_dbm": -40, "memoria_livre": 100_000, "fila_offline": 0,
                   "eventos_descartados": 0, "ligado_ha_s": 12}
        self.assertEqual(self.cliente.post("/telemetria", json=leitura).status_code, 200)
        self.assertEqual(self.consultar("SELECT rssi_dbm, memoria_livre FROM telemetria"), [(-40, 100_000)])
        self.assertIsNotNone(self.consultar("SELECT ultimo_contato FROM dispositivos WHERE id = ?", TOKEN)[0][0])

        desconhecido = dict(leitura, dispositivo="token-99")
        self.assertEqual(self.cliente.post("/telemetria", json=desconhecido).status_code, 404)

    def test_comando_do_dashboard_chega_ao_token_uma_unica_vez(self):
        criado = self.cliente.post("/comandos", json={"dispositivo_id": TOKEN, "tipo": "MENSAGEM", "parametro": "Ola equipe"}).json()
        comandos = self.comandos_do_token()
        self.assertEqual(comandos, [{"id": criado["comando_id"], "tipo": "MENSAGEM", "parametro": "Ola equipe"}])

        self.cliente.post(f"/comandos/{criado['comando_id']}/executado")
        self.assertEqual(self.comandos_do_token(), [])
        self.assertEqual(self.consultar("SELECT status FROM comandos")[0][0], "EXECUTADO")

    def test_comandos_invalidos_sao_recusados(self):
        self.assertEqual(self.cliente.post("/comandos", json={"dispositivo_id": TOKEN, "tipo": "MENSAGEM"}).status_code, 422)
        self.assertEqual(self.cliente.post("/comandos", json={"dispositivo_id": TOKEN, "tipo": "FORMATAR"}).status_code, 422)
        self.assertEqual(self.cliente.post("/comandos", json={"dispositivo_id": "token-99", "tipo": "ALARME"}).status_code, 404)

    def test_pix_retido_antes_de_reiniciar_a_api_vira_expirado(self):
        pix_id = self.pedir_pix(15_000).json()["pix_id"]
        self.cliente.__exit__(None, None, None)
        api.tokens = TabelaTokens()
        self.cliente = TestClient(api.app)
        self.cliente.__enter__()
        self.assertEqual(self.status_pix(pix_id), "EXPIRADO")


if __name__ == "__main__":
    unittest.main()
