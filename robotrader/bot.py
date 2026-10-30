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

from robotrader import config, news
from robotrader.binance_client import INTERVAL_MS, ORDER_NOT_FOUND, BinanceClient, BinanceError, client_order_id
from robotrader.strategy import BREAKOUTS, CATALOG, DAY_MS, STRATEGIES, TREND_PERIOD, atr, candles_needed, catalog, rules_for

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(sys._MEIPASS)
else:
    RESOURCE_DIR = Path(__file__).resolve().parent
    BASE_DIR = RESOURCE_DIR.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

STATE_FILE = DATA_DIR / "state.json"
TRADES_FILE = DATA_DIR / "trades.csv"
LOG_FILE = DATA_DIR / "bot.log"
SETTINGS_FILE = DATA_DIR / "settings.json"
LOCK_FILE = DATA_DIR / "bot.lock"
NEWS_CACHE_FILE = DATA_DIR / "news_cache.json"
SIGNALS_FILE = DATA_DIR / "signals.json"

BOT_TAG = "bot"
INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
           "DOGEUSDT", "TRXUSDT", "LTCUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT"]
DONE_STATUSES = {"CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}
ATR_STOPS = ["none", "fixed", "trailing"]
SIZINGS = ["fixed", "risk"]
CATALOG_INTERVALS = ["1m", "5m", "15m", "30m", "1h"]
STRATEGY_NAMES = {"sma": "Médias", "turtle_20_10": "Tartarugas 20/10", "turtle_55_20": "Tartarugas 55/20",
                  CATALOG: "Catalogação"}


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
        "stop_sl": None,
        "stop_tp": None,
        "pending_ids": [],
        "peak": None,
        "entry_candle": None,
        "gale": 0,
        "gale_due": False,
    }
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    if state["protection"] and state["stop_sl"] is None and state["stop_tp"] is None:
        state["stop_sl"], state["stop_tp"] = state["protection"]["sl"], state["protection"]["tp"]
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
        "daily_target_usdt": config.DAILY_TARGET_USDT,
        "catalog_days": config.CATALOG_DAYS,
        "catalog_rate": config.CATALOG_RATE,
        "hold_candles": config.HOLD_CANDLES,
        "martingale_steps": config.MARTINGALE_STEPS,
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
            "daily_target_usdt": float(values.get("daily_target_usdt") or 0),
            "catalog_days": int(values.get("catalog_days", config.CATALOG_DAYS)),
            "catalog_rate": float(values.get("catalog_rate", config.CATALOG_RATE)),
            "hold_candles": int(values.get("hold_candles", config.HOLD_CANDLES)),
            "martingale_steps": int(values.get("martingale_steps") or 0),
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
    if s["daily_target_usdt"] < 0:
        raise ValueError("A meta de lucro por dia não pode ser negativa (0 desliga)")
    if s["strategy"] == CATALOG:
        if s["interval"] not in CATALOG_INTERVALS:
            raise ValueError("A catalogação usa tempo gráfico de 1m a 1h")
        if s["trend_filter"]:
            raise ValueError("O filtro da média de 200 não vale para a catalogação")
        if not 5 <= s["catalog_days"] <= 60:
            raise ValueError("Os dias analisados precisam estar entre 5 e 60")
        if not 50 < s["catalog_rate"] <= 100:
            raise ValueError("O acerto mínimo precisa estar entre 50 e 100%")
        if not 1 <= s["hold_candles"] <= 10:
            raise ValueError("A expiração precisa estar entre 1 e 10 candles")
    if not 0 <= s["martingale_steps"] <= 2:
        raise ValueError("Use de 0 a 2 gales")
    if s["martingale_steps"] and (s["strategy"] != CATALOG or s["sizing"] != "fixed"):
        raise ValueError("O martingale só vale na catalogação, com valor fixo por compra")
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
    price = quote / float(qty) if qty else 0.0
    row = [
        datetime.now().isoformat(timespec="seconds"),
        side,
        str(qty),
        f"{price:.8f}".rstrip("0").rstrip("."),
        f"{quote:.2f}",
        "" if pnl is None else f"{pnl:.2f}",
        symbol,
    ]
    try:
        new_file = not TRADES_FILE.exists()
        with TRADES_FILE.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(["data", "lado", "quantidade", "preco_medio", "valor_usdt", "lucro_usdt", "par"])
            writer.writerow(row)
    except OSError as e:
        # Excel locks the file while it is open
        log(f"Não foi possível gravar no trades.csv ({e.strerror}). Operação não registrada: {','.join(row)}")


