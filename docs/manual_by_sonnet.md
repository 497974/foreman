# Foreman 使用说明手册

> 面向普通用户的中文手册。内容全部依据本仓库的 `README.md`、
> `docs/EXISTING_PROJECTS.md`、`docs/SECURITY.md`、`main.py`、`serve.py`、
> `foreman/config.py` 等实际代码与文档整理，不包含未验证过的功能描述。

---

## 1. 这是什么

Foreman 是一个"AI 工头"（工程监理）式的多智能体系统，用来解决一个常见痛点：

> 给编码 AI 一份 20 项的需求清单，它一小时后说"全部完成"，但实际上只做对了 3 项。

Foreman 的做法是：把需求清单拆解成一个个**可独立验证的小任务**，每个任务交给一个"干净上下文"的执行者（Executor）去做，做完之后由**独立的验证者（Verifier）**用真实的测试/命令去检查，验证者说通过才算通过——执行者自己不能给自己判"完成"。

系统由五种角色组成：

| 角色 | 使用模型（默认） | 干什么 |
|---|---|---|
| Planner（规划者） | `qwen-max` | 把需求文档拆成有依赖顺序的任务图，每个任务自带验收标准和可运行的测试策略 |
| Dispatcher（调度器） | 纯 Python，不调用 LLM | 任务依赖解析、原子认领、崩溃恢复、限速 |
| Executor（执行者） | `qwen3-coder-plus` 或 `qwen-plus` | 每次只拿到一个任务的"干净上下文"，通过读文件/写文件/列目录/跑命令的工具循环去实现 |
| Verifier（验证者） | `qwen-plus`（JSON 模式） | 先跑真实测试/命令这类"客观关卡"，再做 LLM 打分 |
| Arbiter（仲裁者）/ Replanner | `qwen-max` | 执行者对判定不服时的申诉仲裁；多次失败后升级重新规划 |

任务经过 `PENDING → READY → IN_PROGRESS → PENDING_REVIEW → DONE → ARCHIVED` 的状态机流转，`BLOCKED` 是升级失败状态。所有进度和日志都记录在一个 SQLite 账本（ledger）里，这也是 `--resume` 断点续跑的依据。

---

## 2. Foreman 能干什么

- **把一份 Markdown 需求清单，自动拆解 → 分派 → 执行 → 验证 → 落地代码**，全程可在网页控制台或命令行观察。
- **诚实的完成度判定**：验证者先跑真实的 `pytest` / 命令测试（"客观关卡"），不是单纯让 LLM 自己说"我做完了"。
- **失败重试与升级**：每个任务有重试上限（3 次），超过后自动升级为 `BLOCKED`（阻塞）状态，而不是无限重试。
- **执行者可以申诉**：如果验证者打回但所有客观关卡都已经通过，执行者可以发起一次"申诉"，由仲裁者（更高级别模型）读取实际证据文件后裁决"维持"或"推翻"原判。
- **中断后可续跑**（`--resume`）：账本是持久化的，进程重启/中断后可以从上次状态继续，不会重新规划、不会丢进度。
- **两种工作模式**：
  - **全新沙盒模式（默认）**：在一个隔离的临时工作目录里从零搭建项目。
  - **改现有项目模式（`--project-dir`）**：直接指向你电脑上一个真实的 git 仓库，在一个独立分支 `foreman/<run_id>` 上工作，每完成一个任务提交一次 commit。**主分支永远不会被动**，Foreman 也**永远不会自动合并或推送**——最终由你自己 review 和合并。
- **本地网页控制台**（`http://127.0.0.1:8787`）：
  - 每个角色（planner/executor/verifier）可以单独选模型，支持任意自定义模型名。
  - 可以同时开多个跑批（run），列表每 3 秒轮询一次状态。
  - 支持"停止"（在任务边界优雅停止，不会打断正在进行中的执行）和"续跑"。
  - 实时费用与 token 统计（估算的美元花费）。
  - 工作区打包下载、跑批归档。
  - 需求模板下拉框（读取 `demo/*.md`）。
  - 配置健康面板：显示 `DASHSCOPE_API_KEY` 是否已配置（只显示打码预览，密钥本身不会发到浏览器）。
