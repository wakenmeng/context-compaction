# context-compaction

Markov 状态压缩实验，基于 [You don't need long context](https://arxiv.org/pdf/2611.05306)

完整方案见 [docs/plan.md](docs/plan.md)。这是预登记版本，在任何 agent 运行之前提交，成功与失败的判定规则以它为准

当前进度：M0 完成（抽样、锁定环境、x86 gold 验证），B/C/E 还没有开始跑

## 版本

所有固定值集中在 `src/context_compaction/pins.py`，依赖全部由 `uv.lock` 锁定

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.12 | 只锁一个解释器，锁文件只有一份解析结果 |
| mini-swe-agent | 2.4.0 | 论文只写 v2.4，小版本是我们的选择 |
| swebench | 5.0.2 | 官方评测；论文未写版本 |
| 数据集 | SWE-bench/SWE-bench_Verified @ `78f471bf` | 500 题，parquet sha256 写在 pins.py |

数据集不用 `princeton-nlp/SWE-bench_Verified`：swebench 5.x 评测需要 `image`、`eval_script`、`log_parser` 字段，只有 `SWE-bench/` 版本有。两者 500 个 ID、题面、补丁、base commit 完全相同；PASS_TO_PASS 在 `astropy__astropy-7606`、`django__django-10097` 两题上有删减。agent 运行和评测都读同一个本地快照 `data/SWE-bench_Verified`

## 抽样

`configs/instances.json` 已生成并冻结，种子 20261104

- 正式集 100 题：按仓库分层，配额用随机化系统取整，每题入样概率都是 1/5，直接取平均就是 500 题成功率的无偏估计。单次样本里小仓库可能为 0（本次 Flask、Seaborn 为 0），这是抽样波动，不是偏差
- 冒烟集 20 题：从剩余 400 题中抽取，每个仓库先抽 1 题，其余按比例分配。只用于调参和环境验证，不进入任何估计
- 输出里有 `records_sha256`，覆盖每条记录的全部字段，数据内容有任何变化都能检测到

## 运行环境

所有 Docker 任务（gold 评测、冒烟、正式实验、官方评测）都在 GCP 的 x86 云主机上跑。SWE-bench 镜像都是 x86_64，在 Mac 上要靠仿真运行，环境失败和模型失败就分不清了。Mac 只用来开发和跑单元测试

云主机默认配置：`n2-standard-16`（16 vCPU / 64 GB），Ubuntu 24.04 amd64，500 GB pd-balanced。默认项目由 `CC_PROJECT` 指定，区域 `us-central1-b`，不受 gcloud 全局配置影响；在你自己的账号下运行时，用 `CC_PROJECT` 换成你的项目 ID。可以用 `CC_PROJECT`、`CC_ZONE`、`CC_VM`、`CC_MACHINE`、`CC_DISK_GB` 覆盖。实验结束后执行 `gcloud projects delete <project-id>` 就能清掉所有资源

```bash
scripts/cloud/gcp.sh create
scripts/cloud/gcp.sh setup
CC_JOB=check scripts/cloud/gcp.sh run python scripts/check_env.py
CC_JOB=gold-smoke scripts/cloud/gcp.sh run python scripts/gold_eval.py --set smoke --workers 8
scripts/cloud/gcp.sh logs gold-smoke
scripts/cloud/gcp.sh pull
scripts/cloud/gcp.sh stop
```

- `setup` 会先 `push`：用 rsync 同步代码（含 `.git`，不含 `.venv`、`data`、`results`），把 key 放到云主机的 `~/.config/context-compaction/`（权限 600），然后安装 Docker、uv 和锁定的依赖，跑单测并下载数据快照
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

- API key：优先读环境变量 `OPENAI_API_KEY`，没有时读 `~/.config/context-compaction/openai.txt`（一行裸 key，目录可用 `CC_CREDENTIALS_DIR` 覆盖）。key 不进仓库
- `check_env.py`：快速检查，任何一项失败都返回非零，不会打印密钥
- `gold_eval.py`：用 gold 补丁跑官方评测，列出没有 resolved 的题，有这样的题时返回非零。在云主机上先跑冒烟集，再跑全部 120 题；同一个 `--set` 重跑时会跳过已完成的题，结果写在 `results/gold/`。没通过的题再重跑 2 次，仍不通过就写入 `configs/env_unsolvable.json`，在跑 agent 之前冻结，所有组的主分析统一排除这些题
- `sample_instances.py`：重新运行时如果结果和已冻结的文件不同，会拒绝覆盖
