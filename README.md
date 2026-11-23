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

## 纸质试卷身份核验与成绩发布

同一考试存在 A、B 两种题序不同的试卷，答题卡只登记 exam 与考生姓名，扫描本身不能决定答案键版本。系统以**原纸 `paper_id`** 为身份单位，只接受两类真实物理来源补证：

- **路线 A（原纸二维码 + 封袋双签）**：原纸上找回的二维码残片指向某版本封袋，且该封袋的监考交接链每一环都有两名不同监考员独立签录、链条始终不离开这张原纸。
- **路线 B（逐纸发放 + 换座双签）**：封袋内逐纸发放记录点名该原纸，发卷座位经座位表快照与逐次双签换座回执（原座位、新座位双向签录）重放后确实坐着该考生。

规则要点：考生姓名相同不构成原纸身份（同名卡片按独立原纸处理）；同一原纸多次扫描只算一次考试（取最早扫描作答，重复扫描留痕；作答互斥则挂起）；身份未定时保持 `pending_review`，不会选择让考生及格的版本；同一条封袋交接指向两张原纸、或同一原纸集齐互斥版本证明时，该纸成绩保持待核，无关原纸照常发布。迟到的座位更正经双签到达后可重裁当前成绩，已发通知原文与发送时间永不改写，只追加 `correction` 更正通知并关联被替换通知。

计分先按所绑定版本的 `question_map` 把卡面 `q1/q2/...` 映射回统一知识题，再与知识题正式答案键比对、套用该版本及格线；各版本自身的正式答案键一并留档。`fixtures/exam_ok.json` 仍作为旧版单题序兼容输入，走 `exam_key`/`answer_scan` 直评路径。

从扫描到成绩/审计的复现命令（路线 A：绑定 A 版、及格；把 `route_a.json` 换成 `route_b.json` 即路线 B：绑定 B 版、仅一题对、不及格）：

```bash
python -B -m homevisit exam-import --db paper.sqlite --file fixtures/paper/exam_versions.json
python -B -m homevisit exam-import --db paper.sqlite --file fixtures/paper/scan_p.json
python -B -m homevisit exam-import --db paper.sqlite --file fixtures/paper/route_a.json
python -B -m homevisit report  --db paper.sqlite   # paper_binding 版本/依据、conflicts、notices 责任链
python -B -m homevisit audit   --db paper.sqlite   # 从原始文件重放，逐字节比较决策状态
```

其余 fixture 演示同纸重扫（`rescan_p.json`）、同名他纸（`samename_yi.json`）、迟到座位更正与通知留痕（`seat_correction.json`）；两套互斥证明同时到齐时报告 `exclusive_version_proofs` 冲突且不任选较晚一份。
