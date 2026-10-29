import argparse
import csv
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import requests
from dotenv import load_dotenv

import config
import news
from binance_client import INTERVAL_MS, BinanceClient, BinanceError
from strategy import BREAKOUTS, STRATEGIES, TREND_PERIOD, atr, candles_needed, rules_for

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).resolve().parent
    RESOURCE_DIR = BASE_DIR

STATE_FILE = BASE_DIR / "state.json"
TRADES_FILE = BASE_DIR / "trades.csv"
LOG_FILE = BASE_DIR / "bot.log"
SETTINGS_FILE = BASE_DIR / "settings.json"
LOCK_FILE = BASE_DIR / "bot.lock"
NEWS_CACHE_FILE = BASE_DIR / "news_cache.json"

BOT_TAG = "bot"
INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
           "DOGEUSDT", "TRXUSDT", "LTCUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT"]
DONE_STATUSES = {"CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}
ATR_STOPS = ["none", "fixed", "trailing"]
SIZINGS = ["fixed", "risk"]
STRATEGY_NAMES = {"sma": "Médias", "turtle_20_10": "Tartarugas 20/10", "turtle_55_20": "Tartarugas 55/20"}


def log(message: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state() -> dict:
    state = {
        "symbol": config.SYMBOL,
        "position_qty": "0",
        "entry_cost": 0.0,
        "day": today(),
        "day_pnl": 0.0,
        "last_candle": 0,
        "protection": None,
        "peak": None,
    }
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    if state["day"] != today():
        state["day"] = today()
        state["day_pnl"] = 0.0
    return state


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def check_position_symbol(symbol: str) -> None:
    state = load_state()
    if Decimal(state["position_qty"]) > 0 and state["symbol"] != symbol:
        raise ValueError(f"O robô tem uma compra aberta em {state['symbol']}. "
                         "Deixe ele ligado nesse par até a compra fechar antes de trocar.")


def default_settings() -> dict:
    return {
        "symbol": config.SYMBOL,
        "interval": config.INTERVAL,
        "fast_sma": config.FAST_SMA,
        "slow_sma": config.SLOW_SMA,
        "strategy": config.STRATEGY,
        "trend_filter": config.TREND_FILTER,
        "trade_usdt": config.TRADE_USDT,
        "max_daily_loss_usdt": config.MAX_DAILY_LOSS_USDT,
        "stop_loss_pct": config.STOP_LOSS_PCT,
        "take_profit_pct": config.TAKE_PROFIT_PCT,
        "atr_stop": config.ATR_STOP,
        "atr_mult": config.ATR_MULT,
        "sizing": config.SIZING,
        "risk_pct": config.RISK_PCT,
        "news_filter": config.NEWS_FILTER,
        "news_before_min": config.NEWS_BEFORE_MIN,
        "news_after_min": config.NEWS_AFTER_MIN,
        "news_countries": config.NEWS_COUNTRIES,
        "news_impact": config.NEWS_IMPACT,
    }


def load_settings() -> dict:
    settings = default_settings()
    if SETTINGS_FILE.exists():
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        settings.update({k: v for k, v in saved.items() if k in settings})
    return settings


def is_true(value) -> bool:
    return value in (True, 1, "1", "true")


def validate_settings(values: dict) -> dict:
    try:
        s = {
            "symbol": str(values.get("symbol", config.SYMBOL)),
            "interval": str(values["interval"]),
            "fast_sma": int(values["fast_sma"]),
            "slow_sma": int(values["slow_sma"]),
            "strategy": str(values.get("strategy", "sma")),
            "trend_filter": is_true(values.get("trend_filter")),
            "trade_usdt": float(values["trade_usdt"]),
            "max_daily_loss_usdt": float(values["max_daily_loss_usdt"]),
            "stop_loss_pct": float(values.get("stop_loss_pct") or 0),
            "take_profit_pct": float(values.get("take_profit_pct") or 0),
            "atr_stop": str(values.get("atr_stop", "none")),
            "atr_mult": float(values.get("atr_mult") or 0),
            "sizing": str(values.get("sizing", "fixed")),
            "risk_pct": float(values.get("risk_pct") or 0),
            "news_filter": is_true(values.get("news_filter")),
            "news_before_min": int(values.get("news_before_min") or 0),
            "news_after_min": int(values.get("news_after_min") or 0),
            "news_countries": str(values.get("news_countries", "")),
            "news_impact": str(values.get("news_impact", "")),
        }
    except (KeyError, TypeError, ValueError):
        raise ValueError("Preencha todos os campos com números válidos")

    if s["symbol"] not in SYMBOLS:
        raise ValueError(f"Par inválido: {s['symbol']}")
    if s["interval"] not in INTERVALS:
        raise ValueError(f"Tempo gráfico inválido: {s['interval']}")
    if not 2 <= s["fast_sma"] < s["slow_sma"] <= 200:
        raise ValueError("A média rápida precisa ser menor que a lenta (entre 2 e 200)")
    if s["trade_usdt"] < 5:
        raise ValueError("O valor por compra precisa ser de pelo menos 5 USDT")
    if s["max_daily_loss_usdt"] <= 0:
        raise ValueError("A perda máxima por dia precisa ser maior que zero")
    if not 0 <= s["stop_loss_pct"] < 50:
        raise ValueError("Stop loss precisa estar entre 0 e 50% (0 desliga)")
    if not 0 <= s["take_profit_pct"] < 100:
        raise ValueError("Stop gain precisa estar entre 0 e 100% (0 desliga)")
    if s["strategy"] not in STRATEGIES:
        raise ValueError(f"Estratégia inválida: {s['strategy']}")
    if s["atr_stop"] not in ATR_STOPS:
        raise ValueError("Tipo de stop de ATR inválido")
    if s["atr_stop"] != "none" and not 0.5 <= s["atr_mult"] <= 10:
        raise ValueError("O multiplicador do ATR precisa estar entre 0,5 e 10")
    if s["atr_stop"] != "none" and s["stop_loss_pct"]:
        raise ValueError("Use o stop de ATR ou o stop loss %, não os dois")
    if s["sizing"] not in SIZINGS:
        raise ValueError("Tipo de tamanho da compra inválido")
    if s["sizing"] == "risk":
        if not 0.1 <= s["risk_pct"] <= 10:
            raise ValueError("O risco por operação precisa estar entre 0,1 e 10%")
        if s["atr_stop"] == "none" and not s["stop_loss_pct"]:
            raise ValueError("O tamanho pelo risco precisa de um stop (ATR ou stop loss %)")
    if not (0 <= s["news_before_min"] <= 240 and 0 <= s["news_after_min"] <= 240):
        raise ValueError("A pausa antes e depois das notícias precisa estar entre 0 e 240 minutos")
    if s["news_impact"] not in news.IMPACTS:
        raise ValueError("Impacto das notícias inválido")
    s["news_countries"] = news.validate_countries(s["news_countries"])
    return s


def save_settings(values: dict) -> dict:
    s = validate_settings(values)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
    return s


def record_trade(symbol: str, side: str, qty: Decimal, quote: float, pnl: float | None) -> None:
    new_file = not TRADES_FILE.exists()
    with TRADES_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["data", "lado", "quantidade", "preco_medio", "valor_usdt", "lucro_usdt", "par"])
        price = quote / float(qty) if qty else 0.0
        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            side,
            str(qty),
            f"{price:.8f}".rstrip("0").rstrip("."),
            f"{quote:.2f}",
            "" if pnl is None else f"{pnl:.2f}",
            symbol,
        ])


