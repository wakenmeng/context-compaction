# context-compaction

[English](README.md) | 中文

针对编程 agent 的 Markov 状态压缩实验，基于论文 [You don't need long context](https://arxiv.org/pdf/2611.05306)（arXiv 2611.05306）

论文的做法是定期把历史重写成一段文本交接（EoCC），以此保持短上下文。我们要验证：在同样的 16K/12K 预算下，改用按字段增量更新、带证据和失效规则的结构化工作状态，能否减少信息丢失和重复工作，同时不降低成功率

完整方案见 [docs/plan.md](docs/plan.md)。这是预登记版本，在任何 agent 运行之前提交，成功与失败的判定以它为准

## 进度

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| M0 | 抽样、锁定环境、x86 gold 验证 | 已完成 |
| M1 | 事件日志、状态 schema、patch 校验器、失效规则、回退和重建 | 已完成 |
| M2 | 上下文组装、token 预算、`hist` 检索、录制 trace 回放 | 下一步 |
| M3 | 接入 mini-swe-agent，B/C/E 配置，1 题端到端 | |
| M4 | 20 题冒烟，调参后冻结配置 | |
| M5 | 100 题正式实验和官方评测 | |
| M6 | 决策回放、统计分析、报告 | |

目前还没有跑过任何 agent

## 实验组

| 组 | 上下文策略 | 压缩后保留什么 |
| --- | --- | --- |
| B | 论文 EoCC，固定 16K 上限，12K 触发压缩 | 用论文附录 G.1 提示词写的文本交接 |
| C | 同样 16K/12K | 结构化状态 JSON：模型提交字段级 patch，harness 校验后才写入；完整历史进事件日志，模型可用 `hist` 查询 |
| E | C 加上临时扩容到 48K 或 128K，每题最多 3 次 | 同 C |

三组都会实现。正式实验只比较 B 和 E，C 用来验证代码。这个范围调整会在任何运行之前写进预登记方案

## 固定版本

所有固定值集中在 `src/context_compaction/pins.py`，依赖全部由 `uv.lock` 锁定

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.12 | 只锁一个解释器，锁文件只有一份解析结果 |
| mini-swe-agent | 2.4.0 | 论文只写 v2.4，小版本是我们的选择 |
| swebench | 5.0.2 | 官方评测；论文未写版本 |
| 数据集 | SWE-bench/SWE-bench_Verified @ `78f471bf` | 500 题，parquet sha256 写在 pins.py |

数据集不用 `princeton-nlp/SWE-bench_Verified`：swebench 5.x 评测需要 `image`、`eval_script`、`log_parser` 字段，只有 `SWE-bench/` 版本有。两者 500 个 ID、题面、补丁、base commit 完全相同；`PASS_TO_PASS` 在 `astropy__astropy-7606`、`django__django-10097` 两题上有删减。agent 运行和评测都读同一个本地快照 `data/SWE-bench_Verified`

## 抽样

`configs/instances.json` 已生成并冻结，种子 20261104

- 正式集 100 题：按仓库分层，配额用随机化系统取整，每题入样概率都是 1/5，直接取平均就是 500 题成功率的无偏估计。单次样本里小仓库可能为 0（本次 Flask、Seaborn 为 0），这是抽样波动，不是偏差
- 冒烟集 20 题：从剩余 400 题中抽取，每个仓库先抽 1 题，其余按比例分配。只用于调参和环境验证，不进入任何估计
- `records_sha256` 覆盖每条记录的全部字段，数据内容有任何变化都能检测到

## gold 验证

在 x86 上用 gold 补丁对抽中的 120 题跑官方评测，119 题通过。`psf__requests-1724`（冒烟集）5 次都没通过，原因是它的 `PASS_TO_PASS` 测试要访问外网 httpbin.org（429 限流和证书过期）。它已冻结在 `configs/env_unsolvable.json`，在所有组的主分析中排除。正式集 100 题全部通过。汇总在 `results/gold/`

## 代码

| 路径 | 内容 |
| --- | --- |
| `src/context_compaction/event_log.py` | 只追加的事件日志，保存在容器外，记录每个动作改了哪些文件 |
| `src/context_compaction/state.py` | 状态 schema 和带版本的存储 |
| `src/context_compaction/patch.py` | patch 校验、过期证据的失效、回退后的重建 |
| `src/context_compaction/compactor.py` | 一次压缩：patch、共用一次重试、文本回退、重建 |
| `src/context_compaction/pins.py`、`credentials.py`、`tokens.py` | 固定值、API key 读取、token 计数 |
| `scripts/` | 抽样、数据下载、环境检查、gold 评测、云主机管理 |
| `tests/` | 单元测试（`pytest`） |

## 运行环境

所有 Docker 任务（gold 评测、冒烟、正式实验、官方评测）都在 GCP 的 x86 云主机上跑。SWE-bench 镜像都是 x86_64，在 Apple Silicon 的 Mac 上要靠仿真运行，环境失败和模型失败就分不清了。Mac 只用来开发和跑单元测试

云主机默认配置：`n2-standard-16`（16 vCPU / 64 GB），Ubuntu 24.04 amd64，500 GB pd-balanced。用 `CC_PROJECT` 指定你的 GCP 项目 ID；`CC_ZONE`、`CC_VM`、`CC_MACHINE`、`CC_DISK_GB` 可覆盖 `scripts/cloud/gcp.sh` 里的其余默认值。用一个专用项目跑实验，结束后一条 `gcloud projects delete` 就能清掉所有资源

```bash
scripts/cloud/gcp.sh create
scripts/cloud/gcp.sh setup
CC_JOB=check scripts/cloud/gcp.sh run python scripts/check_env.py
CC_JOB=gold-smoke scripts/cloud/gcp.sh run python scripts/gold_eval.py --set smoke --workers 8
scripts/cloud/gcp.sh logs gold-smoke
scripts/cloud/gcp.sh pull
scripts/cloud/gcp.sh stop
```

- `setup` 会先 `push`：用 rsync 同步代码（含 `.git`，不含 `.venv`、`data`、`results`），把 API key 文件（`CC_KEY_FILE`）复制到云主机（权限 600），然后安装 Docker、uv 和锁定的依赖，跑单测并下载数据快照
- `run` 在 tmux 里后台执行，断开 SSH 不影响；日志写在云主机的 `logs/<CC_JOB>.log`
- 改了代码之后先 `push` 再 `run`；结果用 `pull` 拉回本地的 `results/`
- 不用的时候 `stop`，停机后只收磁盘费；`delete` 会连磁盘一起删掉
- `gcloud compute config-ssh` 会往 `~/.ssh/config` 写入 `cc-x86.<zone>.<project>` 主机条目
- Docker 已配置 `mirror.gcr.io` 镜像缓存；如果拉镜像遇到 429 限流，在云主机上执行一次 `docker login`

## 在 Mac 上开发

```bash
uv sync --frozen --extra dev
source .venv/bin/activate
pytest
python scripts/fetch_dataset.py
```

- API key：优先读环境变量 `OPENAI_API_KEY`，没有时读 `CC_CREDENTIALS_DIR` 目录下的 `openai.txt`（一行裸 key）。key 要放在仓库之外
- `check_env.py`：快速检查，任何一项失败都返回非零，不会打印密钥
- `gold_eval.py`：用 gold 补丁跑官方评测，列出没有 resolved 的题，有这样的题时返回非零。先跑冒烟集，再跑全部 120 题；同一个 `--set` 重跑时会跳过已完成的题，`--run-id` 可以开一次全新的运行。结果写在 `results/gold/`。重跑 2 次仍不通过的题写入 `configs/env_unsolvable.json`，在跑 agent 之前冻结
- `sample_instances.py`：重新运行时如果结果和已冻结的文件不同，会拒绝覆盖
