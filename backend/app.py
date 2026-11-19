#API DO BYTEBANK HARDWARE KEY (FASTAPI)
#
#COMO RODAR (NA RAIZ DO REPOSITÓRIO):
#  .venv/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8000
#  DOCUMENTAÇÃO INTERATIVA EM http://localhost:8000/docs
#
#FLUXO DE UM PIX DE ALTO VALOR:
#  1. DASHBOARD -> POST /pix                 PIX ACIMA DE R$ 10.000 NASCE "RETIDO" E GANHA UM CÓDIGO NA TABELA HASH
#  2. ESP32     -> GET  /token/pendente      O TOKEN BUSCA O CÓDIGO E MOSTRA NO DISPLAY
#  3. DASHBOARD -> POST /pix/{id}/codigo     O CLIENTE DIGITA O CÓDIGO QUE ESTÁ NO DISPLAY (BUSCA O(1))
#  4. ESP32     -> POST /token/confirmar     O CLIENTE APERTA O BOTÃO. DÉBITO E CRÉDITO NUMA TRANSAÇÃO ATÔMICA
#  CÓDIGO ERRADO 3 VEZES EM 5 MINUTOS -> CONTA BLOQUEADA E ALARME NO TOKEN

import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from backend import db
from backend.estruturas import FilaTentativas, TabelaTokens

#REGRAS DE NEGÓCIO
LIMITE_SEM_TOKEN = 10_000.00
VALIDADE_CODIGO_S = 60
TELEMETRIA_GUARDADA = 2000

tokens = TabelaTokens()
tentativas = FilaTentativas(limite=3, janela_s=300)

#TABELA HASH chave_pix -> id DA CONTA. CARREGADA AO SUBIR A API: ACHAR O DESTINO DE UM PIX É O(1)
chaves_pix = {}

#AS ESTRUTURAS EM MEMÓRIA SÃO COMPARTILHADAS ENTRE AS REQUISIÇÕES (CADA UMA RODA NUMA THREAD)
trava = threading.Lock()


#RELÓGIO ISOLADO NUMA FUNÇÃO PARA OS TESTES CONSEGUIREM AVANÇAR O TEMPO SEM ESPERAR DE VERDADE
def agora():
    return time.time()


def texto_data(instante):
    return datetime.fromtimestamp(instante).strftime("%Y-%m-%d %H:%M:%S")


def carregar_estado():
    conexao = db.conectar()
    try:
        chaves_pix.clear()
        for linha in conexao.execute("SELECT id, chave_pix FROM contas"):
            chaves_pix[linha["chave_pix"].lower()] = linha["id"]

        #OS CÓDIGOS VIVEM SÓ NA MEMÓRIA. PIX QUE FICOU RETIDO ANTES DE A API REINICIAR NÃO TEM MAIS CÓDIGO
        with db.transacao(conexao):
            conexao.execute(
                "UPDATE transferencias_pix SET status = 'EXPIRADO', finalizado_em = ? WHERE status = 'RETIDO'",
                (texto_data(agora()),),
            )
    finally:
        conexao.close()


@asynccontextmanager
async def ciclo_de_vida(_app):
    db.preparar_banco()
    carregar_estado()
    yield


app = FastAPI(title="ByteBank Hardware Key", version="1.0", lifespan=ciclo_de_vida)


#---------------------------------------------------------------------------
#MODELOS DE ENTRADA (O FASTAPI VALIDA O JSON E RECUSA COM 422 O QUE NÃO BATER)
#---------------------------------------------------------------------------

class PedidoPix(BaseModel):
    conta_origem_id: int
    chave_destino: str = Field(min_length=3, max_length=80)
    valor: float = Field(gt=0, le=1_000_000)


class CodigoDigitado(BaseModel):
    otp: str = Field(pattern=r"^\d{6}$")


class ConfirmacaoBotao(BaseModel):
    dispositivo: str
    pix_id: int
    idade_ms: int = Field(default=0, ge=0, le=600_000)


class LeituraTelemetria(BaseModel):
    dispositivo: str
    rssi_dbm: Optional[int] = None
    memoria_livre: Optional[int] = None
    fila_offline: Optional[int] = None
    eventos_descartados: Optional[int] = None
    ligado_ha_s: Optional[int] = None


