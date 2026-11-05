# context-compaction

[English](README.md) | 中文

**编程 agent 能不能在 16K 上下文里工作，同时不忘掉已经弄清楚的事？** 这个项目要验证一种结构化、可校验的做法，替代现在 agent 压缩上下文时写的文本交接

## 结论：上下文窗口有限时，上下文压缩好不好？

**按现有证据，好。** 在 16K 窗口里工作，把上下文压缩成结构化状态，压缩放不下时退到 24K，agent 解出的题和论文里保留完整历史的 agent 一样多，每题花费更低

| 设置 | 模型看到的上下文 | 解决 | 每题费用 | 来源 |
| --- | --- | --- | --- | --- |
| **本项目：压缩，16K，放不下时用 24K** | 16K；只有溢出的 8 道题用 24K | **85/100**（95% 区间 77–91%） | **$0.30** | 100 题 |
| 本项目：压缩，只用 16K | 16K | 82/100（95% 区间 73–88%） | $0.27 | 100 题 |
| 论文：不压缩（完整历史） | 完整历史 | 84.4% | $0.34 | 500 题 |
| 论文：文本交接压缩（EoCC） | 16K | 84.6% | $0.41 | 500 题 |

所有行都是同一个模型（GPT 5.6 Sol），都按同一份标价计费（每百万新输入 / 缓存输入 / 输出 token $4 / $0.40 / $20）。16K/24K 那一行里，一道题只有在 16K 下溢出才换到 24K，与测试结果无关，两次运行的费用都计入

- **准确率没有损失。** 85% 对比完整历史的 84.4%，而 100 道题里有 92 道，模型看到的上下文从未超过 16K
- **更便宜。** 每题 $0.30，完整历史是 $0.34，论文的 16K 文本交接是 $0.41。原因是状态按字段打补丁、不整体重写，压缩后上下文从很短重新开始
- **不会让 agent 重复干活。** 压缩之后只有 0.35% 的动作重复了之前做过的事
- **只用 16K 偏紧。** 8% 的题在 patch 被拒后没有空间重试而溢出；有 24K 兜底时一题都没有

**这些结论还说明不了什么。** 论文的数字来自全部 500 题，用的是它自己的提示词和 harness，所以这是外部对比，不是配对比较；区间宽约 ±7 个百分点，差几个点看不出来。16K/24K 那一行是在 16K 正式结果出来之后合并的。在同一批 100 题上跑一组不压缩的基线，就能把它变成配对结果。完整分析见 [docs/results.zh-CN.md](docs/results.zh-CN.md)

## 为什么做

