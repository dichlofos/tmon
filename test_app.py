from decimal import Decimal
import unittest

from app import add_hypothetical_pnl, format_number, normalize_close_price, parse_period, summarize


def money(units, nano=0, currency="rub"):
    return {"units": str(units), "nano": nano, "currency": currency}


class CalculationsTest(unittest.TestCase):
    def test_money_is_rounded_to_hundredths(self):
        self.assertEqual(format_number(Decimal("12.345")), "12,35")
        self.assertEqual(format_number(Decimal("12")), "12,00")

    def test_period_is_inclusive_and_converted_from_moscow(self):
        self.assertEqual(parse_period("01.01.2026", "02.01.2026"), (
            "2025-12-31T21:00:00Z",
            "2026-01-02T21:00:00Z",
        ))

    def test_weighted_prices_quantities_and_commission(self):
        result = summarize([
            {"type": "OPERATION_TYPE_BUY", "quantityDone": "2", "price": money(100), "commission": money(-1)},
            {"type": "OPERATION_TYPE_BUY", "quantityDone": "1", "price": money(130), "commission": money(0, -500_000_000)},
            {"type": "OPERATION_TYPE_SELL", "quantityDone": "4", "price": money(150), "commission": money(-2)},
        ])
        self.assertEqual(result["buy_quantity"], 3)
        self.assertEqual(result["sell_quantity"], 4)
        self.assertEqual(result["money"][0]["average_buy"], Decimal("110"))
        self.assertEqual(result["money"][0]["average_sell"], Decimal("150"))
        self.assertEqual(result["money"][0]["commission"], Decimal("3.5"))
        self.assertEqual(len(result["open_instruments"]), 1)
        self.assertEqual(result["open_instruments"][0]["average_buy"], Decimal("110"))

    def test_closed_trade_pnl_after_commission(self):
        result = summarize([
            {"type": "OPERATION_TYPE_BUY", "ticker": "TEST", "quantity": "2", "price": money(100), "commission": money(-1)},
            {"type": "OPERATION_TYPE_SELL", "ticker": "TEST", "quantity": "2", "price": money(120), "commission": money(-1)},
        ])
        self.assertEqual(len(result["closed_instruments"]), 1)
        self.assertEqual(result["closed_instruments"][0]["pnl"], Decimal("38"))

    def test_same_ticker_with_different_uids_is_one_row(self):
        result = summarize([
            {"type": "OPERATION_TYPE_BUY", "ticker": "CHMF", "instrumentUid": "old", "quantity": "1", "price": money(900)},
            {"type": "OPERATION_TYPE_BUY", "ticker": "CHMF", "instrumentUid": "new", "quantity": "2", "price": money(1050)},
        ])
        self.assertEqual(len(result["open_instruments"]), 1)
        self.assertEqual(result["open_instruments"][0]["ticker"], "CHMF")
        self.assertEqual(result["open_instruments"][0]["buy_quantity"], 3)
        self.assertEqual(result["open_instruments"][0]["quantity_delta"], 3)
        self.assertEqual(result["open_instruments"][0]["average_buy"], Decimal("1000"))

    def test_hypothetical_pnl_sells_remaining_quantity_at_close(self):
        result = summarize([
            {"type": "OPERATION_TYPE_BUY", "ticker": "TEST", "instrumentUid": "uid", "quantity": "3", "price": money(100), "commission": money(-1)},
            {"type": "OPERATION_TYPE_SELL", "ticker": "TEST", "instrumentUid": "uid", "quantity": "1", "price": money(120), "commission": money(-1)},
        ])
        import app
        original = app.get_closing_price
        app.get_closing_price = lambda *_: (Decimal("110"), "2026-01-02")
        try:
            add_hypothetical_pnl(result, "02.01.2026")
        finally:
            app.get_closing_price = original
        self.assertEqual(result["open_instruments"][0]["hypothetical_pnl"], Decimal("38"))

    def test_t_split_normalizes_old_trade_when_report_crosses_split(self):
        result = summarize([
            {"date": "2026-04-10T10:00:00Z", "type": "OPERATION_TYPE_BUY", "ticker": "T", "quantity": "2", "price": money(3200)},
            {"date": "2026-04-20T10:00:00Z", "type": "OPERATION_TYPE_SELL", "ticker": "T", "quantity": "20", "price": money(330)},
        ], "30.04.2026")
        row = result["closed_instruments"][0]
        self.assertEqual(row["buy_quantity"], 20)
        self.assertEqual(row["sell_quantity"], 20)
        self.assertEqual(row["average_buy"], Decimal("320"))
        self.assertEqual(row["pnl"], Decimal("200"))

    def test_t_split_does_not_change_report_ending_before_split(self):
        result = summarize([
            {"date": "2026-04-10T10:00:00Z", "type": "OPERATION_TYPE_BUY", "ticker": "T", "quantity": "2", "price": money(3200)},
        ], "10.04.2026")
        row = result["open_instruments"][0]
        self.assertEqual(row["buy_quantity"], 2)
        self.assertEqual(row["average_buy"], Decimal("3200"))

    def test_pre_split_close_is_normalized_during_trading_pause(self):
        self.assertEqual(
            normalize_close_price("T", Decimal("3200"), "2026-04-10", "16.04.2026"),
            Decimal("320"),
        )


