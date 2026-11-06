#DRIVER DO DISPLAY LCD 16x2 COM ADAPTADOR I2C (PCF8574)
#O LCD (CONTROLADOR HD44780) TEM 8 PINOS DE DADOS. A PLAQUINHA PCF8574 SOLDADA ATRÁS DELE
#TRANSFORMA ESSES PINOS EM I2C, ENTÃO BASTAM 2 FIOS (SDA E SCL). CADA BYTE ENVIADO PARA O PCF8574
#ACENDE OU APAGA 8 SAÍDAS: 4 DE DADOS (D4 A D7) E 4 DE CONTROLE (RS, RW, ENABLE E LUZ DE FUNDO).
#COMO SÓ HÁ 4 FIOS DE DADOS, CADA CARACTERE VAI EM DUAS METADES DE 4 BITS (MODO 4 BITS).

import time

#BITS DE CONTROLE NO BYTE DO PCF8574
RS = 0x01
ENABLE = 0x04
LUZ_DE_FUNDO = 0x08

#COMANDOS DO CONTROLADOR HD44780
LIMPAR = 0x01
MODO_4_BITS_2_LINHAS = 0x28
LIGAR_SEM_CURSOR = 0x0C
ESCRITA_PARA_DIREITA = 0x06
INICIO_LINHA = (0x80, 0xC0)


class LCD:

    def __init__(self, i2c, endereco=0x27, colunas=16):
        self.i2c = i2c
        self.endereco = endereco
        self.colunas = colunas
        self.luz = LUZ_DE_FUNDO

        self._escrever_byte(self.luz)
        time.sleep_ms(50)
        self.sincronizar()
        self.limpar()

    #SEQUÊNCIA DE INICIALIZAÇÃO DO DATASHEET: ENTRA EM 8 BITS TRÊS VEZES E DEPOIS PASSA PARA 4 BITS.
    #SE UMA METADE DE 4 BITS SE PERDER (RUÍDO DO WI-FI, MAU CONTATO), O LCD PASSA A JUNTAR METADES
    #ERRADAS E TODO O TEXTO SAI EMBARALHADO. REPETIR ESTA SEQUÊNCIA RECOLOCA O LCD NO COMPASSO
    #SEM APAGAR A TELA, ENTÃO O main.py CHAMA ANTES DE CADA TROCA DE TELA
    def sincronizar(self):
        for _ in range(3):
            self._enviar_metade(0x03, 0)
            time.sleep_ms(5)
        self._enviar_metade(0x02, 0)

        for comando in (MODO_4_BITS_2_LINHAS, LIGAR_SEM_CURSOR, ESCRITA_PARA_DIREITA):
            self._comando(comando)

    def _escrever_byte(self, valor):
        self.i2c.writeto(self.endereco, bytes([valor]))

    #O LCD SÓ LÊ OS DADOS NO PULSO DO PINO ENABLE (LIGA E DESLIGA)
    def _enviar_metade(self, quatro_bits, modo):
        valor = (quatro_bits << 4) | self.luz | modo
        self._escrever_byte(valor | ENABLE)
        time.sleep_us(1)
        self._escrever_byte(valor)
        time.sleep_us(100)

    def _enviar(self, valor, modo):
        self._enviar_metade(valor >> 4, modo)
        self._enviar_metade(valor & 0x0F, modo)

    def _comando(self, comando):
        self._enviar(comando, 0)
        time.sleep_ms(2)

    def limpar(self):
        self._comando(LIMPAR)

    #ESCREVE UMA LINHA INTEIRA, COMPLETANDO COM ESPAÇOS PARA APAGAR O TEXTO ANTERIOR SEM PISCAR A TELA.
    #O LCD NÃO TEM ACENTOS: OS TEXTOS DO TOKEN SÃO ESCRITOS SEM ELES
    def escrever_linha(self, numero, texto):
        self._comando(INICIO_LINHA[numero])
        texto = (texto + " " * self.colunas)[:self.colunas]
        for caractere in texto:
            self._enviar(ord(caractere), RS)
