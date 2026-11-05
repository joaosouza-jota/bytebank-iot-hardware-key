#SERVIDOR DE TESTE DO TOKEN (ENTREGA 2)
#SIMULA O BACKEND PARA VALIDAR A COMUNICAÇÃO COM O ESP32 ANTES DA API REAL EM FASTAPI (ENTREGA 3).
#USA SÓ A BIBLIOTECA PADRÃO DO PYTHON: NÃO PRECISA INSTALAR NADA.
#
#COMO USAR:
#  python3 backend/servidor_teste.py
#  NO NAVEGADOR, ABRA http://localhost:8000/teste/novo-pix PARA CRIAR UM PIX RETIDO.
#  O TOKEN BUSCA O CÓDIGO, EXIBE NO DISPLAY E, AO APERTAR O BOTÃO, ENVIA A CONFIRMAÇÃO.

import json
import random
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORTA = 8000
VALIDADE_CODIGO_S = 60

#PIX RETIDOS AGUARDANDO O TOKEN. TABELA HASH: pix_id -> DADOS DO PIX
pendentes = {}

#RESULTADO DE CADA PIX JÁ DECIDIDO. SE O ESP32 REENVIAR A MESMA CONFIRMAÇÃO
#(RESPOSTA PERDIDA NO CAMINHO), RECEBE O MESMO RESULTADO E NADA É PROCESSADO DUAS VEZES
resultados = {}

proximo_id = 1


def agora_texto():
    return datetime.now().strftime("%H:%M:%S")


def criar_pix_teste():
    global proximo_id
    pix = {
        "pix_id": proximo_id,
        "otp": f"{random.randint(0, 999999):06d}",
        "valor": 15000.0,
        "destino": "Bruno",
        "expira_em": time.time() + VALIDADE_CODIGO_S,
    }
    pendentes[proximo_id] = pix
    proximo_id += 1
    print(f"[{agora_texto()}] PIX {pix['pix_id']} RETIDO | R$ {pix['valor']:.2f} | código {pix['otp']}")
    return pix


#DEVOLVE O PIX PENDENTE MAIS ANTIGO AINDA DENTRO DO PRAZO E MARCA OS VENCIDOS COMO EXPIRADOS
def buscar_pendente_valido():
    for pix_id in list(pendentes):
        pix = pendentes[pix_id]
        if time.time() > pix["expira_em"]:
            del pendentes[pix_id]
            resultados[pix_id] = "EXPIRADO"
            print(f"[{agora_texto()}] PIX {pix_id} EXPIRADO sem confirmação")
            continue
        return pix
    return None


def confirmar(pacote):
    pix_id = pacote.get("pix_id")

    if pix_id in resultados:
        return resultados[pix_id]

    #BUSCA O(1) NA TABELA HASH
    pix = pendentes.pop(pix_id, None)
    if pix is None:
        return "DESCONHECIDO"

    #O HORÁRIO REAL DO CLIQUE É A HORA DE CHEGADA MENOS A IDADE INFORMADA PELO ESP32
    idade_s = pacote.get("idade_ms", 0) / 1000
    momento_clique = time.time() - idade_s

    status = "LIBERADO" if momento_clique <= pix["expira_em"] else "EXPIRADO"
    resultados[pix_id] = status

    clique = (datetime.now() - timedelta(seconds=idade_s)).strftime("%H:%M:%S")
    print(f"[{agora_texto()}] PIX {pix_id} {status} | botão apertado às {clique} (há {idade_s:.1f}s)")
    return status


class Manipulador(BaseHTTPRequestHandler):

    def responder(self, dados, codigo=200):
        corpo = json.dumps(dados).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def ler_json(self):
        tamanho = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(tamanho) or b"{}")
        except json.JSONDecodeError:
            return None

    def do_GET(self):
        url = urlparse(self.path)

        if url.path == "/token/pendente":
            pix = buscar_pendente_valido()
            if pix is None:
                return self.responder({"pendente": None})

            dispositivo = parse_qs(url.query).get("dispositivo", ["?"])[0]
            restante = max(1, int(pix["expira_em"] - time.time()))
            print(f"[{agora_texto()}] Token {dispositivo} buscou o PIX {pix['pix_id']}")
            return self.responder({"pendente": {
                "pix_id": pix["pix_id"],
                "otp": pix["otp"],
                "valor": pix["valor"],
                "destino": pix["destino"],
                "expira_em_s": restante,
            }})

        if url.path == "/teste/novo-pix":
            pix = criar_pix_teste()
            return self.responder({"criado": pix["pix_id"], "otp": pix["otp"]})

        self.responder({"erro": "rota não encontrada"}, 404)

    def do_POST(self):
        pacote = self.ler_json()
        if pacote is None:
            return self.responder({"erro": "JSON inválido"}, 400)

        print(f"[{agora_texto()}] POST {self.path} <- {json.dumps(pacote, ensure_ascii=False)}")

        if self.path == "/token/confirmar":
            return self.responder({"status": confirmar(pacote)})

        if self.path == "/telemetria":
            return self.responder({"ok": True})

        self.responder({"erro": "rota não encontrada"}, 404)

    #SILENCIA O LOG PADRÃO DO http.server: AS MENSAGENS ACIMA JÁ MOSTRAM O QUE IMPORTA
    def log_message(self, formato, *args):
        pass


if __name__ == "__main__":
    servidor = ThreadingHTTPServer(("0.0.0.0", PORTA), Manipulador)
    print(f"Servidor de teste ouvindo na porta {PORTA}. Ctrl+C para encerrar.")
    print(f"Crie um PIX de teste em http://localhost:{PORTA}/teste/novo-pix")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
