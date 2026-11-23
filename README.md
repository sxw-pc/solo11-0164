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

## 公开评价采录与来源归并

`review-import` 从合成 HTML 采录平台内 `review_key`、job、评分、正文、发布时间与作者标签，并接收平台签署的转载映射、原作者撤回/署名声明与客户服务订单证明（均为离线证据标签，签名为内容的确定性摘要）。同一条客户经历的多站转载与两个独立客户的投诉由此区分：

- 相同文字或相同图片摘要不足以认定转载；只有签名转载映射才归并页面。
- 平台内 `review_key` 复用到另一 job 时保留旧评价的历史归属（按 平台/key/job 分别建档）。
- 编辑页按发布版本而非抓取顺序决定有效版本，晚到库的旧抓取不会压过新发布。
- 作者撤回只撤其明确引用的页面版本，不回溯其他版本，也不抹掉另一客户的独立评价。
- 质量案分列已确认独立负评数 `negative_count` 与待核身份冲突数 `pending_count`；未确认的转载关系不会默默降低独立投诉数。

双路线复现（同一批页面与订单证明，质检策略不同）：

```powershell
python -m homevisit review-import --db state.sqlite --file fixtures/review_route_conservative.json
python -m homevisit review-import --db state.sqlite --file fixtures/review_author.json
python -m homevisit report --db state.sqlite
python -m homevisit audit --db state.sqlite
```

`review_route_conservative.json`（保守分离）暂不把 B 站页面并入 A 站评价，质量案保留一条已确认负评与一条待核第二主张；`review_route_signed.json`（先采用签名转载）暂把 B 页视为 A 页镜像并另开身份核验。两条路线都计一条已确认负评、都不把待核当作已证实的第二次服务失败；`review_author.json` 补上 B 页作者签名后两路收敛到同一归属（本例为独立评价，负评数收敛为 2）。`review_edit_*.json` 演示晚到编辑按发布版本重算，`review_retract.json` 演示只撤明确引用版本，`review_key_reuse.json` 演示 key 复用保留历史，`review_similar.json` 演示相同文字图片不自动归并。原始 HTML、跨站签名与处理历史均由 `audit` 从文件日志重放校验。
