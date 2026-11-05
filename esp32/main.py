#APLICAÇÃO PRINCIPAL DO TOKEN BYTEBANK HARDWARE KEY
#RODA DEPOIS DO boot.py, QUE JÁ TENTOU CONECTAR NO WI-FI.
#
#O QUE ESTE ARQUIVO FAZ EM LOOP:
#1. PERGUNTA AO BACKEND SE EXISTE UM PIX DE ALTO VALOR AGUARDANDO CONFIRMAÇÃO (GET /token/pendente)
#2. SE EXISTIR, MOSTRA O CÓDIGO OTP NO OLED, TOCA O BUZZER E INICIA A CONTAGEM REGRESSIVA
#3. QUANDO O BOTÃO É APERTADO, A CONFIRMAÇÃO ENTRA NA FILA OFFLINE
#4. A FILA É ENVIADA EM JSON VIA urequests.post() (POST /token/confirmar). SE A REDE CAIR, NADA SE PERDE
#5. A CADA 30 SEGUNDOS ENVIA TELEMETRIA DO TOKEN (POST /telemetria)

import gc
import sys
import time
import network
import machine
from machine import Pin, PWM, I2C

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
PINO_SDA = 21
PINO_SCL = 22

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

#BUZZER NO PWM: A FREQUÊNCIA DEFINE O TOM E O DUTY LIGA (METADE) OU DESLIGA (ZERO) O SOM
buzzer = PWM(Pin(PINO_BUZZER), freq=2000, duty_u16=0)


#O TOKEN CONTINUA FUNCIONANDO SEM O DISPLAY: AS TELAS SAEM SÓ NO TERMINAL SERIAL
def iniciar_oled():
    try:
        from ssd1306 import SSD1306_I2C
        i2c = I2C(0, scl=Pin(PINO_SCL), sda=Pin(PINO_SDA), freq=400000)
        return SSD1306_I2C(128, 64, i2c)
    except Exception as erro:
        print(f"[AVISO] Display OLED não encontrado ({erro}). Telas só no terminal.")
        return None


oled = iniciar_oled()


#O DISPLAY TEM 128x64 PIXELS: CABEM 7 LINHAS DE ATÉ 16 CARACTERES, SEM ACENTO
def exibir(linhas):
    if linhas == estado["ultima_tela"]:
        return

    estado["ultima_tela"] = linhas
    print("[TELA] " + " | ".join(linhas))

    if oled is None:
        return

    try:
        oled.fill(0)
        for numero, linha in enumerate(linhas[:7]):
            oled.text(linha[:16], 0, numero * 9)
        oled.show()
    except OSError as erro:
        print(f"[ERRO] Falha ao escrever no OLED: {erro}")


def bipar(vezes=1, duracao_ms=120, frequencia=2000):
    buzzer.freq(frequencia)
    for _ in range(vezes):
        buzzer.duty_u16(32768)
        time.sleep_ms(duracao_ms)
        buzzer.duty_u16(0)
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


def tela_aguardando():
    rede = "Wi-Fi OK" if wlan.isconnected() else "SEM REDE"
    linhas = ["BYTEBANK KEY", "", "Aguardando PIX", "de alto valor", "", rede]
    if not fila.esta_vazia():
        linhas.append(f"Na fila: {len(fila)}")
    exibir(linhas)


def mostrar_resultado(linhas):
    estado["modo"] = EXIBINDO_RESULTADO
    estado["fim_resultado_ms"] = time.ticks_add(time.ticks_ms(), TEMPO_TELA_RESULTADO)
    exibir(linhas)


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
        mostrar_resultado(["BYTEBANK KEY", "", "CODIGO EXPIRADO", "", "PIX cancelado"])
        bipar(1, 400, 800)
        return

    pendente = estado["pendente"]
    otp = str(pendente["otp"])
    exibir([
        "BYTEBANK KEY",
        f"PIX R$ {formatar_reais(pendente['valor'])}",
        f"p/ {pendente['destino']}",
        f"Codigo: {otp[:3]} {otp[3:]}",
        f"Expira em: {restante_ms // 1000 + 1}s",
        "",
        "Aperte o botao",
    ])


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
        mostrar_resultado(["BYTEBANK KEY", "", "SEM CONEXAO", "Confirmacao", "guardada na fila", "", f"Na fila: {len(fila)}"])


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
        mostrar_resultado(["BYTEBANK KEY", "", "PIX LIBERADO", "", "Transferencia", "concluida"])
        bipar(2, 80, 2500)
    elif status == "EXPIRADO":
        mostrar_resultado(["BYTEBANK KEY", "", "CODIGO EXPIRADO", "", "PIX cancelado"])
        bipar(1, 400, 800)
    elif status == "BLOQUEADO":
        mostrar_resultado(["BYTEBANK KEY", "", "!! BLOQUEADO !!", "", "Procure seu", "gerente"])
        bipar(5, 200, 3000)
    else:
        mostrar_resultado(["BYTEBANK KEY", "", "Resposta:", status[:16]])


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
    buzzer.duty_u16(0)
    print("[TOKEN] Interrompido pelo usuário")
except Exception as erro:
    buzzer.duty_u16(0)
    sys.print_exception(erro)
    exibir(["BYTEBANK KEY", "", "ERRO INTERNO", "Reiniciando..."])
    time.sleep(5)
    machine.reset()