class NovoComando(BaseModel):
    dispositivo_id: str
    tipo: Literal["ALARME", "MENSAGEM", "REINICIAR"]
    parametro: Optional[str] = Field(default=None, max_length=32)


#---------------------------------------------------------------------------
#FUNÇÕES DE APOIO
#---------------------------------------------------------------------------

def registrar_log(conexao, evento, resultado, pix_id=None, conta_id=None, dispositivo_id=None, detalhe=None):
    conexao.execute(
        "INSERT INTO logs_autenticacao (pix_id, conta_id, dispositivo_id, evento, resultado, detalhe) VALUES (?, ?, ?, ?, ?, ?)",
        (pix_id, conta_id, dispositivo_id, evento, resultado, detalhe),
    )


def enfileirar_comando(conexao, dispositivo_id, tipo, parametro=None):
    conexao.execute(
        "INSERT INTO comandos (dispositivo_id, tipo, parametro) VALUES (?, ?, ?)",
        (dispositivo_id, tipo, parametro),
    )


def marcar_contato(conexao, dispositivo_id):
    cursor = conexao.execute(
        "UPDATE dispositivos SET ultimo_contato = ? WHERE id = ?",
        (texto_data(agora()), dispositivo_id),
    )
    if cursor.rowcount == 0:
        raise HTTPException(404, f"Dispositivo {dispositivo_id} não cadastrado")


def status_no_banco(conexao, pix_id):
    linha = conexao.execute("SELECT status FROM transferencias_pix WHERE id = ?", (pix_id,)).fetchone()
    return linha["status"] if linha else None


def expirar(conexao, token):
    tokens.remover(token.pix_id)
    with db.transacao(conexao):
        conexao.execute(
            "UPDATE transferencias_pix SET status = 'EXPIRADO', finalizado_em = ? WHERE id = ? AND status = 'RETIDO'",
            (texto_data(agora()), token.pix_id),
        )
        registrar_log(conexao, "CODIGO_EXPIRADO", "EXPIRADO", token.pix_id, token.conta_id, token.dispositivo_id)


#DÉBITO, CRÉDITO, STATUS E LOG. SEMPRE CHAMADA DENTRO DE db.transacao(): OU TUDO É GRAVADO, OU NADA.
#O "saldo >= ?" NO UPDATE CONFERE O SALDO NO MESMO INSTANTE DO DÉBITO: SE OUTRO PIX GASTOU O DINHEIRO
#ENQUANTO ESTE ESPERAVA O TOKEN, NADA É DEBITADO E O PIX É RECUSADO
def mover_dinheiro(conexao, pix_id, origem_id, destino_id, valor, status_final, evento_log, dispositivo_id=None):
    debito = conexao.execute(
        "UPDATE contas SET saldo = saldo - ? WHERE id = ? AND saldo >= ?",
        (valor, origem_id, valor),
    )
    if debito.rowcount == 0:
        conexao.execute(
            "UPDATE transferencias_pix SET status = 'RECUSADO', finalizado_em = ? WHERE id = ?",
            (texto_data(agora()), pix_id),
        )
        registrar_log(conexao, evento_log, "SALDO_INSUFICIENTE", pix_id, origem_id, dispositivo_id)
        return "RECUSADO"

    conexao.execute("UPDATE contas SET saldo = saldo + ? WHERE id = ?", (valor, destino_id))
    conexao.execute(
        "UPDATE transferencias_pix SET status = ?, finalizado_em = ? WHERE id = ?",
        (status_final, texto_data(agora()), pix_id),
    )
    registrar_log(conexao, evento_log, status_final, pix_id, origem_id, dispositivo_id)
    return status_final


def liberar(conexao, token):
    tokens.remover(token.pix_id)
    tentativas.zerar(token.conta_id)
    with db.transacao(conexao):
        pix = conexao.execute(
            "SELECT conta_origem_id, conta_destino_id, valor FROM transferencias_pix WHERE id = ? AND status = 'RETIDO'",
            (token.pix_id,),
        ).fetchone()
        if pix is None:
            return status_no_banco(conexao, token.pix_id) or "DESCONHECIDO"
        return mover_dinheiro(
            conexao, token.pix_id, pix["conta_origem_id"], pix["conta_destino_id"], pix["valor"],
            "LIBERADO", "PIX_LIBERADO", token.dispositivo_id,
        )