def fee_in(order: dict, asset: str) -> Decimal:
    return sum((Decimal(f["commission"]) for f in order.get("fills", []) if f["commissionAsset"] == asset), Decimal(0))


class Market:
    def __init__(self, info: dict):
        self.base = info["baseAsset"]
        self.quote = info["quoteAsset"]
        filters = {f["filterType"]: f for f in info["filters"]}
        self.step = Decimal(filters["LOT_SIZE"]["stepSize"]).normalize()
        self.min_qty = Decimal(filters["LOT_SIZE"]["minQty"])
        self.tick = Decimal(filters["PRICE_FILTER"]["tickSize"]).normalize()
        self.digits = max(0, -self.tick.as_tuple().exponent)
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        self.min_notional = float(notional.get("minNotional", 0))

    def floor_qty(self, qty: Decimal) -> Decimal:
        return qty.quantize(self.step, rounding=ROUND_DOWN)

    def round_price(self, price: float) -> str:
        return format(Decimal(str(price)).quantize(self.tick), "f")


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._file = None

    def acquire(self) -> bool:
        f = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._file = f
        return True

    def release(self) -> None:
        if self._file is None:
            return
        if os.name == "nt":
            import msvcrt
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        self._file.close()
        self._file = None

    def held_elsewhere(self) -> bool:
        if self._file is not None:
            return False
        if self.acquire():
            self.release()
            return False
        return True


