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

物料批次为第四个独立采录入口，不改变上述合同、派工与结算决定：

```powershell
python -m homevisit material-import --db state.sqlite --file fixtures/material_ok.json
python -m homevisit material-import --db state.sqlite --file fixtures/material_recall_late.json
python -m homevisit report --db state.sqlite
python -m homevisit audit --db state.sqlite
```

批次以（批次号、供应商、来源单据）唯一标识，同名不同源不得混同。领用交接需双方签收，一次领用可按 `splits` 分拆给多作业；现场耗用必须指向有效领用的分拆额度，不能从同箱未拆封库存推断已使用。预约量（分拆）与实际消耗（耗用）分别入账，库内重分配不改写已发生耗用；未拆封退回回到可用量可再分配，已拆封余量隔离不得转给其他客户。物料决定状态：`ready` 可开工、`pending` 待备料、`shortage` 供应不足、`expired` 批次过期、`unsigned` 交接缺签、`recall_hold` 持有召回批次未拆封、`recall_risk` 已耗用批次被召回、`review` 耗用与领用不匹配、`deferred` 客户授权改期。召回只追溯实际耗用与仍持有该批次的服务（`materials.recalls` 的 `consumed_by`/`held_by`），`materials.pending_review` 列出仍待复核的服务；验收之后迟到的召回不影响已结算费用。客户同时签署“可换等效批次”与“可改期等原指定批次”时，路线事件二选一：`material_route_substitute.json` 保留本次派工并把耗用归到替换批次，`material_route_reschedule.json` 本次禁止拆封、释放领用等待新窗口，库存中等效批次的存在不会自动算已完成。

签名、人员资质及到离场信息在首版均为离线证据标签；不验证真实 GPS、机构资质或支付。
