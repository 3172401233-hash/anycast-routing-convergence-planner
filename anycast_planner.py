"""Anycast convergence planner.

Computes, per time slice, the effective announcement state of every PoP/prefix
based on health probes, route priorities, upstream convergence delays and
traffic weights.  The tool only produces a control-plane *plan*; it never
touches routers or BGP sessions itself.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Input model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Pop:
    """A point of presence announcing one or more prefixes."""

    name: str
    region: str
    prefixes: Tuple[str, ...]
    neighbors: Tuple[str, ...]
    local_pref: int
    convergence_slices: int  # upstream BGP convergence time, in time slices
    weight: float = 1.0      # relative traffic weight / capacity


@dataclass(frozen=True)
class ClientGroup:
    """A client range whose traffic lands on exactly one PoP per slice."""

    name: str
    cidr: str
    share: float  # fraction of total traffic (0..1)


@dataclass(frozen=True)
class PlannerConfig:
    """Tuning knobs for the convergence state machine."""

    slices: int
    failure_threshold: int = 3                 # consecutive failures to withdraw
    recovery_threshold: int = 2                # consecutive successes to re-announce
    ramp_steps: Tuple[float, ...] = (0.25, 0.5, 1.0)  # gradual weight restore


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

PHASE_ANNOUNCED = "announced"
PHASE_WITHDRAWN = "withdrawn"
PHASE_RAMPING = "ramping"


@dataclass(frozen=True)
class PopPhase:
    """Effective state of one PoP in one time slice."""

    slice_index: int
    pop: str
    prefixes: Tuple[str, ...]
    phase: str
    effective_weight: float


@dataclass(frozen=True)
class Migration:
    """Traffic of one client group moving between PoPs."""

    slice_index: int
    client: str
    cidr: str
    from_pop: Optional[str]
    to_pop: Optional[str]
    traffic_share: float


@dataclass(frozen=True)
class RollbackStep:
    order: int
    pop: str
    action: str
    detail: str


@dataclass
class Plan:
    phases: List[PopPhase] = field(default_factory=list)
    migrations: List[Migration] = field(default_factory=list)
    affected_clients: List[str] = field(default_factory=list)
    rollback: List[RollbackStep] = field(default_factory=list)

    def phases_at(self, slice_index: int) -> List[PopPhase]:
        return [p for p in self.phases if p.slice_index == slice_index]

    def phase_of(self, slice_index: int, pop: str) -> Optional[PopPhase]:
        for p in self.phases:
            if p.slice_index == slice_index and p.pop == pop:
                return p
        return None


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

_ST_ANNOUNCED = "announced"
_ST_WITHDRAWN = "withdrawn"
_ST_RAMPING = "ramping"


class _PopRuntime:
    __slots__ = (
        "committed", "ramp_index", "consec_fail", "consec_ok",
        "effective_phase", "effective_weight", "pending",
    )

    def __init__(self, pop: Pop):
        self.committed = _ST_ANNOUNCED
        self.ramp_index = 0
        self.consec_fail = 0
        self.consec_ok = 0
        self.effective_phase = PHASE_ANNOUNCED
        self.effective_weight = pop.weight
        # Scheduled effective changes: (effective_slice, phase, weight)
        self.pending: List[Tuple[int, str, float]] = []


class AnycastPlanner:
    """Simulates slice-by-slice control-plane behaviour and emits a plan."""

    def __init__(self, pops: Sequence[Pop], clients: Sequence[ClientGroup],
                 config: PlannerConfig):
        if config.failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if config.recovery_threshold < 1:
            raise ValueError("recovery_threshold must be >= 1")
        if not config.ramp_steps or config.ramp_steps[-1] != 1.0:
            raise ValueError("ramp_steps must be non-empty and end at 1.0")
        self.pops = {p.name: p for p in pops}
        self.clients = list(clients)
        self.config = config

    # -- state machine ------------------------------------------------------

    def _decide(self, rt: _PopRuntime, healthy: bool) -> None:
        cfg = self.config
        if healthy:
            rt.consec_ok += 1
            rt.consec_fail = 0
        else:
            rt.consec_fail += 1
            rt.consec_ok = 0

        if rt.committed in (_ST_ANNOUNCED, _ST_RAMPING):
            # Only withdraw after *consecutive* failures reach the threshold;
            # a single healthy probe resets the counter (flap damping).
            if rt.consec_fail >= cfg.failure_threshold:
                rt.committed = _ST_WITHDRAWN
                rt.ramp_index = 0
                return
        if rt.committed == _ST_WITHDRAWN:
            if rt.consec_ok >= cfg.recovery_threshold:
                # Re-announce at the first (lowest) ramp step.
                rt.committed = _ST_RAMPING
                rt.ramp_index = 0
        elif rt.committed == _ST_RAMPING and healthy:
            # Advance one ramp step per healthy slice while ramping.
            if rt.ramp_index < len(cfg.ramp_steps) - 1:
                rt.ramp_index += 1
            if rt.ramp_index == len(cfg.ramp_steps) - 1:
                rt.committed = _ST_ANNOUNCED

    def _committed_weight(self, pop: Pop, rt: _PopRuntime) -> float:
        if rt.committed == _ST_WITHDRAWN:
            return 0.0
        if rt.committed == _ST_RAMPING:
            return pop.weight * self.config.ramp_steps[rt.ramp_index]
        return pop.weight

    def _committed_phase(self, rt: _PopRuntime) -> str:
        if rt.committed == _ST_WITHDRAWN:
            return PHASE_WITHDRAWN
        if rt.committed == _ST_RAMPING:
            return PHASE_RAMPING
        return PHASE_ANNOUNCED

    # -- main loop ----------------------------------------------------------

    def plan(self, probes: Dict[str, Sequence[bool]]) -> Plan:
        """Build the convergence plan.

        ``probes`` maps a PoP name to a per-slice sequence of booleans
        (True = healthy).  Missing entries count as healthy.
        """
        cfg = self.config
        runtime = {name: _PopRuntime(pop) for name, pop in self.pops.items()}
        plan = Plan()
        serving: Dict[str, Optional[str]] = {c.name: None for c in self.clients}
        affected = set()

        for t in range(cfg.slices):
            # 1. Apply scheduled effective changes whose convergence elapsed.
            for rt in runtime.values():
                due = [e for e in rt.pending if e[0] <= t]
                rt.pending = [e for e in rt.pending if e[0] > t]
                for _, phase, weight in due:
                    rt.effective_phase, rt.effective_weight = phase, weight

            # 2. Observe probes and decide target state.
            for name, pop in self.pops.items():
                series = probes.get(name, ())
                healthy = series[t] if t < len(series) else True
                rt = runtime[name]
                before = (self._committed_phase(rt),
                          self._committed_weight(pop, rt))
                self._decide(rt, healthy)
                after = (self._committed_phase(rt),
                         self._committed_weight(pop, rt))
                if after != before:
                    # Upstream convergence: the decision only becomes
                    # effective after the PoP's convergence delay.
                    rt.pending.append(
                        (t + pop.convergence_slices, after[0], after[1]))

            # 3. Record effective phases for this slice.
            for name, pop in self.pops.items():
                rt = runtime[name]
                plan.phases.append(PopPhase(
                    slice_index=t, pop=name, prefixes=pop.prefixes,
                    phase=rt.effective_phase,
                    effective_weight=rt.effective_weight))

            # 4. Assign clients to the best effectively-announcing PoP.
            best = self._best_pop(runtime)
            for client in self.clients:
                prev = serving[client.name]
                if best != prev:
                    serving[client.name] = best
                    if prev is None:
                        # Initial placement, not a migration.
                        continue
                    plan.migrations.append(Migration(
                        slice_index=t, client=client.name,
                        cidr=client.cidr, from_pop=prev, to_pop=best,
                        traffic_share=client.share))
                    affected.add(client.name)

        plan.affected_clients = sorted(affected)
        plan.rollback = self._build_rollback(runtime)
        return plan

    def _best_pop(self, runtime: Dict[str, _PopRuntime]) -> Optional[str]:
        candidates = []
        for name, pop in self.pops.items():
            rt = runtime[name]
            if rt.effective_phase == PHASE_WITHDRAWN or rt.effective_weight <= 0:
                continue
            candidates.append((pop.local_pref, rt.effective_weight, name))
        if not candidates:
            return None
        # Highest local_pref wins; weight breaks ties; name keeps it stable.
        return max(candidates)[2]

    # -- rollback -----------------------------------------------------------

    def _build_rollback(self, runtime: Dict[str, _PopRuntime]) -> List[RollbackStep]:
        """Steps to restore every PoP to full announcement, in safe order."""
        cfg = self.config
        steps: List[RollbackStep] = []
        order = 1
        degraded = sorted(
            (name for name, rt in runtime.items()
             if self._committed_phase(rt) != PHASE_ANNOUNCED
             or self._committed_weight(self.pops[name], rt)
             < self.pops[name].weight),
            key=lambda n: (-self.pops[n].local_pref, n))
        for name in degraded:
            pop = self.pops[name]
            steps.append(RollbackStep(
                order=order, pop=name, action="verify-health",
                detail="require %d consecutive healthy probes before "
                       "re-announcement" % cfg.recovery_threshold))
            order += 1
            ramp = " -> ".join("%.0f%%" % (s * 100) for s in cfg.ramp_steps)
            steps.append(RollbackStep(
                order=order, pop=name, action="re-announce",
                detail="re-announce %s to neighbors %s with weight ramp %s"
                       % (", ".join(pop.prefixes), ", ".join(pop.neighbors),
                          ramp)))
            order += 1
            steps.append(RollbackStep(
                order=order, pop=name, action="await-convergence",
                detail="wait %d slice(s) for upstream convergence, then "
                       "verify traffic levels" % pop.convergence_slices))
            order += 1
        return steps
