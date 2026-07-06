# Foreman 使用说明手册

> 面向最终用户的中文使用指南。本手册只描述代码与文档中真实存在的功能，不虚构任何特性。

---

## 一、Foreman 是什么

Foreman（工头）是一个**多智能体任务编排系统**，专门解决一个真实痛点：

> 给编程 AI 一张 20 条需求的清单（本该干一整天的活），一个小时后它宣布"全做完了"——实际上只做完了 3 条。

Foreman 的做法是把这张清单**拆成一个个小的、可独立验收的任务**，一次只派发一个给"执行者"，并且**在一个独立的"验收者"用真实的测试/构建证明它做完之前，绝不承认任何一条做完了**。

它内部由几个分工不同的角色组成：

| 角色 | 职责 |
|---|---|
| **Planner（规划者）** | 把需求清单拆成有依赖关系的任务图，每个任务带验收标准和可运行的测试策略 |
| **Dispatcher（派发者）** | 纯 Python、不调用大模型；负责依赖解析、原子领取任务、崩溃恢复 |
| **Executor（执行者）** | 在**干净的上下文**里，一次只干一个任务；能读写文件、列目录、执行命令 |
| **Verifier（验收者）** | 先跑真实的客观测试（该任务自己的测试 + 一遍 `pytest -q` 回归），通过后再让大模型打分 |
| **Arbiter（仲裁者）** | 当执行者对被驳回不服时，读真实证据文件，裁决"推翻"或"维持" |

所有状态和历史都写进一个**持久化的 SQLite 账本（Ledger）**——即使程序中断，`--resume` 能从账本原样接着跑，不需要重新规划。

---

## 二、它能干什么

- 把一份 Markdown 需求清单**自动规划、逐条执行、逐条验收**，直到全部完成或卡住。
- **在全新沙箱里从零搭建**一个项目（默认模式，最安全）。
- **在你已有的 git 仓库上工作**（`--project-dir` 模式）——在隔离分支上改真实文件，每完成一个任务提交一次。
- **中断后恢复**（`--resume`）——不重新规划，从账本继续。
- 提供一个**本地网页控制台**：可视化任务墙、实时事件流、多任务并行、成本估算、停止/恢复、下载工作区、归档已完成的运行。
- **演示模式（Demo / Mock）**：不需要任何 API key，用脚本化的假角色跑完整个流程，用来体验产品或录演示。

---

## 三、它不能干什么（诚实的限制）

这些是 v1 版本真实存在的边界，请务必了解：

- **默认是"从零建项目"，改现有项目是可选开关。** 不加 `--project-dir` 时，执行者永远在一个一次性沙箱里干活。让 Foreman 改你真实的代码仓库需要额外开启，并受 git 安全检查约束。
- **验收以 pytest 为主。** 客观验收假设任务的测试命令是 `pytest` 或 `python -c` 能跑的。`npm test`、`go test` 等目前不是一等公民（只是路线图上的计划）。
- **账本是单机 SQLite。** 这是本地"双击即用"工具的刻意选择，没有多机扩展能力（路线图里才有）。
- **评测是演示规模的。** 三条件 A/B/C 评测在 5 条需求的清单上端到端跑过（Foreman 5/5 通过）；20 条的完整评测尚未做（受 API 额度限制）。
- **不是容器、不是虚拟机、没有进程隔离。** 执行者能执行真实 shell 命令（`shell=True`），权限和运行 Foreman 的人一样。命令策略只拦截"明显自毁"的命令（`rm -rf`、`format`、`shutdown` 等），**挡不住** `python -c "..."` 里写的任意代码，也挡不住联网/数据外泄。详见下文安全一节。
- **Foreman 永不合并、变基或推送。** 它只在隔离分支上提交，合不合并由你自己决定。
- **Function Compute（阿里云函数计算）部署**步骤已写在 `docs/DEPLOY.md`，但尚未端到端实跑验证过。

---

## 四、环境准备

### 4.1 依赖

Foreman 刻意保持极简依赖，只需要：

- **Python 3**
- `openai`（用来以 OpenAI 兼容模式访问 DashScope）
- `pytest`（跑测试和客观验收）

安装依赖：

```bash
pip install -r requirements.txt
```

网页控制台**不依赖** Flask/FastAPI，用的是 Python 标准库自带的 HTTP 服务器。

