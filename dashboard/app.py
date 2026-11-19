#DASHBOARD DO BYTEBANK HARDWARE KEY (STREAMLIT)
#
#COMO RODAR (NA RAIZ DO REPOSITÓRIO, COM A API JÁ RODANDO):
#  .venv/bin/streamlit run dashboard/app.py
#
#LEITURA: CONSULTA O BANCO SQLITE DIRETO, A CADA 2 SEGUNDOS (TELEMETRIA, PIX, COMANDOS, AUDITORIA).
#ESCRITA: TODA AÇÃO PASSA PELA API, QUE É QUEM APLICA AS REGRAS DE NEGÓCIO E GUARDA OS CÓDIGOS NA TABELA HASH.
#O DASHBOARD NUNCA VÊ O CÓDIGO OTP: ELE SÓ EXISTE NA MEMÓRIA DA API E NO DISPLAY DO TOKEN.

import sqlite3
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.db import caminho_banco  # noqa: E402

API_PADRAO = "http://localhost:8000"
DISPOSITIVO = "token-01"
ONLINE_ATE_S = 30

st.set_page_config(page_title="ByteBank Hardware Key", page_icon="🔐", layout="wide")


#---------------------------------------------------------------------------
#ACESSO AO BANCO E À API
#---------------------------------------------------------------------------

def consultar(sql, parametros=()):
    #MODO SOMENTE LEITURA: O DASHBOARD NÃO TEM COMO ALTERAR O BANCO POR ENGANO
    conexao = sqlite3.connect(f"file:{caminho_banco()}?mode=ro", uri=True, timeout=5)
    try:
        return pd.read_sql_query(sql, conexao, params=parametros)
    finally:
        conexao.close()


def chamar_api(metodo, rota, corpo=None):
    try:
        resposta = requests.request(metodo, st.session_state.api + rota, json=corpo, timeout=5)
    except requests.RequestException:
        return None, "API fora do ar. Rode: .venv/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8000"
    try:
        dados = resposta.json()
    except ValueError:
        dados = {}
    if resposta.ok:
        return dados, None
    detalhe = dados.get("detail", resposta.text)
    if isinstance(detalhe, list):
        detalhe = "; ".join(item.get("msg", str(item)) for item in detalhe)
    return None, str(detalhe)


#O DISPLAY DO TOKEN SÓ TEM CARACTERES ASCII: "Atenção" VIRA "Atencao"
def sem_acentos(texto):
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()


def reais(valor):
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


#---------------------------------------------------------------------------
#BARRA LATERAL
#---------------------------------------------------------------------------

if "api" not in st.session_state:
    st.session_state.api = API_PADRAO

with st.sidebar:
    st.header("ByteBank Hardware Key")
    st.caption("Módulo 5 · Token físico MFA para PIX acima de R$ 10.000")
    st.session_state.api = st.text_input("Endereço da API", st.session_state.api)
    estado_api, erro_api = chamar_api("GET", "/")
    if erro_api:
        st.error(erro_api)
    else:
        st.success(f"API no ar · {estado_api['codigos_pendentes']} código(s) aguardando")

if not caminho_banco().exists():
    st.warning("Banco ainda não criado. Ligue a API uma vez: ela cria o banco com a carga de teste.")
    st.stop()


#---------------------------------------------------------------------------
#STATUS DO TOKEN (ATUALIZA SOZINHO A CADA 2 SEGUNDOS)
#---------------------------------------------------------------------------

@st.fragment(run_every="2s")
def painel_token():
    dispositivo = consultar("SELECT ultimo_contato FROM dispositivos WHERE id = ?", (DISPOSITIVO,))
    ultima = consultar(
        "SELECT * FROM telemetria WHERE dispositivo_id = ? ORDER BY id DESC LIMIT 1", (DISPOSITIVO,)
    )

    contato = dispositivo["ultimo_contato"].iloc[0] if not dispositivo.empty else None
    segundos = None
    if contato:
        segundos = int((datetime.now() - datetime.strptime(contato, "%Y-%m-%d %H:%M:%S")).total_seconds())

    online = segundos is not None and segundos <= ONLINE_ATE_S
    colunas = st.columns(5)
    colunas[0].metric("Token", "🟢 Online" if online else "🔴 Offline",
                      f"visto há {segundos}s" if segundos is not None else "nunca visto", delta_color="off")
    if ultima.empty:
        for coluna in colunas[1:]:
            coluna.metric("—", "sem telemetria")
        return

    leitura = ultima.iloc[0]
    colunas[1].metric("Sinal Wi-Fi", f"{leitura['rssi_dbm']} dBm")
    colunas[2].metric("Memória livre", f"{leitura['memoria_livre'] / 1024:.0f} KB")
    colunas[3].metric("Fila offline", int(leitura["fila_offline"]), f"{int(leitura['eventos_descartados'])} descartados",
                      delta_color="off")
    colunas[4].metric("Ligado há", f"{int(leitura['ligado_ha_s']) // 60} min {int(leitura['ligado_ha_s']) % 60} s")


st.title("🔐 ByteBank Hardware Key")
painel_token()