- **零 API Key 的演示模式（`--mock` / Demo mode）**：用脚本化的假执行者/假验证者跑通完整流程，体验全部功能（并行跑批、停止续跑、费用统计、下载、归档），不消耗任何真实 API 额度，也不需要联网。
- **三条件评测脚本**（`scripts/evaluate.py`）：可以自己跑 A（单智能体一次性做完）/ B（顺序执行不验证）/ C（完整 Foreman 流程）三种条件的对比实验。

---

## 3. Foreman 不能干什么（诚实的限制）

请务必先读这一节，再决定怎么用它。

1. **默认只在隔离沙盒里干活**。除非你显式加 `--project-dir` 指向真实仓库，否则 Foreman 是在临时目录里"从零搭"一个项目，不会碰你电脑上其它任何文件。
2. **验证机制目前只认 pytest 风格的测试**。Verifier 假定 `test_strategy` 是 `pytest` 或 `python -c ...` 这类可直接运行的命令；`npm test`、`go test` 等其它语言生态的测试跑法目前不是一等公民（这是路线图里明确写着还没做的部分）。
3. **单机 SQLite 账本，不是分布式系统**。这是刻意的设计（本地双击即用的工具），不支持多节点协同。
4. **不是沙盒/容器/虚拟机**。`docs/SECURITY.md` 明确写了：
   - 有一个"文件操作监狱"（`Workspace._resolve`），`read_file`/`write_file`/`list_dir` 会拒绝越权路径（`..`、绝对路径、软链接逃逸）。
   - 有一个命令黑名单（`CommandPolicy`），拦截类似 `rm -rf` 绝对路径删除、`format`、`shutdown`、注册表操作等"整机灾难级"命令；在改现有项目模式下还额外拦截 `git push`、`git reset --hard`、`git clean -f` 等会破坏你代码或跑偏分支的 git 命令。
   - 但这**不是**容器隔离，没有网络策略，没有进程隔离。`run_command` 本质上是把真实的 shell 交给了模型（`shell=True`）。一个被提示注入（prompt injection）的模型，完全可以通过 `python -c "..."` 或装个恶意包之类的方式绕过黑名单去搞破坏——黑名单挡的是"写明了自毁"的命令，挡不住运行时才产生恶意行为的代码。
   - **官方建议**：如果你要喂给 Foreman 的需求来源不可信，或者想让模型完全自主选择命令、没有人在旁边看着，**请在容器或一次性虚拟机里跑，且不要给它任何真实凭据和不必要的网络权限**。Foreman 自带的这些防护是"本地开发工具该有的基本防线"，不是"面向恶意输入系统"该有的强度。
5. **不会自动合并/推送你的代码**。改现有项目模式下产生的所有提交都留在 `foreman/<run_id>` 分支上，是否合入主分支、要不要 cherry-pick、要不要删掉分支，永远是你自己来决定和操作。
6. **20 项完整需求清单的评测尚未跑完**（README 的 Status 里写着这是待办项，目前只有 5 项小清单的 A/B/C 对比数据），阿里云函数计算（Function Compute）部署也只写了步骤文档，还没有端到端跑通验证。
7. **不保证结果一次就对**。验证-重试-仲裁机制是为了提高完成质量，但重试有上限（3 次），超限任务会变成 `BLOCKED` 需要人工介入，而不是"包完美"。

---

## 4. 环境准备

### 4.1 前置条件

- 一台装好 Python 的电脑（Windows 已提供 `start_foreman.bat` 一键脚本）。
- 如果要跑真实模型（非 Demo/mock 模式），需要一个**阿里云百炼（DashScope）的 API Key**，Foreman 通过 DashScope 国际站的 OpenAI 兼容接口调用 Qwen 系列模型。
- 如果只想先体验一下流程、不想申请 Key：**直接用 `--mock` 或网页里的 "Demo mode"**，完全不需要 API Key，也不联网。

### 4.2 安装依赖

Foreman 的运行依赖非常轻量，写在 `requirements.txt` 里：

```
openai>=1.40.0      # 唯一的真实运行时依赖，用来调用 DashScope 的 OpenAI 兼容接口
pytest>=8.0.0        # 跑测试套件 / 客观验证关卡
```

手动安装：

```bash
python -m pip install -r requirements.txt
```