if __name__ == "__main__":
    unittest.main()


class TmonTest(unittest.TestCase):
    def test_fractional_units_and_fifo_partial_sale(self):
        from unittest.mock import patch
        from app import add_tmon_benchmark
        operations = [
            {"ticker": "TEST", "type": "OPERATION_TYPE_BUY", "date": "2026-01-01T10:00:00Z", "quantity": "2", "price": money(75)},
            {"ticker": "TEST", "type": "OPERATION_TYPE_SELL", "date": "2026-01-02T10:00:00Z", "quantity": "1", "price": money(90)},
        ]
        result = summarize(operations)
        result["open_instruments"][0]["hypothetical_pnl"] = Decimal(40)
        prices = {"01.01.2026": Decimal(100), "02.01.2026": Decimal(110), "03.01.2026": Decimal(120)}
        with patch("app.get_closing_price", side_effect=lambda instrument, day: (prices[day], "2026-01-01")):
            add_tmon_benchmark(result, operations, "03.01.2026")
        row = result["open_instruments"][0]
        self.assertEqual(row["tmon_pnl"], Decimal("22.5"))
        self.assertEqual(row["tmon_difference"], Decimal("17.5"))
        self.assertEqual(row["tmon_difference_pp"], Decimal("17.5") / Decimal(150) * 100)

    def test_missing_quotes_do_not_hide_report(self):
        from unittest.mock import patch
        from app import add_tmon_benchmark
        operations = [{"ticker": "TEST", "type": "OPERATION_TYPE_BUY", "date": "2026-01-01T10:00:00Z", "quantity": "1", "price": money(75)}]
        result = summarize(operations)
        with patch("app.get_closing_price", return_value=None):
            add_tmon_benchmark(result, operations, "03.01.2026")
        self.assertIsNone(result["open_instruments"][0]["tmon_pnl"])
        self.assertTrue(result["open_instruments"][0]["tmon_error"])


class BondBenchmarkTest(unittest.TestCase):
    def test_bonds_are_excluded_without_fetching_quotes(self):
        from unittest.mock import patch
        from app import add_tmon_benchmark, app
        operations = [{"ticker": "BOND", "instrumentType": "bond", "type": "OPERATION_TYPE_BUY", "date": "2026-01-01T10:00:00Z", "quantity": "1", "price": money(100)}]
        result = summarize(operations)
        with patch("app.get_closing_price") as quote:
            add_tmon_benchmark(result, operations, "03.01.2026", lambda *args: None)
            quote.assert_not_called()
        row = result["open_instruments"][0]
        row.update(close_price=None, close_date=None, hypothetical_pnl=None)
        self.assertTrue(row["is_bond"])
        self.assertIsNone(row["tmon_pnl"])
        with app.app_context():
            html = app.jinja_env.get_template("index.html").render(accounts=[], form={}, result=result, error=None)
        self.assertNotIn('<h2 class="h5">Сравнение с TMON</h2>', html)


