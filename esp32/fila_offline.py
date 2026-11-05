#FILA OFFLINE DO TOKEN (FILA CIRCULAR / RING BUFFER)
#GUARDA AS CONFIRMAÇÕES DO BOTÃO QUANDO O WI-FI OU O SERVIDOR ESTÃO FORA DO AR.
#QUANDO A CONEXÃO VOLTA, OS EVENTOS SÃO REENVIADOS NA ORDEM EM QUE ACONTECERAM (FIFO).
#
#POR QUE FILA CIRCULAR E NÃO UMA LISTA COMUM:
#- A MEMÓRIA DO ESP32 É PEQUENA. O VETOR É ALOCADO UMA VEZ COM TAMANHO FIXO E NUNCA CRESCE.
#- REMOVER DO INÍCIO DE UMA LISTA (pop(0)) DESLOCA TODOS OS ITENS: O(n).
#  AQUI SÓ O ÍNDICE DO INÍCIO ANDA, ENTÃO ENFILEIRAR E DESENFILEIRAR SÃO O(1).
#- QUANDO A FILA ENCHE, O EVENTO MAIS ANTIGO É DESCARTADO PARA DAR LUGAR AO NOVO.
#  UMA CONFIRMAÇÃO ANTIGA JÁ ESTARIA EXPIRADA NO SERVIDOR (O CÓDIGO VALE 60 SEGUNDOS).


class FilaOffline:

    def __init__(self, capacidade=20):
        self._itens = [None] * capacidade
        self._capacidade = capacidade
        self._inicio = 0
        self._tamanho = 0
        #QUANTOS EVENTOS FORAM PERDIDOS POR FILA CHEIA (ENVIADO NA TELEMETRIA)
        self.descartados = 0

    def __len__(self):
        return self._tamanho

    def esta_vazia(self):
        return self._tamanho == 0

    def esta_cheia(self):
        return self._tamanho == self._capacidade

    #ADICIONA NO FIM DA FILA. SE ESTIVER CHEIA, SOBRESCREVE O MAIS ANTIGO
    def enfileirar(self, item):
        if self.esta_cheia():
            self._itens[self._inicio] = None
            self._inicio = (self._inicio + 1) % self._capacidade
            self._tamanho -= 1
            self.descartados += 1

        fim = (self._inicio + self._tamanho) % self._capacidade
        self._itens[fim] = item
        self._tamanho += 1

    #CONSULTA O PRIMEIRO SEM REMOVER: O EVENTO SÓ SAI DA FILA DEPOIS QUE O SERVIDOR CONFIRMA O RECEBIMENTO
    def primeiro(self):
        if self.esta_vazia():
            return None
        return self._itens[self._inicio]

    #REMOVE E DEVOLVE O PRIMEIRO DA FILA
    def desenfileirar(self):
        if self.esta_vazia():
            return None

        item = self._itens[self._inicio]
        self._itens[self._inicio] = None
        self._inicio = (self._inicio + 1) % self._capacidade
        self._tamanho -= 1
        return item
