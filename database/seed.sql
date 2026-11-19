-- CARGA INICIAL DE TESTE: UM TOKEN FÍSICO E QUATRO CONTAS.
-- A ANA É A TITULAR DO TOKEN: TODO PIX DELA ACIMA DE R$ 10.000 PRECISA DA CONFIRMAÇÃO NO ESP32.

INSERT INTO dispositivos (id, descricao) VALUES
    ('token-01', 'ESP32 da bancada - ByteBank Hardware Key');

INSERT INTO contas (titular, cpf, chave_pix, saldo, dispositivo_id) VALUES
    ('Ana Souza',      '11111111111', 'ana@bytebank.com',     50000.00, 'token-01'),
    ('Bruno Lima',     '22222222222', 'bruno@bytebank.com',    3200.00, NULL),
    ('Carla Mendes',   '33333333333', 'carla@bytebank.com',   18000.00, NULL),
    ('Diego Ferreira', '44444444444', 'diego@bytebank.com',     750.00, NULL);
