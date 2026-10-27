# v9.14.0 验证与升级记录

发布日期：2026-10-27。基于 `c435d44cd657fb3fc7067a8a16d689a22c9262ef`。

## 问题与修复

在欧姆龙原生ST路径，显式比较/赋值先由扫描器记录，而CASE分支读证据原先在整个 `STParser.seq()` 成功后才回填。CASE前后出现不透明调用等不支持语句时，整块异常会丢失CASE读证据与完整迁移；显式IF比较仍然留存。因此可以出现同一程序中 `iCmdNo` 有候选而 `iCmdStep` 为零。

本版将状态发现从完整解析中分离，并将可恢复的CASE片段作为待核验候选保留。完整证明路径继续排除 `guard_complete=false`；未知写入、外层门控、动态赋值与扫描顺序限制继续阻断等价证明。

`recall.py` 的三个比较值门槛属于PLCopen旧召回路径，不是所有格式的统一准入规则。名称过滤发生在候选生成之后；另外，默认glob不能将点写成正则转义 `\.`。

## 合成复现

测试构造29个显式命令比较和两个CASE状态，不使用真实项目源码或GT。

| 场景 | 版本 | CASE候选 | 命令候选 | 过滤后候选 | 主采集对 | 完整门控迁移 | 原子核验行 |
|---|---|---:|---:|---:|---:|---:|---:|
| 干净CASE | v9.13基线 | 4 | 29 | 4 | 2 | 2 | 0 |
| 干净CASE | v9.14 | 4 | 29 | 4 | 2 | 2 | 0 |
| CASE后追加 `Housekeeping();` | v9.13基线 | 0 | 29 | 0 | 0 | 0 | 0 |
| CASE后追加 `Housekeeping();` | v9.14 | 4 | 29 | 4 | 2 | 0 | 4 |

后者恢复的两条主采集对均标记“源码ST片段迁移-待核验”。原子核验行包含不同原候选的投影来源，不能把4行解释为4个唯一动作。

复现命令：

```bash
python -m pip install -r requirements.txt
python plc_cli.py --version
python -m unittest discover -q
python validation/reproduce_case_loss_v9_14.py --out outputs/case_regression_v9_14 --expected fixed
```

如需对照基线，将同一脚本的 `--repo` 指向v9.13代码目录，使用 `--expected baseline`，输出另存到独立目录。脚本只生成测试XML与解析结果。

默认glob：`stEM*.ProcessCtrl.iCmdStep`。正则：`re:stEM.*\.ProcessCtrl\.iCmdStep`。默认glob中的反斜杠会按字面字符参与匹配；本版增加提示，不静默改写用户模式。

## 自动化检查

- 原有290项及新增24项，合计314项通过。
- 新增用例覆盖三种格式的CASE发现、未知调用前后、未知函数右值、循环、作用域/证据保留、旧邻接候选保全、过滤提示、诊断位置及工艺推荐阻断。
- 验证 `--version` 与预测 `prediction_provenance.json` 一致读取 `VERSION`。
- `SHA256SUMS.json` 覆盖本版所有受版本管理的文件，自身除外；不包含运行输出和临时补丁。

验证数据：`validation/release_metrics_v9_14.json`；测试日志：`validation/test_run_v9_14.txt`。
日志中的 `--gt truth.tsv` 参数拒绝来自预测/评价隔离的反向测试，最终结果为314项 `OK`。

## 升级与回退

更新代码后使用新的输出目录重新运行predict；既有预测文件不会自动补齐。随后按原流程处理全部OpenCode批次并finish。

本次保留配置恢复、逻辑修复、版本记录三个独立提交。`release/v9.14.0` 分支记录本次完整版本。若需回退，优先在新提交中revert相应变更；需要复查旧行为时可单独检出上述基线提交，避免覆盖后续工作。

## 验证边界

本版验证是合成复现与仓库回归测试，尚未获得所述新项目的完整PLC源码并进行全量重跑，不能把合成样例的数量视为该项目的恢复数量。本轮未执行真实OpenCode/GLM工艺批次或PLC在线采样验证。

片段恢复不等于完整ST CFG；不透明语句位于CASE内部时仍可能只能发现状态而不能恢复迁移。动态状态目标、计算型选择器、不透明副作用及跨任务扫描顺序继续保留待核验。

恢复的OpenCode配置来自历史发行包：提交 `93f6c78afdbabbd4977a08ecd7e3dc2333e93622` 中的v9.13 ZIP，其SHA256为 `c5f95bec13914f8890d6b90213b9d71496acb7c4704f5b6dd757b8458b914546`；未引入新的Skill规则。
