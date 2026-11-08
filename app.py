from __future__ import annotations

import os
import secrets
import hashlib
from contextvars import ContextVar
import sqlite3
import threading
import time as clock
import urllib.error
import urllib.request
import json
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request

from api_cache import ApiCache


API_ROOT = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1"
MOSCOW = ZoneInfo("Europe/Moscow")
BUY_TYPES = {
    "OPERATION_TYPE_BUY",
    "OPERATION_TYPE_BUY_CARD",
    "OPERATION_TYPE_BUY_MARGIN",
    "OPERATION_TYPE_DELIVERY_BUY",
    "OPERATION_TYPE_PRIMARY_ORDER",
}
SELL_TYPES = {
    "OPERATION_TYPE_SELL",
    "OPERATION_TYPE_SELL_CARD",
    "OPERATION_TYPE_SELL_MARGIN",
    "OPERATION_TYPE_DELIVERY_SELL",
}
COUPON_TYPES = {"OPERATION_TYPE_COUPON"}
DIVIDEND_TYPES = {"OPERATION_TYPE_DIVIDEND", "OPERATION_TYPE_DIVIDEND_TRANSFER", "OPERATION_TYPE_DIV_EXT"}
INCOME_TAX_TYPES = {
    "OPERATION_TYPE_DIVIDEND_TAX", "OPERATION_TYPE_DIVIDEND_TAX_PROGRESSIVE",
    "OPERATION_TYPE_BOND_TAX", "OPERATION_TYPE_BOND_TAX_PROGRESSIVE",
    "OPERATION_TYPE_TAX_CORRECTION_COUPON",
}
REPAYMENT_TYPES = {"OPERATION_TYPE_BOND_REPAYMENT", "OPERATION_TYPE_BOND_REPAYMENT_FULL"}
CASH_TYPES = COUPON_TYPES | DIVIDEND_TYPES | INCOME_TAX_TYPES | REPAYMENT_TYPES
TRADE_TYPES = sorted(BUY_TYPES | SELL_TYPES | CASH_TYPES)
CORPORATE_ACTIONS = {
    # Effective date and number of new shares received for one old share.
    "T": [(datetime(2026, 4, 15, tzinfo=timezone.utc), Decimal("10"))],
}

app = Flask(__name__)
REPORT_CACHE = ContextVar("report_cache", default=None)
JOBS = {}
JOBS_LOCK = threading.Lock()


class TBankError(RuntimeError):
    pass


def read_token() -> str:
    path = Path(os.getenv("TBANK_TOKEN_FILE", "~/.tbank_token")).expanduser()
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise TBankError(f"Не удалось прочитать токен из {path}: {exc}") from exc
    if not token:
        raise TBankError(f"Файл токена {path} пуст")
    return token.removeprefix("Bearer ").strip()


def form_settings(value=None):
    try:
        cache = ApiCache(os.getenv("TMON_CACHE_FILE", str(Path(__file__).parent / ".cache" / "api.sqlite3")))
        key = cache.key(hashlib.sha256(read_token().encode()).hexdigest(), "ui", "last_form", {})
        if value is not None:
            cache.set_setting(key, {field: value.get(field, "") for field in ("account_id", "date_from", "date_till", "ticker")})
            return value
        saved = cache.get_setting(key)
        return saved if isinstance(saved, dict) else {}
    except (TBankError, OSError, sqlite3.Error, ValueError):
        app.logger.warning("Could not persist or restore last report form")
        return {}


def cache_ttl(service: str, method: str, payload: dict) -> int:
    if method in {"GetPortfolio", "GetPositions"}:
        return 0
    if method in {"GetOperationsByCursor", "GetCandles", "GetAccruedInterests"}:
        try:
            end = datetime.fromisoformat(payload["to"].replace("Z", "+00:00"))
            if end > datetime.now(timezone.utc) - timedelta(days=2):
                return 0 if method == "GetOperationsByCursor" else 60
        except (KeyError, ValueError):
            return 0
        return 3600 if method == "GetOperationsByCursor" else 86400
    return 60 if method == "GetAccounts" else 86400


def api_call(service: str, method: str, payload: dict) -> dict:
    ttl = cache_ttl(service, method, payload)
    token = read_token()
    cache = ApiCache(os.getenv("TMON_CACHE_FILE", str(Path(__file__).parent / ".cache" / "api.sqlite3")))
    key = cache.key(hashlib.sha256(token.encode()).hexdigest(), service, method, payload)
    memo = REPORT_CACHE.get()
    if memo is not None and key in memo:
        return memo[key]
    if ttl:
        try:
            cached = cache.get(key)
            if cached is not None:
                if memo is not None:
                    memo[key] = cached
                return cached
        except (OSError, sqlite3.Error, ValueError):
            app.logger.warning("API cache read failed; requesting fresh data")
    response = uncached_api_call(service, method, payload, token)
    if ttl:
        try:
            cache.put(key, response, ttl)
        except (OSError, sqlite3.Error, ValueError):
            app.logger.warning("API cache write failed")
    if memo is not None:
        memo[key] = response
    return response


