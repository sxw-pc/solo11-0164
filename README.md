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

## 公开报价承诺核验

`offer-import` 采录页面正文与脚注的最短时长、上门费、地域限制、优惠互斥条款，另采独立的结算页/订单回执与供应商签署勘误。抓取时间仅说明何时看到页面；承诺的生效时窗、地区与版本由页面 meta 或勘误证明。报价主结果按（服务， 地区）给出当前可引用的成交条件及状态：`quoted`（旧版单页直接报价）、`verified`（已核）、`pending`（待证）、`disputed`（争议）、`expired`（失效）。`market_quote` 只是当前摘要；每次状态变化追加 `quote_version`，每次对客户发出的报价是按报价版本与接收者标识的不可变 `quote_delivery` 快照。

核验规则：正文与脚注矛盾（如脚注“可能收上门费”）时待证，不产生可引用金额；同供应商、同服务、同地区、同有效窗且金额复算一致的结算页加订单回执可证明脚注收费成立；供应商签署勘误必须指明其替换的页面版本（不得全站覆盖），可豁免费用；两类签署证明冲突时转争议并可在 report 中定位已交付客户；新页面撤销（retracts/supersedes）后旧交付保持原貌，新询价不得沿用。不同区域的同名供应商与相同活动码不按字符串合并。旧格式页面（无地区/脚注）保持旧语义。

```powershell
python -m homevisit offer-import --db q.sqlite --file fixtures/offer_r1.json            # 待证：正文 80/小时 vs 脚注可能收 40
python -m homevisit offer-import --db q.sqlite --file fixtures/offer_r1_receipts.json   # 已核 200：同区同窗结算页+订单回执
python -m homevisit offer-import --db q.sqlite --file fixtures/offer_delivery.json      # 交付 client-a（不可变快照）
python -m homevisit offer-import --db q.sqlite --file fixtures/offer_r1_counter.json    # 迟到签署反证 → 争议并定位客户
python -m homevisit quote --db q.sqlite --service cleaning --region city-east
python -m homevisit report --db q.sqlite
python -m homevisit audit --db q.sqlite
```

`offer_r1_errata.json` 为另一条独立补证路线（签署勘误豁免上门费 → 已核 160）；`offer_r1_cross_region.json` 演示跨区同活动码回执不合并；`offer_r1_retract.json` 演示撤销后报价失效、旧交付不变。
