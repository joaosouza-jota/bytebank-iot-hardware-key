#APLICAÇÃO PRINCIPAL DO TOKEN BYTEBANK HARDWARE KEY
#RODA DEPOIS DO boot.py, QUE JÁ TENTOU CONECTAR NO WI-FI.
#
#O QUE ESTE ARQUIVO FAZ EM LOOP:
#1. PERGUNTA AO BACKEND SE EXISTE UM PIX DE ALTO VALOR AGUARDANDO CONFIRMAÇÃO (GET /token/pendente)
#2. SE EXISTIR, MOSTRA O CÓDIGO OTP NO DISPLAY LCD, TOCA O BUZZER E INICIA A CONTAGEM REGRESSIVA
#3. QUANDO O BOTÃO É APERTADO, A CONFIRMAÇÃO ENTRA NA FILA OFFLINE
#4. A FILA É ENVIADA EM JSON VIA urequests.post() (POST /token/confirmar). SE A REDE CAIR, NADA SE PERDE
#5. A CADA 30 SEGUNDOS ENVIA TELEMETRIA DO TOKEN (POST /telemetria)

import gc
import sys
import time
import network
import machine
from machine import Pin, SoftI2C

try:
    import urequests as requests
except ImportError:
    import requests

import config
from fila_offline import FilaOffline

#PINOS (MESMOS DA TABELA DO README)
PINO_BOTAO = 15
PINO_BUZZER = 4
PINO_LED = 2
PINO_SDA = 22
PINO_SCL = 21

#ENDEREÇO I2C DA PLAQUINHA PCF8574 DO DISPLAY (ENCONTRADO COM i2c.scan())
ENDERECO_DISPLAY = 0x27

#INTERVALOS DO LOOP PRINCIPAL EM MILISSEGUNDOS
INTERVALO_CONSULTA = 2000
INTERVALO_TELEMETRIA = 30000
INTERVALO_RECONEXAO = 10000
TEMPO_TELA_RESULTADO = 4000
INTERVALO_MINIMO_BOTAO = 300

#TEMPO MÁXIMO DE ESPERA POR UMA RESPOSTA HTTP, EM SEGUNDOS
TIMEOUT_HTTP = 3

#QUANTAS CONFIRMAÇÕES A FILA OFFLINE GUARDA SEM REDE
CAPACIDADE_FILA = 20

#ESTADOS DO TOKEN
AGUARDANDO = "AGUARDANDO"
EXIBINDO_CODIGO = "EXIBINDO_CODIGO"
EXIBINDO_RESULTADO = "EXIBINDO_RESULTADO"


#---------------------------------------------------------------------------
#HARDWARE
#---------------------------------------------------------------------------

led = Pin(PINO_LED, Pin.OUT)

#BOTÃO COM PULL-UP INTERNO: SOLTO = 1, PRESSIONADO = 0 (IGUAL AO GUIA DA DISCIPLINA)
botao = Pin(PINO_BOTAO, Pin.IN, Pin.PULL_UP)

#BUZZER ATIVO: TEM OSCILADOR PRÓPRIO, ENTÃO BASTA LIGAR (1) E DESLIGAR (0) O PINO
buzzer = Pin(PINO_BUZZER, Pin.OUT, value=0)


#O TOKEN CONTINUA FUNCIONANDO SEM O DISPLAY: AS TELAS SAEM SÓ NO TERMINAL SERIAL
def iniciar_display():
    try:
        from lcd_i2c import LCD
        i2c = SoftI2C(sda=Pin(PINO_SDA), scl=Pin(PINO_SCL), freq=100000)
        return LCD(i2c, ENDERECO_DISPLAY)
    except Exception as erro:
        print(f"[AVISO] Display LCD não encontrado ({erro}). Telas só no terminal.")
        return None


display = iniciar_display()


