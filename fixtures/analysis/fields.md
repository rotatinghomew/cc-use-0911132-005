# 业务演练数据口径

turnover.value 的 null 是未响应，0 是确认无成交；单位为千元人民币。frame.weight 为总体框权重，不是某条交易的件数。缺报增长情景相对该商户 base 值；max 为 null 表示业务未给上界，不可把它解释为零。index_reference 仅引用既有发布，不授权重算发布。

各 ID 均为本地合成标识，不包含真实人员或生产数据。时刻使用带时区 ISO 8601；as_of 是分析可知信息的截止时刻，不是业务发生时间上界。晚到事实允许保留在输入中，但只能在 available_at 到达之后采用。source_versions 标识输入版本；baseline_case_id 关联同库既有演示运行。空值表示未知，不应未经说明转成零。
