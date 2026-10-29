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
| `app.py` e `ui/` | Painel visual: gráfico ao vivo, saldo, posição e operações |

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

## Painel visual

```powershell
python app.py
```

Abre uma janela com o gráfico de candles ao vivo, as duas médias, setas em cada compra (C) e
venda (V) feita na conta, o saldo, as ordens abertas, a posição do robô e o histórico. Os botões
no topo mudam o tempo gráfico só do gráfico. O robô continua usando o `INTERVAL` do `config.py`.

O robô e o painel são programas separados: rode o `bot.py` em outra janela do PowerShell.

### Ordem manual

- **Comprar**: compra o valor em USDT a mercado. Se preencher stop loss e/ou stop gain (em %),
  logo depois da compra ele registra a venda automática na Binance, calculada sobre o preço pago:
  - os dois preenchidos: ordem OCO (quando uma executa, a outra é cancelada);
  - só stop loss: vende a mercado se o preço cair até o limite;
  - só stop gain: ordem de venda no preço alvo.
- **Vender**: vende o valor em USDT a mercado, sem stops.
- Em **Ordens abertas**, "Cancelar" remove os stops (a moeda continua na conta) e "Vender agora"
  remove os stops e vende a mercado.

Os stops ficam na Binance e funcionam com o painel fechado. As compras manuais não entram na
"Posição do robô": o robô só cuida do que ele mesmo comprou. A Binance aceita no máximo 5 ordens
de stop abertas por par.

Precisa de internet (o gráfico é carregado da unpkg.com) e do Microsoft Edge WebView2, que já vem
no Windows 10 e 11.

## 3. Dinheiro real (só depois de semanas na testnet)

1. Crie a chave na Binance real com **saque desativado** e restrita ao IP do seu computador.
2. Coloque as chaves reais no `.env`.
3. Em `config.py`, mude `USE_TESTNET = False` e comece com `TRADE_USDT` pequeno.
4. O robô pede para digitar `DINHEIRO REAL` antes de iniciar.

Os preços da testnet não são iguais aos do mercado real. Bons resultados lá não garantem nada
com dinheiro de verdade.
