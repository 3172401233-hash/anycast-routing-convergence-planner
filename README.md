# anycast-routing-convergence-planner
规划 Anycast 路径切换、区域撤回和流量收敛

> **注意**：本工具只生成网络**控制面计划**（何时撤回/恢复 BGP 发布、
> 流量将如何迁移、如何回滚）。它不连接路由器、不修改任何 BGP 会话，
> 也不下发配置；输出需由运维流程审核后另行执行。

## 功能

输入 PoP、前缀、邻居、健康探测序列、路由优先级（local-pref）、上游收敛
时间和流量权重，按时间片模拟控制面行为并输出：

- **路由阶段**：每个时间片每个 PoP 的有效发布状态
  （`announced` / `ramping` / `withdrawn`）与有效权重；
- **迁移流量**：客户端网段在 PoP 之间的迁移记录及流量份额；
- **受影响客户端范围**：发生迁移的客户端网段列表；
- **回滚计划**：将降级 PoP 恢复为全量发布的有序步骤
  （健康验证 → 渐进权重重新发布 → 等待上游收敛）。

## 行为规则

- 只有**连续**失败探测达到 `failure_threshold` 才撤回发布；单次成功探测
  即重置计数器，避免探测抖动引发反复撤收。
- 区域恢复需连续 `recovery_threshold` 次成功探测，随后按
  `ramp_steps`（如 25% → 50% → 100%）渐进恢复流量权重。
- 所有状态变更延迟对应 PoP 的 `convergence_slices` 个时间片后才生效，
  以模拟上游 BGP 收敛时间的不一致。

## 使用

```python
from anycast_planner import AnycastPlanner, ClientGroup, PlannerConfig, Pop

pops = [
    Pop(name="pop-fra", region="eu", prefixes=("203.0.113.0/24",),
        neighbors=("upstream-a",), local_pref=200, convergence_slices=1),
    Pop(name="pop-iad", region="na", prefixes=("203.0.113.0/24",),
        neighbors=("upstream-b",), local_pref=150, convergence_slices=2),
]
clients = [ClientGroup(name="clients-eu", cidr="198.51.100.0/25", share=0.5)]
config = PlannerConfig(slices=10, failure_threshold=3,
                       recovery_threshold=2, ramp_steps=(0.25, 0.5, 1.0))

plan = AnycastPlanner(pops, clients, config).plan({
    "pop-fra": [True, False, False, False, True, True, True, True, True, True],
})
print(plan.phases, plan.migrations, plan.affected_clients, plan.rollback)
```

## 测试

```
python -m unittest
```

覆盖单区域故障、探测抖动、上游收敛不一致、多区域故障与渐进恢复。
兼容 Python 3.8.2+。