#O DISPLAY TEM 2 LINHAS DE 16 CARACTERES, SEM ACENTO.
#SÓ REESCREVE QUANDO O TEXTO MUDA: O LCD É LENTO E REESCREVER SEM PARAR FARIA A TELA TREMER
def exibir(linha1, linha2=""):
    linhas = (linha1, linha2)
    if linhas == estado["ultima_tela"]:
        return

    estado["ultima_tela"] = linhas
    print(f"[TELA] {linha1} | {linha2}")

    if display is None:
        return

    try:
        display.escrever_linha(0, linha1)
        display.escrever_linha(1, linha2)
    except OSError as erro:
        print(f"[ERRO] Falha ao escrever no display: {erro}")


def bipar(vezes=1, duracao_ms=120):
    for _ in range(vezes):
        buzzer.value(1)
        time.sleep_ms(duracao_ms)
        buzzer.value(0)
        time.sleep_ms(80)


#DETECTA O MOMENTO EM QUE O BOTÃO É APERTADO (BORDA DE DESCIDA), IGNORANDO O REPIQUE MECÂNICO
def botao_foi_pressionado():
    valor_atual = botao.value()
    pressionou = False

    if valor_atual == 0 and estado["botao_anterior"] == 1:
        if time.ticks_diff(time.ticks_ms(), estado["ultimo_toque"]) > INTERVALO_MINIMO_BOTAO:
            estado["ultimo_toque"] = time.ticks_ms()
            pressionou = True

    estado["botao_anterior"] = valor_atual
    return pressionou


#---------------------------------------------------------------------------
#REDE E HTTP
#---------------------------------------------------------------------------

wlan = network.WLAN(network.STA_IF)


#NÃO BLOQUEIA O LOOP: SÓ PEDE A RECONEXÃO E SEGUE LENDO O BOTÃO
def tentar_reconectar():
    print("[WI-FI] Sem conexão. Tentando reconectar...")
    try:
        wlan.active(True)
        wlan.connect(config.WIFI_SSID, config.WIFI_SENHA)
    except OSError as erro:
        print(f"[ERRO] Falha no rádio Wi-Fi: {erro}")


#TODA RESPOSTA PRECISA SER FECHADA: SEM O close() O ESP32 FICA SEM MEMÓRIA DEPOIS DE ALGUMAS REQUISIÇÕES
def enviar_json(caminho, dados):
    resposta = None
    try:
        resposta = requests.post(config.SERVIDOR_URL + caminho, json=dados, timeout=TIMEOUT_HTTP)
        if 200 <= resposta.status_code < 300:
            return True, resposta.json()
        print(f"[HTTP] {caminho} respondeu {resposta.status_code}")
    except Exception as erro:
        print(f"[HTTP] Falha ao enviar para {caminho}: {erro}")
    finally:
        if resposta is not None:
            resposta.close()
    return False, None


def buscar_json(caminho):
    resposta = None
    try:
        resposta = requests.get(config.SERVIDOR_URL + caminho, timeout=TIMEOUT_HTTP)
        if resposta.status_code == 200:
            return resposta.json()
        print(f"[HTTP] {caminho} respondeu {resposta.status_code}")
    except Exception as erro:
        print(f"[HTTP] Falha ao consultar {caminho}: {erro}")
    finally:
        if resposta is not None:
            resposta.close()
    return None


#---------------------------------------------------------------------------
#LÓGICA DO TOKEN
#---------------------------------------------------------------------------

fila = FilaOffline(CAPACIDADE_FILA)

estado = {
    "modo": AGUARDANDO,
    "pendente": None,
    "prazo_ms": 0,
    "fim_resultado_ms": 0,
    "ultima_tela": None,
    "botao_anterior": 1,
    "ultimo_toque": 0,
}


#R$ 15000.0 -> "15.000" (O MICROPYTHON NÃO TEM SEPARADOR DE MILHAR NO format)
def formatar_reais(valor):
    inteiro = str(int(valor))
    grupos = []
    while len(inteiro) > 3:
        grupos.insert(0, inteiro[-3:])
        inteiro = inteiro[:-3]
    grupos.insert(0, inteiro)
    return ".".join(grupos)


