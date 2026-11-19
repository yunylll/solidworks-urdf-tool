# SolidWorks URDF 2026 Tool

[English](README.md) | **简体中文** | [日本語](README.ja.md)

在后台调用 SolidWorks 2026，把装配体或零件导出为可直接使用、并经过独立校验的 ROS URDF 包。工具可以通过命令行、Python 或 MCP 使用，所有 CAD 操作都在隐藏的私有 SolidWorks 会话里、对源模型的副本进行；不需要旧插件向导，也不需要鼠标或 computer-use。

导出核心由 [ros/solidworks_urdf_exporter](https://github.com/ros/solidworks_urdf_exporter) 的源码构建（核心版本 2.0.0），针对无界面运行、SolidWorks 2026 API、错误处理、坐标系和大型装配体做了适配；运行时不加载安装目录中的旧 SW2URDF.dll。

## 目录

- [主要特性](#主要特性)
- [系统要求与安装](#系统要求与安装)
- [快速上手](#快速上手)
- [命令行参考](#命令行参考)
- [JSON 配置](#json-配置)
- [导出结果与校验](#导出结果与校验)
- [CAD 版本与上一次导出](#cad-版本与上一次导出)
- [大型装配体](#大型装配体)
- [任务运行与恢复](#任务运行与恢复)
- [发布与清理](#发布与清理)
- [源模型保护](#源模型保护)
- [Python](#python)
- [MCP](#mcp)
- [测试](#测试)
- [当前限制](#当前限制)
- [来源与许可证](#来源与许可证)

## 主要特性

| 方面 | 内容 |
|---|---|
| 导出 | 装配体与单零件（单零件为 1 个 Link、0 个关节）；fixed、continuous、revolute、prismatic 关节，以及无几何的坐标框架 Link；mimic 联动关节；world 根变换（Z 轴朝上、落地、居中）。 |
| 配置 | 读取旧插件保存在模型里的 Link/Joint 配置，或使用可编辑的 JSON 配置：Link 树、组件归属、坐标系、关节类型与方向、限位、阻尼与摩擦。 |
| 校验 | 质量、惯量、URDF 结构、零位时 Link 的相对位置、网格与 CAD 几何一致性、与上一次导出的差异；同时生成三视图预览图。 |
| 源模型保护 | 源文件只读并计算哈希；跨目录依赖做快照、改写引用并 Pack and Go；导出结束后核对哈希；恢复 SolidWorks 的 STL 设置。 |
| 任务 | 持久的任务记录、后台运行、分阶段进度、等待、取消、卡住检测、各阶段耗时、清理旧任务、发布到项目目录。 |
| 大型装配体 | 一次性选择组件、按零件文件计算质量属性、几何未变时复用网格；3250 个组件的整机首次导出约 24 分钟。 |
| 接口 | 命令行 `tool_cli.py`、Python `tool_service`、本地 stdio MCP 服务（10 个工具）。 |

验证记录见[工具报告](validation/TOOL_REPORT.zh-CN.md)；早期桥接方案的可行性测试见[早期报告](validation/FEASIBILITY.zh-CN.md)。

## 系统要求与安装

- Windows x64、SolidWorks 2026、.NET Framework 4.7.2 或更高版本（编译器 Roslyn 需要）。
- Python 环境由安装脚本在工作区内创建（`.venv`）。

首次安装（创建 Python 环境、安装固定版本的依赖、下载微软 Roslyn 编译器并编译工具）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
```

之后修改 C# 代码时只需重新编译：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\build-tool.ps1
```

编译使用本机 SolidWorks 2026 的 API DLL，以及 URDFExporter 安装目录中的 MathNet、CsvHelper、log4net；位置不同时可用 `-SolidWorksDir` 和 `-ExporterDir` 指定。没有安装旧插件的机器会沿用上一次编译留在 `build\bin` 中的这三个 DLL。

编译产物为 `build\bin\SolidWorksUrdf.exe`（导出核心）和 `build\bin\SolidWorksProbe.exe`（每次任务前使用的只读会话探针）。对上游源码的每一处适配都会核对匹配次数；上游变动导致补丁不匹配时，编译直接失败。核心接受内部请求 JSON，日常使用请通过下文的命令行、Python 或 MCP 接口。

## 快速上手

以下命令均在工作区根目录运行。

```powershell
# 1. 检查模型：列出组件，返回可编辑的配置（configuration_path）
.\.venv\Scripts\python.exe scripts\tool_cli.py inspect "C:\path\robot.SLDASM"

# 2. 编辑配置后，先只检查：跑到 Link 坐标系和关节即停，几分钟内暴露配置错误
.\.venv\Scripts\python.exe scripts\tool_cli.py check "C:\path\robot.SLDASM" --config "C:\path\robot-config.json"

# 3. 导出；大型模型在后台运行，再等待结果。--publish-to 可选：通过后把包复制到项目目录
.\.venv\Scripts\python.exe scripts\tool_cli.py start-export "C:\path\robot.SLDASM" --package robot_description --config "C:\path\robot-config.json" --publish-to "D:\robots\my_robot"
.\.venv\Scripts\python.exe scripts\tool_cli.py job <job_id> --wait 300
```

只有 `status=succeeded` 且 `passed=true` 才表示所有检查都通过。`queued`、`running`、`timed_out`、`interrupted`、`cancelled`，以及“已经生成了部分文件”，都不能当作成功。

## 命令行参考

用法：`.\.venv\Scripts\python.exe scripts\tool_cli.py <命令> [参数]`

| 命令 | 作用 |
|---|---|
| `inspect <模型>` | 列出组件和已保存的配置，返回可编辑的 `configuration_path` |
| `prepare <模型>` | 只生成自包含的 CAD 副本（`reusable_model`） |
| `check <模型> --config <配置>` | 只检查：跑到 Link 坐标系和关节即停，不计算惯量、不导出网格 |
| `export <模型> [--package 名称] [--config 配置]` | 导出并等待完成；不给 `--config` 时使用模型内保存的旧配置 |
| `start-export …` | 与 `export` 相同，但在后台运行并立即返回 `job_id`；`--check-only` 改为只检查 |
| `job <job_id> [--wait 秒]` | 查询任务；`--wait` 等到任务结束或超时即返回 |
| `cancel <job_id>` | 取消排队中或运行中的任务 |
| `validate-config <配置> [--model 模型]` | 不启动 SolidWorks 检查配置；给出 `--model` 时还会判断配置是否对应模型当前的 CAD 版本 |
| `validate-urdf <urdf> [--reference 参考] [--previews]` | 校验任意位置的 URDF 包；`--reference` 严格比较两次导出；`--previews` 生成三视图 |
| `cleanup-jobs …` | 列出（加 `--delete` 才删除）旧任务的 CAD 快照，见[发布与清理](#发布与清理) |

CAD 命令（inspect、prepare、check、export、start-export）的通用选项：

| 选项 | 默认 | 说明 |
|---|---|---|
| `--timeout` | 14400 | 总时长上限（秒） |
| `--stall-timeout` | 900 | 无进度、无 CPU 活动多少秒后判为卡住，见[任务运行与恢复](#任务运行与恢复) |
| `--use-saved-files` | 关 | 模型在 SolidWorks 中有未保存修改时，仍按磁盘上已保存的文件运行 |

check、export、start-export 另有：

| 选项 | 说明 |
|---|---|
| `--config` | JSON 配置文件 |
| `--accept-source-change` | 配置对应的 CAD 版本与当前不同时，仍按配置中的关节导出 |
| `--kinematics-tolerance-mm` / `--kinematics-tolerance-deg` | 比较组件位姿或重算关节时的容差，默认 0.1 mm / 0.1° |
| `--write-config-source` | 通过后把验证过的 CAD 版本（source 块）写回配置文件 |
| `--recompute-preflight` | 配置已过时时，先在单独会话里用当前 CAD 重算关节并比较 |

export、start-export 另有：

| 选项 | 说明 |
|---|---|
| `--package` | ROS 包名（小写字母、数字、下划线），默认 `robot_description` |
| `--reference` | 必须与之一致的参考 URDF，任何差异判为失败 |
| `--no-compare-previous` | 不与上一次成功的导出比较 |
| `--no-reuse-meshes` | 每个 Link 的网格都重新导出 |
| `--publish-to` | 通过后发布到该目录，见[发布与清理](#发布与清理) |

## JSON 配置

先运行 inspect，再编辑它返回的 `configuration_path`。可运行的示例见 [arm-config.json](examples/arm-config.json) 和[带限位与坐标框架的配置](examples/arm-custom-config.json)。

```json
{
  "schema_version": 1,
  "robot_name": "arm",
  "recompute_kinematics": false,
  "links": [
    {"name": "base_link", "parent": null, "components": ["base-1"],
     "coordinate_system": "Assembly Origin", "mesh_quality": "coarse", "frame_only": false, "joint": null},
    {"name": "arm_link", "parent": "base_link", "components": ["arm-1", "arm_asm-1/bracket-2"],
     "coordinate_system": "Automatically Generate", "mesh_quality": "coarse", "frame_only": false,
     "joint": {"name": "arm_joint", "type": "revolute", "xyz": [0, 0, 0.1], "rpy": [0, 0, 0], "axis": [0, 0, 1],
               "lower": -1.57, "upper": 1.57, "effort": 10, "velocity": 1}}
  ]
}
```

顶层字段：

| 字段 | 说明 |
|---|---|
| `schema_version` | 必须为 1 |
| `robot_name` | 机器人名 |
| `recompute_kinematics` | true：从 CAD 重算关节；false：使用 JSON 中的关节原点和轴（见下文） |
| `links` | Link 列表，恰好一个根 Link（`parent` 为 null） |
| `world` | 可选，在根 Link 之上加 world Link（见下文） |
| `exclude_hidden_bodies` | 可选，零件文件名列表，这些零件的隐藏实体不计入质量（见下文） |
| `source` | 由工具写入：配置验证过的 CAD 版本，见 [CAD 版本与上一次导出](#cad-版本与上一次导出) |

Link 字段：`name`、`parent`、`components`（检查返回的完整实例名，例如 `gantry_asm-1/gantry_x1_asm-1/gantry_x1_p03-2`）、`coordinate_system`、`mesh_quality`（`coarse` 或 `fine`）、`frame_only`、`joint`。关节字段：`name`、`type`、`axis_name`、`axis`、`xyz`、`rpy`、`lower`、`upper`、`effort`、`velocity`、`damping`、`friction`，以及可选的 `mimic`。

### 关节与坐标系

- **`recompute_kinematics=true`**：从配置指定的 CAD 坐标系和轴重算关节变换；坐标系或轴为 `"Automatically Generate"` 时，由原导出器根据配合推断。推断只看每个子 Link 的**第一个组件**在父 Link 固定后剩余的自由度，看不到柔性子装配体内部或经过其他 Link 的配合。推断出的关节类型与 JSON 中的 `type` 不一致时，导出直接失败并列出每个关节，不会静默生成 fixed 关节。
- **`recompute_kinematics=false`**：使用 JSON 中的局部 `xyz`、`rpy` 和 `axis`（`axis` 在子 Link 坐标系中）。`coordinate_system` 为 `"Automatically Generate"` 的 Link，工具按关节链在 CAD 副本中创建坐标系 `Origin_<关节名>`，网格和惯量都在该 Link 自己的坐标系中导出；指定了已有坐标系的 Link，其位置必须与 JSON 中的原点一致，否则报错并给出该坐标系对应的 `xyz`/`rpy`。continuous 和 revolute 关节的 `xyz` 应位于转轴上。
- **根 Link**：`coordinate_system` 为 `"Automatically Generate"` 时沿用原导出器的 `Origin_global`，它假定模型 Y 轴朝上，把 CAD 的 +Y 转成 URDF 的 +Z。按 Z 轴朝上建模的装配体请改用 `"Assembly Origin"`，直接使用装配体的原点和坐标轴。
- **限位**：revolute 和 prismatic 关节必须给出有限的 `lower`/`upper` 和正的 `effort`/`velocity`；角度用弧度，平移用米。
- **`frame_only=true`**：该 Link 不导出质量、视觉和碰撞几何，它的子树照常处理。
- 坐标系和参考轴应来自模型中已有的参考几何。工具不会凭空推断真实的关节意图或执行器参数。

### 质量与惯量

- 质量、质心和惯量取自 SolidWorks 的组件质量属性。原导出器把各实体的惯量直接相加，对由多个零件组成的 Link，结果比按导出网格算得的小 3–5 倍；它还按几何和密度计算质量，会丢掉 SolidWorks 中覆盖的质量。组件质量属性与网格一致，也包含覆盖值。
- 工具先确认：没有覆盖的 Link，两种算法的质量和质心一致；各 Link 质量之和等于装配体质量。两项都满足才替换原导出器的值，否则保留原值并在 `warnings` 中说明。用到覆盖值的 Link 和组件列在 `warnings` 和 `bridge.massOverrides` 中，原导出器的惯量保留在 `bridge.exporterInertia`。
- **`exclude_hidden_bodies`**（顶层）：零件文件名列表，例如 `["gantry_z1_p05.SLDPRT"]`。这些零件里的隐藏实体不计入质量和惯量，也不计入网格检查使用的 CAD 体积。适用于 CAD 仍需要的隐藏实体，例如被配合引用的镜像源实体，删掉它会使配合失效。SolidWorks 计算装配体质量时包含隐藏实体，因此各项质量检查会减去被排除的部分；`warnings` 和 `bridge.excludedHiddenBodies` 按 Link 列出排除的质量。文件名不属于任何组件时立即报错，并给出相近的文件名。必须由 SolidWorks 在装配体中称重的实例（见 `bridge.partMassCheck`）仍包含隐藏实体，并列为“未生效”。

### 导出后的处理

以下两项在导出器写出 URDF 之后由工具加入。导出器的原始文件保留为任务目录中的 `native.urdf`，改动记录在 `postprocess` 中。

- **`joint.mimic`**：`{"joint": "<跟随的关节>", "multiplier": 1, "offset": 0}`，生成 URDF 的 `<mimic>` 元素（例如对称夹爪的另一根手指）。两个关节都必须是活动关节，mimic 关节不能再跟随另一个 mimic 关节。
- **`world`**（顶层）：在根 Link 之上加一个根 Link（默认 `world`）和固定关节（默认 `world_to_base`）。`up_axis`（`+z`、`-z`、`+y`、`-y`、`+x`、`-x`）指定模型的哪根轴变成 URDF 的 +Z（Y 轴朝上的模型用 `+y`），也可以用 `rpy` 直接给出旋转；`ground: true` 让网格最低点落在 z=0，`center_xy: true` 让网格包围盒在 x=y=0 处居中，两者都按导出的网格、全部关节处于零位计算；`xyz` 在此基础上再加偏移。示例：`"world": {"up_axis": "+y", "ground": true, "center_xy": true}`。

### 配置检查

- 模型里没有保存的配置（或保存的配置引用了已不存在的组件）时，inspect 返回一份骨架配置（`configuration_source=template`）：所有顶层组件都归到同一个 `base_link`。它可以通过检查、按单个刚体导出，可在此基础上拆出活动 Link。
- 检查器会拒绝循环或断开的 Link 树、重复的组件归属、错误的轴长度、无效的限位和非有限数值，每条错误都写明 Link 或关节、期望值和实际值。
- inspect 结果中的 `unassigned_components` 列出未归属任何 Link 的实体零件，`overlapping_components` 列出既直接分配、又随父子装配体分配的组件；两者非空时 `configuration_ready=false`，导出会在生成网格前失败并列出组件名。
- 子装配体可以作为整体列在 Link 中，工具会导出并称量它下面所有未压缩、非封套的零件（包括隐藏零件，与质量计算一致）。结果在 `subassembly_components` 中列出这类组件并给出警告；`expanded_config_path` 是改为逐个列出零件的配置。`recompute_kinematics=true` 时，子 Link 中排在第一个的子装配体保持整体，因为关节是根据它识别的。
- JSON 配置覆盖上述 Link/Joint 字段；旧配置中的外观设置尚未开放到 JSON。

## 导出结果与校验

每次操作都会生成 `validation\jobs\<job_id>\`：

| 文件 | 内容 |
|---|---|
| `result.json` | 状态、`passed`、错误、警告、源文件哈希、各项校验、开始与结束时间、`duration_seconds` 和 `stage_durations` |
| `request.json`、`robot-config.json` | 任务请求和本次使用的配置 |
| `native.urdf` | 导出器写出的原始 URDF（导出后处理之前） |
| `output\<package>\` | URDF 包：`urdf\`、`meshes\`、`previews\`；最终 URDF 的绝对路径在 `bridge.urdf` |
| `output\cad\` | 自包含的 CAD 副本（`reusable_model`） |
| `mesh-keys.json` | 通过的导出中各 Link 网格的指纹，供后续导出复用 |
| `stdout.log`、`stderr.log` | 原生日志；`stderr.log` 带时间戳，按阶段记录 |

工具不会覆盖旧任务或旧输出。导出完成后会运行以下检查，任何一项失败都会使 `passed=false`：

| 检查 | 失败条件 |
|---|---|
| URDF 结构 | 不是一棵有根的树、关节或 Link 重名、轴不是单位向量、限位无效、数值非有限 |
| 质量与惯量 | 质量非正、惯量不满足物理约束；各 Link 质量之和与 CAD 装配体质量不一致 |
| 网格与 CAD 几何（`validation.cad_geometry`） | 列出的零件，或整体列出的子装配体下的零件，其 CAD 包围盒内没有网格顶点；网格体积不在 CAD 体积的 85–115% 之间（超出 95–105% 只给警告，通常是网格化误差） |
| 零位时的 Link 位置 | 子 Link 网格包围盒与父 Link 的距离超过机器人尺寸的 0.5%（至少 2 mm），可发现被 `"Automatically Generate"` 旋转的根坐标系之类的错误；各 Link 的世界包围盒见 `validation.link_world_bounds_m` |
| 与参考 URDF 比较 | 使用 `--reference` 时，任何数值差异；或网格与关节原点不一致，见下一节 |

网格检查的必要性在于：质量取自 CAD 组件而不是网格，STL 里少了零件，质量仍然是对的。检查失败时，消息会写明是哪个 Link、哪些零件可能缺失；体积对不上的原因如果是零件内部的隐藏实体（它们计入质量，但不进入 STL），消息会逐个列出实体名和体积。早期版本对嵌套在另一个子装配体里、又被作为整体列出的子装配体（例如工作站料架上的样品瓶）一个零件也不导出，用这种配置做过的导出应重新导出。

每次导出还会在包内 `previews\` 生成 `front.png`、`side.png`、`top.png`（分别从 +X、-Y、+Z 方向看，每个 Link 一种颜色），路径见 `previews.images`。使用导出结果前请先看一眼：绕父 Link 内部某点的旋转，仍可能让两个包围盒相接而不被发现。`validate-urdf --previews` 和 MCP 的 `validate_urdf(render_previews=true)` 可为任意 URDF 生成同样的图。

## CAD 版本与上一次导出

`recompute_kinematics=false` 的配置保存的是从某一版 CAD 抄下来的绝对关节原点。如果之后装配体被重新保存，模型相对装配体原点发生了移动，网格会跟着 CAD 移动，关节原点却不会，而新的 URDF 单独看仍能通过所有检查。以下两项检查用来发现这种情况。

**配置对应的 CAD 版本。** 带 JSON 配置的 check 或 export 通过后，工具记录它验证过的 CAD 版本：任务中的 `config_with_source_path` 是附带 `source` 块的配置，内容包括 `model_sha256`、`model_path`、`exported_at`、`job_id`、`operation`、`link_frames`（各 Link 坐标系在根 Link 坐标系中的位姿）和 `component_poses`（每个 Link 第一个组件在装配体中的位姿）。`--write-config-source` 会把该块写回配置文件本身；如果文件在任务期间被改动，则不写。

- 没有 `source` 块的配置，会按同一模型、使用相同关节原点的历史任务推断其来源（`config_source.origin=inferred_from_job`）。
- 该版本与模型当前的 SHA-256 不同时（可复用副本则比较其原始模型），`warnings` 中会给出提示并置 `config_stale=true`；`validate-config --model` 不启动 SolidWorks 也能做出同样的判断。
- 随后导出会在同一个会话里、在建立任何 Link 坐标系和网格之前，把每个 Link 第一个组件的位姿与记录值比较，只需几秒。平移或转动超过 `--kinematics-tolerance-mm` / `--kinematics-tolerance-deg` 时，任务以 `CONFIG_OUTDATED` 失败，消息逐个列出 Link 的新旧位置；若全部是同一个刚体平移，还会给出该向量（明细见 `component_pose_check`）。没有记录位姿的配置只给出警告，依靠下面的“上一次导出”比较兜底。
- `--recompute-preflight` 改为先在单独的只检查会话（`preflight\`）里用当前 CAD 重算 `"Automatically Generate"` 关节，比较原点和轴，以 `CONFIG_OUTDATED` 或 `KINEMATICS_PREFLIGHT_FAILED` 失败。
- `--accept-source-change` 跳过这些检查，按配置中的关节导出。

**上一次导出。** 除非给出 `--reference` 或 `--no-compare-previous`，导出都会与同一模型（否则同一包名）最近一次成功的导出比较（`previous_export`）。差异列在 `validation.reference_changes` 并汇总到 `warnings`，不判为失败。`validation.reference_motion` 给出每个网格（包围盒中心和质心）以及每个关节原点的位移，都在最上层带网格的 Link 坐标系中计算。

- 如果所有网格都平移了同一个向量，每个 revolute 或 continuous 关节的原点都必须跟着移动；沿自身转轴的滑移不影响运动，不计入（垂直于轴的残差见 `reference_motion.rotation_axis_offset_mm`）。任一转轴落后超过 0.5 mm，导出就以 `Mesh/joint-origin mismatch` 失败，消息给出该向量、每个没跟上的转轴及其偏离距离，以及已经跟上的转轴，所以只修正了部分转轴的配置仍会失败。
- 没有移动的 prismatic 和 fixed 关节原点只改变 Link 坐标系的位置，列为变化；加 `--accept-source-change` 时降为警告。
- 显式给出的 `--reference` 始终是严格比较，任何差异都判为失败。

## 大型装配体

以 lab_workstation 工作站（3250 个组件，19 个 Link）为例：

| 操作 | 耗时 |
|---|---|
| 首次完整导出 | 约 24 分钟（改进前约 57 分钟） |
| 网格全部复用的再次导出 | 约 17 分钟 |
| 只检查（`check`，`recompute_kinematics=true`，重算 19 个关节） | 约 8 分钟 |

- **只检查。** `check`（MCP：`check_only=true`）按导出流程运行到 Link 坐标系和关节就停止，不计算质量属性，也不导出 STL。结果中有 `resolved_joints`（每个关节的类型、原点和轴）、`link_frames`，`recompute_kinematics=true` 时还有 `detected_joint_types`。组件名写错、坐标系与关节原点不一致、关节类型识别不符等问题，几分钟内就能看到。
- **复用准备好的副本。** 运行过 Pack and Go 的 inspect、prepare、check 或 export 都会返回 `reusable_model`：一份自包含的 CAD 副本，附带文件哈希清单，导出失败后同样可用。下次把它作为模型路径传入，工具核对哈希、复制文件夹后直接打开，跳过只读打开原模型和 Pack and Go 两步。
- **复用网格。** 通过的导出会在 `mesh-keys.json` 中记录每个 Link 网格的指纹：所显示的每个零件文件的哈希和配置（导入件和虚拟件使用它们的几何数据）、零件在装配体中的位姿、网格质量、整个文档的尺寸（SolidWorks 据此决定粗/细网格的公差），以及该 Link 的 CAD 体积（装配体特征切除也会改变它）。之后的导出中，指纹相同的 Link 直接复用该 STL；只改了关节原点时，STL 被刚性移到新的 Link 坐标系中。复用的网格同样要经过网格与 CAD 的检查，列在 `reused_meshes` 中。`--no-reuse-meshes`（`reuse_meshes=false`）关闭复用。
- **选择组件。** SolidWorks 逐个追加选择组件时，耗时与已选数量成正比（800 个零件 194 秒）。导出时的显示/隐藏、关节重算时的固定/解除固定，现在都一次性选择（0.9 秒）。
- **质量属性。** 每个零件文件只在它自己的文档中计算一次质量属性（包含零件中的覆盖值和隐藏实体），再按每个实例在 Link 坐标系中的位姿旋转、平移，用平行轴定理精确合成。每个文件先用一个实例与 SolidWorks 在装配体中的称重结果比对（例如在装配体中给组件指定了材料时两者不同）；比对不一致的文件、有装配体级覆盖的子装配体，以及使用非激活配置的组件，仍由 SolidWorks 称重（见 `bridge.partMassCheck`）。质量覆盖通过读取每个文档的覆盖标志判断，只对有覆盖的组件再次称重。
- **进度与耗时。** `job` / `get_export_job` 返回 `progress`：当前阶段（如 `export_meshes`）、摘要（如 `Exporting STL meshes: 7/18 (gripper_z)`）、步数、已用时间、本阶段用时、SolidWorks 的 CPU 时间、距上次活动的 `idle_seconds`，以及最后几行日志。任务结束后，`result.json` 中有 `duration_seconds` 和 `stage_durations`（扣除电脑暂停的时间）。失败时 `error.stage` 写明出错的阶段。

## 任务运行与恢复

- **排队。** CAD 任务串行执行，以免多个导出相互覆盖全局 STL 设置。后到的任务保持 `queued`（`queue=waiting_for_cad_lock`），最多等待 3600 秒，超时返回 `CAD_BUSY`；不保证先到先得。
- **工作进程。** 执行进程会登记 PID 和启动时间；进程消失，或 120 秒内未登记的任务，由 `get_export_job` 标记为 `interrupted`。
- **卡住与超时。** 运行中的任务只有在“卡住”时才会被停止：工具进程及其 SolidWorks 进程在 `stall_timeout_seconds`（默认 900 秒）内既没有进度更新，也没有 CPU 时间增长。原导出器自己的日志（`bridge-export.log`）在增长，也算有活动；单个大 STL 等长步骤期间 SolidWorks 一直在计算，不算卡住。`timeout_seconds`（默认 14400 秒）只是总时长上限。卡住以 `error.code=CAD_STALLED`、超时以 `CAD_TIMEOUT` 结束，`status` 均为 `timed_out`，消息中写明所处阶段。
- **睡眠与待机。** 工作进程被暂停（睡眠、待机、机器卡死）后恢复时，暂停的时间既不计入空闲时间，也不计入总时长；它们记录在 `paused_seconds` 和 `pauses` 中。可选的重算预检是第二个 SolidWorks 会话，单独按 `--timeout` 计时。
- **取消。** `cancel` / `cancel_export_job` 可停止排队中或运行中的任务（`status=cancelled`）。
- **清理与恢复设置。** 无论任务以何种方式结束，工具都会核对 PID 和启动时间后关闭私有会话。私有会话与本机 SolidWorks 共用用户设置：原生核心启动后立即把 STL 相关设置保存到 `output\preferences-snapshot.json`；任务超时或失败、未确认设置已恢复时，工具另起私有会话恢复这些设置，结果写入 `preference_restore`（`changed_keys` 为实际改回的项）。工作进程意外退出的任务会被标记为待恢复，在下一个 CAD 任务开始前执行。
- `timed_out`、`interrupted`、`cancelled` 任务的输出不能用于正式模型。

## 发布与清理

**发布。** `--publish-to <目录>`（`publish_to`）只在导出 `status=succeeded` 且 `passed=true` 时生效，执行以下操作：

- 把 URDF 包复制到 `<目录>\urdf\<package>\`；
- 把任务的 `robot-config.json`（没有时用 `configuration.json`）、`request.json`、`result.json` 复制为 `<目录>\export\export-config.json`、`export-request.json`、`export-result.json`；
- 目标位置已有的文件或包目录，先移到 `<目录>\export\history\<时间戳>_job-<job_id 前 12 位>\`，绝不覆盖；
- 复制后逐个校验哈希；`result.json` 的 `publish` 字段记录路径、哈希和移入历史的旧文件。

发布失败不改变 `passed`，原因写在 `publish.error`。

**清理。** 一个整机任务约占 650 MB，几乎都是 `output\cad` 中的 CAD 快照。

```powershell
# 只列出（dry run）：30 天前的任务，或每个模型和包只保留最近 3 个
.\.venv\Scripts\python.exe scripts\tool_cli.py cleanup-jobs --older-than-days 30
.\.venv\Scripts\python.exe scripts\tool_cli.py cleanup-jobs --keep-latest 3 --delete
```

- 不加 `--delete`（MCP / Python：`dry_run=false`）时只列出将要删除的快照（`output\cad`、`preflight\output\cad`，加 `--include-logs` 时还有日志）和可释放的字节数，不删除任何文件。`--job` 可指定具体任务。
- 始终保留：`result.json`、`request.json`、各配置、`native*.json`、`mesh-keys.json` 和 URDF 包（后续导出会复用其中的网格）。
- 跳过：排队或运行中的任务；仍被保留任务用作 `model_path`、`reusable_model` 或源清单的快照；每个模型最近一次成功导出的快照（除非 `--no-keep-latest-success`）。
- 被清理的任务在 `result.json` 中记录 `cleaned_at` 和 `cleanup`，`reusable_model` 改为 `reusable_model_removed`；若保留的任务中有同一模型、同一 CAD 版本（`cad_version_sha256`）的快照，`equivalent_snapshot` 和 `reusable_model_hint` 会指向它。

## 源模型保护

- **只读源文件。** 源文件只被读取和计算哈希。工具先复制源模型和依赖，只对快照做引用改写、Pack and Go 和导出；即使 API 在打包时保存模型，保存的也只是快照。结束后再次核对全部源文件的哈希（`source_unchanged`）。
- **缺失的引用。** 模型保存的引用指向本机不存在的文件时（旧配置、导入源文件、库零件等），无需先在 SolidWorks 中打开模型：工具跳过这些路径建立快照，打开快照后检查每个未压缩组件都已加载。只有活动组件缺少文件时才失败，并列出组件名和路径；被跳过的引用和缺少文件的压缩组件写入 `warnings`。
- **已打开的模型。** 该装配体正在 SolidWorks 中打开时，工具读取只读的活动模型清单，要求快照的配置名、组件数和质量与之一致。打开的模型有未保存修改时，任务以 `UNSAVED_CHANGES` 失败：请保存，或关闭且不保存，然后重新运行；也可以使用 `--use-saved-files`，按磁盘上已保存的文件导出，`warnings` 会注明这一点。

## Python

```python
import sys
sys.path.insert(0, r"C:\path\to\solidworks-urdf-tool\scripts")
from tool_service import inspect_model, check_configuration, export_urdf, start_export, wait_job, cancel_job, cleanup_jobs

check = check_configuration(r"C:\path\robot.SLDASM", r"C:\path\robot-config.json")
result = export_urdf(check.get("reusable_model") or r"C:\path\robot.SLDASM", "robot_description",
                     config_path=r"C:\path\robot-config.json")
assert result["passed"], result.get("error")
print(result["bridge"]["urdf"])
```

包名之后的选项（`config_path`、`reference_urdf`、`timeout_seconds`、`stall_timeout_seconds`、`use_saved_files`、`accept_source_change`、`kinematics_tolerance_mm`、`kinematics_tolerance_deg`、`compare_previous`、`write_config_source`、`recompute_preflight`、`reuse_meshes`、`publish_to`）只能按关键字传入，以免两个路径参数被位置调换。

## MCP

服务脚本为 `scripts\mcp_server.py`，用工作区的 Python 启动。其他 stdio 客户端可参考 [mcp-connection.example.json](mcp-connection.example.json)，把 `<REPO_ROOT>` 换成本仓库路径；建议启动超时 60 秒、工具超时 1200 秒。不需要公网端口、OAuth 或 API key。在 Codex 中注册后，需要重新加载 MCP 配置（在设置的 MCP servers 中重启连接，或重启客户端后查看 `/mcp`），参见 [MCP 配置文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

| 工具 | 作用 |
|---|---|
| `inspect_solidworks` | 读取 SolidWorks 版本和活动文档，不做修改 |
| `inspect_model_configuration` | 检查模型，返回组件、可编辑配置和未归属组件 |
| `prepare_model` | 生成自包含的 CAD 副本 |
| `validate_configuration` | 不启动 SolidWorks 检查配置；可选 `model_path` 用于判断 CAD 版本 |
| `export_urdf` | 导出（或 `check_only=true` 只检查），等待结果 |
| `start_urdf_export` | 在后台开始导出，立即返回 `job_id` |
| `get_export_job` | 查询任务；`wait_seconds` 等到任务结束即返回 |
| `cancel_export_job` | 取消任务 |
| `validate_urdf` | 校验任意 URDF，可选 `reference_urdf` 和 `render_previews` |
| `cleanup_jobs` | 清理旧任务的快照，默认 `dry_run=true` |

`export_urdf` 和 `start_urdf_export` 接受与命令行相同的选项：`check_only`、`use_saved_files`、`timeout_seconds`、`stall_timeout_seconds`、`compare_previous`（默认 true）、`accept_source_change`、`kinematics_tolerance_mm`、`kinematics_tolerance_deg`（默认 0.1）、`write_config_source`、`recompute_preflight`、`reuse_meshes`（默认 true）和 `publish_to`。

大型模型请使用 `start_urdf_export`，再带 `wait_seconds`（例如 300）调用 `get_export_job`：调用会一直等到任务结束，一到终态立即返回，客户端无需 shell sleep 或频繁轮询。阻塞式工具（`export_urdf`、`inspect_model_configuration`、`prepare_model`）同样在后台任务中运行，最多等待 1000 秒（环境变量 `SW_URDF_SYNC_WAIT_SECONDS` 可调，应小于客户端的工具超时）；届时未完成会返回 `still_running=true` 和 `job_id`，任务继续执行。

## 测试

```powershell
.\.venv\Scripts\python.exe scripts\test_configuration.py
```

| 测试 | 需要 SolidWorks | 覆盖内容 |
|---|---|---|
| `test_configuration.py` | 否 | 配置校验、导出后处理字段、子装配体展开、`exclude_hidden_bodies` |
| `test_validation.py` | 否 | 校验器能否发现损坏的导出：缺失网格、单位错误、关节方向、零位位置、网格与 CAD 不一致、隐藏实体 |
| `test_postprocess.py` | 否 | mimic 和 world 后处理 |
| `test_jobs.py` | 否 | 排队、工作进程存活检测、卡住与暂停、取消、进度、读写 JSON 时的文件占用、网格复用候选、耗时记录 |
| `test_provenance.py` | 否 | 配置来源与 CAD 版本判断；在 `scripts\testdata\provenance-jobs` 的合成任务上运行，另可在 `private-fixture.json` 指定的真实任务上运行 |
| `test_maintenance.py` | 否 | 发布与清理 |
| `test_cad_regressions.py` | 是 | 含虚拟零件的 Pack and Go、共用原点的关节、只检查模式、复用副本、进度、mimic/world、写明错误组件名的报错 |
| `test_nested_subassemblies.py` | 是 | 两层深的整体子装配体导出完整、分块与整体质量属性一致、网格复用与移动；本机有工作站 CAD 时还测试料架和 `exclude_hidden_bodies`（路径可用 `SW_URDF_VIAL_RACK` 指定） |
| `test_tool_mcp.py`、`test_mcp.py` | 是 | MCP 工具端到端 |
| `test_registered_mcp.py`、`test_mcp_smoke.py` | 是 | 已注册的 MCP 服务；`test_mcp_smoke.py` 需要已打开的 SolidWorks 会话 |

## 当前限制

- 输出为传统 ROS URDF 包。ROS 2 启动文件、MJCF/USD、自动建模和全自动关节推断不在本版本范围内。导出的模型仍应在目标仿真环境中验证。
- 关节自动推断只看每个子 Link 的第一个组件（见[关节与坐标系](#关节与坐标系)）。
- 旧配置中的外观设置尚未开放到 JSON。

## 来源与许可证

- 上游源码快照：ros/solidworks_urdf_exporter `882169e28952f0d17c87d7eab98826454421aabf`（MIT）。`build\core-source-manifest.json` 记录每个上游文件的哈希和是否经过适配，`build\core-source` 中可以审查生成后的源码，适配规则在 `scripts\prepare-core.py`。
- `vendor` 中保留的 solidworks_urdf_exporter2 仅供参考，本工具不用它导出。上游许可证保留在 `vendor` 中，并随本地构建复制为 `SW2URDF-LICENSE.txt`。
- 本工具使用 MIT 许可证，见 [LICENSE](LICENSE)；上游许可证见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES)。
