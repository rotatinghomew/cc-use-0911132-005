# 非应答商户下交易景气结论的敏感性边界

项目使用 Python 3.11 标准库与 SQLite，现有逐案处理入口为 `market_index_bureau.publisher.cli`。保留原有处理器、持久化与测试；分析业务使用本地合成资料，不连接生产系统。无需 pip 安装、下载模型或启动额外服务。

## 现有程序

在仓库根目录运行 `bash scripts/demo.sh`，依次完成现有产品回归、分析输入核对、语法构建和真实业务入口调用。所有运行文件位于临时目录，结束后清理；运行失败立即退出，不使用预存输出代替执行。

单独测试：`python3 -m unittest discover -s tests/index_vintages -p 'test_*.py' -v`。

输入检查：`python3 -m unittest discover -s tests -p test_analysis_inputs.py -v`。

构建检查：`python3 -m compileall -q market_index_bureau tests`。

业务调用：`python3 -m market_index_bureau.publisher.cli fixtures/demo.json --database /tmp/market_index_bureau-demo.sqlite3`。

## 分析资料与联调

`fixtures/analysis/input.json` 保存业务演练数据，字段解释在 `fixtures/analysis/fields.md`。以下已核对的数据形态用于分析回归：missing_not_zero、unbounded_assumption、large_merchants。现有输入测试检查这些形态是否真实存在，不代替分析方法或数值正确性的验证。

分析入口为 `python3 -m market_index_bureau.coverage_sensitivity --input <panel_json> --database <vintage_db> --output <sensitivity_json>`，可用 `--missing-growth-min/--missing-growth-max` 调节缺报相对变化范围（`--missing-growth-max unbounded` 表示业务未给上界）。方法为区间包络：全框指数对缺报值线性，界限在情景角点精确取得，是确定性转折点而非误差线。输出含 `study_id, coverage, distribution, composition, envelopes, tipping_points, assumptions`；底稿以 `study_id` 指纹存入 `coverage_studies` 表，同一指数版本可对应多份假设底稿，旧底稿只增不改，同输入重放返回同一记录。缺报不按随机缺失处理，情景填充值标记为假设而非观测；无上界情景的包络上界输出 `null`。

执行 `bash scripts/feature_demo.sh`：先重跑现有产品，再从干净数据库计算分析、同输入重放、核对输出结构并打印完整 JSON。本地案例同时展示：全响应子样本包络收缩为点、非随机缺报下 mild/severe 情景方向可能反转、unbounded 情景保留未知上界。结构核验不能代替统计验证，方法正确性由 `tests/test_coverage_sensitivity.py` 覆盖。

## 离线环境

使用 Python 3.11.2 的 Linux 环境即可复现。容器环境预置解释器后，以只读方式挂载仓库，将文件复制到容器临时目录再运行脚本；运行时关闭网络。基础测试和小规模本地数据适合在 90 秒内展示，正式展示命令为 `bash scripts/feature_demo.sh`。不要在展示脚本里安装依赖或等待外部服务。
