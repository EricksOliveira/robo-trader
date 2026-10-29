# Robô trader (estudo)

Robô de cruzamento de médias móveis para a Binance, feito para aprender. Roda por padrão na
**testnet**, com dinheiro fictício.

Estratégia: quando a média dos últimos 9 candles cruza para cima da média dos últimos 21, compra.
Quando cruza para baixo, vende. Os valores ficam em `config.py`.

## Arquivos

| Arquivo | O que faz |
|---|---|
| `config.py` | Par, tempo gráfico, médias, valor por compra, limite de perda diária |
| `strategy.py` | Calcula as médias e marca os sinais de compra e venda |
| `backtest.py` | Testa a estratégia no histórico real da Binance (não precisa de chave) |
| `bot.py` | Robô ao vivo: compra e vende na testnet |
| `binance_client.py` | Conversa com a API da Binance |

## Instalação

```powershell
cd C:\Server\robo-trader
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Nas próximas vezes, basta `cd C:\Server\robo-trader` e `.venv\Scripts\activate`.

## 1. Backtest

```powershell
python backtest.py
python backtest.py --days 730 --interval 4h --fast 20 --slow 50
```

Compare sempre com a linha "Comprar e segurar". Se o robô não ganha de simplesmente comprar e
esperar, a estratégia não está ajudando.

## 2. Testnet

1. Entre em https://testnet.binance.vision com sua conta do GitHub.
2. Gere uma chave **HMAC_SHA256** e copie a Secret Key na hora (ela só aparece uma vez).
3. Copie `.env.example` para `.env` e preencha as duas chaves. O `.env` nunca deve ser
   compartilhado nem enviado para o GitHub.
4. Teste a conexão:

   ```powershell
   python bot.py --check
   ```

5. Inicie o robô (Ctrl+C para parar):

   ```powershell
   python bot.py
   ```

O robô age uma vez a cada candle fechado (de hora em hora, com `INTERVAL = "1h"`). Para ver
operações mais rápido nos testes, use `INTERVAL = "1m"` no `config.py`.

Registros:

- `bot.log`: tudo o que o robô fez;
- `trades.csv`: cada compra e venda, com o resultado (abre no Excel);
- `state.json`: posição atual. Apague só com o robô parado e sem compra aberta.

O limite de perda diária (`MAX_DAILY_LOSS_USDT`) conta apenas vendas já feitas no dia (horário
UTC). Ao atingir o limite, o robô para de comprar até o dia seguinte.

## 3. Dinheiro real (só depois de semanas na testnet)

1. Crie a chave na Binance real com **saque desativado** e restrita ao IP do seu computador.
2. Coloque as chaves reais no `.env`.
3. Em `config.py`, mude `USE_TESTNET = False` e comece com `TRADE_USDT` pequeno.
4. O robô pede para digitar `DINHEIRO REAL` antes de iniciar.

Os preços da testnet não são iguais aos do mercado real. Bons resultados lá não garantem nada
com dinheiro de verdade.