def bloquear(conexao, token):
    tokens.remover(token.pix_id)
    with db.transacao(conexao):
        conexao.execute("UPDATE contas SET bloqueada = 1 WHERE id = ?", (token.conta_id,))
        conexao.execute(
            "UPDATE transferencias_pix SET status = 'BLOQUEADO', finalizado_em = ? WHERE id = ?",
            (texto_data(agora()), token.pix_id),
        )
        registrar_log(conexao, "CONTA_BLOQUEADA", "BLOQUEADO", token.pix_id, token.conta_id, token.dispositivo_id,
                      f"{tentativas.limite} códigos errados em {tentativas.janela_s // 60} minutos")
        #O TOKEN ESTÁ MOSTRANDO O CÓDIGO NA TELA: O COMANDO FAZ ELE TOCAR O ALARME E EXIBIR O BLOQUEIO
        enfileirar_comando(conexao, token.dispositivo_id, "RESULTADO", "BLOQUEADO")


#---------------------------------------------------------------------------
#ROTAS
#---------------------------------------------------------------------------

@app.get("/")
def estado_da_api():
    return {"servico": "ByteBank Hardware Key", "codigos_pendentes": len(tokens)}


@app.post("/pix")
def solicitar_pix(pedido: PedidoPix):
    conexao = db.conectar()
    try:
        with trava:
            origem = conexao.execute("SELECT * FROM contas WHERE id = ?", (pedido.conta_origem_id,)).fetchone()
            if origem is None:
                raise HTTPException(404, "Conta de origem não encontrada")
            if origem["bloqueada"]:
                raise HTTPException(423, "Conta bloqueada por excesso de tentativas. Procure o gerente.")

            destino_id = chaves_pix.get(pedido.chave_destino.strip().lower())
            if destino_id is None:
                raise HTTPException(404, "Chave PIX de destino não encontrada")
            if destino_id == origem["id"]:
                raise HTTPException(400, "A chave de destino pertence à própria conta")
            if pedido.valor > origem["saldo"]:
                raise HTTPException(400, f"Saldo insuficiente. Disponível: R$ {origem['saldo']:.2f}")

            valor = round(pedido.valor, 2)

            #ATÉ R$ 10.000: TRANSFERE NA HORA, COMO NO BYTEBANK ORIGINAL
            if valor <= LIMITE_SEM_TOKEN:
                with db.transacao(conexao):
                    cursor = conexao.execute(
                        "INSERT INTO transferencias_pix (conta_origem_id, conta_destino_id, valor, status) VALUES (?, ?, ?, 'RETIDO')",
                        (origem["id"], destino_id, valor),
                    )
                    pix_id = cursor.lastrowid
                    status = mover_dinheiro(conexao, pix_id, origem["id"], destino_id, valor, "CONCLUIDO", "PIX_DIRETO")
                return {"pix_id": pix_id, "status": status}

            #ACIMA DE R$ 10.000: SÓ COM O TOKEN FÍSICO
            if origem["dispositivo_id"] is None:
                raise HTTPException(400, "PIX acima de R$ 10.000 exige um token físico cadastrado na conta")

            criado = agora()
            expira = criado + VALIDADE_CODIGO_S
            with db.transacao(conexao):
                cursor = conexao.execute(
                    "INSERT INTO transferencias_pix (conta_origem_id, conta_destino_id, valor, status, expira_em) VALUES (?, ?, ?, 'RETIDO', ?)",
                    (origem["id"], destino_id, valor, texto_data(expira)),
                )
                pix_id = cursor.lastrowid
                registrar_log(conexao, "PIX_RETIDO", "AGUARDANDO_TOKEN", pix_id, origem["id"], origem["dispositivo_id"])
            tokens.registrar(pix_id, origem["id"], origem["dispositivo_id"], expira)

            #O CÓDIGO NÃO VOLTA NESTA RESPOSTA: ELE SÓ APARECE NO DISPLAY DO TOKEN
            return {"pix_id": pix_id, "status": "RETIDO", "expira_em_s": VALIDADE_CODIGO_S}
    finally:
        conexao.close()


