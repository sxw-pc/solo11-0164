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

## 保险门户报案 RPA（离线模拟）

`insurance-import` 采录客户签署的损坏通知（可多次补录，只更新可申报内容，不抹掉已提交原案）；`rpa-run` 每次提交**先持久化 `submit_intent`**（原通知事件、内容摘要、唯一操作号），再由合成门户 fixture 驱动提交/查询/超时/异步回执；`rpa-recover` 处理超时：先按操作号查询门户，已见该案则绑定既有案号，查无此案才允许用**同一操作号**重报——绝不盲目开新案。外部案号只经操作绑定，不按金额/job 猜测关联；同一案号被两份客户通知引用即进入争议。撤回以撤回回执生效，晚到的旧“受理”回执不能复活已撤案件。门户原响应存于 `portal_exchange`，`audit` 从原始文件重放全部绑定。

```powershell
python -m homevisit insurance-import --db s.sqlite --file fixtures/insurance_ok.json
python -m homevisit rpa-run --db s.sqlite --file fixtures/rpa_submit.json        # ext-1 受理 5000
python -m homevisit insurance-import --db s.sqlite --file fixtures/insurance_supplement.json  # 补录清单 6500
# 路线一：同案追加（ext-1 唯一有效，1500 同案待审核）
python -m homevisit rpa-run --db s.sqlite --file fixtures/rpa_append.json
# 路线二：撤回重报（须先收到撤回回执才创建 ext-2，原案退出可追索金额）
python -m homevisit rpa-run --db s.sqlite --file fixtures/rpa_withdraw.json
python -m homevisit portal-import --db s.sqlite --file fixtures/portal_withdrawn.json
python -m homevisit rpa-run --db s.sqlite --file fixtures/rpa_refile.json
# 超时恢复：rpa_refile_timeout.json 使提交超时，恢复只绑定 ext-2，不生成 ext-3
python -m homevisit rpa-run --db s.sqlite --file fixtures/rpa_refile_timeout.json
python -m homevisit rpa-recover --db s.sqlite --file fixtures/rpa_recover.json
python -m homevisit report --db s.sqlite   # 一一绑定、可追索金额、待查提交、案号链
python -m homevisit audit --db s.sqlite
```

`report` 的 `insurance` 段给出每个索赔的本地通知↔外部案件绑定、`recoverable`（可追索金额）、`pending_review`（待审金额）、`pending_queries`（待查提交）与 `case_chain`（更正/撤回案号链），`disputes` 列出跨通知共享的案号。其余 fixture：`rpa_timeout*.json`/`rpa_recover*.json`/`rpa_resubmit.json` 演示超时查询与查无此案后的同号重报；`portal_dispute.json` 演示同案号争议；`portal_late_accept.json` 演示晚到旧受理回执不复活撤案。
