import copy
import unittest

from wind_map.aggregates import allocations_for, build_totals, region_tree
from wind_map.core import WindError, date_value
from wind_map.events import permit_state


KNOWN = "2026-11-13T00:00:00+00:00"
PERMIT = {"id": "synthetic-permit", "project_id": "synthetic", "authority": "synthetic", "number": "synthetic"}


def fact(ident, kind, when, **extra):
    return {"id": ident, "type": kind, "permit_id": PERMIT["id"], "date": date_value(when),
            "known_at": KNOWN, "review_state": "approved", "evidence_id": "synthetic", **extra}


def grant():
    return fact("g", "grant", "2026-01-01", capacity_w=10_000_000, is_initial=True,
                preparation_deadline=date_value("2029-12-31"), construction_plan_deadline=date_value("2027-12-31"))


class ExtensionDeadlineTests(unittest.TestCase):
    def test_both_deadlines_change_at_effective_and_known_times_without_changing_capacity(self):
        extension = fact("x", "extend", "2026-02-01", preparation_deadline=date_value("2030-12-31"),
                         construction_plan_deadline=date_value("2028-12-31"))
        extension["known_at"] = "2026-11-13T01:00:00+00:00"
        events = [grant(), extension]
        for as_of, known in [("2026-01-31", extension["known_at"]), ("2026-02-01", KNOWN)]:
            with self.subTest(as_of=as_of, known=known):
                state = permit_state(PERMIT, events, as_of, known)
                self.assertEqual(state["construction_plan_deadline"], date_value("2027-12-31"))
                self.assertEqual(state["preparation_deadline"], date_value("2029-12-31"))
        state = permit_state(PERMIT, events, "2026-02-01", extension["known_at"])
        self.assertEqual(state["construction_plan_deadline"], date_value("2028-12-31"))
        self.assertEqual(state["preparation_deadline"], date_value("2030-12-31"))
        self.assertEqual((state["capacity_w"], state["capacity_event_id"]), (10_000_000, "g"))
        self.assertEqual(state["first_ebl_date"], date_value("2026-01-01"))
        self.assertEqual(state["capacity_ebl_date"], date_value("2026-01-01"))

    def test_construction_only_extension_preserves_preparation_and_does_not_revive_cancelled_permit(self):
        events = [grant(), fact("c", "cancel", "2026-02-01"),
                  fact("x", "extend", "2026-03-01", construction_plan_deadline=date_value("2028-12"))]
        state = permit_state(PERMIT, events, "2026-03-01", KNOWN)
        self.assertEqual(state["construction_plan_deadline"], date_value("2028-12"))
        self.assertEqual(state["preparation_deadline"], date_value("2029-12-31"))
        self.assertEqual((state["status"], state["capacity_w"], state["capacity_event_id"]), ("cancelled", 0, "c"))
        for extra in [{}, {"construction_plan_deadline": "2028-12-31"}]:
            with self.subTest(extra=extra), self.assertRaises(WindError):
                permit_state(PERMIT, [grant(), fact("bad", "extend", "2026-02-01", **extra)], "2026-03-01", KNOWN)


class CancelledRegionTests(unittest.TestCase):
    def setUp(self):
        self.regions = region_tree([{"id": "KR", "name": "synthetic root", "parent_id": None},
                                    {"id": "province", "name": "synthetic province", "parent_id": "KR"},
                                    {"id": "city", "name": "synthetic city", "parent_id": "province"}])
        self.state = permit_state(PERMIT, [grant(), fact("c", "cancel", "2026-03-01")], "2026-03-01", KNOWN)
        self.zero = {"id": "a-zero", "permit_id": PERMIT["id"], "capacity_event_id": "c",
                     "date": date_value("2026-03-01"), "known_at": KNOWN, "review_state": "approved",
                     "evidence_id": "synthetic", "parts": [{"region_id": "city", "capacity_w": 0}]}
        self.original = {**self.zero, "id": "a-grant", "capacity_event_id": "g", "date": date_value("2026-01-01"),
                         "parts": [{"region_id": "city", "capacity_w": 10_000_000}]}

    def test_explicit_current_zero_allocation_preserves_regional_cancelled_count_and_exact_total(self):
        parts = allocations_for(self.state, [self.original, self.zero], self.regions, "2026-03-01", KNOWN)
        self.assertEqual(parts, [{"region_id": "city", "capacity_w": 0, "unallocated": False, "evidence_id": "synthetic"}])
        totals = build_totals([{**self.state, "allocations": parts}], list(self.regions.values()), [])
        for region in totals["cancelled"]:
            self.assertEqual((region["capacity_w"], region["permit_count"], region["display_capacity_w"]), (0, 1, 0))
        for records in [[], [self.original], [self.original, {**self.zero, "review_state": "pending"}],
                        [self.original, {**self.zero, "known_at": "2026-11-14T00:00:00+00:00"}]]:
            with self.subTest(records=records):
                unknown = allocations_for(self.state, records, self.regions, "2026-03-01", KNOWN)
                self.assertEqual((unknown[0]["region_id"], unknown[0]["unallocated"]), ("KR", True))

    def test_current_zero_allocation_still_rejects_wrong_capacity_duplicate_parts_and_conflicts(self):
        wrong_capacity = copy.deepcopy(self.zero)
        wrong_capacity["parts"][0]["capacity_w"] = 1
        duplicate_parts = copy.deepcopy(self.zero)
        duplicate_parts["parts"].append(copy.deepcopy(duplicate_parts["parts"][0]))
        for records in [[wrong_capacity], [duplicate_parts], [self.zero, {**self.zero, "id": "other"}]]:
            with self.subTest(records=records), self.assertRaises(WindError):
                allocations_for(self.state, records, self.regions, "2026-03-01", KNOWN)
