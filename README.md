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

签名、人员资质及到离场信息在首版均为离线证据标签；不验证真实 GPS、机构资质或支付。