### 4.2 API Key（真实运行才需要）

真实运行需要一个 **DashScope（阿里云百炼国际站）API key**。在**仓库根目录**新建一个 `.env` 文件：

```
DASHSCOPE_API_KEY=sk-你的密钥
```

可选配置项（都有默认值，不填即用默认）：

| 环境变量 | 作用 | 默认值 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 你的密钥（**必填**，除非只跑 mock） | 无 |
| `DASHSCOPE_BASE_URL` | 接口地址 | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| `FOREMAN_PLANNER_MODEL` | 规划者/仲裁者模型 | `qwen-max` |
| `FOREMAN_EXECUTOR_MODEL` | 执行者模型 | `qwen3-coder-plus` |
| `FOREMAN_VERIFIER_MODEL` | 验收者模型 | `qwen-plus` |
| `FOREMAN_FALLBACK_MODELS` | 主模型触发额度不足/限流时依次降级用的模型（逗号分隔） | `qwen-turbo,qwen-flash,qwen3-coder-flash` |
| `FOREMAN_MOCK_DELAY` | mock 模式下每次假 execute/verify 睡多少秒（方便录屏），会被限制在 0–30 | `0` |
| `FOREMAN_COMMAND_POLICY` | 设为 `off` 可关闭命令安全策略（**危险**，不建议） | 开启 |

> 说明：`.env` 里已存在于系统环境变量中的键不会被覆盖；文件支持 UTF-8（含 BOM）。

### 4.3 不需要 key 也能先跑起来

编排核心本身**不需要 API key**，可以先用这两个命令感受一下：

```bash
python demo/smoke_run.py                                    # 假执行者/验收者，看整个循环转起来
python main.py --checklist demo/requirements_mini.md --mock  # 同样的循环，走 main.py 的 CLI
```

跑测试套件（109 个测试，无需 key）：

```bash
python -m pytest -q
```

---

## 五、命令行（CLI）用法

入口是 `main.py`。**必须**二选一提供 `--checklist` 或 `--resume`。

### 5.1 全部参数

| 参数 | 说明 |
|---|---|
| `--checklist <路径>` | 指向一份 Markdown 需求清单（与 `--resume` 互斥，二选一） |
| `--resume <RUN_ID>` | 按 run_id 恢复一次已有的运行，不重新规划（与 `--checklist` 互斥） |
| `--mock` | 用脚本化的假执行者/验收者，无需 API key 和网络 |
| `--mock-delay <秒>` | 仅配合 `--mock`：每次假调用睡这么久，让演示慢到可以录屏（默认 0） |
| `--run-root <目录>` | 运行产物存放的根目录（默认 `runs`） |
| `--project-dir <路径>` | 改现有项目模式：指向一个真实 git 仓库而非新沙箱（只能配 `--checklist`） |
| `--force-dirty` | 改现有项目模式：即使仓库有未提交改动也继续（**不推荐**，只能配 `--checklist`） |

### 5.2 常见用法

**真实运行一份清单（全新项目）：**

```bash
python main.py --checklist demo/requirements_mini.md
```

**免费演示（无需 key）：**

```bash
python main.py --checklist demo/requirements_mini.md --mock --mock-delay 0.5
```

### 5.3 运行结束时你会看到

一段最终摘要，包含：run_id、run_dir（产物目录）、完成/总任务数、被阻塞数、领取次数、耗时、每个任务的尝试次数、以及是否全部完成。

CLI 运行时还会打印一个状态图例：
`[#]done [>]running [?]review [X]blocked [ ]ready [.]pending`

---

## 六、网页控制台用法

控制台是一个功能完整的"控制室"，不只是状态墙。

### 6.1 启动

```bash
start_foreman.bat      # Windows：装依赖、检查 .env、自动打开控制台
python serve.py         # 或直接启动
```

`serve.py` 的参数：

| 参数 | 说明 | 默认 |
|---|---|---|
| `--port` | 端口 | `8787` |
| `--host` | 绑定地址（云/容器环境用 `0.0.0.0`） | `127.0.0.1`（仅本机回环） |
| `--no-browser` | 不自动打开浏览器（用于无头/预览环境） | 关闭 |

启动后访问 **`http://127.0.0.1:8787`**。

### 6.2 主要功能

