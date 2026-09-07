# 知识库问答评测报告

- 总题数：50
- 作答：43，拒答：7
- 检索命中率：43/43 = 1.0
- 事实覆盖率：43/43 = 1.0
- 拒答正确率：7/7 = 1.0
- 块级检索：Recall@5=1.0，Hit@1=0.9535，Hit@3=1.0，MRR=0.9767（n=43）
- 难度分层 Recall@5：L1:21/21  L2:15/15  L3:7/7

| 状态 | ID | 分类/难度 | 知识库 | 问题 | 回答 | 块级 | 引用 |
|---|---|---|---|---|---|---|---|
| PASS | hr-001 | factual/L1 | enterprise | 员工累计工作满 8 年，每年能享受几天带薪年假？ | 员工累计工作满 8 年，属于“满 1 年不满 10 年”的区间，因此每年享受 **5 天**带薪年休假 [1]。 | @1 | employee-handbook.md, employee-handbook.md, travel-policy.txt, finance-policy.md, travel-policy.txt |
| PASS | hr-002 | factual/L1 | enterprise | 员工累计工作满 15 年，带薪年假是多少天？ | 根据员工手册规定，员工累计工作满 10 年不满 20 年的，享受 10 天带薪年休假 [1]。   满 15 年属于该区间（10 ≤ 工作年限 < 20），因此带薪年假为 **10 天**。 | @1 | employee-handbook.md, employee-handbook.md, travel-policy.txt, finance-policy.md, finance-policy.md |
| PASS | hr-003 | factual/L1 | enterprise | 请假需要提前多久提交申请？ | 请假应至少提前一个工作日在系统中提交申请 [2]。 | @1 | employee-handbook.md, travel-policy.txt, employee-handbook.md, finance-policy.md, finance-policy.md |
| PASS | hr-004 | factual/L1 | enterprise | 遇到紧急情况无法提前请假时应该怎么做？ | 遇到紧急情况无法提前请假时，应先电话告知直属主管，并在恢复工作后补交请假申请 [2]。 | @1 | employee-handbook.md, travel-policy.txt, employee-handbook.md, it-policy.pdf, travel-policy.txt |
| PASS | hr-101 | factual/L2 | enterprise | 我累计工作满 12 年了，今年能休几天带薪年假？ | 根据员工手册规定，员工累计工作满 10 年不满 20 年的，享受 10 天带薪年休假 [1]。   您累计工作满 12 年，属于该区间，因此今年可休 **10 天** 带薪年假。 | @1 | employee-handbook.md, employee-handbook.md, travel-policy.txt, finance-policy.md, travel-policy.txt |
| PASS | hr-102 | factual/L2 | enterprise | 想请年假的话，需要预留多长时间提交申请？ | 根据《员工手册》规定，年假属于请假的一种，应遵循统一的请假流程：**请假应至少提前一个工作日在系统中提交申请，由直属主管审批** [1]。  因此，想请年假的话，需要**至少提前一个工作日**提交申请。 | @1 | employee-handbook.md, travel-policy.txt, employee-handbook.md, finance-policy.md, finance-policy.md |
| PASS | hr-103 | factual/L2 | enterprise | 突然生病没办法提前请假，公司规定怎么处理？ | 根据公司规定，如遇突发疾病无法提前请假，可先电话告知直属主管，待恢复工作后补交病假申请[2]。   但需注意：病假须提供医院诊断证明；若连续病假超过3天，须提供二级以上医院出具的证明[1]。 | @1 | employee-handbook.md, travel-policy.txt, employee-handbook.md, travel-policy.txt, finance-policy.md |
| PASS | hr-104 | factual/L3 | enterprise | 员工累计工作满 20 年，带薪年假是多少天？ | 员工累计工作满 20 年，享受 15 天带薪年休假 [1]。 | @1 | employee-handbook.md, employee-handbook.md, travel-policy.txt, finance-policy.md, finance-policy.md |
| PASS | hr-105 | factual/L3 | enterprise | 累计工作满 10 年但不满 20 年的员工，年假标准是什么？ | 累计工作满 10 年但不满 20 年的员工，享受 10 天带薪年休假 [1]。 | @1 | employee-handbook.md, travel-policy.txt, employee-handbook.md, travel-policy.txt, finance-policy.md |
| PASS | hr-106 | factual/L1 | enterprise | 标准工作时间的起止是几点？ | 标准工作时间为周一至周五 9:00-18:00 [4]。 | @1 | employee-handbook.md, it-policy.pdf, travel-policy.txt, employee-handbook.md, it-policy.pdf |
| PASS | hr-107 | factual/L1 | enterprise | 婚假有多少天？ | 婚假有3天[5]。 | @1 | employee-handbook.md, travel-policy.txt, employee-handbook.md, travel-policy.txt, finance-policy.md |
| PASS | hr-108 | factual/L2 | enterprise | 正式员工离职需要提前多久书面通知公司？ | 正式员工离职须提前30天书面通知公司 [1]。 | @1 | employee-handbook.md, employee-handbook.md, it-policy.pdf, finance-policy.md, travel-policy.txt |
| PASS | hr-109 | factual/L3 | enterprise | 连续病假超过多少天需要提供二级以上医院证明？ | 连续病假超过 3 天需要提供二级以上医院证明 [3]。 | @1 | employee-handbook.md, travel-policy.txt, travel-policy.txt, employee-handbook.md, finance-policy.md |
| PASS | fin-001 | factual/L1 | enterprise | 一线城市住宿费报销上限是多少？ | 一线城市（北京、上海、广州、深圳）住宿费报销上限为 **600 元/晚** [1]。 | @1 | finance-policy.md, travel-policy.txt, travel-policy.txt, finance-policy.md, employee-handbook.md |
| PASS | fin-002 | factual/L1 | enterprise | 费用发生后多少天内需要提交报销？ | 员工应在费用发生后30日内提交报销[1]。   （注：该时限出自《财务管理制度》第二条“报销流程与时限”；差旅报销另有更严格要求——出差返回后10个工作日内提交，见[3]，但问题问的是泛指的“费用发生后”，故以通用制度为准。） | @1 | finance-policy.md, finance-policy.md, travel-policy.txt, employee-handbook.md, travel-policy.txt |
| PASS | fin-003 | factual/L1 | enterprise | 单笔采购 5000 元以下由谁审批？ | 单笔采购 5000 元以下由部门经理审批 [1]。 | @1 | finance-policy.md, finance-policy.md, travel-policy.txt, travel-policy.txt, it-policy.pdf |
| PASS | fin-004 | factual/L1 | enterprise | 出差借款需要在多长时间内核销？ | 出差借款应在出差返回后 15 个工作日内报销核销 [1][2]。 | @1 | finance-policy.md, finance-policy.md, travel-policy.txt, travel-policy.txt, it-policy.pdf |
| PASS | fin-101 | factual/L2 | enterprise | 去广州出差，住宿每晚最多能报销多少？ | 去广州出差，住宿每晚最多能报销600元[1]。 | @2 | travel-policy.txt, finance-policy.md, travel-policy.txt, finance-policy.md, employee-handbook.md |
| PASS | fin-102 | factual/L2 | enterprise | 报销审批通过后，钱多久能打到工资卡？ | 报销款在审批完成后 5 个工作日内打款至员工工资卡 [2]。 | @1 | finance-policy.md, finance-policy.md, employee-handbook.md, travel-policy.txt, employee-handbook.md |
| PASS | fin-103 | factual/L3 | enterprise | 单笔采购 6 万元需要谁审批？ | 单笔采购 6 万元需要总经理审批，并须签订正式合同 [1]。 | @1 | finance-policy.md, finance-policy.md, travel-policy.txt, it-policy.pdf, travel-policy.txt |
| PASS | fin-104 | factual/L2 | enterprise | 业务招待每人每次的报销标准是多少？ | 业务招待每人每次的报销标准是不超过 300 元 [1]。 | @1 | finance-policy.md, travel-policy.txt, finance-policy.md, travel-policy.txt, employee-handbook.md |
| PASS | fin-105 | factual/L3 | enterprise | 单笔采购 3 万元由谁审批？ | 单笔采购 3 万元由分管副总审批 [1]。 | @1 | finance-policy.md, finance-policy.md, travel-policy.txt, travel-policy.txt, it-policy.pdf |
| PASS | fin-106 | factual/L3 | enterprise | 一次业务招待总花费 2500 元，需要谁审批？ | 根据资料 [1]《财务管理制度（示例）》中“1.2 业务招待费”规定：   **“单次招待总额超过 2000 元的，须报分管副总审批。”**    本次业务招待总花费为 2500 元，超过 2000 元，因此需报**分管副总审批** [1] | @1 | finance-policy.md, travel-policy.txt, finance-policy.md, travel-policy.txt, it-policy.pdf |
| PASS | fin-107 | factual/L2 | enterprise | 出租车单次报销的上限是多少？ | 出租车单次报销的上限是 **80 元** [1]。 | @1 | finance-policy.md, travel-policy.txt, finance-policy.md, travel-policy.txt, employee-handbook.md |
| PASS | fin-108 | factual/L1 | enterprise | 部门经理每月的岗位话费补贴是多少？ | 部门经理每月的岗位话费补贴是100元[1]。 | @1 | finance-policy.md, finance-policy.md, travel-policy.txt, employee-handbook.md, employee-handbook.md |
| PASS | fin-109 | factual/L3 | enterprise | 单次培训费超过多少需要人力资源部与财务部联合审批？ | 单次培训费超过 1 万元的，须报人力资源部与财务部联合审批 [1]。 | @1 | finance-policy.md, finance-policy.md, travel-policy.txt, employee-handbook.md, travel-policy.txt |
| PASS | trv-001 | factual/L1 | enterprise | 每天餐补合计不能超过多少？ | 每天餐补合计不能超过 100 元 [1]。 | @1 | travel-policy.txt, employee-handbook.md, finance-policy.md, finance-policy.md, employee-handbook.md |
| PASS | trv-002 | factual/L1 | enterprise | 出差申请需要提前几个工作日提交？ | 出差须提前 3 个工作日在 OA 系统提交《出差申请单》[4]。 | @1 | travel-policy.txt, employee-handbook.md, finance-policy.md, finance-policy.md, employee-handbook.md |
| PASS | trv-003 | factual/L1 | enterprise | 出差返回后多久内要提交报销？ | 出差返回后须在 **10 个工作日内** 提交报销 [1]。   （注：另一份资料中提到“费用发生后30日内提交报销”[3]，但针对差旅报销的专项规定明确为“出差返回后10个工作日内”，依据优先级，应以《差旅与报销细则》为准。） | @1 | travel-policy.txt, finance-policy.md, finance-policy.md, travel-policy.txt, employee-handbook.md |
| PASS | trv-101 | factual/L2 | enterprise | 去北京出差，住宿每晚的上限是多少？ | 去北京出差，住宿每晚的上限是 600 元[1]。 | @2 | finance-policy.md, travel-policy.txt, travel-policy.txt, finance-policy.md, employee-handbook.md |
| PASS | trv-102 | factual/L1 | enterprise | 午餐的餐补标准是多少？ | 午餐的餐补标准是40元[1]。 | @1 | travel-policy.txt, employee-handbook.md, finance-policy.md, finance-policy.md, employee-handbook.md |
| PASS | trv-103 | factual/L2 | enterprise | 飞机单程飞行超过多久可以申请升舱？ | 飞机单程飞行时间超过 6 小时的，可申请升舱，须总经理审批 [1]。 | @1 | travel-policy.txt, travel-policy.txt, finance-policy.md, employee-handbook.md, employee-handbook.md |
| PASS | trv-104 | factual/L2 | enterprise | 连续出差多长时间可以申请返程或调休？ | 连续出差超过 15 天的，每满 15 天可申请一次返程或调休 [7]。 | @1 | travel-policy.txt, employee-handbook.md, finance-policy.md, finance-policy.md, employee-handbook.md |
| PASS | trv-105 | factual/L1 | enterprise | 两人及以上同行出差，住宿怎么安排？ | 两人及以上同行出差，住宿按标准间安排；如需单间，须部门负责人审批 [1]。 | @1 | travel-policy.txt, travel-policy.txt, finance-policy.md, finance-policy.md, employee-handbook.md |
| PASS | trv-106 | factual/L2 | enterprise | 境外出差费用按什么汇率折算报销？ | 境外出差费用按报销当日中国银行外汇牌价折算为人民币报销[8]。 | @1 | travel-policy.txt, finance-policy.md, finance-policy.md, travel-policy.txt, employee-handbook.md |
| PASS | it-001 | factual/L1 | enterprise | 系统密码长度至少要求多少位？ | 系统密码长度至少要求10位[1]。 | @1 | it-policy.pdf, it-policy.pdf, employee-handbook.md, it-policy.pdf, travel-policy.txt |
| PASS | it-002 | factual/L1 | enterprise | 密码需要多长时间更换一次？ | 密码每 90 天更换一次 [1]。 | @1 | it-policy.pdf, it-policy.pdf, it-policy.pdf, travel-policy.txt, employee-handbook.md |
| PASS | it-003 | factual/L1 | enterprise | 离职员工需要在多久内归还办公设备？ | 离职员工须在 3 个工作日内将全部设备归还 IT 部，并完成数据清除 [1]。 | @1 | it-policy.pdf, employee-handbook.md, finance-policy.md, employee-handbook.md, finance-policy.md |
| PASS | it-101 | factual/L2 | enterprise | 主管级别可以额外申请什么办公设备？ | 主管级别可以额外申请外接显示器一台 [1]。 | @1 | it-policy.pdf, it-policy.pdf, finance-policy.md, travel-policy.txt, employee-handbook.md |
| PASS | it-102 | factual/L1 | enterprise | 连接公共 Wi-Fi 时公司要求必须使用什么？ | 公司要求连接公共 Wi-Fi 时必须使用公司 VPN [1]。 | @1 | it-policy.pdf, it-policy.pdf, it-policy.pdf, employee-handbook.md, travel-policy.txt |
| PASS | it-103 | factual/L2 | enterprise | 设置密码时必须包含哪几类字符？ | 设置密码时必须包含以下四类字符：   - 大写字母   - 小写字母   - 数字   - 特殊字符    依据：密码长度不少于 10 位，须包含大写字母、小写字母、数字和特殊字符 [1]。 | @1 | it-policy.pdf, it-policy.pdf, it-policy.pdf, travel-policy.txt, employee-handbook.md |
| PASS | it-104 | factual/L1 | enterprise | 发生数据泄露等信息安全事件，多久内要上报？ | 发生数据泄露等信息安全事件，须在1小时内上报IT安全组[1]。 | @1 | it-policy.pdf, it-policy.pdf, employee-handbook.md, employee-handbook.md, travel-policy.txt |
| PASS | it-105 | factual/L2 | enterprise | 远程办公需要经过谁审批？ | 远程办公须经部门负责人审批 [1]。 | @1 | it-policy.pdf, travel-policy.txt, finance-policy.md, travel-policy.txt, employee-handbook.md |
| PASS | neg-001 | negative/L1 | enterprise | 公司为员工提供宿舍吗？ | 知识库中未找到相关依据。 | - | travel-policy.txt, employee-handbook.md, employee-handbook.md, travel-policy.txt, finance-policy.md |
| PASS | neg-002 | negative/L1 | enterprise | 员工商业补充医疗保险的报销比例是多少？ | 知识库中未找到相关依据。 | - | - |
| PASS | neg-003 | negative/L1 | enterprise | 公司有股票期权激励计划吗？ | 知识库中未找到相关依据。 | - | - |
| PASS | neg-004 | negative/L1 | enterprise | 员工购车有哪些补贴政策？ | 知识库中未找到相关依据。 | - | - |
| PASS | neg-005 | negative/L1 | enterprise | 公司团建活动经费标准是多少？ | 知识库中未找到相关依据。 | - | - |
| PASS | neg-006 | negative/L1 | enterprise | 员工心理咨询服务怎么预约？ | 知识库中未找到相关依据。 | - | - |
| PASS | neg-007 | negative/L1 | enterprise | 员工子女教育补贴是多少？ | 知识库中未找到相关依据。 | - | - |