#LINHA 2 MOSTRA A REDE E, SE HOUVER, QUANTAS CONFIRMAÇÕES ESPERAM NA FILA OFFLINE
def tela_aguardando():
    rede = "Aguardando PIX" if wlan.isconnected() else "SEM REDE"
    if not fila.esta_vazia():
        rede = ("Rede OK" if wlan.isconnected() else "SEM REDE") + f" fila:{len(fila)}"
    exibir("BYTEBANK KEY", rede)


def mostrar_resultado(linha1, linha2=""):
    estado["modo"] = EXIBINDO_RESULTADO
    estado["fim_resultado_ms"] = time.ticks_add(time.ticks_ms(), TEMPO_TELA_RESULTADO)
    exibir(linha1, linha2)


#PERGUNTA AO BACKEND SE HÁ UM PIX RETIDO ESPERANDO ESTE TOKEN
def consultar_pendente():
    resposta = buscar_json(f"/token/pendente?dispositivo={config.ID_DISPOSITIVO}")
    if resposta is None or not resposta.get("pendente"):
        return

    pendente = resposta["pendente"]
    estado["pendente"] = pendente
    estado["prazo_ms"] = time.ticks_add(time.ticks_ms(), int(pendente["expira_em_s"]) * 1000)
    estado["modo"] = EXIBINDO_CODIGO
    print(f"[TOKEN] PIX {pendente['pix_id']} aguardando confirmação física")
    bipar(2)


#CONTAGEM REGRESSIVA DO CÓDIGO NA TELA. QUANDO ZERA, O CÓDIGO NÃO VALE MAIS
def atualizar_tela_codigo():
    restante_ms = time.ticks_diff(estado["prazo_ms"], time.ticks_ms())

    if restante_ms <= 0:
        estado["pendente"] = None
        mostrar_resultado("CODIGO EXPIRADO", "PIX cancelado")
        bipar(1, 600)
        return

    #LINHA 1: VALOR E DESTINATÁRIO | LINHA 2: CÓDIGO E SEGUNDOS RESTANTES
    pendente = estado["pendente"]
    exibir(
        f"R${formatar_reais(pendente['valor'])} {pendente['destino']}",
        f"Cod {pendente['otp']} {restante_ms // 1000 + 1:>2}s",
    )


#A CONFIRMAÇÃO SEMPRE PASSA PELA FILA, COM OU SEM REDE. ASSIM EXISTE UM ÚNICO CAMINHO DE ENVIO
def ao_pressionar_botao():
    if estado["modo"] != EXIBINDO_CODIGO:
        print("[BOTAO] Nenhum PIX pendente para confirmar")
        bipar(1, 60)
        return

    pendente = estado["pendente"]
    fila.enfileirar({
        "caminho": "/token/confirmar",
        "pix_id": pendente["pix_id"],
        "criado_ms": time.ticks_ms(),
    })
    estado["pendente"] = None
    print(f"[FILA] Confirmação do PIX {pendente['pix_id']} enfileirada ({len(fila)} na fila)")
    bipar(1, 60)

    esvaziar_fila()

    #SE AINDA SOBROU ALGO NA FILA, O ENVIO FALHOU: AVISA QUE FICOU GUARDADO
    if not fila.esta_vazia():
        mostrar_resultado("SEM CONEXAO", f"Guardado fila:{len(fila)}")


#ENVIA A FILA DO MAIS ANTIGO PARA O MAIS NOVO (FIFO).
#O EVENTO SÓ SAI DA FILA DEPOIS QUE O SERVIDOR RESPONDE: SE A REDE CAIR NO MEIO, ELE CONTINUA LÁ
def esvaziar_fila():
    while not fila.esta_vazia() and wlan.isconnected():
        evento = fila.primeiro()

        #O ESP32 NÃO TEM RELÓGIO SINCRONIZADO. ENVIAMOS HÁ QUANTO TEMPO O BOTÃO FOI APERTADO
        #E O SERVIDOR CALCULA O HORÁRIO REAL: HORA DE CHEGADA - IDADE
        pacote = {
            "dispositivo": config.ID_DISPOSITIVO,
            "pix_id": evento["pix_id"],
            "idade_ms": time.ticks_diff(time.ticks_ms(), evento["criado_ms"]),
        }
        enviado, resposta = enviar_json(evento["caminho"], pacote)

        if not enviado:
            break

        fila.desenfileirar()
        print(f"[FILA] PIX {evento['pix_id']} enviado. Restam {len(fila)} na fila")
        tratar_resultado(resposta)