论文 [You don't need long context](https://arxiv.org/pdf/2611.05306)（arXiv 2611.05306）表明：前沿模型只要频繁压缩、保持短上下文，在 SWE-bench Verified 上的准确率就能接近全历史基线。它的压缩方式是每次把整段历史重写成一份新的文本交接，论文自己也承认留下了三个空白：

- 多次重写，损失会累积
- 模型不知道自己忘了什么，光有检索工具解决不了
- 只看最终成功率，没有检验压缩状态能否替代完整历史

## 思路

把压缩看成维护一个 **Markov 工作状态**：给定状态和可访问的环境，下一步决策不再需要完整历史

- **结构化状态，按字段 patch。** 目标、事实、改动、检查、死胡同、计划、下一步都放在一份 JSON 状态里。模型提交 patch，harness 校验后才写入，每个版本都保留，不会被悄悄重写
- **harness 能核实的证据。** 事实和检查要引用事件日志里的事件，日志只追加、保存在容器外。harness 在每个动作之后对仓库文件做哈希，知道文件什么时候变过，并自动把依赖它的事实和检查标为 stale。没有更新的观察，模型无法让过期证据恢复有效
- **指针代替复制。** 大段输出留在日志里，模型需要时用 `hist` 读取有长度上限的片段
- **弹性上下文。** 平时 16K/12K；分块读取解决不了时，agent 可以临时扩到 48K 或 128K，几步之后再压回常态

## 实验

| 组 | 上下文策略 | 压缩后保留什么 |
| --- | --- | --- |
| B | 论文 EoCC 基线，固定 16K，12K 触发压缩 | 用论文附录 G.1 提示词写的文本交接 |
| C | 同样 16K/12K | 经过校验的结构化状态、事件日志和 `hist` |
| E | C 加上临时扩容，每题最多 3 次 | 同 C |

只跑 C：只用 20 道冒烟题调参，冻结配置后再跑 100 道正式题。B（论文基线）和 E（C 加扩容）在方案里有完整设计，但不运行，所以和 B 的配对假设不做检验；C 的结果按描述性结果报告（成功率及置信区间、压缩后的重复工作、费用、压缩结果）。论文 EoCC 16K/12K 在全部 500 题上的 84.6% 只作外部参照，不是配对比较

方案、指标和每一次修订，都在它们影响的运行之前登记在 [docs/plan.md](docs/plan.md)。目前的修订都来自两道冒烟题的试跑，没有一次依据正式题

## 进度

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| M0 | 抽样、锁定环境、x86 gold 验证 | 已完成 |
| M1 | 事件日志、状态 schema、patch 校验器、失效规则、回退和重建 | 已完成 |
| M2 | 上下文组装、token 预算、`hist`、脚本化 trace 的端到端测试 | C 已完成 |
| M3 | 接入 mini-swe-agent，1 题端到端 | C 已完成：两道冒烟题试跑，压缩路径已在真实运行中验证 |
| M4 | 20 题冒烟，调参后冻结配置 | 已完成：两轮都是 16/20；冻结在 `configs/frozen_c.json` |
| M5 | C 跑 100 题，官方评测 | 已完成：82/100 |
| M6 | 统计分析和报告 | 已完成：[docs/results.zh-CN.md](docs/results.zh-CN.md)；8 道溢出题的 24K 补跑 |

正式运行用的是两轮冒烟（都是 16/20）之后冻结的配置。有 8 道正式题溢出，原因是 patch 被拒后的重试放不进 16K；同样 8 道题在 24K/18K 下没有溢出，其中 6 道解决。正式结果按冻结时的样子保留；修复这个缺陷、以及在全部 100 题上做 16K 与 24K 的对比，是 [docs/results.zh-CN.md](docs/results.zh-CN.md) 里的下一步

## 工程说明

- **无偏样本。** 正式集 100 题按仓库分层，配额用随机化系统取整，每题入样概率都恰好是 1/5，直接取平均就是 500 题的无偏估计。排序用 sha256，与 Python 版本无关（[sample_instances.py](scripts/sample_instances.py)）
- **固定输入。** 数据集固定到 Hugging Face 的 revision 和 parquet 的 sha256，样本里记录了覆盖每条记录全部字段的指纹。依赖用 `uv.lock` 锁定
- **先证明环境可用，再跑 agent。** 抽中的 120 题都在 x86 上用 gold 补丁跑过官方评测，119 题通过。唯一失败的那题需要访问外网，在任何运行之前就冻结为排除（[env_unsolvable.json](configs/env_unsolvable.json)）
- **模型钻不了空子的状态。** 证据的新旧按引用的最新动作判断，不看模型自己写的时间点；stale 条目无论绕道什么状态都保留重新验证门槛；格式错误的 patch 一律是拒绝，不会让程序崩溃（[patch.py](src/context_compaction/patch.py)）
- **对抗式评审。** 方案和 M1 代码由另一个模型（OpenAI Codex）反复评审，直到没有阻塞问题。每条意见都先复现再修，并配回归测试；M1 的几轮评审堵上了十几种模型可能绕过证据规则的方式

## 目录

| 路径 | 内容 |
| --- | --- |
| `docs/plan.md` | 预登记方案：假设、指标、统计方法、里程碑、修订记录 |
| `docs/results.zh-CN.md` | 结果与分析 |
| `src/context_compaction/event_log.py` | 只追加的事件日志，记录每个动作改了哪些文件 |
| `src/context_compaction/state.py` | 状态 schema 和带版本的存储 |
| `src/context_compaction/patch.py` | patch 校验、失效、回退后的重建 |
| `src/context_compaction/compactor.py` | 一次压缩：patch、共用一次重试、文本回退、重建 |
| `src/context_compaction/agent.py` | 接入 mini-swe-agent：token 预算、压缩与重置、`hist`、逐动作记录 |
| `src/context_compaction/context.py`、`hist.py`、`repo_files.py` | 上下文组装、历史检索工具、容器内文件哈希 |
| `src/context_compaction/pins.py` | 固定的版本和数据快照 |
| `scripts/` | 抽样、数据下载、环境检查、gold 评测、云主机、分组运行、评测、分析 |
| `configs/` | 冻结的题目名单和排除名单 |
| `results/gold/` | gold 评测汇总 |
| `results/summary/`、`results/c-formal/`、`results/c24k-overflow/` | 报告所用运行的分析汇总和每题记录（运行、用量、评测、patch） |
| `tests/` | 单元测试 |

## 复现

开发和单元测试用 [uv](https://docs.astral.sh/uv/)，在任何机器上都能跑：

```bash
uv sync --frozen --extra dev
uv run pytest
uv run python scripts/fetch_dataset.py
```

与本机相关的设置放在被 gitignore 的 `.env` 里，从模板开始：

```bash
cp .env.example .env
```

API key 读环境变量 `OPENAI_API_KEY`，或者 `CC_CREDENTIALS_DIR` 目录下的 `openai.txt`。key 要放在仓库之外

凡是要跑 SWE-bench 容器的步骤都需要 x86：镜像都是 x86_64，在 Apple Silicon 上靠仿真运行，环境失败会被误判成模型失败。`scripts/cloud/gcp.sh` 在 `CC_PROJECT` 指定的项目里管理一台 GCP 云主机（默认 `n2-standard-16`、Ubuntu 24.04、500 GB）：

```bash
scripts/cloud/gcp.sh create
scripts/cloud/gcp.sh setup
CC_JOB=check scripts/cloud/gcp.sh run python scripts/check_env.py
CC_JOB=gold scripts/cloud/gcp.sh run python scripts/gold_eval.py --set all --workers 8
scripts/cloud/gcp.sh logs gold
scripts/cloud/gcp.sh pull
scripts/cloud/gcp.sh stop
```

- `setup` 同步代码（不含 `.env`、数据和结果），把 key 文件以 600 权限复制到云主机，安装 Docker、uv 和锁定的依赖，跑单测并下载数据
- `run` 在云主机的 tmux 里启动任务；`pull` 把 `results/` 拉回本地；`stop` 后只收磁盘费
- `check_env.py` 任何一项失败都返回非零，不会打印 key。`gold_eval.py` 有 gold 补丁没通过时返回非零；`--run-id` 可以开一次全新的运行
- `run_group.py --group c --set smoke` 跑一个组，结果写在 `results/c/<instance_id>/`（事件、状态、压缩记录、轨迹、用量、patch）；重跑会续跑，未完成的尝试会归档，每题最多 3 次。`evaluate_group.py --group c --set smoke` 用官方 harness 给一个冻结集合打分

## 固定版本

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.12 | 只锁一个解释器，锁文件只有一份解析结果 |
| mini-swe-agent | 2.4.0 | 论文写的是 v2.4 |
| swebench | 5.0.2 | 论文没写版本 |
| 数据集 | SWE-bench/SWE-bench_Verified @ `78f471bf` | 500 题 |

用 `SWE-bench/SWE-bench_Verified` 而不是 `princeton-nlp/SWE-bench_Verified`，是因为 swebench 5.x 需要前者的 `image`、`eval_script`、`log_parser` 字段。两者 500 个 ID、题面、补丁、base commit 完全相同；`PASS_TO_PASS` 在 `astropy__astropy-7606`、`django__django-10097` 两题上有删减
