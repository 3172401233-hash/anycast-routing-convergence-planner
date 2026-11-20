import unittest

from anycast_planner import (
    PHASE_ANNOUNCED,
    PHASE_RAMPING,
    PHASE_WITHDRAWN,
    AnycastPlanner,
    ClientGroup,
    Plan,
    PlannerConfig,
    Pop,
)


def make_pops():
    return [
        Pop(name="pop-fra", region="eu", prefixes=("203.0.113.0/24",),
            neighbors=("upstream-a",), local_pref=200, convergence_slices=1,
            weight=1.0),
        Pop(name="pop-iad", region="na", prefixes=("203.0.113.0/24",),
            neighbors=("upstream-b",), local_pref=150, convergence_slices=2,
            weight=0.8),
        Pop(name="pop-nrt", region="apac", prefixes=("203.0.113.0/24",),
            neighbors=("upstream-c",), local_pref=100, convergence_slices=1,
            weight=0.6),
    ]


def make_clients():
    return [
        ClientGroup(name="clients-eu", cidr="198.51.100.0/25", share=0.5),
        ClientGroup(name="clients-na", cidr="198.51.100.128/25", share=0.3),
    ]


def healthy(slices):
    return [True] * slices


class PlannerTestBase(unittest.TestCase):
    def build(self, slices=10, **cfg_kwargs):
        cfg = PlannerConfig(slices=slices, **cfg_kwargs)
        return AnycastPlanner(make_pops(), make_clients(), cfg)


class TestSingleRegionFailure(PlannerTestBase):
    def test_withdrawal_after_consecutive_failures(self):
        planner = self.build(slices=8)
        probes = {
            "pop-fra": [True, False, False, False, False, False, False, False],
            "pop-iad": healthy(8),
            "pop-nrt": healthy(8),
        }
        plan = planner.plan(probes)

        # Decision at slice 3 (3rd consecutive failure), effective after
        # pop-fra's 1-slice convergence -> withdrawn from slice 4 on.
        self.assertEqual(plan.phase_of(2, "pop-fra").phase, PHASE_ANNOUNCED)
        self.assertEqual(plan.phase_of(4, "pop-fra").phase, PHASE_WITHDRAWN)
        self.assertEqual(plan.phase_of(4, "pop-fra").effective_weight, 0.0)

        # Traffic migrates from pop-fra to the next-best PoP (pop-iad).
        migrations = [m for m in plan.migrations if m.from_pop == "pop-fra"]
        self.assertTrue(migrations)
        for m in migrations:
            self.assertEqual(m.to_pop, "pop-iad")
            self.assertEqual(m.slice_index, 4)
        migrated_share = sum(m.traffic_share for m in migrations)
        self.assertAlmostEqual(migrated_share, 0.8)

        # Affected client ranges are reported.
        self.assertEqual(plan.affected_clients,
                         ["clients-eu", "clients-na"])

        # Rollback plan exists for the degraded PoP.
        actions = [s.action for s in plan.rollback if s.pop == "pop-fra"]
        self.assertEqual(actions,
                         ["verify-health", "re-announce", "await-convergence"])


class TestProbeFlapping(PlannerTestBase):
    def test_flapping_never_reaches_threshold(self):
        planner = self.build(slices=10)
        # Alternating failures: never 3 consecutive failures.
        flap = [True, False, False, True, False, False, True, False, False, True]
        probes = {
            "pop-fra": flap,
            "pop-iad": healthy(10),
            "pop-nrt": healthy(10),
        }
        plan = planner.plan(probes)

        for t in range(10):
            self.assertEqual(plan.phase_of(t, "pop-fra").phase,
                             PHASE_ANNOUNCED)
        # No withdrawal -> no cross-PoP migration at all.
        self.assertFalse(
            [m for m in plan.migrations if m.from_pop is not None])
        self.assertEqual(plan.affected_clients, [])
        self.assertEqual(plan.rollback, [])


class TestUpstreamConvergenceMismatch(PlannerTestBase):
    def test_convergence_delay_defers_effective_withdrawal(self):
        pops = make_pops()
        # Give pop-fra a much slower upstream (4 slices).
        pops[0] = Pop(name="pop-fra", region="eu",
                      prefixes=("203.0.113.0/24",),
                      neighbors=("upstream-a",), local_pref=200,
                      convergence_slices=4, weight=1.0)
        cfg = PlannerConfig(slices=10)
        planner = AnycastPlanner(pops, make_clients(), cfg)
        probes = {
            "pop-fra": [False] * 10,
            "pop-iad": healthy(10),
            "pop-nrt": healthy(10),
        }
        plan = planner.plan(probes)

        # Decision at slice 2 (threshold 3), effective at slice 2 + 4 = 6.
        self.assertEqual(plan.phase_of(5, "pop-fra").phase, PHASE_ANNOUNCED)
        self.assertEqual(plan.phase_of(6, "pop-fra").phase, PHASE_WITHDRAWN)

        migration_slices = {m.slice_index for m in plan.migrations
                            if m.from_pop == "pop-fra"}
        self.assertEqual(migration_slices, {6})

    def test_per_pop_convergence_times_are_independent(self):
        planner = self.build(slices=12)
        # Both fast (1) and slow (2) PoPs fail at the same time.
        probes = {
            "pop-fra": [False] * 12,   # convergence 1
            "pop-iad": [False] * 12,   # convergence 2
            "pop-nrt": healthy(12),
        }
        plan = planner.plan(probes)
        # pop-fra effective at slice 2+1=3, pop-iad at slice 2+2=4.
        self.assertEqual(plan.phase_of(3, "pop-fra").phase, PHASE_WITHDRAWN)
        self.assertEqual(plan.phase_of(3, "pop-iad").phase, PHASE_ANNOUNCED)
        self.assertEqual(plan.phase_of(4, "pop-iad").phase, PHASE_WITHDRAWN)


