from __future__ import annotations

import os
import secrets
import threading
import time as clock
import urllib.error
import urllib.request
import json
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request


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
DIVIDEND_TYPES = {"OPERATION_TYPE_DIVIDEND", "OPERATION_TYPE_DIVIDEND_TRANSFER"}
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


def api_call(service: str, method: str, payload: dict) -> dict:
    url = f"{API_ROOT}.{service}/{method}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {read_token()}",
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


def get_operations(account_id: str, date_from: str, date_till: str, progress=None) -> list[dict]:
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
        if cursor:
            payload["cursor"] = cursor
        page = api_call("OperationsService", "GetOperationsByCursor", payload)
        operations.extend(page.get("items", []))
        if progress:
            progress("Загрузка операций", len(operations), None)
        if not page.get("hasNext"):
            # Payout records can omit ticker; join only using explicit instrument IDs.
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
            return operations
        next_cursor = page.get("nextCursor")
        if not next_cursor or next_cursor == cursor:
            raise TBankError("API сообщил о следующей странице, но не вернул новый курсор")
        cursor = next_cursor


def get_closing_price(instrument_id: str, date_till: str) -> tuple[Decimal, str] | None:
    target = datetime.strptime(date_till.strip(), "%d.%m.%Y").date()
    start = datetime.combine(target - timedelta(days=14), time.min, timezone.utc)
    end = datetime.combine(target + timedelta(days=1), time.min, timezone.utc)
    data = api_call("MarketDataService", "GetCandles", {
        "instrumentId": instrument_id,
        "from": start.isoformat().replace("+00:00", "Z"),
        "to": end.isoformat().replace("+00:00", "Z"),
        "interval": "CANDLE_INTERVAL_DAY",
        "candleSourceType": "CANDLE_SOURCE_EXCHANGE",
    })
    candidates = []
    for candle in data.get("candles", []):
        candle_time = candle.get("time", "")
        try:
            candle_date = datetime.fromisoformat(candle_time.replace("Z", "+00:00")).astimezone(MOSCOW).date()
        except ValueError:
            continue
        if candle_date <= target:
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
            dates.add(datetime.fromisoformat(operation.get("date", "").replace("Z", "+00:00")).astimezone(MOSCOW).strftime("%d.%m.%Y"))
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
        row.update(tmon_pnl=None, tmon_difference=None, tmon_difference_pp=None, tmon_error=None)
        if row.get("is_bond"):
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
                day = timestamp.astimezone(MOSCOW).strftime("%d.%m.%Y")
                quantity = Decimal(int(op.get("quantityDone") or op.get("quantity") or 0))
                quantity *= split_factor(row["ticker"], op.get("date", ""), date_till)
                if quantity <= 0:
                    continue
                if op.get("type") in BUY_TYPES:
                    capital = decimal_value(op.get("price")) * quantity / split_factor(row["ticker"], op.get("date", ""), date_till)
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
            actual = row.get("pnl", row.get("hypothetical_pnl"))
            if actual is not None:
                row["tmon_difference"] = actual - profit
                if row["buy_cost"] > 0:
                    row["tmon_difference_pp"] = (actual - profit) / row["buy_cost"] * 100
        except (TBankError, ValueError) as exc:
            row["tmon_error"] = str(exc)


@app.template_filter("rubles")
def format_rubles(value: Decimal | int | None) -> str:
    if value is None:
        return "—"
    rounded = Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{rounded:,.0f}".replace(",", " ")


@app.template_filter("number")
def format_number(value: Decimal | int | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        text = f"{rounded:,.2f}"
    else:
        text = f"{value:,}"
    return text.replace(",", " ").replace(".", ",")


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
        operations = get_operations(form["account_id"], form["date_from"], form["date_till"], progress)
        if form["ticker"]:
            operations = [op for op in operations if (op.get("ticker") or "").upper() == form["ticker"]]
        progress("Подсчёт операций", 0, None)
        result = summarize(operations, form["date_till"])
        add_hypothetical_pnl(result, form["date_till"], progress)
        add_tmon_benchmark(result, operations, form["date_till"], progress)
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
    form = {
        "date_from": request.form.get("date_from", today.replace(day=1).strftime("%d.%m.%Y")),
        "date_till": request.form.get("date_till", today.strftime("%d.%m.%Y")),
        "account_id": request.form.get("account_id", ""),
        "ticker": request.form.get("ticker", "").strip().upper(),
    }
    accounts: list[dict] = []
    result = None
    error = None
    try:
        accounts = get_accounts()
        if request.method == "POST":
            if not any(item["id"] == form["account_id"] for item in accounts):
                raise ValueError("Выберите доступный счёт")
            operations = get_operations(form["account_id"], form["date_from"], form["date_till"])
            if form["ticker"]:
                operations = [
                    operation for operation in operations
                    if (operation.get("ticker") or "").upper() == form["ticker"]
                ]
            result = summarize(operations, form["date_till"])
            add_hypothetical_pnl(result, form["date_till"])
            add_tmon_benchmark(result, operations, form["date_till"])
    except (TBankError, ValueError) as exc:
        error = str(exc)
    return render_template("index.html", accounts=accounts, form=form, result=result, error=error)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
