# Robô trader (estudo)

Robô de trading para a Binance, feito para aprender. Roda por padrão na **testnet**, com dinheiro
fictício.

Estratégias disponíveis:

- **Médias**: compra quando a média rápida (9) cruza para cima da lenta (21) e vende no cruzamento
  para baixo;
- **Tartarugas 20/10 e 55/20** (Richard Dennis): compra quando o fechamento passa da máxima dos
  últimos 20 (ou 55) candles e vende quando fica abaixo da mínima dos últimos 10 (ou 20);
- **Filtro da média de 200** (Paul Tudor Jones), combinável com qualquer uma: só compra acima da
  média de 200 candles e vende se o preço cair abaixo dela.

Os valores padrão ficam em `config.py`. O que for mudado pelo painel fica salvo em
`settings.json` e tem prioridade.

## Arquivos

| Arquivo | O que faz |
|---|---|
| `config.py` | Testnet ou real, e os valores padrão do robô (inclusive o par) |
| `strategy.py` | Calcula as médias e marca os sinais de compra e venda |
| `backtest.py` | Testa a estratégia no histórico real da Binance (não precisa de chave) |
| `compare.py` | Compara várias estratégias (médias, Tartarugas, MM200, stops de ATR) |
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

### Comparar estratégias

```powershell
python compare.py
python compare.py --symbol ETHUSDT --intervals 4h,1d --risk 1
```

Testa várias estratégias ao mesmo tempo no histórico real (1h: 1 ano, 4h: 3 anos, 1d: 6 anos) e
mostra uma tabela por tempo gráfico, da melhor para a pior:

- **Médias**: o cruzamento de médias que o robô usa hoje;
- **Tartarugas 20/10 e 55/20**: compra quando o fechamento passa da máxima dos últimos 20 (ou 55)
  candles e vende quando fica abaixo da mínima dos últimos 10 (ou 20);
- **+ MM200**: só compra acima da média de 200 candles e vende se o preço cair abaixo dela;
- **stop 2N**: stop a 2 ATR (20 períodos) abaixo do preço de compra;
- **stop móvel 3N**: stop a 3 ATR abaixo do maior fechamento desde a compra.

Os sinais usam o fechamento do candle e a ordem sai na abertura do próximo. `--risk 1` troca "todo
o capital em cada compra" por um tamanho de compra que perde 1% do capital se o stop for atingido.
A escolha da melhor linha num mesmo histórico tende a superestimar o resultado: confira se ela
também vai bem em outra moeda e em outro tempo gráfico.

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

Na testnet, os sinais e o gráfico usam os candles da Binance real (públicos, sem chave), e só as
ordens e o saldo ficam na testnet. A testnet guarda poucas semanas de candles, o que não basta para
a média de 200 em 4h ou 1d. Os preços das duas ficam muito próximos.

Só um robô roda por vez: se ele já estiver ligado no painel, o `bot.py` se recusa a iniciar, e
vice-versa.

O robô age uma vez a cada candle fechado (de hora em hora, com tempo gráfico de 1h). Para ver
operações mais rápido nos testes, use 1m.

Registros:

- `bot.log`: tudo o que o robô fez;
- `trades.csv`: cada compra, venda e stop executado, com o resultado e o par (abre no Excel);
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
só podem ser mudadas com o robô desligado e são salvas ao ligar.

O campo **Par** escolhe a moeda (BTC, ETH, SOL, DOGE e outras, sempre contra USDT) e é salvo na
hora: o gráfico, o saldo, as ordens abertas, o histórico e a ordem manual passam a ser desse par.
Ele não pode ser trocado enquanto o robô tiver uma compra aberta em outro par: deixe o robô ligado
nesse par até a compra fechar. O robô opera um par por vez. O gráfico mostra as linhas da
estratégia salva: as duas médias, ou a máxima e a mínima das Tartarugas, e a MM200 quando o filtro
está ligado.

"Usar configuração recomendada" preenche a combinação que foi melhor nos testes do `compare.py`
em BTC e ETH: Tartarugas 20/10, filtro da média de 200, stop fixo de 2 ATR e tempo gráfico de 4h.

- **Tamanho da compra**: valor fixo em USDT, ou pelo risco: o robô calcula a compra para perder
  só o percentual escolhido do saldo livre em USDT se o stop for atingido (limitada ao saldo). Exige
  um stop.
- **Stop de ATR**: fixo (preço de compra menos N x ATR de 20 períodos) ou móvel (maior fechamento
  desde a compra menos N x ATR). O stop móvel só sobe: a cada candle, o robô cancela o stop antigo
  na Binance e cria um novo mais alto. Não pode ser usado junto com o stop loss %.
- **Stop loss % e stop gain %**: como antes. O stop gain pode ser combinado com o stop de ATR.

Logo depois de cada compra, o robô registra os stops na Binance, iguais aos da ordem manual. Se um
stop executar, o robô percebe no próximo candle e registra o resultado. Num sinal de venda, ele
cancela os stops e vende a mercado. Os stops do robô aparecem em "Ordens abertas" marcados como
"robô" e sem botões, para não confundir a posição dele.

Se a compra ficar sem stop (a Binance recusou a ordem ou alguém cancelou o stop pelo site), o robô
cria o stop de novo no próximo candle. Se o preço já tiver passado do stop, ele vende a mercado.

Fechar o painel desliga o robô. Os stops que já estão na Binance continuam valendo.

O card **Resultados do robô** soma, a partir do `trades.csv`, os ganhos, as perdas e o total das
vendas e stops do robô já fechados (com as taxas descontadas, inclusive as pagas em BNB, convertidas
pelo preço do momento), de hoje, dos últimos 7 ou 30 dias ou
de tudo. Soma todos os pares, em USDT. Compras manuais e a compra ainda aberta não entram.

### Filtro de notícias

Com "Pausar perto de notícias" marcado, o robô ignora sinais de compra entre X minutos antes e X
minutos depois de cada notícia relevante (por padrão, 30 e 30, impacto alto, moeda USD). Vendas e
stops continuam funcionando normalmente. O card "Próximas notícias" mostra a lista com contagem
regressiva, e um aviso amarelo aparece no topo durante a janela de pausa.

O calendário vem do arquivo público do ForexFactory (`nfs.faireconomy.media`), baixado no máximo a
cada 4 horas e guardado em `news_cache.json`. O ForexFactory bloqueia quem consulta com frequência
maior. Os horários já aparecem no fuso do computador. O arquivo não traz o resultado real da
notícia, só a previsão e o valor anterior. Se o calendário estiver indisponível ou desatualizado
(mais de 12 horas sem conseguir baixar, e a semana da cópia guardada já acabou), o robô segue sem o
filtro e avisa no registro, e o painel mostra "Desatualizado".

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