class IncomeTest(unittest.TestCase):
    def test_dividends_and_tax_are_in_closed_pnl(self):
        operations = [
            {"ticker": "TEST", "type": "OPERATION_TYPE_BUY", "quantity": "1", "price": money(100)},
            {"ticker": "TEST", "type": "OPERATION_TYPE_SELL", "quantity": "1", "price": money(110)},
            {"ticker": "TEST", "type": "OPERATION_TYPE_DIVIDEND", "payment": money(20)},
            {"ticker": "TEST", "type": "OPERATION_TYPE_DIVIDEND_TAX", "payment": money(-3)},
        ]
        row = summarize(operations)["closed_instruments"][0]
        self.assertEqual(row["pnl"], Decimal(27))
        self.assertEqual(row["dividends"], Decimal(20))
        self.assertEqual(row["income_tax"], Decimal(3))
        self.assertEqual(row["currency"], "RUB")

    def test_amortization_coupon_and_full_redemption(self):
        operations = [
            {"ticker": "BOND", "instrumentType": "bond", "type": "OPERATION_TYPE_BUY", "quantity": "2", "price": money(990), "payment": money(-2000)},
            {"ticker": "BOND", "type": "OPERATION_TYPE_BOND_REPAYMENT", "quantity": "2", "payment": money(400)},
            {"ticker": "BOND", "type": "OPERATION_TYPE_COUPON", "payment": money(100)},
            {"ticker": "BOND", "type": "OPERATION_TYPE_BOND_TAX", "payment": money(-13)},
        ]
        result = summarize(operations)
        self.assertEqual(result["open_instruments"][0]["quantity_delta"], 2)
        operations.append({"ticker": "BOND", "type": "OPERATION_TYPE_BOND_REPAYMENT_FULL", "quantity": "2", "payment": money(1600)})
        row = summarize(operations)["closed_instruments"][0]
        self.assertEqual(row["pnl"], Decimal(87))
        self.assertEqual(row["quantity_delta"], 0)
        self.assertEqual(row["repayments"], Decimal(2000))

    def test_income_is_in_open_pnl(self):
        from unittest.mock import patch
        operations = [
            {"ticker": "TEST", "instrumentUid": "uid", "type": "OPERATION_TYPE_BUY", "quantity": "1", "price": money(100)},
            {"ticker": "TEST", "type": "OPERATION_TYPE_DIVIDEND", "payment": money(20)},
            {"ticker": "TEST", "type": "OPERATION_TYPE_DIVIDEND_TAX", "payment": money(-3)},
        ]
        result = summarize(operations)
        with patch("app.get_closing_price", return_value=(Decimal(110), "2026-01-01")):
            add_hypothetical_pnl(result, "01.01.2026")
        self.assertEqual(result["open_instruments"][0]["hypothetical_pnl"], Decimal(27))


class PayoutGroupingTest(unittest.TestCase):
    def test_payout_without_ticker_is_added_to_instrument_row(self):
        result = summarize([
            {"ticker": "TEST", "instrumentUid": "uid", "name": "Instrument", "type": "OPERATION_TYPE_BUY", "quantity": "1", "price": money(100)},
            {"instrumentUid": "uid", "name": "Dividend payment", "type": "OPERATION_TYPE_DIVIDEND", "payment": money(20)},
            {"instrumentUid": "uid", "type": "OPERATION_TYPE_DIVIDEND_TAX", "payment": money(-3)},
        ])
        self.assertEqual(len(result["open_instruments"]), 1)
        row = result["open_instruments"][0]
        self.assertEqual(row["ticker"], "TEST")
        self.assertEqual(row["name"], "Instrument")
        self.assertEqual(row["cash_adjustment"], Decimal(17))


