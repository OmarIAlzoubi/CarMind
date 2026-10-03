from dataclasses import replace
from datetime import date, datetime, timezone, timedelta
import unittest

from carmind.maintenance import evaluate_maintenance, reminder_events, ReminderPolicy, add_months
from support import PACK, PROFILE, RULE, NOW, record


class MaintenanceTests(unittest.TestCase):
    def evaluate(self, mileage=24000, when=NOW, history=None, pack=PACK, **kwargs):
        return evaluate_maintenance(pack, PROFILE, "car", when, mileage, [record()] if history is None else history,
                                    operating_condition=kwargs.pop("operating_condition", "NORMAL"), **kwargs)

    def test_not_due(self):
        self.assertEqual(self.evaluate(when=datetime(2026, 3, 1, tzinfo=timezone.utc))[0].status, "NOT_DUE")

    def test_upcoming(self):
        self.assertEqual(self.evaluate(mileage=29200)[0].status, "UPCOMING")

    def test_due_by_mileage(self):
        self.assertEqual(self.evaluate(mileage=30000)[0].status, "DUE")

    def test_due_by_time(self):
        self.assertEqual(self.evaluate(when=datetime(2026, 7, 1, tzinfo=timezone.utc))[0].status, "DUE")

    def test_overdue(self):
        self.assertEqual(self.evaluate(mileage=30001)[0].status, "OVERDUE")

    def test_unknown_history(self):
        result = self.evaluate(history=[])[0]
        self.assertEqual(result.status, "UNKNOWN")
        self.assertIsNone(result.due_date)
        self.assertEqual(reminder_events((result,)), ())

    def test_unknown_mileage_without_time_trigger(self):
        self.assertEqual(self.evaluate(mileage=None)[0].status, "UNKNOWN")

    def test_known_overdue_time_wins_missing_mileage(self):
        self.assertEqual(self.evaluate(mileage=None, when=datetime(2026, 8, 1, tzinfo=timezone.utc))[0].status, "OVERDUE")

    def test_new_record_resets_only_matching_item(self):
        second = replace(RULE, rule_id="tires", maintenance_item="tires")
        pack = replace(PACK, rules=(RULE, second))
        original = [record(), record("tires", record_id="tires-old")]
        before = self.evaluate(mileage=29200, history=original, pack=pack)
        after = self.evaluate(mileage=29200, history=original + [record(mileage=28400, when=NOW, record_id="new-oil")], pack=pack)
        self.assertEqual(after[0].due_odometer_km, 38400)
        self.assertNotEqual(before[0].reminder_id, after[0].reminder_id)
        self.assertEqual(before[1], after[1])

    def test_reminder_provenance_and_determinism(self):
        first = self.evaluate(mileage=29200)
        self.assertEqual(first, self.evaluate(mileage=29200))
        self.assertEqual(first[0].manufacturer_rule_id, RULE.rule_id)
        self.assertEqual(first[0].source_id, RULE.source_id)
        self.assertEqual(first[0].remaining_km, 800)
        self.assertEqual(reminder_events(first), first)

    def test_rollback_rejected(self):
        with self.assertRaises(ValueError):
            self.evaluate(mileage=19000)

    def test_nonfinite_and_negative_inputs_rejected(self):
        for value in (-1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                self.evaluate(mileage=value)

    def test_future_service_does_not_reset(self):
        self.assertEqual(self.evaluate(), self.evaluate(history=[record(), record(when=NOW+timedelta(days=1), record_id="future")]))

    def test_time_only(self):
        rule = replace(RULE, trigger="TIME", interval_km=None)
        result = self.evaluate(mileage=None, pack=replace(PACK, rules=(rule,)), when=datetime(2026, 7, 1, tzinfo=timezone.utc))[0]
        self.assertEqual(result.status, "DUE")
        self.assertIsNone(result.due_odometer_km)

    def test_mileage_only(self):
        rule = replace(RULE, trigger="MILEAGE", interval_months=None)
        result = self.evaluate(mileage=30000, pack=replace(PACK, rules=(rule,)))[0]
        self.assertEqual(result.status, "DUE")
        self.assertIsNone(result.due_date)

    def test_and_requires_both(self):
        pack = replace(PACK, rules=(replace(RULE, trigger="AND"),))
        self.assertNotEqual(self.evaluate(mileage=30000, pack=pack)[0].status, "DUE")
        self.assertEqual(self.evaluate(mileage=30000, when=datetime(2026, 7, 1, tzinfo=timezone.utc), pack=pack)[0].status, "DUE")

    def test_policy_does_not_change_due_point(self):
        normal = self.evaluate(mileage=29200)[0]
        narrow = self.evaluate(mileage=29200, policy=ReminderPolicy(10, 1))[0]
        self.assertEqual(normal.due_odometer_km, narrow.due_odometer_km)
        self.assertEqual(narrow.status, "NOT_DUE")

    def test_wrong_market_unknown(self):
        self.assertEqual(evaluate_maintenance(PACK, replace(PROFILE, market="Other"), "car", NOW, 30000, [record()], "NORMAL")[0].status, "UNKNOWN")

    def test_unknown_or_wrong_condition(self):
        for condition in ("SEVERE", "UNKNOWN"):
            self.assertEqual(self.evaluate(operating_condition=condition)[0].status, "UNKNOWN")

    def test_calendar_months_clamp(self):
        self.assertEqual(add_months(date(2024, 1, 31), 1), date(2024, 2, 29))
        self.assertEqual(add_months(date(2024, 2, 29), 12), date(2025, 2, 28))

    def test_explicit_in_service_origin_and_first_repeat(self):
        rule = replace(RULE, interval_km=None, interval_months=None, first_due_km=20000, first_due_months=12, repeat_km=10000, repeat_months=6)
        pack = replace(PACK, rules=(rule,))
        first = self.evaluate(mileage=10000, history=[], pack=pack, in_service_date=date(2026, 1, 1))[0]
        self.assertEqual(first.due_odometer_km, 20000)
        self.assertEqual(first.due_date, date(2027, 1, 1))
        self.assertEqual(self.evaluate(pack=pack)[0].due_odometer_km, 30000)

    def test_no_llm_or_network_imports(self):
        import inspect
        import carmind.maintenance as module
        source = inspect.getsource(module)
        for name in ("openai", "requests", "urllib", "jev", "whatsapp", "planner_provider"):
            self.assertNotIn(name, source.lower())