@app.get("/token/pendente")
def pix_pendente(dispositivo: str):
    conexao = db.conectar()
    try:
        with trava:
            marcar_contato(conexao, dispositivo)
            token, vencidos = tokens.proximo_do_dispositivo(dispositivo, agora())
            for vencido in vencidos:
                expirar(conexao, vencido)
            if token is None:
                return {"pendente": None}

            pix = conexao.execute(
                """SELECT p.valor, c.titular FROM transferencias_pix p
                   JOIN contas c ON c.id = p.conta_destino_id WHERE p.id = ?""",
                (token.pix_id,),
            ).fetchone()
            return {"pendente": {
                "pix_id": token.pix_id,
                "otp": token.otp,
                "valor": pix["valor"],
                "destino": pix["titular"].split()[0],
                "expira_em_s": max(1, int(token.expira_em - agora())),
            }}
    finally:
        conexao.close()


@app.post("/pix/{pix_id}/codigo")
def validar_codigo(pix_id: int, entrada: CodigoDigitado):
    conexao = db.conectar()
    try:
        with trava:
            #BUSCA O(1) NA TABELA HASH DE TOKENS
            token = tokens.buscar(pix_id)
            if token is None:
                status = status_no_banco(conexao, pix_id)
                if status is None:
                    raise HTTPException(404, "PIX não encontrado")
                return {"status": status}

            instante = agora()
            if token.expirou(instante):
                expirar(conexao, token)
                return {"status": "EXPIRADO"}

            if not TabelaTokens.codigo_confere(token, entrada.otp):
                falhas = tentativas.registrar_falha(token.conta_id, instante)
                with db.transacao(conexao):
                    registrar_log(conexao, "CODIGO_DIGITADO", "INCORRETO", pix_id, token.conta_id, token.dispositivo_id,
                                  f"falha {falhas} de {tentativas.limite}")
                if falhas >= tentativas.limite:
                    bloquear(conexao, token)
                    return {"status": "BLOQUEADO"}
                return {"status": "CODIGO_INCORRETO", "tentativas_restantes": tentativas.limite - falhas}

            token.codigo_validado = True
            with db.transacao(conexao):
                registrar_log(conexao, "CODIGO_DIGITADO", "CORRETO", pix_id, token.conta_id, token.dispositivo_id)
            return {"status": "AGUARDANDO_BOTAO", "expira_em_s": max(1, int(token.expira_em - instante))}
    finally:
        conexao.close()


@app.post("/token/confirmar")
def confirmar_no_botao(confirmacao: ConfirmacaoBotao):
    conexao = db.conectar()
    try:
        with trava:
            marcar_contato(conexao, confirmacao.dispositivo)

            token = tokens.buscar(confirmacao.pix_id)
            if token is None:
                #REENVIO DA FILA OFFLINE DE UMA CONFIRMAÇÃO JÁ PROCESSADA: DEVOLVE O MESMO RESULTADO
                return {"status": status_no_banco(conexao, confirmacao.pix_id) or "DESCONHECIDO"}
            if token.dispositivo_id != confirmacao.dispositivo:
                raise HTTPException(403, "Este PIX pertence a outro token")

            #O HORÁRIO REAL DO CLIQUE É A CHEGADA MENOS A IDADE INFORMADA PELO ESP32 (FILA OFFLINE)
            momento_clique = agora() - confirmacao.idade_ms / 1000
            if momento_clique > token.expira_em:
                expirar(conexao, token)
                return {"status": "EXPIRADO"}

            #ORDEM DOS FATORES: PRIMEIRO O CÓDIGO NO DASHBOARD (PROVA QUE O CLIENTE VÊ O TOKEN),
            #DEPOIS O BOTÃO (PROVA QUE ELE ESTÁ COM O TOKEN NA MÃO E QUER A TRANSFERÊNCIA)
            if not token.codigo_validado:
                with db.transacao(conexao):
                    registrar_log(conexao, "BOTAO_APERTADO", "AGUARDANDO_CODIGO", token.pix_id, token.conta_id, token.dispositivo_id)
                return {"status": "AGUARDANDO_CODIGO"}

            return {"status": liberar(conexao, token)}
    finally:
        conexao.close()