aba_monitor, aba_pix, aba_comandos, aba_auditoria = st.tabs(
    ["📈 Telemetria", "💸 PIX", "📡 Comandos para o ESP32", "📋 Auditoria"]
)


#---------------------------------------------------------------------------
#ABA 1: TELEMETRIA EM TEMPO REAL
#---------------------------------------------------------------------------

@st.fragment(run_every="2s")
def graficos_telemetria():
    leituras = consultar(
        """SELECT recebido_em, rssi_dbm, memoria_livre, fila_offline FROM telemetria
           WHERE dispositivo_id = ? ORDER BY id DESC LIMIT 90""",
        (DISPOSITIVO,),
    )
    if leituras.empty:
        st.info("Nenhuma leitura ainda. O token envia telemetria a cada 10 segundos depois de conectar.")
        return

    leituras = leituras.iloc[::-1].set_index("recebido_em")
    leituras["memoria_livre_kb"] = leituras["memoria_livre"] / 1024
    esquerda, direita = st.columns(2)
    with esquerda:
        st.subheader("Sinal Wi-Fi (dBm)")
        st.line_chart(leituras["rssi_dbm"], height=230)
    with direita:
        st.subheader("Memória livre no ESP32 (KB)")
        st.line_chart(leituras["memoria_livre_kb"], height=230)
    st.subheader("Confirmações guardadas na fila offline")
    st.bar_chart(leituras["fila_offline"], height=160)
    st.caption(f"Últimas {len(leituras)} leituras · atualiza a cada 2 segundos")


with aba_monitor:
    graficos_telemetria()


#---------------------------------------------------------------------------
#ABA 2: PIX (SOLICITAR E DIGITAR O CÓDIGO DO TOKEN)
#---------------------------------------------------------------------------

with aba_pix:
    contas = consultar("SELECT id, titular, chave_pix, saldo, bloqueada, dispositivo_id FROM contas ORDER BY id")
    rotulos = {linha.id: f"{linha.titular} · {reais(linha.saldo)}{' · 🔒 BLOQUEADA' if linha.bloqueada else ''}"
               for linha in contas.itertuples()}

    st.subheader("Solicitar PIX")
    st.caption("Até R$ 10.000 a transferência é imediata. Acima disso, fica retida até a confirmação no token físico.")
    with st.form("novo_pix", clear_on_submit=False):
        origem = st.selectbox("Conta de origem", contas["id"], format_func=lambda conta_id: rotulos[conta_id])
        destinos = contas[contas["id"] != origem]
        chave = st.selectbox("Chave PIX de destino", destinos["chave_pix"])
        valor = st.number_input("Valor (R$)", min_value=0.01, value=15000.00, step=500.00, format="%.2f")
        enviar = st.form_submit_button("Enviar PIX", type="primary")

    if enviar:
        resultado, erro = chamar_api("POST", "/pix", {"conta_origem_id": int(origem), "chave_destino": chave, "valor": valor})
        if erro:
            st.error(erro)
        elif resultado["status"] == "RETIDO":
            st.warning(f"PIX #{resultado['pix_id']} retido. O código apareceu no display do token: "
                       f"digite abaixo e depois aperte o botão físico. Prazo: {resultado['expira_em_s']} s.")
        else:
            st.success(f"PIX #{resultado['pix_id']}: {resultado['status']}")

    st.divider()
    st.subheader("PIX aguardando o token")
    retidos = consultar(
        """SELECT p.id, o.titular AS origem, d.titular AS destino, p.valor, p.expira_em
           FROM transferencias_pix p
           JOIN contas o ON o.id = p.conta_origem_id
           JOIN contas d ON d.id = p.conta_destino_id
           WHERE p.status = 'RETIDO' ORDER BY p.id"""
    )
    if retidos.empty:
        st.caption("Nenhum PIX retido no momento.")

    for pix in retidos.itertuples():
        restante = int((datetime.strptime(pix.expira_em, "%Y-%m-%d %H:%M:%S") - datetime.now()).total_seconds())
        with st.container(border=True):
            st.markdown(f"**PIX #{pix.id}** · {pix.origem} → {pix.destino} · **{reais(pix.valor)}** · "
                        f"{'expira em ' + str(restante) + ' s' if restante > 0 else 'prazo esgotado'}")
            campo, botao = st.columns([3, 1])
            otp = campo.text_input("Código exibido no token", key=f"otp_{pix.id}", max_chars=6,
                                   placeholder="6 dígitos", label_visibility="collapsed")
            if botao.button("Validar código", key=f"validar_{pix.id}", use_container_width=True):
                resultado, erro = chamar_api("POST", f"/pix/{pix.id}/codigo", {"otp": otp.strip()})
                if erro:
                    st.error("Digite os 6 dígitos que aparecem no display." if "pattern" in erro or "String" in erro else erro)
                elif resultado["status"] == "AGUARDANDO_BOTAO":
                    st.success("Código correto. Agora aperte o botão físico do token para liberar o PIX.")
                elif resultado["status"] == "CODIGO_INCORRETO":
                    st.error(f"Código incorreto. Restam {resultado['tentativas_restantes']} tentativa(s) antes do bloqueio.")
                elif resultado["status"] == "BLOQUEADO":
                    st.error("Conta bloqueada por excesso de tentativas. O alarme do token foi acionado.")
                else:
                    st.info(f"Situação do PIX: {resultado['status']}")

    st.divider()

    @st.fragment(run_every="2s")
    def ultimas_transferencias():
        st.subheader("Últimas transferências")
        tabela = consultar(
            """SELECT p.id AS "PIX", o.titular AS "Origem", d.titular AS "Destino", p.valor AS "Valor",
                      p.status AS "Status", p.criado_em AS "Criado em", p.finalizado_em AS "Finalizado em"
               FROM transferencias_pix p
               JOIN contas o ON o.id = p.conta_origem_id
               JOIN contas d ON d.id = p.conta_destino_id
               ORDER BY p.id DESC LIMIT 15"""
        )
        st.dataframe(tabela, hide_index=True, use_container_width=True,
                     column_config={"Valor": st.column_config.NumberColumn(format="R$ %.2f")})

    ultimas_transferencias()


