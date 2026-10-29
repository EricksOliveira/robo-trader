# Robô trader (estudo)

Robô de cruzamento de médias móveis para a Binance, feito para aprender. Roda por padrão na
**testnet**, com dinheiro fictício.

Estratégia: quando a média dos últimos 9 candles cruza para cima da média dos últimos 21, compra.
Quando cruza para baixo, vende. Os valores padrão ficam em `config.py`. O que for mudado pelo
painel fica salvo em `settings.json` e tem prioridade.

## Arquivos

| Arquivo | O que faz |
|---|---|
| `config.py` | Par, testnet ou real, e os valores padrão do robô |
| `strategy.py` | Calcula as médias e marca os sinais de compra e venda |
| `backtest.py` | Testa a estratégia no histórico real da Binance (não precisa de chave) |
| `bot.py` | Robô ao vivo: compra e vende na testnet |
| `binance_client.py` | Conversa com a API da Binance |
| `news.py` | Calendário de notícias econômicas (ForexFactory) |
| `app.py` e `ui/` | Painel visual: gráfico ao vivo, saldo, posição e operações |
| `build.ps1` | Gera o `RoboTrader.exe` |

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

5. Inicie o robô pelo painel (aba **Robô**, veja abaixo) ou pelo PowerShell (Ctrl+C para parar):

   ```powershell
   python bot.py
   ```

Só um robô roda por vez: se ele já estiver ligado no painel, o `bot.py` se recusa a iniciar, e
vice-versa.

O robô age uma vez a cada candle fechado (de hora em hora, com tempo gráfico de 1h). Para ver
operações mais rápido nos testes, use 1m.

Registros:

- `bot.log`: tudo o que o robô fez;
- `trades.csv`: cada compra, venda e stop executado, com o resultado (abre no Excel);
- `state.json`: posição atual e stops do robô. Apague só com o robô parado e sem compra aberta.

O limite de perda diária conta apenas vendas já feitas no dia (horário UTC). Ao atingir o limite,
o robô para de comprar até o dia seguinte.

## Painel visual

```powershell
python app.py
```

Abre uma janela com o gráfico de candles ao vivo, as duas médias, setas em cada compra (C) e
venda (V) feita na conta e o saldo. Os botões no topo mudam o tempo gráfico só do gráfico. À
direita há três abas: Robô, Manual e Histórico.

### Aba Robô

Liga e desliga o robô e mostra a posição dele e o registro (`bot.log`) ao vivo. As configurações
(tempo gráfico, médias, valor por compra, perda máxima por dia, stop loss e stop gain) só podem
ser mudadas com o robô desligado e são salvas ao ligar.

Com stop loss ou stop gain maior que zero, logo depois de cada compra o robô registra os stops na
Binance, iguais aos da ordem manual. Se um stop executar, o robô percebe no próximo candle e
registra o resultado. Num sinal de venda, ele cancela os stops e vende a mercado. Os stops do robô
aparecem em "Ordens abertas" marcados como "robô" e sem botões, para não confundir a posição dele.

Fechar o painel desliga o robô. Os stops que já estão na Binance continuam valendo.

### Filtro de notícias

Com "Pausar perto de notícias" marcado, o robô ignora sinais de compra entre X minutos antes e X
minutos depois de cada notícia relevante (por padrão, 30 e 30, impacto alto, moeda USD). Vendas e
stops continuam funcionando normalmente. O card "Próximas notícias" mostra a lista com contagem
regressiva, e um aviso amarelo aparece no topo durante a janela de pausa.

O calendário vem do arquivo público do ForexFactory (`nfs.faireconomy.media`), baixado no máximo a
cada 4 horas e guardado em `news_cache.json`. O ForexFactory bloqueia quem consulta com frequência
maior. Os horários já aparecem no fuso do computador. O arquivo não traz o resultado real da
notícia, só a previsão e o valor anterior. Se o calendário estiver indisponível, o robô segue sem o
filtro e avisa no registro.

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

Precisa do Microsoft Edge WebView2, que já vem no Windows 10 e 11.

### Gerar o .exe

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

Gera o `RoboTrader.exe` na pasta do projeto. Ele não precisa de Python instalado e lê e grava os
arquivos (`.env`, `settings.json`, `state.json`, `bot.log`, `trades.csv`) na pasta onde estiver.
Para usar em outra pasta ou outro computador, copie o `.exe` junto com o `.env`.

O `config.py` vai embutido no `.exe`: depois de mudar algo nele (como `USE_TESTNET`), gere o `.exe`
de novo. Alguns antivírus acusam programas gerados pelo PyInstaller como suspeitos sem motivo. Se
o Windows Defender bloquear, libere o arquivo em "Proteção contra vírus e ameaças".

O gráfico usa a biblioteca [Lightweight Charts](https://github.com/tradingview/lightweight-charts)
da TradingView (licença Apache 2.0, em `ui/vendor/`).

## 3. Dinheiro real (só depois de semanas na testnet)

1. Crie a chave na Binance real com **saque desativado** e restrita ao IP do seu computador.
2. Coloque as chaves reais no `.env`.
3. Em `config.py`, mude `USE_TESTNET = False` e comece com um valor por compra pequeno.
4. O `bot.py` pede para digitar `DINHEIRO REAL` antes de iniciar, e o painel pede confirmação
   antes de cada ordem e antes de ligar o robô.

Os preços da testnet não são iguais aos do mercado real. Bons resultados lá não garantem nada
com dinheiro de verdade.