Windows 用户也可以直接双击 `start_foreman.bat`，它会自动帮你 `pip install -r requirements.txt`。

### 4.3 配置 `.env`（真实模型运行必需）

在 Foreman 项目根目录新建一个 `.env` 文件（纯文本，`KEY=VALUE` 格式，支持 `#` 开头注释），至少要写：

```
DASHSCOPE_API_KEY=sk-你的密钥
```

可选的其它配置项（都有默认值，一般不用改）：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DASHSCOPE_BASE_URL` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | DashScope 国际站 OpenAI 兼容接口地址 |
| `FOREMAN_PLANNER_MODEL` | `qwen-max` | 规划者用的模型 |
| `FOREMAN_EXECUTOR_MODEL` | `qwen3-coder-plus` | 执行者用的模型 |
| `FOREMAN_VERIFIER_MODEL` | `qwen-plus` | 验证者用的模型 |
| `FOREMAN_EXECUTOR_BACKEND` | `native` | 执行者后端（`native` 手写工具循环，或 `qwen-code` 委托给阿里的 qwen-code CLI） |
| `FOREMAN_FALLBACK_MODELS` | `qwen-turbo,qwen-flash,qwen3-coder-flash` | 主模型配额耗尽/持续 429 时按顺序尝试的兜底模型（逗号分隔） |
| `FOREMAN_COMMAND_POLICY` | 开启 | 设为 `off` 可关闭命令黑名单（**不建议**，见第 3 节安全限制） |
| `FOREMAN_MOCK_DELAY` | `0` | 让 mock 跑批的每一步人为放慢几秒，方便录屏演示（0–30 秒，超出会被截断） |

若某个模型的免费额度耗尽，只需要改 `.env` 里对应的 `FOREMAN_*_MODEL` 换一个模型名即可，不需要改代码——DashScope 的模型目录本身会变化，Foreman 就是设计成"可通过环境变量覆盖"的。

`.env` 里没有 Key 时，任何非 mock 的真实调用都会直接报错：
`DASHSCOPE_API_KEY not found. Put it in .env or the environment.`

### 4.4 需求清单怎么写

需求清单是一份普通的 Markdown 文件，`demo/` 目录下有现成范例可以参考/直接用：

- `demo/requirements_mini.md` — 5 项的小清单，跑得快，适合先体验一遍完整流程。
- `demo/requirements_full.md` — 更大的清单。
- `demo/requirements_backend_demo.md` — 后端场景范例。

自己写的时候，按条目列出需求即可（比如 "1. xxx  2. xxx"），Planner 会自动把它拆解成带依赖关系和验收标准的任务。

---

## 5. 命令行（CLI）用法

Foreman 的 CLI 入口是 `main.py`。

### 5.1 最简单的免费体验（不需要任何 API Key）

```bash
python demo/smoke_run.py                                    # 纯脚本化假流程，看一遍完整闭环
python main.py --checklist demo/requirements_mini.md --mock  # 通过正式 CLI 走一遍 mock 流程
```

可以加 `--mock-delay` 让每一步人为放慢，方便观察（仅在 `--mock` 下生效）：

```bash
python main.py --checklist demo/requirements_mini.md --mock --mock-delay 0.5
```

### 5.2 真实跑批（需要 `.env` 里的 `DASHSCOPE_API_KEY`）

```bash
python main.py --checklist demo/requirements_mini.md
```

也可以换成你自己写的需求文件路径：

```bash
python main.py --checklist path\to\your_requirements.md
```

### 5.3 常用参数一览

| 参数 | 说明 |
|---|---|
| `--checklist PATH` | 需求清单 Markdown 文件路径（与 `--resume` 二选一，必须指定其中一个） |
| `--resume RUN_ID` | 恢复一个已存在的跑批（见第 7 节），不会重新规划 |
| `--mock` | 用脚本化假执行者/假验证者，免 API Key、免联网 |
| `--mock-delay SECONDS` | 仅配合 `--mock` 使用，让每次假执行/验证人为延时，方便录屏 |
| `--run-root DIR` | 跑批产物存放的根目录，默认 `runs` |
| `--project-dir PATH` | 改现有项目模式：指向一个真实 git 仓库根目录（见第 6 节），只能跟 `--checklist` 一起用 |
| `--force-dirty` | 改现有项目模式下，允许仓库有未提交改动也继续跑（不推荐，见第 6 节） |

注意几条互斥规则（代码里会直接报错拦掉）：

- `--resume` 不能跟 `--project-dir` / `--force-dirty` 同时用——恢复时会自动从这次跑批自己的 `project_mode.json` 里读回项目路径和分支，不需要你重复指定。
- `--resume` 不支持 `--mock`（没有假账本可以恢复）。
- `--mock` 不能跟 `--project-dir` / `--force-dirty` 同时用。

### 5.4 跑批过程中会看到什么

终端会打印一个状态墙图例和每个任务的实时状态：

```
legend: [#]done [>]running [?]review [X]blocked [ ]ready [.]pending
```

跑完之后打印一份汇总，包括：`run_id`、产物目录 `run_dir`、完成/阻塞任务数、认领次数、耗时、每个任务的尝试次数，以及是否整体 `complete`。

---

## 6. 网页控制台（Web Console）用法

### 6.1 启动

Windows 下最简单的方式：双击 `start_foreman.bat`。它会依次做：

1. 检查当前目录下是否有 `.env`，没有就报错提示你先建（会同时打印中英文提示）。
2. `pip install -r requirements.txt` 装依赖。
3. 启动 `python serve.py`，控制台默认监听 `http://127.0.0.1:8787`，并自动打开浏览器（无浏览器环境下可加 `--no-browser`）。

也可以手动运行：

```bash
python serve.py            # 默认端口 8787，自动打开浏览器
python serve.py --no-browser   # 不自动开浏览器，适合无图形界面的环境
```

### 6.2 新建一个跑批（New Run）

在网页的 "New Run" 表单里可以配置：

- **需求清单**：直接粘贴文本，或从模板下拉框选一个 `demo/*.md` 现成模板。
- **每个角色的模型**：planner / executor / verifier 各自可选，下拉里有已知模型（`qwen-max`、`qwen-plus`、`qwen3-coder-plus`、`qwen-turbo`、`qwen-flash`），也可以选"custom…"手填任意 DashScope 模型名，不需要改代码。
- **Demo mode（勾选后即 mock 跑批）**：零 API Key 体验全部控制台功能。
- **Existing project folder（改现有项目模式，可选）**：见第 6.4 节。

提交后立刻返回 `run_id`，跑批在后台线程里执行，页面每 3 秒轮询一次列表，用状态点显示：绿色=完成、琥珀色=进行中、红色=有任务被阻塞、灰色=空闲；mock 跑批还会带一个 `MOCK` 徽章。

### 6.3 跑批过程中能做什么

- **实时事件流**：可以看到每个任务的状态变化，`DISPUTE`（申诉）/`ARBITRATION`（仲裁）事件会用琥珀色徽章标出来，方便你看清楚系统内部的"谈判"过程，而不是把它藏起来当成隐形重试。
- **停止（Stop）**：在任务边界优雅停止——已经在执行中的那次尝试会先跑完，不会被腰斩。
- **续跑（Resume）**：见第 7 节。
- **实时费用/token 统计**：跑批头部会显示类似 `≈$0.0123 · 45,678 tok` 的估算读数（基于 `foreman/pricing.py` 的粗略美元估算，仅供参考，不是账单）。
- **工作区打包下载**：把当前跑批的工作目录打包成 zip 下载下来看代码产物。
- **归档（Archive）**：把已完成的跑批从活跃列表移出（不会删除数据）。
- **配置健康面板（`/api/config`）**：显示 `DASHSCOPE_API_KEY` 是否已配置，只给一个打码预览（例如 `sk-abcde…yz`），完整密钥不会发到浏览器端。

### 6.4 网页里的改现有项目模式

在 "New Run" 表单里：

1. 填写 **"Existing project folder (optional)"**，填你本机一个真实 git 仓库的**绝对路径**（必须是仓库根目录，不能是子目录）。
2. 勾选 **"I understand this will create a git branch and modify real files in that folder"**——不勾选这个框，这个路径字段根本不会被发送到服务端。这是一个前端提醒性质的确认框，真正的安全检查是服务端的 `git_safety.ensure_ready`，跟 CLI 用的是同一套检查逻辑。
3. 如果仓库没准备好（比如有未提交的改动），页面会显示明确的错误横幅告诉你具体该怎么修（比如"仓库有未提交的改动——请先 commit 或 stash"）。
4. 跑批开始后，跑批头部会显示 `branch: foreman/<run_id>` 徽章，让你随时知道当前跑批写的是哪个分支。

详细规则见第 8 节。

### 6.5 配额不足时的友好提示

如果某个模型的免费额度耗尽（`insufficient_quota`），控制台会给出友好提示："模型免费额度已耗尽——请在 New Run 里换一个执行模型，或者去控制台加余额/优惠券"，而不是直接抛一堆看不懂的报错堆栈。

---

## 7. 中断恢复（`--resume`）

Foreman 的进度全部记在每次跑批自己的 SQLite 账本（`runs/<run_id>/ledger.db`）里，所以中断后可以直接续跑，**不会重新规划、不会丢进度**。

### 7.1 CLI 续跑

```bash
python main.py --resume run_xxxxxxxxxxxx
```

- `run_xxxxxxxxxxxx` 就是当初跑批开始时打印出来的 `run_id`。
- 恢复时会自动重新打开该跑批目录下的账本和工作区，不会再调用一次 Planner（不会重新拆解任务，已完成的任务不会被重做）。
- 如果这次跑批当初是"改现有项目模式"，续跑会**自动**从跑批目录下的 `project_mode.json` 里读回项目路径和分支，你不需要、也不能再手动传 `--project-dir`。

### 7.2 网页续跑

在控制台的跑批列表里，对一个已停止/中断的跑批点击 "Resume" 按钮即可，效果与 CLI 的 `--resume` 相同。

### 7.3 "停止"和"中断"的区别

- 网页里的 **Stop** 是"优雅停止"：会在当前任务做完这一次尝试后，在任务边界处停下来，不会打断正在进行中的执行。
- 如果是进程被强制杀掉、断电、崩溃这类"硬中断"：账本仍然是持久化的，`--resume` 同样可以继续；进行中但没提交完的任务会依赖调度器的 TTL 租约机制被重新认领，不会永远卡在"进行中"状态。

---

## 8. 改现有项目模式（`--project-dir`）详解

这是 Foreman 比较特殊、也比较需要谨慎对待的功能：让执行者直接操作你**真实的代码仓库**，而不是一个用完即扔的沙盒目录。

### 8.1 前提条件（不满足会直接拒绝执行）

- 目标文件夹必须**已经是一个 git 仓库**（不是的话先 `git init`）。
- 必须指向仓库的**根目录**，不能是子目录（防止只提交半个仓库）。
- 开始前工作树应该是**干净的**（`git status --porcelain` 输出为空）。默认情况下有未提交改动会被直接拒绝——除非你加 `--force-dirty`（见下）。
- **不能有正在进行中的 merge/rebase/cherry-pick**。这条检查**没有** `--force-dirty` 逃生舱可以绕过，因为在冲突合并上开分支会把冲突标记原样提交进去，还会把 git 的合并状态弄得更乱——遇到这种情况请先自己完成或放弃那次操作。
- **同一个仓库同一时间只能跑一个 Foreman 跑批**：靠 `.git/foreman.lock` 锁文件来串行化。如果第二个跑批撞上了，报错信息会直接告诉你是哪个跑批占着锁；如果是之前崩溃残留的死锁文件，报错也会明确告诉你该删哪个文件。

这些检查都在服务端（`foreman/git_safety.py`）强制执行，不是只做个样子的前端校验。

### 8.2 具体怎么用

CLI：

```bash
python main.py --checklist reqs.md --project-dir C:\path\to\repo
```

网页：在 "New Run" 表单里填路径 + 勾选确认框（见 6.4 节）。

### 8.3 会在你的仓库里发生什么

1. Foreman 会创建一个专属的隔离分支 **`foreman/<run_id>`**，你当前所在的分支不会被动。如果这个分支名恰好已经存在（比如之前某次跑批异常残留），Foreman 会直接拒绝，不会在别人的历史上继续提交——只有对同一个跑批做 `--resume` 才会复用它自己创建的分支。
2. Planner 会拿到你仓库的一个简要快照（目录树 + README/package.json/requirements.txt 等文件的预览），据此规划出"尊重你现有项目结构"的任务，而不是重新造轮子——但这只用于规划阶段的参考，Executor 真正执行时读的是磁盘上的真实文件。
3. 每个通过验证的任务（包括申诉后被推翻改判通过的）都会在 `foreman/<run_id>` 分支上产生**独立的一次提交**，提交信息格式是 `Foreman: <task_id> <task title>`。如果某个任务没有实际改动任何文件，就不会产生空提交。如果提交本身在主机层面失败了（磁盘满、权限问题），任务已经拿到的 DONE 判定**不会被撤销**——而是会记一条 `checkpoint_failed` 事件；如果你在控制台/事件流里看到这个，需要手动检查一下 `git log`/`git status`。
4. Foreman 自己的账本、事件日志、跑批配置仍然存在 `runs/<run_id>/` 目录下，**不会污染你的仓库**，进你仓库的只有真正的代码改动。
5. **Foreman 绝不会自动合并、rebase 或 push 任何东西**。跑批结束后（或跑批过程中你想看进度），自己来 review：

   ```bash
   git log foreman/run_xxxxxxxxxxxx
   git diff main...foreman/run_xxxxxxxxxxxx
   ```

   要不要合并这个分支、要不要 cherry-pick、要不要删掉它，永远是你自己的决定和操作。

### 8.4 `--force-dirty` 逃生舱（不推荐）

如果加了 `--force-dirty`（CLI）或网页里传了 `"force_dirty": true`，Foreman 会允许在有未提交改动的仓库上继续跑，但会先把你原有的未提交改动当作**独立的一次"快照提交"**立刻提交到 foreman 分支起点，提交信息明确写着"这是跑批开始前就存在、不是 Foreman 写的改动"，绝不会和后续任务的提交混在一起，这样 `git log`/`git blame` 就不会把你自己的代码错误地归到 AI 生成的任务头上。

即便如此，这依然意味着你未经审查的、未提交的工作会被提交到 foreman 分支上——**如果你有任何疑虑，请先自己 commit 或 stash**。仓库根目录检查、必须是 git 仓库检查、合并中检查这三条，没有任何"信我一次"式的绕过参数，是刻意设计成不可绕过的。

---

## 9. 如何查看和合并结果

### 9.1 全新沙盒模式的产物

产物都在 `runs/<run_id>/` 目录下（`--run-root` 可以改这个根目录），至少包含：

- `ledger.db`：SQLite 账本，任务状态和完整的尝试审计记录（`attempts` 表是只增不改的，每次尝试一行，带完整的 handoff JSON 和判决文字）。
- `workspace/`：执行者实际写代码的工作目录——也就是你要看的"产出代码"所在地。
- `events.jsonl`：事件流日志。
- `config.json`：跑批配置、模型选择、最终 token/费用统计（`usage_final`/`est_usd`）。

网页控制台里可以直接把某次跑批的 `workspace/` 打包下载成 zip。

### 9.2 改现有项目模式的产物

代码改动**不在** `runs/` 目录里，而是直接体现在你指定的那个 git 仓库的 `foreman/<run_id>` 分支上，一个任务一个 commit。查看和合并的方式见 8.3 节：

```bash
git log foreman/run_xxxxxxxxxxxx           # 看这次跑批做了哪些提交
git diff main...foreman/run_xxxxxxxxxxxx   # 看跟主分支的完整差异
```

确认没问题后，用你平时的 git 工作流去合并（`git merge`、`git rebase`、`git cherry-pick` 都行），Foreman 不会替你做这一步。

### 9.3 评测结果

如果你跑了 `scripts/evaluate.py` 三条件对比评测，结果会输出到你指定的 `--out` 目录（README 示例里是 `evals/`），可以直接打开 JSON 结果文件查看每个条件的裁判通过数、耗时、尝试次数、token 消耗。

---

## 10. 常见问题（FAQ）

**Q: 我没有 API Key，能先体验一下吗？**
可以。用 `python main.py --checklist demo/requirements_mini.md --mock`，或者在网页控制台的 New Run 表单里勾选 "Demo mode"。全程不联网、不花钱、不需要 Key。

**Q: 报错 "DASHSCOPE_API_KEY not found"？**
说明你没加 `--mock`，但项目根目录下没有 `.env` 文件或者 `.env` 里没写 `DASHSCOPE_API_KEY`。按第 4.3 节配置好即可。

**Q: 某个模型的免费额度用完了怎么办？**
不用改代码，改 `.env` 里对应角色的环境变量换个模型即可，比如把 `FOREMAN_EXECUTOR_MODEL` 换成额度充足的模型；也可以配置 `FOREMAN_FALLBACK_MODELS` 让系统自动按顺序尝试兜底模型。网页控制台的配额报错也会直接提示你去 New Run 换模型。

**Q: 任务一直卡在 `BLOCKED` 怎么办？**
这是 Foreman 故意设计的"熔断"行为：一个任务连续失败达到重试上限后，不会无限重试，而是升级为阻塞状态等待人工介入。可以查看事件日志/申诉记录了解具体卡在哪个验收标准上，然后考虑手动修正需求描述或代码后重新处理。

**Q: `--project-dir` 报错说仓库有未提交改动？**
先在你的仓库里 `git status` 看一下，`git add` + `git commit`（或者 `git stash`）清干净之后再跑；如果确实想带着未提交改动跑，可以加 `--force-dirty`，但要理解这会把这些改动作为一次"快照提交"提交到 foreman 分支（见 8.4 节）。

**Q: `--project-dir` 报错说仓库正在 merge/rebase 中？**
这一条没有绕过参数。先手动 `git merge --abort` / `git rebase --abort`，或者干脆完成这次合并/变基，再重新跑 Foreman。

**Q: 两个人（或两次）同时对同一个仓库跑 Foreman 会怎样？**
第二次会被 `.git/foreman.lock` 锁文件挡住，报错会告诉你是哪次跑批占用的；如果是之前异常退出留下的死锁，报错也会告诉你该删哪个锁文件。

**Q: 我担心模型在改现有项目模式下把我的仓库搞坏，安全吗？**
Foreman 有文件路径越权防护和命令黑名单（拦截 `rm -rf` 绝对路径、`git push`、`git reset --hard`、`git clean -f` 等），主分支永远不会被直接修改，也永远不会自动合并/推送。但这**不是**容器级隔离——一个被提示注入的模型理论上仍可能通过运行时生成的代码绕过黑名单做坏事。如果你要跑的需求来源不可信，建议在容器或一次性虚拟机里跑，且不要给它任何真实凭据、限制不必要的网络访问。详见第 3 节和 `docs/SECURITY.md`。

**Q: 网页控制台起不来 / 打不开？**
确认 `.env` 文件存在（`start_foreman.bat` 会先检查这个），确认依赖装好了（`pip install -r requirements.txt`），确认 8787 端口没被占用。无图形界面环境可以加 `python serve.py --no-browser`。

**Q: 中断后重新跑一样的需求，会不会从头再来一遍？**
只要用 `--resume run_xxxxxxxxxxxx`（或网页的 Resume 按钮），就不会重新规划、也不会重跑已经 DONE 的任务，会从账本记录的状态继续。如果是重新执行一遍 `--checklist`（不带 `--resume`），那才是全新的一次跑批，会重新规划。

**Q: 网页显示的费用是准确账单吗？**
不是，是基于 `foreman/pricing.py` 里价格表做的粗略估算（`est_usd`），仅供参考，实际扣费以阿里云百炼控制台账单为准。

---

## 11. 一页速查

```bash
# 零 Key 体验
python demo/smoke_run.py
python main.py --checklist demo/requirements_mini.md --mock

# 真实跑批（先在 .env 配好 DASHSCOPE_API_KEY）
python main.py --checklist demo/requirements_mini.md

# 网页控制台（Windows 一键）
start_foreman.bat
# 或
python serve.py

# 改现有项目模式
python main.py --checklist reqs.md --project-dir C:\path\to\repo

# 中断续跑
python main.py --resume run_xxxxxxxxxxxx

# 查看改现有项目模式的产出
git log foreman/run_xxxxxxxxxxxx
git diff main...foreman/run_xxxxxxxxxxxx

# 三条件评测
python scripts/evaluate.py --checklist demo/requirements_mini.md --conditions ABC --out evals/
```