#---------------------------------------------------------------------------
#ABA 3: COMANDOS PARA O ESP32
#---------------------------------------------------------------------------

def enviar_comando(tipo, parametro=None):
    resultado, erro = chamar_api("POST", "/comandos", {"dispositivo_id": DISPOSITIVO, "tipo": tipo, "parametro": parametro})
    if erro:
        st.error(erro)
    else:
        st.success(f"Comando #{resultado['comando_id']} ({tipo}) enviado. O token executa em até 2 segundos.")


with aba_comandos:
    st.caption("O ESP32 consulta a fila de comandos a cada 2 segundos e confirma cada um ao executar.")

    alarme, reiniciar = st.columns(2)
    with alarme:
        st.subheader("🚨 Alarme")
        st.write("Toca o buzzer e mostra o alerta no display.")
        if st.button("Acionar alarme", type="primary", use_container_width=True):
            enviar_comando("ALARME")
    with reiniciar:
        st.subheader("🔄 Reiniciar token")
        st.write("Reinicia o ESP32 remotamente. Ele volta em uns 10 segundos.")
        if st.button("Reiniciar ESP32", use_container_width=True):
            enviar_comando("REINICIAR")

    st.subheader("💬 Mensagem no display")
    with st.form("mensagem", clear_on_submit=True):
        texto = st.text_input("Texto (até 32 caracteres: 16 por linha, sem acentos)", max_chars=32)
        if st.form_submit_button("Enviar mensagem") and texto.strip():
            enviar_comando("MENSAGEM", sem_acentos(texto.strip()))

    st.subheader("🔓 Desbloquear conta")
    bloqueadas = consultar("SELECT id, titular FROM contas WHERE bloqueada = 1")
    if bloqueadas.empty:
        st.caption("Nenhuma conta bloqueada.")
    else:
        conta = st.selectbox("Conta bloqueada", bloqueadas["id"],
                             format_func=lambda conta_id: bloqueadas.set_index("id").loc[conta_id, "titular"])
        if st.button("Desbloquear (gerente)"):
            _, erro = chamar_api("POST", f"/contas/{int(conta)}/desbloquear")
            if erro:
                st.error(erro)
            else:
                st.success("Conta desbloqueada. O token mostra o aviso no display.")
                st.rerun()

    @st.fragment(run_every="2s")
    def historico_comandos():
        st.subheader("Histórico de comandos")
        tabela = consultar(
            """SELECT id AS "Comando", tipo AS "Tipo", parametro AS "Parâmetro", status AS "Status",
                      criado_em AS "Criado em", executado_em AS "Executado em"
               FROM comandos WHERE dispositivo_id = ? ORDER BY id DESC LIMIT 15""",
            (DISPOSITIVO,),
        )
        st.dataframe(tabela, hide_index=True, use_container_width=True)

    historico_comandos()


#---------------------------------------------------------------------------
#ABA 4: AUDITORIA E SALDOS
#---------------------------------------------------------------------------

with aba_auditoria:

    @st.fragment(run_every="2s")
    def auditoria():
        st.subheader("Saldos")
        saldos = consultar(
            """SELECT titular AS "Titular", chave_pix AS "Chave PIX", saldo AS "Saldo",
                      CASE bloqueada WHEN 1 THEN 'BLOQUEADA' ELSE 'ativa' END AS "Situação",
                      COALESCE(dispositivo_id, '—') AS "Token" FROM contas ORDER BY id"""
        )
        st.dataframe(saldos, hide_index=True, use_container_width=True,
                     column_config={"Saldo": st.column_config.NumberColumn(format="R$ %.2f")})

        st.subheader("Log de autenticação")
        logs = consultar(
            """SELECT registrado_em AS "Quando", evento AS "Evento", resultado AS "Resultado",
                      pix_id AS "PIX", detalhe AS "Detalhe" FROM logs_autenticacao ORDER BY id DESC LIMIT 30"""
        )
        st.dataframe(logs, hide_index=True, use_container_width=True)

    auditoria()