def fee_in(order: dict, asset: str) -> Decimal:
    return sum((Decimal(f["commission"]) for f in order.get("fills") or [] if f["commissionAsset"] == asset), Decimal(0))


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


def estimate_missing_fees(order: dict, market: "Market") -> None:
    if order.get("fills") is not None:
        return
    rate = Decimal(str(config.FEE_RATE))
    if order["side"] == "BUY":
        fee, asset = Decimal(order["executedQty"]) * rate, market.base
    else:
        fee, asset = Decimal(order["cummulativeQuoteQty"]) * rate, market.quote
    order["fills"] = [{"commission": str(fee), "commissionAsset": asset, "price": "0"}]
    log(f"Taxa da ordem {order['orderId']} desconhecida, usando a estimativa de {config.FEE_RATE:.1%}.")


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
        self.period_ms = INTERVAL_MS[settings["interval"]]
        self.slots: dict[int, float] = {}
        self.slots_day = None

    def run(self) -> None:
        s = self.s
        name = STRATEGY_NAMES[s["strategy"]]
        if s["strategy"] == "sma":
            name += f" {s['fast_sma']} x {s['slow_sma']}"
        if s["strategy"] == CATALOG:
            name += (f" ({s['catalog_days']} dias, acerto mínimo {s['catalog_rate']:g}%, "
                     f"expiração {s['hold_candles']} candle(s))")
        if s["trend_filter"]:
            name += " + MM200"
        size = f"risco de {s['risk_pct']:g}% por compra" if s["sizing"] == "risk" else f"{s['trade_usdt']:g} USDT por compra"
        if s["martingale_steps"]:
            size += f" e até {s['martingale_steps']} gale(s) de {config.MARTINGALE_FACTOR:g}x"
        stops = [f"stop loss do dia {s['max_daily_loss_usdt']:g} USDT"]
        if s["daily_target_usdt"]:
            stops.append(f"stop win do dia {s['daily_target_usdt']:g} USDT")
        if s["atr_stop"] != "none":
            stops.append(f"stop {'móvel ' if s['atr_stop'] == 'trailing' else ''}{s['atr_mult']:g}N")
        if s["stop_loss_pct"]:
            stops.append(f"stop loss {s['stop_loss_pct']:g}%")
        if s["take_profit_pct"]:
            stops.append(f"stop gain {s['take_profit_pct']:g}%")
        log(f"Robô iniciado: {self.symbol} {s['interval']}, {name}, {size}, " + ", ".join(stops))
        state = load_state()
        # a gale only makes sense right after the loss, not hours later after a restart
        if state["gale_due"] or state["gale"] > s["martingale_steps"]:
            state["gale"], state["gale_due"] = 0, False
            save_state(state)
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
            except Exception as e:
                log(f"Erro inesperado, tentando de novo no próximo candle: {e!r}")
            self._sleep_until_next_candle()
        log("Robô parado.")

    def _sleep_until_next_candle(self) -> None:
        period = self.period_ms / 1000
        now = self.client.now_ms() / 1000
        deadline = time.monotonic() + (now // period + 1) * period + 5 - now
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            self.stop_event.wait(min(1.0, deadline - time.monotonic()))

    def tick(self) -> None:
        s = self.s
        # PC clocks drift, and Binance rejects signed requests more than a second off
        self.client.sync_time()
        self.data.time_offset_ms = self.client.time_offset_ms
        state = load_state()
        self._check_protection(state)
        if self._restore_protection(state):
            return

        df = self.data.klines(self.symbol, s["interval"], limit=candles_needed(s))
        df = df[df["close_time"] < self.client.now_ms()].reset_index(drop=True)
        n = float(atr(df).iloc[-1])
        last = df.iloc[-1]
        last_open = int(last["open_time"])
        next_open = last_open + self.period_ms
        if s["strategy"] == CATALOG:
            self._load_catalog(next_open // DAY_MS)
        else:
            buy_rule, sell_rule = rules_for(df, s)

        if last_open == state["last_candle"]:
            return
        # saved before acting, so a crash mid-order never repeats the order on restart
        state["last_candle"] = last_open
        save_state(state)

        holding = Decimal(state["position_qty"]) > 0
        log(self._describe(df, n, holding, state))

        if holding and s["atr_stop"] == "trailing" and self._trail(state, float(last["close"]), n):
            return
        holding = Decimal(state["position_qty"]) > 0

        if s["strategy"] == CATALOG:
            sell_now = last_open >= (state["entry_candle"] or 0) + (s["hold_candles"] - 1) * self.period_ms
        else:
            sell_now = sell_rule.iloc[-1]
        if holding and sell_now:
            self.sell(state)
            holding = Decimal(state["position_qty"]) > 0
        if holding:
            return

        if s["strategy"] == CATALOG:
            buy_now = state["gale_due"] or next_open % DAY_MS in self.slots
        else:
            buy_now = buy_rule.iloc[-1]
        if not buy_now:
            return
        if state["day_pnl"] <= -s["max_daily_loss_usdt"]:
            reason = "limite de perda diária (stop loss) atingido"
        elif s["daily_target_usdt"] and state["day_pnl"] >= s["daily_target_usdt"]:
            reason = "meta de lucro do dia (stop win) atingida"
        elif s["strategy"] == CATALOG and self.client.now_ms() - next_open > self.period_ms // 2:
            reason = "passou da metade do candle do horário"
        elif blocking := self._news_block():
            when = datetime.fromtimestamp(blocking["time"]).strftime("%d/%m %H:%M")
            reason = f"notícia {blocking['country']} \"{blocking['title']}\" às {when}"
        else:
            self.buy(state, n)
            return
        log(f"{'Gale' if state['gale_due'] else 'Sinal de compra'} ignorado: {reason}.")
        if state["gale"]:
            state["gale"], state["gale_due"] = 0, False
            save_state(state)

    def _describe(self, df, n: float, holding: bool, state: dict) -> str:
        s, d = self.s, self.market.digits
        closes = df["close"]
        parts = [f"Preço {closes.iloc[-1]:.{d}f}"]
        if s["strategy"] in BREAKOUTS:
            entry, exit = BREAKOUTS[s["strategy"]]
            parts.append(f"máx{entry} {df['high'].iloc[-entry - 1:-1].max():.{d}f}")
            parts.append(f"mín{exit} {df['low'].iloc[-exit - 1:-1].min():.{d}f}")
        elif s["strategy"] == CATALOG:
            parts.append(f"{len(self.slots)} sinais hoje")
        else:
            parts.append(f"SMA{s['fast_sma']} {closes.iloc[-s['fast_sma']:].mean():.{d}f}")
            parts.append(f"SMA{s['slow_sma']} {closes.iloc[-s['slow_sma']:].mean():.{d}f}")
        if s["trend_filter"]:
            parts.append(f"MM200 {closes.iloc[-TREND_PERIOD:].mean():.{d}f}")
        parts.append(f"ATR {n:.{d}f}")
        parts.append("comprado" if holding else "fora")
        if state["gale"]:
            parts.append(f"gale {state['gale']}")
        parts.append(f"resultado do dia {state['day_pnl']:+.2f}")
        return " | ".join(parts)

    def _load_catalog(self, day: int) -> None:
        if self.slots_day == day:
            return
        s = self.s
        log(f"Catalogando os últimos {s['catalog_days']} dias de {self.symbol} {s['interval']}...")
        df = self.data.history(self.symbol, s["interval"], s["catalog_days"] + 2)
        df = df[df["close_time"] < self.client.now_ms()].reset_index(drop=True)
        rates = catalog(df, self.period_ms, s["catalog_days"], s["hold_candles"])
        row = rates.loc[day]
        picked = row[row >= s["catalog_rate"] / 100]
        self.slots = {int(slot): float(rate) for slot, rate in picked.items()}
        self.slots_day = day
        signals = [{"time": (day * DAY_MS + slot) // 1000, "rate": rate} for slot, rate in sorted(self.slots.items())]
        SIGNALS_FILE.write_text(json.dumps({
            "symbol": self.symbol, "interval": s["interval"], "day": day, "signals": signals,
        }, indent=2), encoding="utf-8")
        times = ", ".join(datetime.fromtimestamp(x["time"]).strftime("%H:%M") for x in signals)
        log(f"Lista de hoje: {len(signals)} sinal(is) de COMPRA" + (f": {times}" if times else ""))

    def _news_events(self) -> list[dict] | None:
        events, fetched_at = news.load_events(NEWS_CACHE_FILE)
        if not news.is_fresh(events, fetched_at, time.time()):
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

    def _spend(self, state: dict, n: float, price: float) -> float:
        s = self.s
        if s["sizing"] != "risk":
            return s["trade_usdt"] * config.MARTINGALE_FACTOR ** state["gale"]
        free = self.client.balances().get(self.market.quote, 0.0)
        distance = s["atr_mult"] * n / price if s["atr_stop"] != "none" else s["stop_loss_pct"] / 100
        return min(free * 0.99, free * s["risk_pct"] / 100 / distance)

    def _fee_cost(self, order: dict) -> float:
        market = self.market
        total = float(fee_in(order, market.quote))
        others = {}
        for f in order.get("fills", []):
            if f["commissionAsset"] == market.base:
                # on a buy it already came off the quantity received
                if order.get("side") == "SELL":
                    total += float(f["commission"]) * float(f["price"])
            elif f["commissionAsset"] != market.quote:
                others[f["commissionAsset"]] = others.get(f["commissionAsset"], 0.0) + float(f["commission"])
        for asset, amount in others.items():
            try:
                price = float(self.data.klines(asset + market.quote, "1m", limit=1)["close"].iloc[-1])
            except (BinanceError, requests.RequestException):
                log(f"Taxa de {amount:g} {asset} não convertida para {market.quote}, ficou fora do resultado.")
                continue
            total += amount * price
        return total

    def buy(self, state: dict, n: float) -> None:
        market, s = self.market, self.s
        if s["atr_stop"] != "none" and not n > 0:
            log("Sinal de compra ignorado: ATR ainda não calculado.")
            return
        price = float(self.data.klines(self.symbol, "1m", limit=1)["close"].iloc[-1])
        spend = self._spend(state, n, price)
        if spend < market.min_notional:
            log(f"Sinal de compra ignorado: valor da compra ({spend:.2f}) abaixo do mínimo da corretora.")
            return

        order = self.client.market_buy(self.symbol, spend, tag=BOT_TAG)
        estimate_missing_fees(order, market)
        qty = Decimal(order["executedQty"]) - fee_in(order, market.base)
        cost = float(order["cummulativeQuoteQty"]) + self._fee_cost(order)
        avg = cost / float(qty)
        state["symbol"] = self.symbol
        state["position_qty"] = str(qty)
        state["entry_cost"] = cost
        state["protection"] = None
        state["peak"] = avg
        state["entry_candle"] = state["last_candle"] + self.period_ms
        state["gale_due"] = False
        state["stop_sl"] = None
        if s["atr_stop"] != "none":
            state["stop_sl"] = float(market.round_price(avg - s["atr_mult"] * n))
        elif s["stop_loss_pct"]:
            state["stop_sl"] = float(market.round_price(avg * (1 - s["stop_loss_pct"] / 100)))
        state["stop_tp"] = float(market.round_price(avg * (1 + s["take_profit_pct"] / 100))) if s["take_profit_pct"] else None
        save_state(state)
        record_trade(self.symbol, "COMPRA", qty, cost, None)
        label = f"COMPRA (gale {state['gale']})" if state["gale"] else "COMPRA"
        log(f"{label}: {qty} {market.base} por {cost:.2f} {market.quote}")
        self._restore_protection(state)

    def _restore_protection(self, state: dict) -> bool:
        sl, tp = state["stop_sl"], state["stop_tp"]
        if state["protection"] or Decimal(state["position_qty"]) <= 0 or not (sl or tp):
            return False
        if state["pending_ids"]:
            self._resolve_pending(state)
            if self._check_protection(state):
                return True
            if state["protection"]:
                return False
        # the order goes to self.client, so check against its price, not the market data one
        price = float(self.client.klines(self.symbol, "1m", limit=1)["close"].iloc[-1])
        if (sl and price <= sl) or (tp and price >= tp):
            log(f"Preço ({price:.{self.market.digits}f}) já passou do stop, vendendo agora.")
            self.sell(state)
            return True
        qty = self.market.floor_qty(Decimal(state["position_qty"]))
        try:
            state["protection"] = self._place_protection(
                state, qty, self.market.round_price(sl) if sl else None, self.market.round_price(tp) if tp else None
            )
            state["pending_ids"] = []
        except (BinanceError, requests.RequestException) as e:
            log(f"Compra sem stop, tentando criar de novo no próximo candle: {e}")
        save_state(state)
        return False

    def _resolve_pending(self, state: dict) -> None:
        # a placement whose response was lost may still have gone through, and even filled since
        if not state["pending_ids"]:
            return
        found = []
        for client_id in state["pending_ids"]:
            try:
                found.append(self.client.get_order_by_client_id(self.symbol, client_id))
            except BinanceError as e:
                if e.code != ORDER_NOT_FOUND:
                    raise
        state["pending_ids"] = []
        if found:
            stops = [float(o["stopPrice"]) for o in found if o["type"].startswith("STOP_LOSS")]
            gains = [float(o["price"]) for o in found if o["type"] == "LIMIT_MAKER"]
            state["protection"] = {
                "list_id": found[0]["orderListId"],
                "order_ids": [o["orderId"] for o in found],
                "sl": stops[0] if stops else None,
                "tp": gains[0] if gains else None,
            }
            log("Stops do robô encontrados na Binance, acompanhando de novo.")
        save_state(state)

    def _place_protection(self, state: dict, qty: Decimal, sl_price: str | None, tp_price: str | None) -> dict:
        quantity = format(qty, "f")
        sl_id, tp_id = client_order_id(BOT_TAG, "sl"), client_order_id(BOT_TAG, "tp")
        state["pending_ids"] = [i for i, price in ((sl_id, sl_price), (tp_id, tp_price)) if price]
        save_state(state)
        if sl_price and tp_price:
            r = self.client.oco_sell(self.symbol, quantity, tp_price, sl_price, tp_client_id=tp_id, sl_client_id=sl_id)
            order_ids, list_id = [o["orderId"] for o in r["orders"]], r["orderListId"]
            log(f"Stops criados: stop loss {sl_price}, stop gain {tp_price}")
        elif sl_price:
            r = self.client.stop_loss_sell(self.symbol, quantity, sl_price, client_id=sl_id)
            order_ids, list_id = [r["orderId"]], -1
            log(f"Stop criado: stop loss {sl_price}")
        else:
            r = self.client.take_profit_sell(self.symbol, quantity, tp_price, client_id=tp_id)
            order_ids, list_id = [r["orderId"]], -1
            log(f"Stop criado: stop gain {tp_price}")
        return {
            "list_id": list_id,
            "order_ids": order_ids,
            "sl": float(sl_price) if sl_price else None,
            "tp": float(tp_price) if tp_price else None,
        }

    def _trail(self, state: dict, close: float, n: float) -> bool:
        if not state["stop_sl"] or not n > 0:
            return False
        peak = max(state.get("peak") or close, close)
        state["peak"] = peak
        new_sl = float(self.market.round_price(peak - self.s["atr_mult"] * n))
        if new_sl <= state["stop_sl"]:
            save_state(state)
            return False
        if new_sl >= close:
            log(f"Stop móvel ({new_sl:.{self.market.digits}f}) acima do fechamento, vendendo agora.")
            self.sell(state)
            return True

        if self._cancel_protection(state):
            return True
        state["stop_sl"] = new_sl
        save_state(state)
        log(f"Stop móvel subiu para {new_sl:.{self.market.digits}f}")
        return self._restore_protection(state)

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
            o["fills"] = self.client.order_fills(self.symbol, o["orderId"])
            proceeds = float(o["cummulativeQuoteQty"]) - self._fee_cost(o)
            pnl = proceeds - state["entry_cost"]
            record_trade(self.symbol, kind, qty, proceeds, pnl)
            log(f"{kind} executado: {qty} {self.market.base} por {proceeds:.2f} {self.market.quote} "
                f"(resultado {pnl:+.2f})")
            self._settle(state, pnl)
            self._clear_position(state)
            return True

        if all(o["status"] in DONE_STATUSES for o in orders):
            log("Os stops do robô foram cancelados fora dele. Criando de novo.")
            state["protection"] = None
            save_state(state)
        return False

    def _cancel_protection(self, state: dict) -> bool:
        protection = state.get("protection")
        if not protection:
            return False
        try:
            if protection["list_id"] != -1:
                self.client.cancel_order_list(self.symbol, protection["list_id"])
            else:
                self.client.cancel_order(self.symbol, protection["order_ids"][0])
        except BinanceError:
            # a stop that fills after the last check can no longer be cancelled
            if self._check_protection(state):
                return True
            if state["protection"]:
                raise
        state["protection"] = None
        return False

    def sell(self, state: dict) -> None:
        self._resolve_pending(state)
        if self._check_protection(state) or self._cancel_protection(state):
            return

        market = self.market
        qty = market.floor_qty(Decimal(state["position_qty"]))
        if qty < market.min_qty:
            log(f"Quantidade {qty} abaixo do mínimo para vender. Posição zerada no registro.")
        else:
            order = self.client.market_sell(self.symbol, format(qty, "f"), tag=BOT_TAG)
            estimate_missing_fees(order, market)
            proceeds = float(order["cummulativeQuoteQty"]) - self._fee_cost(order)
            pnl = proceeds - state["entry_cost"]
            record_trade(self.symbol, "VENDA", qty, proceeds, pnl)
            log(f"VENDA: {qty} {market.base} por {proceeds:.2f} {market.quote} (resultado {pnl:+.2f})")
            self._settle(state, pnl)
        self._clear_position(state)

    def _settle(self, state: dict, pnl: float) -> None:
        state["day_pnl"] += pnl
        steps = self.s["martingale_steps"]
        if pnl < 0 and state["gale"] < steps:
            state["gale"] += 1
            state["gale_due"] = True
            log(f"Perda: gale {state['gale']} no próximo candle, com {config.MARTINGALE_FACTOR ** state['gale']:g}x o valor.")
            return
        if pnl < 0 and steps:
            log("Perda também no último gale. Voltando ao valor normal.")
        state["gale"], state["gale_due"] = 0, False

    def _clear_position(self, state: dict) -> None:
        state["position_qty"] = "0"
        state["entry_candle"] = None
        state["entry_cost"] = 0.0
        state["protection"] = None
        state["stop_sl"] = None
        state["stop_tp"] = None
        state["pending_ids"] = []
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