- **New Run（新建运行）表单**：粘贴需求，或从模板下拉框直接载入 `demo/*.md`。
- **按角色选模型**：planner / executor / verifier 各自可选，还有 "custom…" 选项可填任意 DashScope 模型名，无需改代码。
- **并行运行**：可连续开多个清单；运行列表每 3 秒轮询，用状态点显示（绿=完成 / 琥珀=进行中 / 红=有被阻塞任务 / 灰=空闲），mock 运行会带 `MOCK` 标签。
- **停止 + 恢复**：Stop 是"任务边界优雅停止"——正在执行的那一步会先跑完，再在任务之间停下；Resume 重开同一个账本继续，不重新规划。
- **实时成本估算**：运行头部显示形如 `≈$0.0123 · 45,678 tok` 的读数（美元为粗略估算）。
- **下载工作区 zip** 和 **归档运行**（把完成的运行移出活动列表但不删除）。
- **四色状态墙**（每个任务一格）+ **实时事件流**；`DISPUTE` / `ARBITRATION`（争议/仲裁）事件会用琥珀色徽章高亮——谈判过程是可见的，不是藏起来的重试。
- **配置健康面板**（`/api/config`）：报告 `DASHSCOPE_API_KEY` 是否存在，并给一个打码预览（`sk-abcde…yz`）；**完整密钥永不发给浏览器**。

### 6.3 演示模式（零 API key 体验全部功能）

在 New Run 表单勾选 **"Demo mode"**（或 POST 时带 `"mock": true`），上面所有功能——并行、停止/恢复、成本估算、下载、归档——都会在一套脚本化的假 planner/executor/verifier 上运行，全程不联网。

mock 运行通常不到一秒就结束，太快没法录屏。想放慢，可在启动控制台前设置 `FOREMAN_MOCK_DELAY`，或在 `POST /api/runs` 的请求体里带 `"mock_delay_s": <秒>`：

```bash
set FOREMAN_MOCK_DELAY=4
start_foreman.bat
```

---

## 七、改现有项目模式（`--project-dir`）

Foreman 可以把执行者对准一个真实的 git 仓库，而不是新沙箱——同样的规划/执行/验收/争议循环，只是**工作区就是你的仓库**。

### 7.1 前置条件（由 `foreman/git_safety.py` 在本机强制检查）

- 目标文件夹必须是**git 仓库**（不是就先 `git init`）。
- 必须指向仓库**根目录**，不能是子目录（否则拒绝，避免产生残缺提交）。
- 工作树应当是**干净的**（`git status --porcelain` 为空）。默认拒绝脏树——除非用 `--force-dirty`（见下）。
- **不能有正在进行的 merge/rebase/cherry-pick。** 这种情况直接拒绝，而且 `--force-dirty` **也不能**绕过——先完成或中止那个操作。
- **同一个仓库同时只能跑一个 Foreman。** 用锁文件 `.git/foreman.lock` 串行化；第二个运行会失败并告诉你锁的持有者。若是崩溃留下的陈旧锁，提示会告诉你该删哪个文件。

### 7.2 用法

```bash
python main.py --checklist reqs.md --project-dir C:\path\to\repo
```

注意：`--project-dir` 和 `--force-dirty` **不能**和 `--mock` 或 `--resume` 一起用。恢复一个改现有项目的运行时，会从该运行自己的 `project_mode.json` 自动重新推导出 `project_dir` 和分支，所以你只需：

```bash
python main.py --resume run_xxxxxxxxxxxx
```

安全检查不通过时，Foreman 会打印一句可操作的提示（告诉你下一步怎么做，而不是只说哪里错），并以退出码 1 结束——没有堆栈跟踪，也不留残缺的运行目录。

网页控制台里：在 New Run 卡片填 **"Existing project folder"**，并勾选 **"我理解这会创建 git 分支并修改该文件夹里的真实文件"** 复选框（不勾就不会把该字段发给服务器）。真正的守卫是服务器端和 CLI 相同的 `git_safety` 检查，前端复选框只是礼貌性提醒。

### 7.3 磁盘上到底发生了什么

