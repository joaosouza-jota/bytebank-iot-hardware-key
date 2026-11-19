#ESTRUTURAS DE DADOS EM MEMÓRIA DO BACKEND
#
#TabelaTokens   -> TABELA HASH (DICIONÁRIO) pix_id -> CÓDIGO OTP. VALIDAÇÃO EM O(1).
#FilaTentativas -> UMA FILA (deque) DE HORÁRIOS DE FALHA POR CONTA, COM JANELA DESLIZANTE.
#                  3 FALHAS EM 5 MINUTOS BLOQUEIAM A CONTA (RATE LIMITING).
#
#OS CÓDIGOS FICAM SÓ NA MEMÓRIA, NUNCA NO BANCO: SE ALGUÉM COPIAR O ARQUIVO DO BANCO,
#NÃO LEVA NENHUM CÓDIGO VÁLIDO. O PREÇO É QUE, SE A API REINICIAR, OS PIX RETIDOS
#PERDEM O CÓDIGO; POR ISSO A API MARCA ESSES PIX COMO EXPIRADOS AO SUBIR.

import hmac
import secrets
from collections import deque


class TokenOTP:

    def __init__(self, pix_id, otp, conta_id, dispositivo_id, expira_em):
        self.pix_id = pix_id
        self.otp = otp
        self.conta_id = conta_id
        self.dispositivo_id = dispositivo_id
        self.expira_em = expira_em
        self.codigo_validado = False

    def expirou(self, instante):
        return instante > self.expira_em


class TabelaTokens:

    def __init__(self):
        #TABELA HASH PRINCIPAL: A CHAVE É O ID DO PIX
        self._por_pix = {}
        #SEGUNDA TABELA HASH: DISPOSITIVO -> FILA DE PIX AGUARDANDO AQUELE TOKEN (ORDEM DE CHEGADA)
        self._por_dispositivo = {}

    def __len__(self):
        return len(self._por_pix)

    #CÓDIGO DE 6 DÍGITOS VINDO DO GERADOR CRIPTOGRÁFICO DO SISTEMA (O random COMUM É PREVISÍVEL)
    def registrar(self, pix_id, conta_id, dispositivo_id, expira_em):
        otp = f"{secrets.randbelow(1_000_000):06d}"
        token = TokenOTP(pix_id, otp, conta_id, dispositivo_id, expira_em)
        self._por_pix[pix_id] = token
        self._por_dispositivo.setdefault(dispositivo_id, deque()).append(pix_id)
        return token

    #BUSCA O(1): VAI DIRETO NA POSIÇÃO DA CHAVE, SEM PERCORRER OS OUTROS TOKENS
    def buscar(self, pix_id):
        return self._por_pix.get(pix_id)

    def remover(self, pix_id):
        token = self._por_pix.pop(pix_id, None)
        if token is not None:
            fila = self._por_dispositivo.get(token.dispositivo_id)
            if fila and pix_id in fila:
                fila.remove(pix_id)
        return token

    #COMPARAÇÃO EM TEMPO CONSTANTE: NÃO DEIXA UM ATACANTE DESCOBRIR QUANTOS DÍGITOS ACERTOU PELO TEMPO DE RESPOSTA
    @staticmethod
    def codigo_confere(token, otp):
        return hmac.compare_digest(token.otp, otp)

    #O PIX MAIS ANTIGO AINDA VÁLIDO PARA ESTE TOKEN FÍSICO. OS VENCIDOS SAEM DA FRENTE DA FILA
    #E SÃO DEVOLVIDOS PARA A API MARCAR COMO EXPIRADOS NO BANCO
    def proximo_do_dispositivo(self, dispositivo_id, instante):
        fila = self._por_dispositivo.get(dispositivo_id)
        vencidos = []
        while fila:
            token = self._por_pix.get(fila[0])
            if token is not None and not token.expirou(instante):
                return token, vencidos
            fila.popleft()
            if token is not None:
                del self._por_pix[token.pix_id]
                vencidos.append(token)
        return None, vencidos


class FilaTentativas:

    def __init__(self, limite=3, janela_s=300):
        self.limite = limite
        self.janela_s = janela_s
        #TABELA HASH conta_id -> FILA COM O HORÁRIO DE CADA FALHA
        self._falhas = {}

    #JANELA DESLIZANTE: AS FALHAS MAIS ANTIGAS QUE A JANELA SAEM PELA FRENTE DA FILA (popleft, O(1))
    def _descartar_antigas(self, fila, instante):
        while fila and instante - fila[0] > self.janela_s:
            fila.popleft()

    def registrar_falha(self, conta_id, instante):
        fila = self._falhas.setdefault(conta_id, deque())
        fila.append(instante)
        self._descartar_antigas(fila, instante)
        return len(fila)

    def falhas(self, conta_id, instante):
        fila = self._falhas.get(conta_id)
        if not fila:
            return 0
        self._descartar_antigas(fila, instante)
        return len(fila)

    def atingiu_limite(self, conta_id, instante):
        return self.falhas(conta_id, instante) >= self.limite

    def zerar(self, conta_id):
        self._falhas.pop(conta_id, None)
