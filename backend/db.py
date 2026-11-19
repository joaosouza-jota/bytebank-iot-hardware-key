#CONEXÃO COM O BANCO SQLITE
#O ARQUIVO FICA EM database/bytebank.db (FORA DO GIT). A VARIÁVEL BYTEBANK_DB TROCA O CAMINHO,
#O QUE OS TESTES USAM PARA RODAR NUM BANCO TEMPORÁRIO SEM TOCAR NO BANCO DA DEMONSTRAÇÃO.

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PASTA_SQL = RAIZ / "database"


def caminho_banco():
    return Path(os.environ.get("BYTEBANK_DB", PASTA_SQL / "bytebank.db"))


def conectar():
    #isolation_level=None DEIXA AS TRANSAÇÕES EXPLÍCITAS (BEGIN/COMMIT) NAS MÃOS DO CÓDIGO
    conexao = sqlite3.connect(caminho_banco(), timeout=10, isolation_level=None, check_same_thread=False)
    conexao.row_factory = sqlite3.Row
    conexao.execute("PRAGMA foreign_keys = ON")
    #WAL: O DASHBOARD LÊ O BANCO AO MESMO TEMPO EM QUE A API ESCREVE, SEM UM TRAVAR O OUTRO
    conexao.execute("PRAGMA journal_mode = WAL")
    return conexao


#TRANSAÇÃO ATÔMICA: OU TODAS AS ALTERAÇÕES DO BLOCO SÃO GRAVADAS, OU NENHUMA.
#BEGIN IMMEDIATE RESERVA A ESCRITA LOGO NO INÍCIO: DUAS LIBERAÇÕES DO MESMO PIX AO MESMO TEMPO
#NÃO CONSEGUEM LER O SALDO ANTIGO E DEBITAR DUAS VEZES
@contextmanager
def transacao(conexao):
    conexao.execute("BEGIN IMMEDIATE")
    try:
        yield conexao
        conexao.execute("COMMIT")
    except Exception:
        conexao.execute("ROLLBACK")
        raise


#CRIA AS TABELAS E A CARGA DE TESTE SE O BANCO AINDA NÃO EXISTIR (OU SE recriar=True)
def preparar_banco(recriar=False):
    caminho = caminho_banco()
    if caminho.exists() and not recriar:
        return False

    caminho.parent.mkdir(parents=True, exist_ok=True)
    conexao = conectar()
    try:
        conexao.executescript((PASTA_SQL / "schema.sql").read_text(encoding="utf-8"))
        conexao.executescript((PASTA_SQL / "seed.sql").read_text(encoding="utf-8"))
    finally:
        conexao.close()
    return True