1. Foreman 创建一个隔离分支 **`foreman/<run_id>`**——你当前分支原封不动。若同名分支已存在，它宁可拒绝也不在别人的历史上乱提交。
2. 规划者会拿到你仓库的一份快照（目录树 + README/package.json/requirements.txt 等预览），以便规划时尊重你已有的结构。快照只用于规划上下文，执行时执行者仍读真实文件。
3. 每个通过验收（直接通过，或争议被推翻后通过）的任务，**单独一次提交**，提交信息 `Foreman: <task_id> <任务标题>`。没动任何真实文件的任务不会产生空提交。若检查点提交在系统层失败（磁盘满、权限），任务已挣得的 DONE **不会**被撤销，而是发出一个 `checkpoint_failed` 事件——看到就自己去 `git log`/`git status` 检查一下。
4. Foreman 自己的账本、事件、运行配置始终在 `runs/<run_id>/` 下，**永不污染你的仓库**。只有代码工作区本身是你的仓库。
5. **Foreman 永不合并、变基或推送。** 运行结束（或中途想看进度）时，自己审查：

   ```bash
   git log foreman/run_xxxxxxxxxxxx
   git diff main...foreman/run_xxxxxxxxxxxx
   ```

   合并、cherry-pick 还是删除这个分支，永远由你决定。

### 7.4 `--force-dirty` 逃生舱（不推荐）

传 `--force-dirty`（CLI）或 `"force_dirty": true`（API）可让 Foreman 在有未提交改动的仓库上照样跑。你原有的未提交工作会**立刻作为一个清楚标注的快照提交**放在 foreman 分支尖端（信息里明确写"运行开始前就存在、非 Foreman 所写"），绝不会被折进某个任务的检查点提交——这样 `git log`/`git blame` 永远不会把你自己的代码算到 AI 头上。但它仍意味着未审查的工作被提交了，所以：**有任何疑虑就先 commit 或 stash。** 仓库根、是否 git 仓库、是否有 merge 进行中这三项检查**没有**"信我"开关，设计上不可绕过。

---

## 八、中断恢复（`--resume`）

- 每次运行的所有状态都写进 `runs/<run_id>/ledger.db`（SQLite，WAL 模式）。
- 恢复：

  ```bash
  python main.py --resume run_xxxxxxxxxxxx
  ```

- **不重新规划**：计划已经在账本的 tasks 表里，直接从那里接着跑。
- `--resume` **不支持** `--mock`（没有假账本可重开）。
- 改现有项目的运行恢复时会自动从 `project_mode.json` 重新指向原来的仓库和分支。
- 网页控制台里对某个运行点 **Resume** 效果相同。

---

## 九、如何查看 / 合并结果

### 全新项目模式

- 产物在 `runs/<run_id>/` 下：`workspace/`（生成的代码）、`ledger.db`（账本）、`events.jsonl`（事件流）、运行配置等。
- 网页控制台可**下载工作区 zip**。

### 改现有项目模式

- 代码就提交在你仓库的 `foreman/<run_id>` 分支上。
- 审查与合并（全部由你手动执行）：

  ```bash
  git log foreman/run_xxxxxxxxxxxx
  git diff main...foreman/run_xxxxxxxxxxxx
  # 满意后自行合并 / cherry-pick / 删除该分支
  ```

### 审计轨迹

账本里的 `attempts` 表是**只追加、永不修改**的审计记录——每次尝试一行，带完整的交接 JSON 和裁决文本。状态墙 UI、评测脚本、`--resume` 都读它。

---

## 十、安全须知（重要）

Foreman 让大模型在一个工具调用循环里跑，其中 `run_command` 给了模型一个**真实 shell**。请务必理解边界：

**受保护的部分：**

- **文件操作牢笼**：`read_file`/`write_file`/`list_dir` 的路径都限制在工作区根目录内，`..` 越界、别处的绝对路径、指向外部的软链接都会被拒。
- **命令策略（DENY 黑名单）**：在真正执行前，拦截"明显自毁"的命令——绝对路径上的 `rm -rf`、`del /s /q`、`format`、`mkfs`、`shutdown`/`reboot`、对 `HKLM` 的 `reg add|delete`、`diskpart`、往 `C:\Windows` 重定向等。**改现有项目模式下**还额外拦截会毁掉你工作或把 HEAD 移出隔离分支的 git 子命令（`git push`、`git reset --hard`、`git clean -f`、`git checkout <分支>`、`git branch -D` 等；单文件回退 `git checkout -- file` 仍放行）。普通相对命令（`pytest`、`python`、`pip install`、`npm ...`）照常放行。可用 `FOREMAN_COMMAND_POLICY=off` 关闭（**危险**）。
- **单命令超时**：跑飞的进程会被杀掉并报告 `timed_out=True`。
- **输出截断**：命令输出有上限，防止撑爆上下文。
- 被策略拦截的命令**不会崩溃**，而是作为普通命令结果（退出码 126）返回给模型，让它换个方式或放弃。