class PnlSortTest(unittest.TestCase):
    def test_profit_descending_with_missing_values_last(self):
        from app import sort_by_pnl
        result = {"open_instruments": [
            {"hypothetical_pnl": None}, {"hypothetical_pnl": Decimal(-10)},
            {"hypothetical_pnl": Decimal(0)}, {"hypothetical_pnl": Decimal(20)},
        ], "closed_instruments": [{"pnl": Decimal(-1)}, {"pnl": Decimal(3)}]}
        for row in result["open_instruments"] + result["closed_instruments"]:
            row["currency"] = "RUB"
        sort_by_pnl(result)
        self.assertEqual(result["pnl_totals"], [{"currency": "RUB", "pnl": Decimal(12), "missing": 1, "count": 5}])
        self.assertEqual([r["hypothetical_pnl"] for r in result["open_instruments"]], [Decimal(20), Decimal(0), Decimal(-10), None])
        self.assertEqual([r["pnl"] for r in result["closed_instruments"]], [Decimal(3), Decimal(-1)])


class InitialPositionTest(unittest.TestCase):
    def test_excess_sales_are_not_valued_as_a_short(self):
        from unittest.mock import patch
        result = summarize([
            {"ticker": "LQDT", "instrumentUid": "uid", "type": "OPERATION_TYPE_BUY", "quantity": "1", "price": money(2)},
            {"ticker": "LQDT", "instrumentUid": "uid", "type": "OPERATION_TYPE_SELL", "quantity": "10", "price": money(2)},
        ])
        with patch("app.get_closing_price") as quote:
            add_hypothetical_pnl(result, "08.11.2026")
            quote.assert_not_called()
        self.assertIsNone(result["open_instruments"][0]["hypothetical_pnl"])
        self.assertIn("начальной позиции", result["open_instruments"][0]["valuation_note"])


class PeriodReportTest(unittest.TestCase):
    def report(self, positions, operations, prices, ticker="TEST", accrued=None):
        from unittest.mock import patch
        from app import build_period_report
        def api(service, method, payload):
            if method == "GetPortfolio":
                return {"positions": positions}
            if method == "GetAccruedInterests":
                day = payload["from"][:10]
                return {"accruedInterests": [{"date": day + "T00:00:00Z", "nominal": money(800), "value": money(10)}]}
            raise AssertionError(method)
        with patch("app.api_call", side_effect=api), patch("app.get_operations", return_value=operations), patch("app.get_closing_price", side_effect=lambda instrument, day: (Decimal(prices[day]), "2025-12-31")), patch("app.add_tmon_benchmark"):
            return build_period_report("test", "01.01.2026", "31.01.2026", ticker)

    def test_opening_position_sale_does_not_become_short(self):
        ops = [{"ticker": "TEST", "instrumentUid": "uid", "type": "OPERATION_TYPE_SELL", "date": "2026-01-10T10:00:00Z", "quantity": "10", "price": money(110)}]
        result = self.report([], ops, {"31.12.2025": "100"})
        row = result["closed_instruments"][0]
        self.assertEqual(row["opening_quantity"], Decimal(10))
        self.assertEqual(row["closing_quantity"], Decimal(0))
        self.assertEqual(row["pnl"], Decimal(100))

    def test_position_without_trades_includes_dividends_and_tax(self):
        position = {"ticker": "TEST", "instrumentUid": "uid", "instrumentType": "share", "quantity": money(10)}
        ops = [
            {"ticker": "TEST", "type": "OPERATION_TYPE_DIVIDEND", "date": "2026-01-10T10:00:00Z", "payment": money(20)},
            {"ticker": "TEST", "type": "OPERATION_TYPE_DIVIDEND_TAX", "date": "2026-01-10T10:00:00Z", "payment": money(-3)},
        ]
        row = self.report([position], ops, {"31.12.2025": "100", "31.01.2026": "110"})["open_instruments"][0]
        self.assertEqual(row["opening_quantity"], Decimal(10))
        self.assertEqual(row["hypothetical_pnl"], Decimal(117))

    def test_trades_after_period_are_reversed(self):
        position = {"ticker": "TEST", "instrumentUid": "uid", "quantity": money(15)}
        ops = [{"ticker": "TEST", "type": "OPERATION_TYPE_BUY", "date": "2026-02-10T10:00:00Z", "quantity": "5", "price": money(120)}]
        row = self.report([position], ops, {"31.12.2025": "100", "31.01.2026": "110"})["open_instruments"][0]
        self.assertEqual(row["closing_quantity"], Decimal(10))
        self.assertEqual(row["hypothetical_pnl"], Decimal(100))

    def test_bond_amortization_uses_historical_nominal_and_nkd(self):
        position = {"ticker": "TEST", "instrumentUid": "uid", "instrumentType": "bond", "quantity": money(2)}
        ops = [{"ticker": "TEST", "type": "OPERATION_TYPE_COUPON", "date": "2026-01-10T10:00:00Z", "payment": money(50)}]
        row = self.report([position], ops, {"31.12.2025": "100", "31.01.2026": "101"})["open_instruments"][0]
        self.assertEqual(row["opening_value"], Decimal(1620))
        self.assertEqual(row["closing_value"], Decimal(1636))
        self.assertEqual(row["hypothetical_pnl"], Decimal(66))


