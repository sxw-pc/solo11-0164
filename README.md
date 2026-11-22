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

## 客户数据访问包

`privacy-import` 采录客户签署的数据访问请求（`recipient` 不同于 `requester` 即为代办委托，须带 `valid_to` 有效期与逗号分隔的 scope 令牌；`withdrawals` 可撤回委托）。就绪的请求自动制作数据访问包：按接收者 scope 逐字段裁出合同条款、到离场回执、验收与决定记录，每项遮盖都带理由，交付清单与仍待授权（来源混装无法安全切分）的字段分别列入 `manifest` 与 `pending`。包内只放披露值与来源锚点（事件 SHA + 字段指针、决定版本），绝不嵌入原始文件或证据原文；同一原文件中的其他 job 不会进入本包。同版本重复制作幂等；迟到改期、撤回授权或新争议只产生下一版本，已交付包字节与交付事实保留，`report` 的 `reissue_required` 标明需补发的版本。

```powershell
python -m homevisit contract-import --db state.sqlite --file fixtures/contract_mixed.json
python -m homevisit worker-import --db state.sqlite --file fixtures/worker_mixed.json
python -m homevisit visit-import --db state.sqlite --file fixtures/visit_mixed.json
python -m homevisit privacy-import --db state.sqlite --file fixtures/privacy_mixed.json
python -m homevisit export-build --db state.sqlite --request privacy-request-self --out pkg-self.json
python -m homevisit export-verify --db state.sqlite --package pkg-self.json
python -m homevisit audit --db state.sqlite
```

`export-build` 不带 `--request` 时重建全部请求；`export-verify` 在持有原库时离线核验包的清单完整性、逐字段来源、遮盖策略与未泄漏性。`*_mixed.json` 为同一 visit 原文件混装客户甲 job-1 与客户乙 job-2 的双 job 样例；`contract_late.json` 演示迟到改期触发补发并使代办委托到期；`visit_unsplittable.json` 的争议正文混入 job-2 标识，该字段被阻断并列入待补来源；`privacy_withdraw.json` 撤回代办委托。

签名、人员资质及到离场信息在首版均为离线证据标签；不验证真实 GPS、机构资质或支付。