**未受保护的部分（务必知晓）：**

- 命令策略**看不见** `python -c "..."` 或某个被 `pip install` 的包在运行时实际做了什么。会写脚本去删文件、外泄数据、联网的模型，本仓库里没有任何东西能拦住。
- **这不是容器/虚拟机/seccomp**：没有进程隔离、没有文件系统覆盖、没有网络策略。`run()` 的权限和运行 Foreman 的人完全一样。
- **黑名单必然不完整**：足够刁钻或混淆的命令能绕过。把它当作抬高"意外自毁"门槛的手段，而**不是**对抗恶意模型的防御。
- **对不可信输入的建议**：如果你把 Foreman 对准你不信任的需求，或让模型完全无人监督地自选命令，请在容器（或一次性虚拟机）里跑，不给凭据、不给多余网络。

---

## 十一、常见问题

**Q：一定要 API key 吗？**
不。编排核心、smoke 测试、mock 模式、测试套件都不需要 key。只有真实运行需要 `DASHSCOPE_API_KEY`。

**Q：控制台报 "DASHSCOPE_API_KEY is not set"？**
在仓库根目录建 `.env`，写 `DASHSCOPE_API_KEY=sk-...`，重启 `serve.py`。或勾选 Demo mode 走 mock。

**Q：想换模型怎么办？**
CLI/环境：设 `FOREMAN_PLANNER_MODEL` / `FOREMAN_EXECUTOR_MODEL` / `FOREMAN_VERIFIER_MODEL`。控制台：New Run 表单里按角色选，或用 "custom…" 填任意模型名。

**Q：模型额度不够 / 被限流会整轮挂掉吗？**
不会。会按 `FOREMAN_FALLBACK_MODELS` 依次降级到备用模型。

**Q：改现有项目时提示仓库有未提交改动？**
先 `git commit` 或 `git stash`。确实想强行进行才用 `--force-dirty`（不推荐）。

**Q：提示有 merge/rebase 正在进行？**
先完成或中止那个 git 操作。`--force-dirty` 无法绕过这项检查。

**Q：提示锁文件被占用？**
同一仓库同时只能跑一个 Foreman。若是崩溃残留的陈旧锁，按提示删掉它指出的 `.git/foreman.lock`。

**Q：运行中断了，进度会丢吗？**
不会。用 `python main.py --resume <run_id>` 从账本接着跑，不重新规划。

**Q：mock 运行太快，录屏看不清？**
设 `FOREMAN_MOCK_DELAY=<秒>`（0–30），或请求体带 `mock_delay_s`，或 CLI 用 `--mock-delay`。

**Q：Foreman 会自动把结果合并进我的主分支吗？**
永远不会。它只在 `foreman/<run_id>` 分支上提交，合并与否完全由你决定。

**Q：一个任务反复失败会一直重试吗？**
不会。3 次尝试封顶后升级；连续失败的熔断器会把任务标为 `BLOCKED`，而不是无限重试。

---

## 十二、快速上手速查

```bash
# 0. 装依赖
pip install -r requirements.txt

# 1. 免费体验（无需 key）
python demo/smoke_run.py
python main.py --checklist demo/requirements_mini.md --mock --mock-delay 0.5

# 2. 真实运行（先在 .env 写好 DASHSCOPE_API_KEY）
python main.py --checklist demo/requirements_mini.md

# 3. 改你自己的 git 仓库
python main.py --checklist reqs.md --project-dir C:\path\to\repo

# 4. 中断后恢复
python main.py --resume run_xxxxxxxxxxxx

# 5. 网页控制台
python serve.py            # 打开 http://127.0.0.1:8787
python serve.py --no-browser   # 无头环境

# 6. 跑测试套件
python -m pytest -q
```

---

*本手册基于 Foreman 仓库中的 README.md、docs/EXISTING_PROJECTS.md、docs/SECURITY.md、main.py、serve.py 与 foreman/config.py 的实际代码与文档撰写，未添加任何不存在的功能。*
