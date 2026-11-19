-- MODELO RELACIONAL DO BYTEBANK HARDWARE KEY (SQLITE)
-- CRIA AS TABELAS DO ZERO. A CARGA INICIAL DE TESTE FICA EM seed.sql.
--
-- contas                 -> TITULARES, SALDOS E CHAVES PIX
-- transferencias_pix     -> TODO PIX SOLICITADO. ACIMA DE R$ 10.000 NASCE "RETIDO" E SÓ SAI COM O TOKEN
-- logs_autenticacao      -> AUDITORIA DE CADA TENTATIVA DE CÓDIGO E DE CADA CONFIRMAÇÃO NO BOTÃO
-- dispositivos           -> TOKENS FÍSICOS CADASTRADOS E A ÚLTIMA VEZ QUE CADA UM FOI VISTO
-- telemetria             -> LEITURAS PERIÓDICAS ENVIADAS PELO ESP32 (SINAL, MEMÓRIA, FILA OFFLINE)
-- comandos               -> FILA DE COMANDOS PARA O ESP32: DO DASHBOARD (ALARME, MENSAGEM, REINICIAR)
--                           E DA PRÓPRIA API (RESULTADO DE UM PIX, AVISO DE DESBLOQUEIO)

PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS comandos;
DROP TABLE IF EXISTS telemetria;
DROP TABLE IF EXISTS logs_autenticacao;
DROP TABLE IF EXISTS transferencias_pix;
DROP TABLE IF EXISTS contas;
DROP TABLE IF EXISTS dispositivos;

CREATE TABLE dispositivos (
    id              TEXT PRIMARY KEY,
    descricao       TEXT NOT NULL,
    ultimo_contato  TEXT
);

CREATE TABLE contas (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    titular         TEXT NOT NULL,
    cpf             TEXT NOT NULL UNIQUE,
    chave_pix       TEXT NOT NULL UNIQUE,
    saldo           REAL NOT NULL DEFAULT 0 CHECK (saldo >= 0),
    dispositivo_id  TEXT REFERENCES dispositivos(id),
    bloqueada       INTEGER NOT NULL DEFAULT 0 CHECK (bloqueada IN (0, 1)),
    criada_em       TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE transferencias_pix (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    conta_origem_id   INTEGER NOT NULL REFERENCES contas(id),
    conta_destino_id  INTEGER NOT NULL REFERENCES contas(id),
    valor             REAL NOT NULL CHECK (valor > 0),
    status            TEXT NOT NULL CHECK (status IN ('CONCLUIDO', 'RETIDO', 'LIBERADO', 'EXPIRADO', 'BLOQUEADO', 'RECUSADO')),
    criado_em         TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    expira_em         TEXT,
    finalizado_em     TEXT,
    CHECK (conta_origem_id <> conta_destino_id)
);

CREATE TABLE logs_autenticacao (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pix_id          INTEGER REFERENCES transferencias_pix(id),
    conta_id        INTEGER REFERENCES contas(id),
    dispositivo_id  TEXT REFERENCES dispositivos(id),
    evento          TEXT NOT NULL,
    resultado       TEXT NOT NULL,
    detalhe         TEXT,
    registrado_em   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE telemetria (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    dispositivo_id       TEXT NOT NULL REFERENCES dispositivos(id),
    rssi_dbm             INTEGER,
    memoria_livre        INTEGER,
    fila_offline         INTEGER,
    eventos_descartados  INTEGER,
    ligado_ha_s          INTEGER,
    recebido_em          TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE comandos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dispositivo_id  TEXT NOT NULL REFERENCES dispositivos(id),
    tipo            TEXT NOT NULL CHECK (tipo IN ('ALARME', 'MENSAGEM', 'REINICIAR', 'DESBLOQUEADO', 'RESULTADO')),
    parametro       TEXT,
    status          TEXT NOT NULL DEFAULT 'PENDENTE' CHECK (status IN ('PENDENTE', 'EXECUTADO')),
    criado_em       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    executado_em    TEXT
);

-- ÍNDICES DAS CONSULTAS MAIS FREQUENTES: O TOKEN PERGUNTA A CADA 2 SEGUNDOS POR PIX RETIDO E POR COMANDOS
CREATE INDEX idx_pix_status ON transferencias_pix(status);
CREATE INDEX idx_comandos_pendentes ON comandos(dispositivo_id, status);
CREATE INDEX idx_telemetria_dispositivo ON telemetria(dispositivo_id, recebido_em);
