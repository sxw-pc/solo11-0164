# 家政上门履约核台

离线采录客户服务契约、家政人员资格及上门履约。合同、派工和工时结算是独立持久化的业务决定；签约改期、资质失效和现场争议会改变下游结论并保留旧版本。全部输入为合成 JSON，无真实家政平台或付款接入。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m homevisit demo --db demo.sqlite
python -m homevisit audit --db demo.sqlite
python -m unittest discover -s tests -v
```

三个独立采录入口：

```powershell
python -m homevisit contract-import --db state.sqlite --file fixtures/contract_ok.json
python -m homevisit worker-import --db state.sqlite --file fixtures/worker_ok.json
python -m homevisit visit-import --db state.sqlite --file fixtures/visit_ok.json
python -m homevisit report --db state.sqlite
```

对应 `*_bad.json` 分别使原服务窗口改期、未获客户授权的替岗阻断派工、验收争议冻结费用。劳动证书必须覆盖整个有效服务窗；双向到离场回执、地址与人员需一致；暂停时长从工时扣除。新增项目必须有客户变更单支持。事件 ID 同内容重放幂等、异内容拒绝，原始文件 SHA 与决策历史保留；`audit` 从原始文件重放比较状态。

## 接力派工与分段履约

一单服务可由两名同类资格人员在约定时窗内接力，证据链为：客户授权的接力申请（`relay_requests`）→ 前后人员双签交接回执（`handovers`）→ 每人独立双向到离场回执（`receipts`）→ 各自劳动证书 → 归属到具体片段的客户验收/争议（带 `worker` 与时间窗的 `acceptances`）→ 客户最终责任确认（`confirmations`，`lead` 全程主责或 `relay` 接力主责）。

系统对**同一批输入**同时裁定两条候选路线并全部落库，仅客户最终责任确认绑定其中一条为生效路线，另一路线及其证据保留；不允许替换输入、补造签名或仅改排序键。规则要点：

- 交接点必须有双方签名且双方当时都在场；接力点可夹在某人的休息中，但无人签交接的空档不能拼成连续服务（`gap_minutes` 单独披露，可识别金额冻结）。
- 两人短暂重叠只按时间轴并集计一次费，绝不双计客户费用；休息时长从责任人片段扣除。
- 证书按人、按其实际负责的片段核验，前任证书不能覆盖后任；迟到补录的证书吊销（`revocations`）只冻结该人相关片段，不抹去另一人已验收片段。
- 迟到争议只冻结其指向的可识别片段；签约改期后原接力点落到新窗外的，按确认路线重新裁定派工与工时，历史接力事实与决策版本仍保留。
- 同一交接点由 `segment.relay_points` 统一产出，派工冲突检查与费用结算共用，不会漂移。

主结果在 `settlement_decision`（服务覆盖区间 `coverage_start/end`、空档分钟、整单应结 `payable`/冻结 `frozen`、生效路线），每段责任、工时、证书、交接与验收来源在 `service_segment`，每人现场工时与按路线负责工时在报告的 `workers` 中，工作台证据汇总在 `relay_workbench`。

```powershell
python -m homevisit demo-relay --db relay.sqlite
python -m homevisit audit --db relay.sqlite
python -m unittest discover -s tests -v
```

`fixtures/seg_*.json` 为独立合成数据，覆盖单人全程、合法接力双路线、交接空档/缺签、短暂重叠与休息中交接、后任证书迟到失效、迟到争议和签约改期；既有单人 fixture 的裁定结果保持不变。

签名、人员资质及到离场信息在首版均为离线证据标签；不验证真实 GPS、机构资质或支付。