class OpeningBenchmarkTest(unittest.TestCase):
    def test_opening_capital_is_invested_in_tmon(self):
        from unittest.mock import patch
        from app import add_tmon_benchmark
        row = {"ticker": "TEST", "currency": "RUB", "buy_cost": Decimal(0), "opening_value": Decimal(1000), "pnl": Decimal(150)}
        result = {"open_instruments": [], "closed_instruments": [row]}
        operations = [
            {"ticker": "TEST", "type": "OPERATION_TYPE_BUY", "date": "2026-01-01T00:00:00Z", "quantity": "10", "benchmark_capital": Decimal(1000), "benchmark_day": "31.12.2025", "benchmark_normalized": True},
            {"ticker": "TEST", "type": "OPERATION_TYPE_SELL", "date": "2026-01-10T10:00:00Z", "quantity": "10"},
        ]
        prices = {"31.12.2025": Decimal(100), "10.01.2026": Decimal(110)}
        with patch("app.get_closing_price", side_effect=lambda instrument, day: (prices[day], "2026-01-01")):
            add_tmon_benchmark(result, operations, "31.01.2026")
        self.assertEqual(row["tmon_pnl"], Decimal(100))
        self.assertEqual(row["tmon_difference"], Decimal(50))
        self.assertEqual(row["tmon_difference_pp"], Decimal(5))
        self.assertEqual(row["tmon_relative_percent"], Decimal(50))


class ReturnPercentTest(unittest.TestCase):
    def test_return_uses_opening_capital_and_purchases(self):
        from app import sort_by_pnl
        row = {"currency": "RUB", "pnl": Decimal(43901), "buy_cost": Decimal(86448), "opening_value": Decimal(100000)}
        result = {"open_instruments": [], "closed_instruments": [row]}
        sort_by_pnl(result)
        self.assertEqual(row["pnl_return_percent"], Decimal(43901) / Decimal(186448) * 100)


class NearZeroBenchmarkTest(unittest.TestCase):
    def test_negligible_tmon_profit_has_no_relative_percent(self):
        from unittest.mock import patch
        from app import add_tmon_benchmark, app
        row = {"ticker": "TEST", "currency": "RUB", "buy_cost": Decimal(100), "pnl": Decimal(20)}
        result = {"open_instruments": [], "closed_instruments": [row]}
        operations = [
            {"ticker": "TEST", "type": "OPERATION_TYPE_BUY", "date": "2026-01-01T10:00:00Z", "quantity": "1", "price": money(100)},
            {"ticker": "TEST", "type": "OPERATION_TYPE_SELL", "date": "2026-01-02T10:00:00Z", "quantity": "1"},
        ]
        prices = [Decimal(3), Decimal('3.000000000000000000000000001')]
        with patch('app.get_closing_price', side_effect=lambda *args: (prices.pop(0), '2026-01-01')):
            add_tmon_benchmark(result, operations, '02.01.2026')
        self.assertIsNone(row['tmon_relative_percent'])
        with app.app_context():
            module = app.jinja_env.get_template("index.html").make_module({"form": {}, "accounts": [], "result": None})
            self.assertNotIn("%", str(module.tmon_cells(row)))

    def test_large_decimal_formats_without_crashing(self):
        from app import format_rubles
        self.assertTrue(format_number(Decimal('1E+30')).endswith(',00'))
        self.assertTrue(format_rubles(Decimal('1E+30')).startswith('1 '))