def uncached_api_call(service: str, method: str, payload: dict, token: str) -> dict:
    url = f"{API_ROOT}.{service}/{method}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise TBankError(f"T-Bank API вернул HTTP {exc.code}: {detail[:500]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TBankError(f"Не удалось подключиться к T-Bank API: {exc}") from exc


def get_accounts() -> list[dict]:
    data = api_call("UsersService", "GetAccounts", {"status": "ACCOUNT_STATUS_OPEN"})
    return [
        account
        for account in data.get("accounts", [])
        if account.get("id") and account.get("status") == "ACCOUNT_STATUS_OPEN"
    ]


def parse_period(date_from: str, date_till: str) -> tuple[str, str]:
    try:
        first = datetime.strptime(date_from.strip(), "%d.%m.%Y").date()
        last = datetime.strptime(date_till.strip(), "%d.%m.%Y").date()
    except ValueError as exc:
        raise ValueError("Введите даты в формате ДД.ММ.ГГГГ") from exc
    if first > last:
        raise ValueError("Дата начала не может быть позже даты окончания")
    start = datetime.combine(first, time.min, MOSCOW).astimezone(timezone.utc)
    end = datetime.combine(last + timedelta(days=1), time.min, MOSCOW).astimezone(timezone.utc)
    return start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")


def link_operation_metadata(operations: list[dict]) -> None:
    """Attach payouts to the same instrument as trades using explicit IDs."""
    metadata = {}
    for operation in operations:
        if operation.get("ticker"):
            for field in ("instrumentUid", "figi", "positionUid"):
                if operation.get(field):
                    metadata[operation[field]] = operation
    for operation in operations:
        for field in ("instrumentUid", "figi", "positionUid"):
            source = metadata.get(operation.get(field))
            if source:
                for key in ("ticker", "instrumentType", "classCode"):
                    if not operation.get(key) and source.get(key):
                        operation[key] = source[key]
                break


def get_operations(account_id: str, date_from: str, date_till: str, progress=None, all_types=False) -> list[dict]:
    parse_period(date_from, date_till)
    first = datetime.strptime(date_from, "%d.%m.%Y").date()
    last = datetime.strptime(date_till, "%d.%m.%Y").date()
    operations = []
    while first <= last:
        next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        chunk_last = min(last, next_month - timedelta(days=1))
        def chunk_progress(stage, completed, total):
            if progress:
                progress(stage, len(operations) + completed, total)
        operations.extend(get_operations_chunk(account_id, first.strftime("%d.%m.%Y"), chunk_last.strftime("%d.%m.%Y"), chunk_progress, all_types))
        first = next_month
    link_operation_metadata(operations)
    return operations


def get_operations_chunk(account_id: str, date_from: str, date_till: str, progress=None, all_types=False) -> list[dict]:
    start, end = parse_period(date_from, date_till)
    cursor = ""
    operations: list[dict] = []
    while True:
        payload = {
            "accountId": account_id,
            "from": start,
            "to": end,
            "limit": 1000,
            "operationTypes": TRADE_TYPES,
            "state": "OPERATION_STATE_EXECUTED",
            "withoutCommissions": False,
            "withoutTrades": True,
            "withoutOvernights": True,
        }
        if all_types:
            payload.pop("operationTypes")
        if cursor:
            payload["cursor"] = cursor
        page = api_call("OperationsService", "GetOperationsByCursor", payload)
        operations.extend(page.get("items", []))
        if progress:
            progress("Загрузка операций", len(operations), None)
        if not page.get("hasNext"):
            operations = [op for op in operations if start <= op.get("date", "") < end]
            link_operation_metadata(operations)
            return operations
        next_cursor = page.get("nextCursor")
        if not next_cursor or next_cursor == cursor:
            raise TBankError("API сообщил о следующей странице, но не вернул новый курсор")
        cursor = next_cursor


def get_closing_price(instrument_id: str, date_till: str) -> tuple[Decimal, str] | None:
    target = datetime.strptime(date_till.strip(), "%d.%m.%Y").date()
    earliest = target - timedelta(days=14)
    month = earliest.replace(day=1)
    candles = []
    while month <= target:
        next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        end_day = min(next_month, datetime.now(timezone.utc).date() + timedelta(days=1))
        if end_day <= month:
            break
        data = api_call("MarketDataService", "GetCandles", {
            "instrumentId": instrument_id,
            "from": datetime.combine(month, time.min, timezone.utc).isoformat().replace("+00:00", "Z"),
            "to": datetime.combine(end_day, time.min, timezone.utc).isoformat().replace("+00:00", "Z"),
            "interval": "CANDLE_INTERVAL_DAY",
            "candleSourceType": "CANDLE_SOURCE_EXCHANGE",
        })
        candles.extend(data.get("candles", []))
        month = next_month
    candidates = []
    for candle in candles:
        candle_time = candle.get("time", "")
        try:
            candle_date = datetime.fromisoformat(candle_time.replace("Z", "+00:00")).astimezone(MOSCOW).date()
        except ValueError:
            continue
        if earliest <= candle_date <= target:
            candidates.append((candle_time, candle))
    if not candidates:
        return None
    _, candle = max(candidates, key=lambda item: item[0])
    return decimal_value(candle.get("close")), candle.get("time", "")[:10]


def decimal_value(value: dict | None) -> Decimal:
    value = value or {}
    return Decimal(str(value.get("units", "0"))) + Decimal(str(value.get("nano", 0))) / Decimal(1_000_000_000)


def split_factor(ticker: str, operation_date: str, as_of_date: str | None) -> Decimal:
    if not as_of_date:
        return Decimal(1)
    report_date = datetime.strptime(as_of_date.strip(), "%d.%m.%Y").date()
    try:
        trade_time = datetime.fromisoformat(operation_date.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return Decimal(1)
    factor = Decimal(1)
    for effective_at, ratio in CORPORATE_ACTIONS.get(ticker, []):
        if report_date >= effective_at.date() and trade_time < effective_at:
            factor *= ratio
    return factor


def normalize_close_price(ticker: str, close_price: Decimal, close_date: str, as_of_date: str) -> Decimal:
    report_date = datetime.strptime(as_of_date.strip(), "%d.%m.%Y").date()
    candle_date = datetime.strptime(close_date, "%Y-%m-%d").date()
    for effective_at, ratio in CORPORATE_ACTIONS.get(ticker, []):
        if report_date >= effective_at.date() and candle_date < effective_at.date():
            close_price /= ratio
    return close_price


def is_bond(operation: dict) -> bool:
    return (str(operation.get("instrumentType", operation.get("instrumentKind", ""))).lower() in {"bond", "instrument_type_bond"}
            or operation.get("type") in COUPON_TYPES | REPAYMENT_TYPES
            or operation.get("type") in {"OPERATION_TYPE_BOND_TAX", "OPERATION_TYPE_BOND_TAX_PROGRESSIVE", "OPERATION_TYPE_TAX_CORRECTION_COUPON"})


def summarize(operations: list[dict], as_of_date: str | None = None) -> dict:
    link_operation_metadata(operations)
    totals = {
        "buy_quantity": 0,
        "sell_quantity": 0,
        "operation_count": len(operations),
    }
    money = defaultdict(lambda: {
        "buy_quantity": 0,
        "sell_quantity": 0,
        "buy_cost": Decimal(0),
        "sell_cost": Decimal(0),
        "commission": Decimal(0),
    })
    instruments = defaultdict(lambda: {
        "ticker": "",
        "name": "",
        "currency": "—",
        "buy_quantity": 0,
        "sell_quantity": 0,
        "buy_cost": Decimal(0),
        "sell_cost": Decimal(0),
        "commission": Decimal(0),
        "instrument_id": "",
        "is_bond": False,
        "coupons": Decimal(0),
        "dividends": Decimal(0),
        "income_tax": Decimal(0),
        "repayments": Decimal(0),
        "redeemed_quantity": 0,
        "cash_adjustment": Decimal(0),
    })
    for operation in operations:
        operation_type = operation.get("type")
        ticker = operation.get("ticker") or ""
        factor = split_factor(ticker, operation.get("date", ""), as_of_date)
        raw_quantity = int(operation.get("quantityDone") or operation.get("quantity") or 0)
        quantity = int(Decimal(raw_quantity) * factor)
        price = decimal_value(operation.get("price")) / factor
        price_currency = ((operation.get("price") or {}).get("currency") or (operation.get("payment") or {}).get("currency") or "—").upper()
        commission = decimal_value(operation.get("commission"))
        commission_currency = (operation.get("commission") or {}).get("currency", price_currency).upper()
        # Historical operations for the same ticker can contain different UID/FIGI
        # values (for example, after instrument metadata changes). The report is
        # user-facing and ticker-based, so merge those records into one row.
        instrument_key = operation.get("ticker") or operation.get("figi") or operation.get("instrumentUid") or "—"
        instrument = instruments[instrument_key]
        instrument["is_bond"] = instrument["is_bond"] or is_bond(operation)
        instrument["ticker"] = operation.get("ticker") or operation.get("figi") or "—"
        if not instrument["name"] or operation_type in BUY_TYPES | SELL_TYPES:
            instrument["name"] = operation.get("name") or operation.get("description") or ""
        instrument["currency"] = price_currency
        market_id = (
            f"{operation.get('ticker')}_{operation.get('classCode')}"
            if operation.get("ticker") and operation.get("classCode")
            else operation.get("instrumentUid") or operation.get("figi") or ""
        )
        if not instrument["instrument_id"] or operation.get("classCode") == "TQBR":
            instrument["instrument_id"] = market_id

        trade_cost = price * quantity
        if instrument["is_bond"] and operation_type in BUY_TYPES | SELL_TYPES and "payment" in operation:
            trade_cost = abs(decimal_value(operation["payment"]))
        if operation_type in CASH_TYPES:
            payment = decimal_value(operation.get("payment"))
            instrument["cash_adjustment"] += payment
            if operation_type in COUPON_TYPES:
                instrument["coupons"] += payment
            elif operation_type in DIVIDEND_TYPES:
                instrument["dividends"] += payment
            elif operation_type in INCOME_TAX_TYPES:
                instrument["income_tax"] -= payment
            elif operation_type in REPAYMENT_TYPES:
                instrument["repayments"] += payment
                if operation_type == "OPERATION_TYPE_BOND_REPAYMENT_FULL":
                    instrument["redeemed_quantity"] += quantity
        if operation_type in BUY_TYPES:
            totals["buy_quantity"] += quantity
            money[price_currency]["buy_quantity"] += quantity
            money[price_currency]["buy_cost"] += trade_cost
            instrument["buy_quantity"] += quantity
            instrument["buy_cost"] += trade_cost
        elif operation_type in SELL_TYPES:
            totals["sell_quantity"] += quantity
            money[price_currency]["sell_quantity"] += quantity
            money[price_currency]["sell_cost"] += trade_cost
            instrument["sell_quantity"] += quantity
            instrument["sell_cost"] += trade_cost
        if commission:
            money[commission_currency]["commission"] += abs(commission)
            instrument["commission"] += abs(commission)

    rows = []
    for currency, values in sorted(money.items()):
        buy_q = values["buy_quantity"]
        sell_q = values["sell_quantity"]
        rows.append({
            "currency": currency,
            "commission": values["commission"],
            "average_buy": values["buy_cost"] / buy_q if buy_q else None,
            "average_sell": values["sell_cost"] / sell_q if sell_q else None,
        })
    totals["money"] = rows
    totals["open_instruments"] = []
    totals["closed_instruments"] = []
    for values in sorted(instruments.values(), key=lambda item: (item["ticker"], item["name"])):
        buy_q = values["buy_quantity"]
        sell_q = values["sell_quantity"]
        disposed_q = sell_q + values["redeemed_quantity"]
        row = {
            **values,
            "quantity_delta": buy_q - disposed_q,
            "average_buy": values["buy_cost"] / buy_q if buy_q else None,
            "average_sell": values["sell_cost"] / sell_q if sell_q else None,
        }
        if buy_q > 0 and buy_q == disposed_q:
            row["pnl"] = values["sell_cost"] - values["buy_cost"] - values["commission"] + values["cash_adjustment"]
            totals["closed_instruments"].append(row)
        else:
            totals["open_instruments"].append(row)
    return totals


def add_hypothetical_pnl(result: dict, date_till: str, progress=None) -> None:
    for index, row in enumerate(result["open_instruments"]):
        if progress:
            progress("Оценка открытых позиций", index, len(result["open_instruments"]))
        row["close_price"] = None
        row["close_date"] = None
        row["hypothetical_pnl"] = None
        if row["quantity_delta"] < 0:
            row["valuation_note"] = "Продано больше, чем куплено за период: неизвестна себестоимость начальной позиции или шорта"
            continue
        if row.get("is_bond"):
            row["valuation_note"] = "Оценка остатка требует исторического номинала и НКД; выплаты учтены отдельно"
            continue
        if not row["instrument_id"]:
            continue
        try:
            close = get_closing_price(row["instrument_id"], date_till)
        except TBankError:
            continue
        if close is None:
            continue
        close_price, close_date = close
        close_price = normalize_close_price(row["ticker"], close_price, close_date, date_till)
        quantity_difference = row["buy_quantity"] - row["sell_quantity"]
        row["close_price"] = close_price
        row["close_date"] = close_date
        row["hypothetical_pnl"] = (
            row["sell_cost"] - row["buy_cost"]
            + Decimal(quantity_difference) * close_price
            - row["commission"] + row.get("cash_adjustment", Decimal(0))
        )


def add_tmon_benchmark(result: dict, operations: list[dict], date_till: str, progress=None) -> None:
    """Match purchase capital and FIFO holding periods, allowing fractional TMON units."""
    prices = {}
    dates = set()
    for operation in operations:
        if operation.get("type") in CASH_TYPES or is_bond(operation) or (operation.get("price") or {}).get("currency", "").upper() != "RUB":
            continue
        try:
            dates.add(operation.get("benchmark_day") or datetime.fromisoformat(operation.get("date", "").replace("Z", "+00:00")).astimezone(MOSCOW).strftime("%d.%m.%Y"))
        except ValueError:
            pass
    if dates:
        dates.add(date_till)
    if progress:
        ordered_dates = sorted(dates, key=lambda day: datetime.strptime(day, "%d.%m.%Y"))
        for index, day in enumerate(ordered_dates):
            progress("Котировки TMON · " + day, index, len(dates))
            try:
                prices[day] = get_closing_price("TMON_TQBR", day)
            except TBankError:
                prices[day] = None
        progress("Котировки TMON", len(dates), len(dates))

    def price_on(day):
        if day not in prices:
            prices[day] = get_closing_price("TMON_TQBR", day)
        close = prices[day]
        if close is None or close[0] <= 0:
            raise TBankError("Нет котировки TMON на " + day)
        return close[0]

    grouped = defaultdict(list)
    for operation in operations:
        key = operation.get("ticker") or operation.get("figi") or operation.get("instrumentUid") or "—"
        grouped[key].append(operation)
    for row in result["open_instruments"] + result["closed_instruments"]:
        row.update(tmon_pnl=None, tmon_difference=None, tmon_difference_pp=None, tmon_relative_percent=None, tmon_error=None)
        if row.get("is_bond"):
            continue
        if row.get("benchmark_blocked"):
            row["tmon_error"] = row["benchmark_blocked"]
            continue
        if row["currency"] != "RUB":
            row["tmon_error"] = "Только рублёвые инструменты"
            continue
        lots = []
        profit = Decimal(0)
        try:
            for op in sorted(grouped[row["ticker"]], key=lambda op: op.get("date", "")):
                timestamp = datetime.fromisoformat(op.get("date", "").replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    raise ValueError("Дата операции без часового пояса")
                day = op.get("benchmark_day") or timestamp.astimezone(MOSCOW).strftime("%d.%m.%Y")
                quantity = Decimal(str(op.get("quantityDone") or op.get("quantity") or 0))
                if not op.get("benchmark_normalized"):
                    quantity *= split_factor(row["ticker"], op.get("date", ""), date_till)
                if quantity <= 0:
                    continue
                if op.get("type") in BUY_TYPES:
                    capital = decimal_value(op.get("price")) * quantity / split_factor(row["ticker"], op.get("date", ""), date_till)
                    capital = op.get("benchmark_capital", capital)
                    lots.append([quantity, capital, capital / price_on(day)])
                elif op.get("type") in SELL_TYPES:
                    close = price_on(day)
                    while quantity > 0:
                        if not lots:
                            raise ValueError("Продажа позиции вне выбранного периода или шорт")
                        lot = lots[0]
                        matched = min(quantity, lot[0])
                        fraction = matched / lot[0]
                        profit += (lot[2] * close - lot[1]) * fraction
                        quantity -= matched
                        if matched == lot[0]:
                            lots.pop(0)
                        else:
                            lot[:] = [value * (1 - fraction) for value in lot]
            if lots:
                close = price_on(date_till)
                profit += sum((units * close - capital for _, capital, units in lots), Decimal(0))
            row["tmon_pnl"] = profit
            actual = row.get("pnl") if row.get("pnl") is not None else row.get("hypothetical_pnl")
            if actual is not None:
                row["tmon_difference"] = actual - profit
                if profit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) > 0:
                    row["tmon_relative_percent"] = (actual - profit) / profit * 100
                invested = row["buy_cost"] + (row.get("opening_value") or Decimal(0))
                if invested > 0:
                    row["tmon_difference_pp"] = (actual - profit) / invested * 100
        except (TBankError, ValueError) as exc:
            row["tmon_error"] = str(exc)


def sort_by_pnl(result: dict) -> None:
    pnl_totals = defaultdict(lambda: {"pnl": Decimal(0), "missing": 0, "count": 0})
    for group, field in (("open_instruments", "hypothetical_pnl"), ("closed_instruments", "pnl")):
        for row in result[group]:
            total = pnl_totals[row["currency"]]
            value = row.get(field)
            invested = row.get("buy_cost", Decimal(0)) + (row.get("opening_value") or Decimal(0))
            row["pnl_return_percent"] = value / invested * 100 if value is not None and invested > 0 else None
            if value is None:
                total["missing"] += 1
            else:
                total["pnl"] += value
                total["count"] += 1
    result["pnl_totals"] = [{"currency": currency, **values} for currency, values in sorted(pnl_totals.items())]
    for group, field in (("open_instruments", "hypothetical_pnl"), ("closed_instruments", "pnl")):
        result[group].sort(
            key=lambda row: (row.get(field) is not None, row.get(field) or Decimal(0)),
            reverse=True,
        )


@app.template_filter("rubles")
def format_rubles(value: Decimal | int | None) -> str:
    if value is None:
        return "—"
    value = Decimal(value)
    with localcontext() as context:
        context.prec = max(28, len(value.as_tuple().digits), value.adjusted() + 4)
        rounded = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{rounded:,.0f}".replace(",", " ")


@app.template_filter("number")
def format_number(value: Decimal | int | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        with localcontext() as context:
            context.prec = max(28, len(value.as_tuple().digits), value.adjusted() + 4)
            rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        text = f"{rounded:,.2f}"
    else:
        text = f"{value:,}"
    return text.replace(",", " ").replace(".", ",")


def build_period_report(account_id, date_from, date_till, ticker="", progress=None):
    marker = REPORT_CACHE.set({})
    try:
        return _build_period_report(account_id, date_from, date_till, ticker, progress)
    finally:
        REPORT_CACHE.reset(marker)


def _build_period_report(account_id, date_from, date_till, ticker="", progress=None):
    start_text, end_text = parse_period(date_from, date_till)
    start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
    end = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
    snapshot_at = datetime.now(timezone.utc)
    if start > snapshot_at or end > snapshot_at + timedelta(days=1):
        raise ValueError("Период не может быть в будущем")
    # Load every operation type: transfers and corporate actions also change balances.
    if progress:
        progress("Текущие позиции и история", 0, None)
    portfolio = api_call("OperationsService", "GetPortfolio", {"accountId": account_id})
    operations = get_operations(account_id, date_from, snapshot_at.astimezone(MOSCOW).strftime("%d.%m.%Y"), progress, all_types=True)
    link_operation_metadata(operations)
    positions = portfolio.get("positions", [])
    metadata = {p.get("ticker"): p for p in positions if p.get("ticker")}
    for op in operations:
        if op.get("ticker"):
            metadata.setdefault(op["ticker"], op)
    def key(op):
        return op.get("ticker") or op.get("figi") or op.get("instrumentUid") or "—"
    def instant(op):
        return datetime.fromisoformat(op["date"].replace("Z", "+00:00"))
    grouped = defaultdict(list)
    for op in operations:
        if not any(op.get(field) for field in ("ticker", "figi", "instrumentUid")):
            continue
        grouped[key(op)].append(op)
    current = defaultdict(Decimal)
    for position in positions:
        current[key(position)] += decimal_value(position.get("quantity"))
    all_keys = set(grouped) | set(current)
    before_day = (start.astimezone(MOSCOW).date() - timedelta(days=1)).strftime("%d.%m.%Y")
    result = {"open_instruments": [], "closed_instruments": [], "money": [], "operation_count": 0, "buy_quantity": 0, "sell_quantity": 0}
    benchmark_ops = []
    price_cache = {}
    def value_on(meta, day):
        instrument_id = meta.get("instrumentUid") or meta.get("figi")
        if not instrument_id and meta.get("ticker") and meta.get("classCode"):
            instrument_id = meta["ticker"] + "_" + meta["classCode"]
        # Prefer the exchange order book for funds with multiple trading modes.
        if meta.get("ticker") in {"TMON", "LQDT"}:
            instrument_id = meta["ticker"] + "_TQBR"
        cache_key = instrument_id, day
        if cache_key in price_cache:
            return price_cache[cache_key]
        close = get_closing_price(instrument_id, day) if instrument_id else None
        if not close:
            raise TBankError("Нет цены на " + day)
        price, close_date = close
        if is_bond(meta):
            day_date = datetime.strptime(day, "%d.%m.%Y").date()
            data = api_call("InstrumentsService", "GetAccruedInterests", {
                "instrumentId": meta.get("instrumentUid") or meta.get("figi") or instrument_id,
                "from": datetime.combine(day_date, time.min, timezone.utc).isoformat(),
                "to": datetime.combine(day_date + timedelta(days=1), time.min, timezone.utc).isoformat(),
            })
            entries = [entry for entry in data.get("accruedInterests", []) if entry.get("date", "")[:10] == day_date.isoformat()]
            if not entries or decimal_value(entries[0].get("nominal")) <= 0:
                raise TBankError("Нет исторического номинала/НКД на " + day)
            price = price * decimal_value(entries[0]["nominal"]) / 100 + decimal_value(entries[0].get("value"))
        else:
            price = normalize_close_price(meta.get("ticker", ""), price, close_date, date_till)
        price_cache[cache_key] = price, close_date
        return price, close_date
    for index, instrument_key in enumerate(sorted(all_keys)):
        if ticker and instrument_key.upper() != ticker:
            continue
        meta = metadata.get(instrument_key, {})
        if meta.get("instrumentType") in {"currency", "futures", "option"}:
            continue
        if progress:
            progress("Остатки и оценка · " + instrument_key, index, len(all_keys))
        ops = grouped[instrument_key]
        period_ops = [op for op in ops if start <= instant(op) < end]
        # Put quantities on the end-of-period split scale, including later trades.
        def factor_at(timestamp):
            factor = Decimal(1)
            for effective, ratio in CORPORATE_ACTIONS.get(instrument_key, []):
                if effective <= timestamp:
                    factor *= ratio
            return factor
        report_at = min(end - timedelta(microseconds=1), snapshot_at)
        report_factor = factor_at(report_at)
        def delta(op):
            quantity = Decimal(str(op.get("quantityDone") or op.get("quantity") or 0))
            quantity *= report_factor / factor_at(instant(op))
            kind = op.get("type")
            if kind in BUY_TYPES | {"OPERATION_TYPE_INPUT_SECURITIES"}:
                return quantity
            if kind in SELL_TYPES | {"OPERATION_TYPE_OUTPUT_SECURITIES", "OPERATION_TYPE_BOND_REPAYMENT_FULL"}:
                return -quantity
            return Decimal(0)
        now_quantity = current[instrument_key] * report_factor / factor_at(snapshot_at)
        opening = now_quantity - sum((delta(op) for op in ops), Decimal(0))
        closing = now_quantity - sum((delta(op) for op in ops if instant(op) >= end), Decimal(0))
        if not period_ops and not opening and not closing:
            continue
        # Reuse the operation aggregation for trade statistics and income breakdown.
        seed = {**meta, "ticker": instrument_key, "type": "PERIOD_POSITION", "quantity": "0", "price": {"currency": meta.get("currency") or (meta.get("currentPrice") or {}).get("currency", "rub")}}
        summary = summarize(period_ops or [seed], date_till)
        rows = summary["open_instruments"] + summary["closed_instruments"]
        row = rows[0]
        row.update(opening_quantity=opening, closing_quantity=closing, quantity_delta=closing,
                   opening_value=None, closing_value=None, close_price=None, close_date=None,
                   hypothetical_pnl=None, pnl=None, period_report=True)
        row["is_bond"] = is_bond(meta) or row["is_bond"]
        unsupported = [op for op in ops if op.get("type") not in set(TRADE_TYPES) | {"OPERATION_TYPE_INPUT_SECURITIES", "OPERATION_TYPE_OUTPUT_SECURITIES"} and int(op.get("quantityDone") or op.get("quantity") or 0)]
        try:
            if unsupported:
                raise TBankError("История содержит изменение количества, требующее обработки: " + unsupported[0]["type"])
            if opening < 0 or closing < 0:
                raise TBankError("Короткие позиции пока не поддерживаются")
            opening_price = value_on(meta, before_day)[0] if opening else Decimal(0)
            close_price, close_date = value_on(meta, date_till) if closing else (Decimal(0), None)
            row.update(opening_value=opening * opening_price, closing_value=closing * close_price,
                       close_price=close_price if closing else None, close_date=close_date)
            transfers = Decimal(0)
            for op in period_ops:
                if op.get("type") in {"OPERATION_TYPE_INPUT_SECURITIES", "OPERATION_TYPE_OUTPUT_SECURITIES"}:
                    day = instant(op).astimezone(MOSCOW).strftime("%d.%m.%Y")
                    transfers += delta(op) * value_on(meta, day)[0]
            pnl = row["closing_value"] - row["opening_value"] + row["sell_cost"] - row["buy_cost"] + row["cash_adjustment"] - row["commission"] - transfers
            row["pnl" if closing == 0 else "hypothetical_pnl"] = pnl
            if opening:
                benchmark_ops.append({**seed, "type": "OPERATION_TYPE_BUY", "date": start_text,
                                      "quantity": str(opening), "price": {"currency": row["currency"], "units": "0"},
                                      "benchmark_capital": row["opening_value"], "benchmark_day": before_day, "benchmark_normalized": True})
            if transfers:
                row["benchmark_blocked"] = "Сравнение при переводах бумаг пока недоступно"
        except (TBankError, ValueError) as exc:
            row["valuation_note"] = str(exc)
            row["benchmark_blocked"] = str(exc)
        result["closed_instruments" if closing == 0 else "open_instruments"].append(row)
        result["buy_quantity"] += row["buy_quantity"]
        result["sell_quantity"] += row["sell_quantity"]
        result["operation_count"] += len(period_ops)
        benchmark_ops.extend(period_ops)
    add_tmon_benchmark(result, benchmark_ops, date_till, progress)
    sort_by_pnl(result)
    return result



def run_report_job(job_id, form):
    def progress(stage, completed, total):
        calendar_percent = None
        if stage.startswith("Котировки TMON · "):
            day = datetime.strptime(stage.split(" · ")[1], "%d.%m.%Y").date()
            first = datetime.strptime(form["date_from"], "%d.%m.%Y").date()
            last = datetime.strptime(form["date_till"], "%d.%m.%Y").date()
            calendar_percent = min(100, max(0, (day - first).days / max(1, (last - first).days) * 100))
        with JOBS_LOCK:
            JOBS[job_id].update(stage=stage, completed=completed, total=total, calendar_percent=calendar_percent)

    try:
        accounts = get_accounts()
        if not any(account["id"] == form["account_id"] for account in accounts):
            raise ValueError("Выберите доступный счёт")
        form_settings(form)
        result = build_period_report(form["account_id"], form["date_from"], form["date_till"], form["ticker"], progress)
        with app.app_context():
            html = render_template("index.html", accounts=accounts, form=form, result=result, error=None)
        with JOBS_LOCK:
            JOBS[job_id].update(status="done", html=html)
    except Exception:
        app.logger.exception("Report calculation failed")
        with JOBS_LOCK:
            JOBS[job_id].update(status="error", error="Не удалось выполнить расчёт. Попробуйте снова или проверьте журнал сервиса.")


@app.post("/reports")
def start_report():
    form = {key: request.form.get(key, "").strip() for key in ("account_id", "date_from", "date_till", "ticker")}
    form["ticker"] = form["ticker"].upper()
    try:
        parse_period(form["date_from"], form["date_till"])
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    with JOBS_LOCK:
        now = clock.monotonic()
        for key in list(JOBS):
            if JOBS[key]["status"] != "running" and now - JOBS[key]["created"] > 3600:
                del JOBS[key]
        if sum(job["status"] == "running" for job in JOBS.values()) >= 2:
            return jsonify(error="Уже выполняются два расчёта. Дождитесь завершения."), 429
        job_id = secrets.token_urlsafe(24)
        JOBS[job_id] = dict(status="running", stage="Загрузка счетов", completed=0, total=None, created=now)
    threading.Thread(target=run_report_job, args=(job_id, form), daemon=True).start()
    return jsonify(job_id=job_id), 202


@app.get("/reports/<job_id>")
def report_progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify(error="Расчёт не найден. Возможно, сервис был перезапущен."), 404
        return jsonify({key: value for key, value in job.items() if key != "created"})


@app.route("/", methods=["GET", "POST"])
def index():
    today = datetime.now(MOSCOW).date()
    saved = form_settings()
    form = {
        "date_from": request.form.get("date_from", saved.get("date_from", today.replace(day=1).strftime("%d.%m.%Y"))),
        "date_till": request.form.get("date_till", saved.get("date_till", today.strftime("%d.%m.%Y"))),
        "account_id": request.form.get("account_id", saved.get("account_id", "")),
        "ticker": request.form.get("ticker", saved.get("ticker", "")).strip().upper(),
    }
    accounts: list[dict] = []
    result = None
    error = None
    try:
        accounts = get_accounts()
        if request.method == "POST":
            if not any(item["id"] == form["account_id"] for item in accounts):
                raise ValueError("Выберите доступный счёт")
            parse_period(form["date_from"], form["date_till"])
            form_settings(form)
            result = build_period_report(form["account_id"], form["date_from"], form["date_till"], form["ticker"])
    except (TBankError, ValueError) as exc:
        error = str(exc)
    return render_template("index.html", accounts=accounts, form=form, result=result, error=error)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