@app.post("/contas/{conta_id}/desbloquear")
def desbloquear_conta(conta_id: int):
    conexao = db.conectar()
    try:
        with trava:
            conta = conexao.execute("SELECT id, dispositivo_id, bloqueada FROM contas WHERE id = ?", (conta_id,)).fetchone()
            if conta is None:
                raise HTTPException(404, "Conta não encontrada")
            tentativas.zerar(conta_id)
            with db.transacao(conexao):
                conexao.execute("UPDATE contas SET bloqueada = 0 WHERE id = ?", (conta_id,))
                registrar_log(conexao, "CONTA_DESBLOQUEADA", "OK", conta_id=conta_id, dispositivo_id=conta["dispositivo_id"],
                              detalhe="desbloqueio pelo gerente no dashboard")
                if conta["dispositivo_id"]:
                    enfileirar_comando(conexao, conta["dispositivo_id"], "DESBLOQUEADO")
            return {"conta_id": conta_id, "bloqueada": False}
    finally:
        conexao.close()


@app.post("/telemetria")
def receber_telemetria(leitura: LeituraTelemetria):
    conexao = db.conectar()
    try:
        with db.transacao(conexao):
            marcar_contato(conexao, leitura.dispositivo)
            conexao.execute(
                """INSERT INTO telemetria (dispositivo_id, rssi_dbm, memoria_livre, fila_offline, eventos_descartados, ligado_ha_s)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (leitura.dispositivo, leitura.rssi_dbm, leitura.memoria_livre, leitura.fila_offline,
                 leitura.eventos_descartados, leitura.ligado_ha_s),
            )
            #GUARDA SÓ AS LEITURAS MAIS RECENTES PARA O BANCO NÃO CRESCER SEM LIMITE
            conexao.execute(
                """DELETE FROM telemetria WHERE dispositivo_id = ? AND id NOT IN
                   (SELECT id FROM telemetria WHERE dispositivo_id = ? ORDER BY id DESC LIMIT ?)""",
                (leitura.dispositivo, leitura.dispositivo, TELEMETRIA_GUARDADA),
            )
        return {"ok": True}
    finally:
        conexao.close()


@app.post("/comandos")
def criar_comando(comando: NovoComando):
    conexao = db.conectar()
    try:
        existe = conexao.execute("SELECT 1 FROM dispositivos WHERE id = ?", (comando.dispositivo_id,)).fetchone()
        if existe is None:
            raise HTTPException(404, "Dispositivo não cadastrado")
        if comando.tipo == "MENSAGEM" and not comando.parametro:
            raise HTTPException(422, "O comando MENSAGEM precisa do texto em 'parametro'")
        with db.transacao(conexao):
            cursor = conexao.execute(
                "INSERT INTO comandos (dispositivo_id, tipo, parametro) VALUES (?, ?, ?)",
                (comando.dispositivo_id, comando.tipo, comando.parametro),
            )
        return {"comando_id": cursor.lastrowid, "status": "PENDENTE"}
    finally:
        conexao.close()


#O ESP32 NÃO RECEBE CONEXÕES: ELE PERGUNTA A CADA 2 SEGUNDOS SE HÁ COMANDO NA FILA (FIFO)
@app.get("/comandos")
def comandos_pendentes(dispositivo: str):
    conexao = db.conectar()
    try:
        with db.transacao(conexao):
            marcar_contato(conexao, dispositivo)
        linhas = conexao.execute(
            "SELECT id, tipo, parametro FROM comandos WHERE dispositivo_id = ? AND status = 'PENDENTE' ORDER BY id",
            (dispositivo,),
        ).fetchall()
        return {"comandos": [dict(linha) for linha in linhas]}
    finally:
        conexao.close()


@app.post("/comandos/{comando_id}/executado")
def comando_executado(comando_id: int):
    conexao = db.conectar()
    try:
        with db.transacao(conexao):
            cursor = conexao.execute(
                "UPDATE comandos SET status = 'EXECUTADO', executado_em = ? WHERE id = ?",
                (texto_data(agora()), comando_id),
            )
        if cursor.rowcount == 0:
            raise HTTPException(404, "Comando não encontrado")
        return {"comando_id": comando_id, "status": "EXECUTADO"}
    finally:
        conexao.close()