class Bot:
    def __init__(self, client: BinanceClient, market: Market, settings: dict, stop_event: threading.Event | None = None):
        self.client = client
        self.data = market_data_client(client)
        self.market = market
        self.s = settings
        self.symbol = settings["symbol"]
        self.stop_event = stop_event or threading.Event()

    def run(self) -> None:
        s = self.s
        name = STRATEGY_NAMES[s["strategy"]]
        if s["strategy"] == "sma":
            name += f" {s['fast_sma']} x {s['slow_sma']}"
        if s["trend_filter"]:
            name += " + MM200"
        size = f"risco de {s['risk_pct']:g}% por compra" if s["sizing"] == "risk" else f"{s['trade_usdt']:g} USDT por compra"
        stops = []
        if s["atr_stop"] != "none":
            stops.append(f"stop {'móvel ' if s['atr_stop'] == 'trailing' else ''}{s['atr_mult']:g}N")
        if s["stop_loss_pct"]:
            stops.append(f"stop loss {s['stop_loss_pct']:g}%")
        if s["take_profit_pct"]:
            stops.append(f"stop gain {s['take_profit_pct']:g}%")
        log(f"Robô iniciado: {self.symbol} {s['interval']}, {name}, {size}" + (", " + " e ".join(stops) if stops else ""))
        if s["news_filter"]:
            events = self._news_events()
            if events is None:
                log("Calendário de notícias indisponível agora. O robô tenta de novo mais tarde.")
            else:
                upcoming = [e for e in events if e["time"] >= time.time()]
                log(f"Filtro de notícias ligado: pausa {s['news_before_min']} min antes e "
                    f"{s['news_after_min']} min depois. {len(upcoming)} notícia(s) relevante(s) pela frente nesta semana.")

        while not self.stop_event.is_set():
            try:
                self.tick()
            except (BinanceError, requests.RequestException) as e:
                log(f"Erro, tentando de novo no próximo candle: {e}")
            self._sleep_until_next_candle()
        log("Robô parado.")

    def _sleep_until_next_candle(self) -> None:
        period = INTERVAL_MS[self.s["interval"]] / 1000
        end = (time.time() // period + 1) * period + 5
        while not self.stop_event.is_set() and time.time() < end:
            self.stop_event.wait(min(1.0, end - time.time()))

    def tick(self) -> None:
        s = self.s
        state = load_state()
        self._check_protection(state)

        df = self.data.klines(self.symbol, s["interval"], limit=candles_needed(s))
        df = df[df["close_time"] < self.client.now_ms()].reset_index(drop=True)
        buy_rule, sell_rule = rules_for(df, s)
        n = float(atr(df).iloc[-1])
        last = df.iloc[-1]

        if int(last["open_time"]) == state["last_candle"]:
            return
        # saved before acting, so a crash mid-order never repeats the order on restart
        state["last_candle"] = int(last["open_time"])
        save_state(state)

        holding = Decimal(state["position_qty"]) > 0
        log(self._describe(df, n, holding, state))

        if holding and s["atr_stop"] == "trailing" and self._trail(state, float(last["close"]), n):
            return
        holding = Decimal(state["position_qty"]) > 0

        if buy_rule.iloc[-1] and not holding:
            if state["day_pnl"] <= -s["max_daily_loss_usdt"]:
                log("Sinal de compra ignorado: limite de perda diária atingido.")
            elif blocking := self._news_block():
                when = datetime.fromtimestamp(blocking["time"]).strftime("%d/%m %H:%M")
                log(f"Sinal de compra ignorado: notícia {blocking['country']} \"{blocking['title']}\" às {when}.")
            else:
                self.buy(state, n)
        elif sell_rule.iloc[-1] and holding:
            self.sell(state)

    def _describe(self, df, n: float, holding: bool, state: dict) -> str:
        s, d = self.s, self.market.digits
        closes = df["close"]
        parts = [f"Preço {closes.iloc[-1]:.{d}f}"]
        if s["strategy"] in BREAKOUTS:
            entry, exit = BREAKOUTS[s["strategy"]]
            parts.append(f"máx{entry} {df['high'].iloc[-entry - 1:-1].max():.{d}f}")
            parts.append(f"mín{exit} {df['low'].iloc[-exit - 1:-1].min():.{d}f}")
        else:
            parts.append(f"SMA{s['fast_sma']} {closes.iloc[-s['fast_sma']:].mean():.{d}f}")
            parts.append(f"SMA{s['slow_sma']} {closes.iloc[-s['slow_sma']:].mean():.{d}f}")
        if s["trend_filter"]:
            parts.append(f"MM200 {closes.iloc[-TREND_PERIOD:].mean():.{d}f}")
        parts.append(f"ATR {n:.{d}f}")
        parts.append("comprado" if holding else "fora")
        parts.append(f"resultado do dia {state['day_pnl']:+.2f}")
        return " | ".join(parts)

    def _news_events(self) -> list[dict] | None:
        events, fetched_at = news.load_events(NEWS_CACHE_FILE)
        if not fetched_at:
            return None
        return news.relevant(events, news.parse_countries(self.s["news_countries"]), self.s["news_impact"])

    def _news_block(self) -> dict | None:
        s = self.s
        if not s["news_filter"]:
            return None
        events = self._news_events()
        if events is None:
            log("Calendário de notícias indisponível. Seguindo sem o filtro.")
            return None
        return news.blocking_event(events, time.time(), s["news_before_min"], s["news_after_min"])

    def _spend(self, n: float, price: float) -> float:
        s = self.s
        if s["sizing"] != "risk":
            return s["trade_usdt"]
        free = self.client.balances().get(self.market.quote, 0.0)
        distance = s["atr_mult"] * n / price if s["atr_stop"] != "none" else s["stop_loss_pct"] / 100
        return min(free * 0.99, free * s["risk_pct"] / 100 / distance)

    def buy(self, state: dict, n: float) -> None:
        market, s = self.market, self.s
        if s["atr_stop"] != "none" and not n > 0:
            log("Sinal de compra ignorado: ATR ainda não calculado.")
            return
        price = float(self.data.klines(self.symbol, "1m", limit=1)["close"].iloc[-1])
        spend = self._spend(n, price)
        if spend < market.min_notional:
            log(f"Sinal de compra ignorado: valor da compra ({spend:.2f}) abaixo do mínimo da corretora.")
            return

        order = self.client.market_buy(self.symbol, spend)
        qty = Decimal(order["executedQty"]) - fee_in(order, market.base)
        cost = float(order["cummulativeQuoteQty"]) + float(fee_in(order, market.quote))
        avg = cost / float(qty)
        state["symbol"] = self.symbol
        state["position_qty"] = str(qty)
        state["entry_cost"] = cost
        state["protection"] = None
        state["peak"] = avg
        save_state(state)
        record_trade(self.symbol, "COMPRA", qty, cost, None)
        log(f"COMPRA: {qty} {market.base} por {cost:.2f} {market.quote}")

        sl_price = None
        if s["atr_stop"] != "none":
            sl_price = market.round_price(avg - s["atr_mult"] * n)
        elif s["stop_loss_pct"]:
            sl_price = market.round_price(avg * (1 - s["stop_loss_pct"] / 100))
        tp_price = market.round_price(avg * (1 + s["take_profit_pct"] / 100)) if s["take_profit_pct"] else None
        if sl_price or tp_price:
            try:
                state["protection"] = self._place_protection(market.floor_qty(qty), sl_price, tp_price)
                save_state(state)
            except BinanceError as e:
                log(f"Compra feita, mas os stops NÃO foram criados: {e}")

    def _place_protection(self, qty: Decimal, sl_price: str | None, tp_price: str | None) -> dict:
        quantity = format(qty, "f")
        if sl_price and tp_price:
            r = self.client.oco_sell(self.symbol, quantity, tp_price, sl_price, tag=BOT_TAG)
            order_ids, list_id = [o["orderId"] for o in r["orders"]], r["orderListId"]
            log(f"Stops criados: stop loss {sl_price}, stop gain {tp_price}")
        elif sl_price:
            r = self.client.stop_loss_sell(self.symbol, quantity, sl_price, tag=BOT_TAG)
            order_ids, list_id = [r["orderId"]], -1
            log(f"Stop criado: stop loss {sl_price}")
        else:
            r = self.client.take_profit_sell(self.symbol, quantity, tp_price, tag=BOT_TAG)
            order_ids, list_id = [r["orderId"]], -1
            log(f"Stop criado: stop gain {tp_price}")
        return {
            "list_id": list_id,
            "order_ids": order_ids,
            "sl": float(sl_price) if sl_price else None,
            "tp": float(tp_price) if tp_price else None,
        }

    def _trail(self, state: dict, close: float, n: float) -> bool:
        protection = state.get("protection")
        if not protection or not protection.get("sl") or not n > 0:
            return False
        peak = max(state.get("peak") or close, close)
        state["peak"] = peak
        new_sl = float(self.market.round_price(peak - self.s["atr_mult"] * n))
        if new_sl <= protection["sl"]:
            save_state(state)
            return False
        if new_sl >= close:
            log(f"Stop móvel ({new_sl:.{self.market.digits}f}) acima do fechamento, vendendo agora.")
            self.sell(state)
            return True

        tp = protection.get("tp")
        self._cancel_protection(state)
        save_state(state)
        qty = self.market.floor_qty(Decimal(state["position_qty"]))
        try:
            state["protection"] = self._place_protection(
                qty, self.market.round_price(new_sl), self.market.round_price(tp) if tp else None
            )
            log(f"Stop móvel subiu para {new_sl:.{self.market.digits}f}")
        except BinanceError as e:
            log(f"Não foi possível subir o stop móvel, a posição ficou sem stop: {e}")
        save_state(state)
        return False

    def _check_protection(self, state: dict) -> bool:
        protection = state.get("protection")
        if not protection or Decimal(state["position_qty"]) <= 0:
            return False

        orders = [self.client.get_order(self.symbol, oid) for oid in protection["order_ids"]]
        filled = [o for o in orders if o["status"] == "FILLED"]
        if filled:
            o = filled[0]
            kind = "STOP LOSS" if o["type"].startswith("STOP_LOSS") else "STOP GAIN"
            qty = Decimal(o["executedQty"])
            proceeds = float(o["cummulativeQuoteQty"])
            pnl = proceeds - state["entry_cost"]
            state["day_pnl"] += pnl
            record_trade(self.symbol, kind, qty, proceeds, pnl)
            log(f"{kind} executado: {qty} {self.market.base} por {proceeds:.2f} {self.market.quote} "
                f"(resultado {pnl:+.2f})")
            self._clear_position(state)
            return True

        if all(o["status"] in DONE_STATUSES for o in orders):
            log("Os stops do robô foram cancelados fora dele. A posição continua aberta, sem stop.")
            state["protection"] = None
            save_state(state)
        return False

    def _cancel_protection(self, state: dict) -> None:
        protection = state.get("protection")
        if not protection:
            return
        try:
            if protection["list_id"] != -1:
                self.client.cancel_order_list(self.symbol, protection["list_id"])
            else:
                self.client.cancel_order(self.symbol, protection["order_ids"][0])
        except BinanceError:
            pass
        state["protection"] = None

    def sell(self, state: dict) -> None:
        if self._check_protection(state):
            return
        self._cancel_protection(state)

        market = self.market
        qty = market.floor_qty(Decimal(state["position_qty"]))
        if qty < market.min_qty:
            log(f"Quantidade {qty} abaixo do mínimo para vender. Posição zerada no registro.")
        else:
            order = self.client.market_sell(self.symbol, format(qty, "f"))
            proceeds = float(order["cummulativeQuoteQty"]) - float(fee_in(order, market.quote))
            pnl = proceeds - state["entry_cost"]
            state["day_pnl"] += pnl
            record_trade(self.symbol, "VENDA", qty, proceeds, pnl)
            log(f"VENDA: {qty} {market.base} por {proceeds:.2f} {market.quote} (resultado {pnl:+.2f})")
        self._clear_position(state)

    def _clear_position(self, state: dict) -> None:
        state["position_qty"] = "0"
        state["entry_cost"] = 0.0
        state["protection"] = None
        state["peak"] = None
        save_state(state)


def make_client() -> BinanceClient:
    load_dotenv(BASE_DIR / ".env")
    client = BinanceClient(
        os.getenv("BINANCE_API_KEY", ""),
        os.getenv("BINANCE_API_SECRET", ""),
        testnet=config.USE_TESTNET,
    )
    client.sync_time()
    return client


def market_data_client(client: BinanceClient) -> BinanceClient:
    # the testnet keeps only a few weeks of candles, too few for long indicators like the 200-period average
    if not config.USE_TESTNET:
        return client
    data = BinanceClient(testnet=False)
    data.time_offset_ms = client.time_offset_ms
    return data


def check(client: BinanceClient, market: Market) -> None:
    balances = client.balances()
    log(f"Conexão OK. Saldo livre: {balances.get(market.quote, 0):.2f} {market.quote}, "
        f"{balances.get(market.base, 0):.8f} {market.base}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Robô de cruzamento de médias móveis")
    parser.add_argument("--check", action="store_true", help="só testa a conexão e mostra o saldo")
    args = parser.parse_args()

    if config.USE_TESTNET:
        log("Modo TESTNET: dinheiro fictício.")
    else:
        print("ATENÇÃO: modo REAL. As ordens vão usar dinheiro de verdade.")
        if input('Digite "DINHEIRO REAL" para continuar: ').strip() != "DINHEIRO REAL":
            sys.exit("Cancelado.")

    settings = load_settings()
    try:
        client = make_client()
        market = Market(client.symbol_info(settings["symbol"]))
        check(client, market)
    except (BinanceError, requests.RequestException) as e:
        sys.exit(f"Falha ao conectar: {e}")

    if args.check:
        return
    try:
        check_position_symbol(settings["symbol"])
    except ValueError as e:
        sys.exit(str(e))

    lock = InstanceLock(LOCK_FILE)
    if not lock.acquire():
        sys.exit("O robô já está rodando (no painel ou em outra janela).")
    print("Ctrl+C para parar.")
    try:
        Bot(client, market, settings).run()
    except KeyboardInterrupt:
        log("Robô parado.")
    finally:
        lock.release()


if __name__ == "__main__":
    main()