#MOSTRA O QUE O BACKEND DECIDIU SOBRE A CONFIRMAÇÃO
def tratar_resultado(resposta):
    status = (resposta or {}).get("status", "DESCONHECIDO")

    if status == "LIBERADO":
        mostrar_resultado("PIX LIBERADO", "Transf. feita")
        bipar(2, 80)
    elif status == "EXPIRADO":
        mostrar_resultado("CODIGO EXPIRADO", "PIX cancelado")
        bipar(1, 600)
    elif status == "BLOQUEADO":
        mostrar_resultado("!! BLOQUEADO !!", "Procure gerente")
        bipar(5, 200)
    else:
        mostrar_resultado("Resposta:", status)


#TELEMETRIA É DESCARTÁVEL: SE FALHAR, NÃO ENTRA NA FILA PARA NÃO OCUPAR O LUGAR DAS CONFIRMAÇÕES
def enviar_telemetria():
    try:
        sinal = wlan.status("rssi")
    except Exception:
        sinal = None

    enviar_json("/telemetria", {
        "dispositivo": config.ID_DISPOSITIVO,
        "rssi_dbm": sinal,
        "memoria_livre": gc.mem_free(),
        "fila_offline": len(fila),
        "eventos_descartados": fila.descartados,
        "ligado_ha_s": time.ticks_ms() // 1000,
    })


#---------------------------------------------------------------------------
#LOOP PRINCIPAL
#---------------------------------------------------------------------------

def executar():
    print("[TOKEN] ByteBank Hardware Key iniciado")
    bipar(1, 80)

    agora = time.ticks_ms()
    ultima_consulta = time.ticks_add(agora, -INTERVALO_CONSULTA)
    ultima_telemetria = time.ticks_add(agora, -INTERVALO_TELEMETRIA)
    ultima_reconexao = agora

    while True:
        agora = time.ticks_ms()
        led.value(1 if wlan.isconnected() else 0)

        if botao_foi_pressionado():
            ao_pressionar_botao()

        if wlan.isconnected():
            if time.ticks_diff(agora, ultima_consulta) >= INTERVALO_CONSULTA:
                ultima_consulta = agora
                esvaziar_fila()
                if estado["modo"] == AGUARDANDO:
                    consultar_pendente()

            if time.ticks_diff(agora, ultima_telemetria) >= INTERVALO_TELEMETRIA:
                ultima_telemetria = agora
                enviar_telemetria()
                gc.collect()

        elif time.ticks_diff(agora, ultima_reconexao) >= INTERVALO_RECONEXAO:
            ultima_reconexao = agora
            tentar_reconectar()

        if estado["modo"] == EXIBINDO_CODIGO:
            atualizar_tela_codigo()
        elif estado["modo"] == EXIBINDO_RESULTADO:
            if time.ticks_diff(estado["fim_resultado_ms"], agora) <= 0:
                estado["modo"] = AGUARDANDO
        else:
            tela_aguardando()

        time.sleep_ms(20)


#UM ERRO INESPERADO NÃO PODE TRAVAR O TOKEN NA BANCADA: MOSTRA O ERRO E REINICIA A PLACA.
#O Ctrl+C (OU O BOTÃO STOP DO THONNY/MICROPICO) PARA O PROGRAMA SEM REINICIAR
try:
    executar()
except KeyboardInterrupt:
    buzzer.value(0)
    print("[TOKEN] Interrompido pelo usuário")
except Exception as erro:
    buzzer.value(0)
    sys.print_exception(erro)
    exibir("ERRO INTERNO", "Reiniciando...")
    time.sleep(5)
    machine.reset()