class TestMultiRegionFailure(PlannerTestBase):
    def test_two_regions_fail_traffic_lands_on_survivor(self):
        planner = self.build(slices=10)
        probes = {
            "pop-fra": [False] * 10,
            "pop-iad": [False] * 10,
            "pop-nrt": healthy(10),
        }
        plan = planner.plan(probes)

        self.assertEqual(plan.phase_of(9, "pop-fra").phase, PHASE_WITHDRAWN)
        self.assertEqual(plan.phase_of(9, "pop-iad").phase, PHASE_WITHDRAWN)
        self.assertEqual(plan.phase_of(9, "pop-nrt").phase, PHASE_ANNOUNCED)

        # Final migrations end at the surviving PoP.
        final = {}
        for m in plan.migrations:
            final[m.client] = m.to_pop
        self.assertEqual(final, {"clients-eu": "pop-nrt",
                                 "clients-na": "pop-nrt"})

        # Rollback covers both degraded PoPs, higher local_pref first.
        pops_in_rollback = []
        for step in plan.rollback:
            if step.action == "re-announce":
                pops_in_rollback.append(step.pop)
        self.assertEqual(pops_in_rollback, ["pop-fra", "pop-iad"])


class TestRecovery(PlannerTestBase):
    def test_gradual_weight_ramp_on_recovery(self):
        planner = self.build(slices=14)
        probes = {
            # Fail slices 0-4, then recover.
            "pop-fra": [False] * 5 + [True] * 9,
            "pop-iad": healthy(14),
            "pop-nrt": healthy(14),
        }
        plan = planner.plan(probes)

        # Withdrawn effective at slice 2 + 1 = 3.
        self.assertEqual(plan.phase_of(3, "pop-fra").phase, PHASE_WITHDRAWN)

        # Recovery threshold 2: healthy at slices 5,6 -> ramping decision at
        # slice 6, effective at slice 7 with 25% weight.
        ramp = plan.phase_of(7, "pop-fra")
        self.assertEqual(ramp.phase, PHASE_RAMPING)
        self.assertAlmostEqual(ramp.effective_weight, 0.25)

        # Weight ramps up one step per healthy slice (1-slice convergence).
        self.assertAlmostEqual(plan.phase_of(8, "pop-fra").effective_weight,
                               0.5)
        full = plan.phase_of(9, "pop-fra")
        self.assertEqual(full.phase, PHASE_ANNOUNCED)
        self.assertAlmostEqual(full.effective_weight, 1.0)

        # Traffic migrated away during the outage and back after recovery.
        away = [m for m in plan.migrations if m.from_pop == "pop-fra"]
        back = [m for m in plan.migrations if m.to_pop == "pop-fra"]
        self.assertTrue(away)
        self.assertTrue(back)
        self.assertGreater(back[0].slice_index, away[0].slice_index)

        # Fully recovered -> no rollback needed.
        self.assertEqual(plan.rollback, [])

    def test_recovery_interrupted_by_new_failure(self):
        planner = self.build(slices=16)
        probes = {
            # Fail, briefly recover (not fully ramped), fail again.
            "pop-fra": ([False] * 4 + [True] * 3 + [False] * 4
                        + [True] * 5),
            "pop-iad": healthy(16),
            "pop-nrt": healthy(16),
        }
        plan = planner.plan(probes)
        # Second outage must withdraw again (4 consecutive failures >= 3).
        phases = [plan.phase_of(t, "pop-fra").phase for t in range(16)]
        self.assertIn(PHASE_WITHDRAWN, phases[10:])
        # And it recovers by the end of the window.
        self.assertEqual(phases[-1], PHASE_ANNOUNCED)


class TestPlanShape(PlannerTestBase):
    def test_phases_cover_every_pop_and_slice(self):
        planner = self.build(slices=6)
        plan = planner.plan({})
        self.assertEqual(len(plan.phases), 6 * 3)
        self.assertIsInstance(plan, Plan)
        # All healthy -> no migrations, no rollback.
        self.assertEqual(plan.migrations, [])
        self.assertEqual(plan.rollback, [])

    def test_invalid_config_rejected(self):
        with self.assertRaises(ValueError):
            self.build(slices=5, failure_threshold=0)
        with self.assertRaises(ValueError):
            self.build(slices=5, ramp_steps=(0.5,))


if __name__ == "__main__":
    unittest.main()
