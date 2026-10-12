# skill-forge v1.1 修改与验证报告

日期：2026-10-03。基于用户提供的skill-forge-v1.0-source.zip修改；原包与原始Downloads目录保持原样。版本号统一为1.1.0，数据库head为0007。

## 修改内容

| 问题 | 修改 | 验证证据 |
|---|---|---|
| 删除运行中项目后工具继续写文件/Worker异常 | BEGIN IMMEDIATE串行化领取与删除；running返回409，结束后可正常删除 | 阻塞假模型+真实Worker线程+DELETE接口测试 |
| 删除中间取消任务提前commit，失败后无法整体回滚 | queued取消、事件、子表删除单事务提交 | 对真实session.delete注入故障，项目/文件/queued状态保留 |
| 数据库删除成功而磁盘清理失败 | 先改名到.trash暂存；失败恢复；提交后清理失败返回待清理标记 | 注入rmtree权限失败后重启对账清理；模拟提交前崩溃恢复 |
| 阻塞模型调用没有持续心跳 | 独立线程+独立Session按lease/3续租，更新检查worker归属 | 0.3秒租约下0.65秒模型调用，另一Worker恢复检查不误标 |
| 陈旧Session覆盖interrupted终态 | complete/fail/cancel使用条件SQL更新，Worker提交检查归属 | 缓存running后他会话恢复，迟到complete不覆盖终态 |
| Worker异常后Session处于失败事务状态 | 写任务失败前rollback | 生产异常处理路径复核，既有Worker测试回归 |
| 响应已超预算仍执行本轮工具 | 模型返回后检查消耗；带工具且预算耗尽立即退出 | 115/100token响应不写late.md，保留已产出文件 |
| 取消在模型调用期间到达仍执行工具 | 返回后及逐工具前查取消/归属 | 假模型返回前设置取消，工具不执行 |
| completed掩盖正常结束请求的预算超额 | 新增持久化budget_json与API字段，界面显示超额 | Executor预算元数据测试、迁移往返、类型检查与构建 |
| 旧删除测试替换整个服务，未测试生产实现 | 改为真实Session故障注入 | 新增tests/test_v11_safety.py与原删除测试 |
| 文档与实际行为有误差 | 修正远程发送数据、密钥落盘边界、8710端口、备份无签名/逐文件哈希、前置skill定位 | 对照当前实现，不沿用历史审计报告的全通过结论 |

现有项目、需求、草稿、版本、队列、SSE、脚本工具、运行、对照评测和备份功能均保留。运行中先取消/等待再删除是并发保护，结束后完整删除功能仍可使用。取消是协作式，不能立即终止已开始的模型请求或脚本。

## 测试与构建

- 新增9项回归测试：先在原生产实现上9项失败，修改后9项通过。
- 后端完整套件：134 passed、1 skipped（135项，25.91秒）；跳过的是已有None-Origin占位用例。最初沙箱限制导致的2项端口测试在获得网络权限后通过，最终完整套件已包含这2项。
- TypeScript类型检查通过；使用frontend/package-lock.json执行npm ci后重新Vite生产构建，dist与修改界面一致。
- 前端parseRoute独立测试8/8通过；文档brief JSON示例经BriefDraft结构与validate_brief业务校验通过。
- 独立代码复核：生产diff与0007迁移复查，新回归9项复跑通过，未发现阻断本次修复的新增实质缺陷。

运行条件：使用本机bundled Python与已有纯Python依赖执行后端测试，前端依赖在Mac上按锁文件新装。未执行干净环境uv sync全套安装测试，未调用真实模型端点，也未完成Windows运行回归和完整浏览器交互验收。假模型测试验证控制流，不代表真实供应商的兼容性、计费或网络可靠性。依赖弃用警告与现有None-Origin占位跳过单独保留说明。

## 升级与日常使用

1. 停止旧启动器、API和Worker，按原CLI先导出备份。备份应放在data_dir之外。
2. 解压本次源码包到新目录，保留原data目录；用config.yaml或SKILLFORGE_DATA_DIR指向需要升级的数据目录，也可在空目录恢复备份。
3. 执行uv sync；前端dist已随包附带，重新修改前端时执行npm ci、npm run typecheck、npm run build。
4. 执行uv run python launcher.py。启动器迁移到0007，再对账.trash，再启动API与Worker。
5. 默认端口8710；浏览器未自动打开时用终端打印的带#token地址。令牌是凭据，终端共享或截屏时自行隐藏；它不通过URL请求片段发往服务器。
6. 老运行记录budget默认为{}，界面不伪造历史预算。新记录同时显示完成状态与预算用量。
7. 若删除提示项目运行中，取消并等待终态后重试。若暂存清理失败，API返回X-Cleanup-Pending:true；重启会自动重试。原目录冲突或权限错误保留pending并提示，不覆盖现有目录。

手动重试暂存对账（先停止API和Worker）：

```bash
uv run python -c 'from app.core.config import load_config; from app.services.projects import reconcile_project_trash; print(reconcile_project_trash(load_config()))'
```

该命令按数据库记录判断恢复/清理，不是直接清空回收站。旧数据库与0007同时使用旧应用不在兼容承诺内；需要回退时恢复停服前的完整备份，而非仅替换Python文件。

## 仍然保留的边界

- 数据库与文件系统没有真正的跨介质原子事务；本次通过暂存、回滚和启动对账覆盖删除的崩溃窗口。
- token预算按提供商回报usage控制调用边界。输入token无法由max_tokens限制，单次请求可能超额；正常stop的完成状态与超额预算分别记录。
- 目录沙箱只约束工具路径，脚本是普通子进程；模型提示词、工具输出可能发到配置的远程端点。当前没有操作系统级隔离和密钥环境隔离。
- 备份检查包标识、合法路径与SQLite quick_check，没有逐文件签名/哈希验真，不能称作防篡改备份。
- 自动生成技能、原生宿主适配、盲评、自动迭代、触发优化和安装器仍未实现。本次没有删掉这些目标，也没有将其写成已交付。

前置skill的完整适配判断、目录建议、Schema示例与测试方案见[前置skill适配评估](前置skill适配评估.md)。
