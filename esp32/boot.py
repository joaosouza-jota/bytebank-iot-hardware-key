#BOOT DO TOKEN BYTEBANK HARDWARE KEY
#O MICROPYTHON EXECUTA ESTE ARQUIVO AUTOMATICAMENTE AO LIGAR A PLACA, ANTES DO main.py.
#AQUI SÓ CONECTAMOS NO WI-FI. SE A CONEXÃO FALHAR, A PLACA SEGUE PARA O main.py MESMO ASSIM,
#QUE VAI GUARDAR OS EVENTOS NA FILA OFFLINE ATÉ A REDE VOLTAR.

import network
import time
from machine import Pin

#LED AZUL DA PLACA: PISCA ENQUANTO CONECTA, FICA ACESO QUANDO CONECTADO
led = Pin(2, Pin.OUT)

#TEMPO MÁXIMO DE ESPERA POR TENTATIVA E QUANTIDADE DE TENTATIVAS
TIMEOUT_SEGUNDOS = 15
MAX_TENTATIVAS = 3


#LÊ AS CREDENCIAIS DO config.py, QUE FICA SÓ NA PLACA E FORA DO GIT
def carregar_config():
    try:
        import config
        return config.WIFI_SSID, config.WIFI_SENHA
    except ImportError:
        print("[ERRO] config.py não encontrado na placa. Copie o config_exemplo.py e preencha.")
        return None, None


#TENTA CONECTAR NA REDE ATÉ O TIMEOUT, PISCANDO O LED ENQUANTO ESPERA
def conectar_wifi(ssid, senha):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if wlan.isconnected():
        return wlan

    for tentativa in range(1, MAX_TENTATIVAS + 1):
        print(f"[WI-FI] Conectando em '{ssid}' (tentativa {tentativa}/{MAX_TENTATIVAS})...")

        try:
            wlan.connect(ssid, senha)
        except OSError as erro:
            print(f"[ERRO] Falha no rádio Wi-Fi: {erro}")

        inicio = time.time()

        while not wlan.isconnected() and time.time() - inicio < TIMEOUT_SEGUNDOS:
            led.value(not led.value())
            time.sleep(0.25)

        if wlan.isconnected():
            return wlan

        #DESCONECTA ANTES DE TENTAR DE NOVO PARA LIMPAR O ESTADO DO RÁDIO
        wlan.disconnect()
        time.sleep(1)

    return None


ssid, senha = carregar_config()

if ssid is not None:
    wlan = conectar_wifi(ssid, senha)

    if wlan is not None:
        led.value(1)
        ip, mascara, gateway, dns = wlan.ifconfig()
        print(f"[SUCESSO] Conectado. IP do token: {ip} | Gateway: {gateway}")
    else:
        led.value(0)
        print("[AVISO] Não foi possível conectar. O token vai operar offline.")
        print("[DICA] Confira se a rede é 2,4 GHz e se o nome e a senha estão corretos.")
