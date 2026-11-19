# SolidWorks URDF 2026 Tool

[English](README.md) | **简体中文** | [日本語](README.ja.md)

本地工具，支持通过命令行、Python 或 MCP 调用 SolidWorks 2026，检查模型配置、生成隔离 CAD 副本并导出 URDF/STL。所有模型操作在独立 SolidWorks 进程内进行，无需 computer-use、鼠标或旧插件向导。

导出核心由原始 [ros/solidworks_urdf_exporter](https://github.com/ros/solidworks_urdf_exporter) 的源码构建，并针对无界面调用、2026 API、错误处理和坐标框架做了适配。当前核心版本为 2.0.0；运行工具不再加载安装目录中的旧 SW2URDF.dll。

## 已具备的能力

- SolidWorks 2026 原生 COM，x64、STA，按 PID 连接独立会话。
- 装配体与单零件导出；单零件生成 1 个 Link、0 个关节。
- 读取旧插件保存的 Link/Joint 配置，并生成可编辑 JSON。
- 用 JSON 配置 Link 树、组件归属、坐标系、关节类型/方向、限位及阻尼/摩擦。
- 支持 fixed、continuous、revolute、prismatic，以及无几何的固定坐标框架。
- 跨目录 CAD 依赖快照、引用重写和 Pack and Go；重名依赖分配独立文件名。
- 真实文件哈希审计、质量/惯量/网格校验、STL 设置恢复、独立进程清理。
- 持久任务记录、异步导出、带阶段和步数的进度查询、等待、取消、卡住检测和明确的失败返回。
- “只检查不导出”模式（跑到关节和 Link 坐标系即停）和复用已准备好的 CAD 副本，便于反复调整配置。
- JSON 配置支持 mimic 联动关节和 world 根坐标变换（Z 轴朝上、落地、居中），一次导出即得可用 URDF。
- 本地 stdio MCP 的 9 个工具；已注册到本机 Codex，服务名 solidworks_urdf_2026。

验证记录见 [工具报告](validation/TOOL_REPORT.zh-CN.md)；旧桥接可行性测试保留在 [早期报告](validation/FEASIBILITY.zh-CN.md)。

## 准备环境

本机需要 Windows x64、SolidWorks 2026 和 .NET Framework。当前 Python 环境与编译结果已准备好。

重新准备可执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
```

setup.ps1 创建工作区 Python 环境、安装固定依赖、下载微软 Roslyn 编译器并编译工具。源码构建使用本机 SolidWorks 2026 API DLL，以及安装目录 URDFExporter 下的 MathNet/CsvHelper/log4net 依赖；需要改位置时，使用 build-tool.ps1 的 -SolidWorksDir 与 -ExporterDir 参数。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\build-tool.ps1
```

产物是 build/bin/SolidWorksUrdf.exe 和每次任务前使用的只读会话探针 build/bin/SolidWorksProbe.exe。上游源码适配补丁每处都校验匹配次数，上游变动导致补丁失配时构建直接失败。它接受请求 JSON；常规使用通过下面的 Python/CLI/MCP 接口，避免手工维护内部任务请求。

## 命令行

从工作区根目录运行：

```powershell
# 检查组件与已有配置，结果中返回 configuration_path
.\.venv\Scripts\python.exe scripts\tool_cli.py inspect "C:\path\robot.SLDASM"

# 使用模型内保存的旧配置导出
.\.venv\Scripts\python.exe scripts\tool_cli.py export "C:\path\robot.SLDASM" --package robot_description

# 使用 JSON 配置导出
.\.venv\Scripts\python.exe scripts\tool_cli.py export "C:\path\robot.SLDASM" --package robot_description --config "C:\path\robot-config.json"

# 单零件
.\.venv\Scripts\python.exe scripts\tool_cli.py export "C:\path\part.SLDPRT" --package part_description

# 只检查 JSON 配置：跑到关节和 Link 坐标系即停，不算惯量、不导出网格
.\.venv\Scripts\python.exe scripts\tool_cli.py check "C:\path\robot.SLDASM" --config "C:\path\robot-config.json"

# 为大型模型后台启动任务，再等待它（完成即返回）或取消它
.\.venv\Scripts\python.exe scripts\tool_cli.py start-export "C:\path\robot.SLDASM" --package robot_description --config "C:\path\robot-config.json"
.\.venv\Scripts\python.exe scripts\tool_cli.py job "返回的job_id" --wait 300
.\.venv\Scripts\python.exe scripts\tool_cli.py cancel "返回的job_id"

# 仅生成自包含 CAD 副本
.\.venv\Scripts\python.exe scripts\tool_cli.py prepare "C:\path\robot.SLDASM"

# 不启动 SolidWorks 的配置检查
.\.venv\Scripts\python.exe scripts\tool_cli.py validate-config examples\arm-custom-config.json

# 同时报告该配置是否对应模型当前的 CAD 版本（只读文件）
.\.venv\Scripts\python.exe scripts\tool_cli.py validate-config "C:\path\robot-config.json" --model "C:\path\robot.SLDASM"

# 校验本机任意位置的 URDF 包，例如拷到项目里的
.\.venv\Scripts\python.exe scripts\tool_cli.py validate-urdf "C:\project\robot_description\urdf\robot_description.urdf"

# 比较两次导出：数值差异判为失败，reference_motion 给出每个网格和关节原点的位移
.\.venv\Scripts\python.exe scripts\tool_cli.py validate-urdf "C:\new\urdf\robot.urdf" --reference "C:\old\urdf\robot.urdf"
```

CAD 命令都接受 --timeout（总时长上限，默认 14400 秒）、--stall-timeout（默认 900 秒，见下文）和 --use-saved-files。check、export 和 start-export 还接受 --accept-source-change、--kinematics-tolerance-mm、--kinematics-tolerance-deg、--write-config-source 和 --recompute-preflight，export/start-export 另接受 --no-compare-previous（见下文“CAD 版本与上一次导出”）、--no-reuse-meshes（见下文“复用网格”）和 --publish-to（见下文“发布与清理任务”）。

每次操作生成 validation/jobs/<job_id>/。result.json 记录状态、原文件 SHA-256、设置恢复、物理校验和错误；stdout.log/stderr.log 保留原生日志。status=succeeded 且 passed=true 才表示该操作所有检查通过。queued/running 和“已生成部分文件”均不能视为成功。

导出校验还会把每个 Link 放到零位，若子 Link 的网格包围盒与父 Link 的相距超过机器人尺寸的 0.5%（至少 2 mm）即判为失败，可拦住被 "Automatically Generate" 旋转的根坐标系等坐标系偏移；`validation.link_world_bounds_m` 列出各 Link 的世界包围盒。每次导出还会在包内 `previews/` 生成 `front.png`、`side.png`、`top.png`（分别从 +X、-Y、+Z 看，每个 Link 一种颜色），路径见 result.json 的 `previews.images`；使用导出前请先看一眼，因为绕父 Link 内部某点的旋转仍可能让包围盒相接。`validate-urdf --previews` 和 `validate_urdf(render_previews=true)` 可为任意 URDF 生成同样的图。

质量取自 CAD 组件而不是网格，STL 里少了零件，质量仍然是对的。因此每次导出还会把每个 Link 的网格与其 CAD 几何比较（`validation.cad_geometry`）：列出的每个零件、以及作为整体列出的子装配体下的每个零件，其 CAD 包围盒内都必须有网格顶点；网格体积须在 CAD 体积的 85–115% 之间（超出 95–105% 给警告，通常是网格化误差）。否则导出失败，并写明是哪个 Link、哪些零件可能缺失。作为整体列出的子装配体按其全部未压缩、非封套的零件导出（与质量一致，隐藏的零件也算）。旧版本对嵌套在另一个子装配体里的子装配体（如工作站中料架上的样品瓶）一个零件也不导出；用这种配置做过的导出应重做。结果在 `subassembly_components` 中列出这类组件并给出警告，`expanded_config_path` 是改为逐个列出其零件的配置（`recompute_kinematics` 为 true 时，子 Link 中排在第一个的子装配体保持整体，因为关节是根据它识别的）。

目录中可能同时存在源快照、准备后的 CAD 和导出包；最终 URDF 的绝对路径在 bridge.urdf 中。默认不覆盖旧任务或旧输出。

### 加快排查

- **只检查不导出。** check（MCP：check_only=true）按导出流程跑到 Link 坐标系和关节就停，不算惯量和质量属性，也不导出 STL。结果给出 resolved_joints（每个关节的类型、原点和轴）、link_frames，recompute_kinematics=true 时还有 detected_joint_types。组件名写错、坐标系与关节原点不一致、关节类型识别不符等配置问题，几分钟内就能看到，不必等完整导出。
- **复用准备好的副本。** 每次跑过 Pack and Go 的 inspect、prepare、check 或 export 都会返回 reusable_model：一份自包含的 CAD 副本，附带文件哈希清单；只要副本没被改动，导出失败后同样可用。下次把它作为模型路径传入：工具核对哈希、复制文件夹后直接打开，跳过只读打开原模型和 Pack and Go。
- **进度。** job / get_export_job 返回 progress：当前阶段（如 export_meshes）、摘要（如 “Exporting STL meshes: 7/18 (gripper_z)”）、步数、已用时间、本阶段用时、SolidWorks CPU 时间、距上次有活动的 idle_seconds，以及最后几行日志。stderr.log 带时间戳。
- 失败时 error.stage 写明出错的阶段。
- **复用网格。** 导出通过后，任务目录里的 mesh-keys.json 记下每个 Link 网格的几何指纹：显示出的每个零件文件的哈希和配置、它在装配体中的位姿、网格质量、整个文档的尺寸（SolidWorks 据此决定粗/细网格的公差）以及该 Link 的 CAD 体积（装配体特征切除也会改变它）。以后的导出中指纹相同的 Link 直接复用那个 STL，只改了关节原点时把它刚性移到新的 Link 坐标系，不再显示组件、重新网格化；复用的网格同样经过与 CAD 的几何一致性检查，列在 reused_meshes 中。--no-reuse-meshes（reuse_meshes=false）关闭复用。
- **大装配体的耗时。** SolidWorks 逐个追加选择组件的耗时与已选数量成正比（800 个零件 194 秒），导出、关节重算的固定/解除固定和显示/隐藏现在一次性选择（0.9 秒）。读取组件质量属性的耗时同样随组件数增长很快（800 个零件 106 秒，1434 个相同吸头每 100 个 25 秒），现在每个零件文件只在它自己的文档里读一次质量属性（含零件中的覆盖值和隐藏实体），按每个实例在 Link 坐标系中的位姿转动、平移后用平行轴定理精确合并；每个文件先用一个实例与 SolidWorks 在装配体中称得的结果比对（例如在装配体中给组件指定了材料时两者不同），不一致的文件、装配体级覆盖和未激活配置的组件仍由 SolidWorks 称重（结果中的 bridge.partMassCheck）。质量覆盖改为每个文档读一次覆盖标志，只对有覆盖的组件再称重。

### 发布与清理任务\n
- **发布到机器人孪生目录。** --publish-to <目录>（publish_to）只在导出 status=succeeded 且 passed=true 时，把 URDF 包复制到 <目录>\urdf\<package>\，把任务的 robot-config.json（没有则用 configuration.json）、request.json、result.json 复制为 <目录>\export\export-config.json、export-request.json、export-result.json。目标已有的文件或包目录先移到 <目录>\export\history\<时间戳>_job-<job_id 前 12 位>\，绝不覆盖。复制后逐个校验哈希，result.json 的 publish 字段记录路径、哈希和移入历史的旧文件。发布失败不改变 passed，原因写在 publish.error。
- **清理旧任务。** 一个整机任务约占 650 MB，几乎都是 output\cad 里的 CAD 快照。cleanup-jobs --older-than-days 30 或 --keep-latest 3（按模型和包分组；MCP 工具 cleanup_jobs，Python cleanup_jobs）列出已结束旧任务的快照（output\cad、preflight\output\cad，加 --include-logs 时还有日志）及可释放的字节数；不加 --delete（dry_run=false）什么都不删。result.json、request.json、各配置、native*.json、mesh-keys.json 和 URDF 包（以后的导出会复用其中的网格）始终保留。排队或运行中的任务、仍被保留任务用作 model_path、reusable_model 或源清单的快照，以及每个模型最近一次成功导出的快照（除非 --no-keep-latest-success）会跳过。清理后的任务在 result.json 记录 cleaned_at 和 cleanup，reusable_model 改为 reusable_model_removed；若保留任务中有同一模型、同一 CAD 版本（cad_version_sha256）的快照，equivalent_snapshot 和 reusable_model_hint 指向它。\n
### CAD 版本与上一次导出

recompute_kinematics=false 的配置保存的是从某一版 CAD 抄来的绝对关节原点。如果之后装配体被重新保存、模型相对装配体原点发生了移动，网格会跟着 CAD 走，关节原点却不会，而新 URDF 单独看仍能通过所有检查。下面两项检查用来发现这种情况：

- **配置对应的 CAD 版本。** 带 JSON 配置的 check 或 export 通过后，会记录它验证过的 CAD 版本：任务里的 config_with_source_path 是附带 source 块的配置（model_sha256、model_path、exported_at、job_id、operation、link_frames：各 Link 坐标系在根 Link 坐标系中的位姿，以及 component_poses：每个 Link 第一个组件在装配体中的位姿）；--write-config-source（MCP/Python：write_config_source=true）会把该块写回 --config 文件本身，若文件在任务期间被改动则不写。没有 source 块的配置，会按同一模型、使用相同关节原点的历史任务推断其来源（config_source.origin=inferred_from_job；若那次任务记录了组件位姿，也一并使用）。当该版本与模型当前的 SHA-256 不同（可复用副本则比较其原始模型）时，结果的 warnings 中会给出警告并置 config_stale=true；validate-config --model 不启动 SolidWorks 也能给出同样的判断。随后导出会在同一个 SolidWorks 会话里、解析完配置之后、建立任何 Link 坐标系和网格之前，把每个 Link 第一个组件的位姿与记录值比较，只需几秒。任一组件的平移或转动超过 --kinematics-tolerance-mm / --kinematics-tolerance-deg（默认 0.1 mm / 0.1°），任务以 CONFIG_OUTDATED 失败；消息逐个列出 Link 的旧位置和新位置，若全部是同一个刚体平移，还会给出这个向量（明细在 component_pose_check）。没有记录位姿的配置（此检查之前的配置）只给出警告，依靠下面的“与上一次导出比较”兜底。--recompute-preflight（recompute_preflight=true）则改为先在单独的仅检查会话（preflight/）里用当前 CAD 重算 "Automatically Generate" 关节，比较原点和轴，以 CONFIG_OUTDATED 或 KINEMATICS_PREFLIGHT_FAILED 失败；对几百个零件的装配体，所需的约束分析要几个小时，因此默认关闭。--accept-source-change（accept_source_change=true）跳过这些检查，按配置中的关节导出。
- **上一次导出。** 除非给了 --reference 或指定 --no-compare-previous（compare_previous=false），导出都会与同一模型（否则同一包名）最近一次成功的导出比较（result.json 中的 previous_export）。与它的差异列在 validation.reference_changes 并汇总到 warnings，不判为失败。validation.reference_motion 给出每个网格（包围盒中心和质心）以及每个关节原点的位移，均在最上层带网格的 Link 坐标系中计算。如果所有网格都平移了同一个向量，每个 revolute 或 continuous 关节的原点都必须跟着移动；沿自身转轴方向的滑移不影响运动，不计入（reference_motion.rotation_axis_offset_mm 给出每个关节在垂直于轴方向上的残差）。任一转轴落后超过 0.5 mm，导出就以“Mesh/joint-origin mismatch”错误失败，错误中给出该向量、每个没跟上的转轴及其偏离零件的距离，以及已跟上的转轴；因此只修正了部分转轴的配置仍会失败。没动的 prismatic 和 fixed 关节原点只改变 Link 坐标系的位置，列为变化；加 --accept-source-change 时降为警告。显式的 --reference 仍是严格比较：任何差异都判为失败。

等待中的任务不会因睡眠或待机被误停：两次轮询间隔超过 60 秒视为挂起时间，既不计入 --stall-timeout 的空闲时间，也不计入 --timeout。可选的重算预检是第二个 SolidWorks 会话，单独按 --timeout 计时。

## JSON 配置

先 inspect，再编辑它返回的 configuration_path。可运行样例见 [arm-config.json](examples/arm-config.json) 和 [带限位及固定框架的配置](examples/arm-custom-config.json)。

配置格式为 schema_version=1、robot_name、recompute_kinematics 和 links，以及可选的 world 和 source（配置验证过的 CAD 版本，由工具写入，见“CAD 版本与上一次导出”）。每个 Link 指定 name、parent、components、coordinate_system、mesh_quality、frame_only 和 joint；组件名采用检查返回的完整实例名。

- recompute_kinematics=true：从配置中指定的 CAD 坐标系/轴重算关节变换；坐标系或轴为 "Automatically Generate" 时由原导出器根据配合推断。推断只看每个子 Link 的**第一个组件**在父 Link 固定时剩余的自由度，柔性子装配内部或经过其他 Link 的配合看不到。推断出的关节类型与 JSON 的 type 不一致时，导出直接失败并列出每个关节，不会静默生成 fixed。
- recompute_kinematics=false：采用 JSON 中的局部 xyz、rpy 和 axis（axis 在子 Link 坐标系中）。coordinate_system 为 "Automatically Generate" 的 Link，工具按关节链在 CAD 副本中创建坐标系 Origin_<关节名>，网格和惯量都按该 Link 自己的坐标系导出；指定了已有坐标系的 Link 必须与 JSON 原点一致，否则报错并给出该坐标系对应的 xyz/rpy。continuous/revolute 关节的 xyz 应位于转轴上。
- 根 Link 的 coordinate_system 取 "Automatically Generate" 时沿用原导出器的 Origin_global：它假定模型 Y 轴朝上，把 CAD 的 +Y 转成 URDF 的 +Z。按 Z 轴朝上建模的装配体应改用 "Assembly Origin"，直接使用装配体原点和坐标轴。
- 质量、质心和惯量取自 SolidWorks 组件质量属性。原导出器把各实体的惯量直接相加，由多个零件组成的 Link 惯量比导出网格实际算得的小 3–5 倍；它还按几何和密度计算质量，丢失 SolidWorks 中覆盖的质量。组件质量属性与网格一致，也计入覆盖值。工具先确认未覆盖的 Link 两种算法的质量和质心一致、各 Link 质量之和等于装配体质量，才替换；否则保留原导出器的值并在 warnings 中说明。用到覆盖值的 Link 和组件列在 warnings 与 bridge.massOverrides 中，原导出器的惯量保留在 bridge.exporterInertia。
- revolute/prismatic 必须给出有限的 lower/upper 与正的 effort/velocity；角度使用弧度、平移使用米。
- frame_only=true 的 Link 不导出质量、视觉或碰撞几何；它的子树仍正常处理。
- exclude_hidden_bodies（顶层）：零件文件名列表，例如 ["gantry_z1_p05.SLDPRT"]，这些零件中的隐藏实体不计入质量、惯量，也不计入网格检查用的 CAD 体积。用于 CAD 仍需要的隐藏实体，例如被配合引用的镜像源实体：删掉它会使配合失效。SolidWorks 计算装配体质量时包含隐藏实体，因此各项质量检查会减去排除的部分；warnings 和 bridge.excludedHiddenBodies 按 Link 列出排除的质量。文件名不是任何组件的文件时立即报错，并给出相近的名字。必须由 SolidWorks 在装配体中称重的实例（见 bridge.partMassCheck）仍包含隐藏实体，并列为未生效。
- 坐标系和参考轴应来自模型已有参考几何。工具不会凭空推断真实机器人关节意图或执行器参数。

下面两项由工具在 CAD 导出器写出 URDF 之后加入；导出器原始文件保留为任务目录下的 native.urdf，改动记录在 postprocess 中：

- joint.mimic：{"joint": "<跟随的关节>", "multiplier": 1, "offset": 0}，生成 URDF 的 <mimic> 元素（例如对称夹爪的另一根手指）。两个关节都必须是活动关节；mimic 关节不能再跟随另一个 mimic 关节。
- world（顶层）：在根 Link 之上加一个根 Link（默认 world）和固定关节（默认 world_to_base）。up_axis（+z、-z、+y、-y、+x、-x）指定模型的哪根轴变成 URDF 的 +Z（Y 轴朝上的模型用 +y），也可以用 rpy 直接给旋转；ground: true 让网格最低点落在 z=0，center_xy: true 让网格包围盒在 x=y=0 居中，两者都按导出的网格、全部关节取零位计算；xyz 再叠加一个偏移。例："world": {"up_axis": "+y", "ground": true, "center_xy": true}。

模型里没有保存的配置（或保存的配置引用了已不存在的组件）时，inspect 在 configuration_path 返回一份骨架配置（configuration_source=template）：所有顶层组件都归到同一个 base_link。它能通过检查、按单个刚体导出，可在此基础上拆出活动 Link。

检查器会拒绝循环/断开的 Link 树、重复组件归属、错误轴长度、无效限位和非有限数值，每条报错都写明是哪个 Link 或关节、期望值和实际值。inspect 结果中的 unassigned_components 列出未归属任何 Link 的实体零件，overlapping_components 列出既直接分配又随父子装配体分配的组件；两者非空时 configuration_ready=false，导出会在生成网格前直接失败并给出组件名。JSON 模式覆盖的是上述 Link/Joint 字段；旧配置中的外观设置尚未暴露到 JSON 编辑接口。

## Python

```python
import sys
sys.path.insert(0, r"C:\path\to\solidworks-urdf-tool\scripts")
from tool_service import inspect_model, check_configuration, export_urdf, start_export, wait_job, cancel_job

result = export_urdf(r"C:\path\robot.SLDASM", "robot_description")
# config_path / reference_urdf / timeout_seconds / stall_timeout_seconds / use_saved_files 以及
# accept_source_change / kinematics_tolerance_mm / kinematics_tolerance_deg / compare_previous / write_config_source / recompute_preflight / reuse_meshes / publish_to 只能按关键字传入
check = check_configuration(r"C:\path\robot.SLDASM", r"C:\path\robot-config.json")
result = export_urdf(check.get("reusable_model") or r"C:\path\robot.SLDASM", "robot_description", config_path=r"C:\path\robot-config.json")
assert result["passed"], result.get("error")
print(result["bridge"]["urdf"])
```

## MCP

本机已注册 solidworks_urdf_2026，启动程序为工作区 Python，服务脚本为 scripts/mcp_server.py；其他 stdio 客户端可参考 mcp-connection.example.json，把 <REPO_ROOT> 换成本仓库路径。启动超时 60 秒，工具超时 1200 秒。Codex 配置仅新增此服务；其他服务器和安全设置保留。

10 个工具：inspect_solidworks、inspect_model_configuration、prepare_model、validate_configuration、export_urdf、start_urdf_export、get_export_job、cancel_export_job、validate_urdf、cleanup_jobs。export_urdf 和 start_urdf_export 接受 check_only、use_saved_files、timeout_seconds、stall_timeout_seconds、compare_previous（默认 true）、accept_source_change、kinematics_tolerance_mm、kinematics_tolerance_deg（默认均为 0.1）、write_config_source、recompute_preflight、reuse_meshes（默认 true）和 publish_to；validate_configuration 可选 model_path，用于检查 CAD 版本。大型模型优先使用 start_urdf_export，再带 wait_seconds（例如 300）调用 get_export_job：调用会等任务结束，一到终态立即返回，客户端既不需要 shell sleep，也不用频繁轮询。validate_urdf 可校验本机任意位置的 URDF，并可选 reference_urdf。

阻塞式工具（export_urdf、inspect_model_configuration、prepare_model）同样在后台任务中运行，最多等待 1000 秒（环境变量 SW_URDF_SYNC_WAIT_SECONDS 可调，应小于客户端工具超时）。届时未完成会返回 still_running=true 和 job_id，任务继续执行，用 get_export_job 查询即可。

Codex 客户端需要重新加载 MCP 配置后才会把新服务加入当前工具目录；可在设置的 MCP servers 中重启连接，或重新启动客户端后查看 /mcp。依据：[官方 MCP 配置文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。无需公网端口、OAuth 或 OpenAI API key。

## 模型保护与边界

原文件只做读取和哈希。工具先复制原模型和依赖，仅对快照做引用重写、Pack and Go 和导出；API 即使在打包时保存模型，也只会保存快照。

模型保存的引用指向本机不存在的文件时（旧配置、导入源文件、库零件等），无需先在 SolidWorks 中打开模型：工具跳过这些路径建立快照，打开快照后检查每个未压缩组件都已加载。只有活动组件缺文件时才失败，并列出组件名和路径；被跳过的引用和缺文件的压缩组件写入 result.json 的 warnings。

若该装配体已保存且正在 SolidWorks 中打开，工具另外读取只读活动模型清单，要求快照的配置名、组件数和原生质量与之一致。打开的模型有未保存修改时，任务以 UNSAVED_CHANGES 失败：请在 SolidWorks 中保存，或关闭且不保存，然后重跑；也可以加 use_saved_files=true（CLI：--use-saved-files），按磁盘上已保存的文件导出、忽略未保存修改，结果的 warnings 会注明这一点。

操作串行执行，避免多个导出相互覆盖全局 STL 设置：后到的任务保持 queued（result.json 中 queue=waiting_for_cad_lock）最多 3600 秒，超时返回 CAD_BUSY；不保证先到先得。执行进程会登记 PID 与启动时间，进程消失或 120 秒内未登记的任务由 get_export_job 标记为 interrupted。运行中的任务在“卡住”时才会被停止：工具进程和其 SolidWorks 进程在 stall_timeout_seconds（默认 900 秒）内既没有进度更新，也没有 CPU 时间增长（单个大 STL 等长步骤期间 SolidWorks 一直在算，不算卡住）。timeout_seconds（默认 14400 秒）只是总时长硬上限。原导出器自己的日志（bridge-export.log）增长也算有活动。工作进程被暂停（睡眠、待机、机器卡死）后恢复时，暂停的时间不计入空闲时间，记录在 result.json 的 paused_seconds 和 pauses 中。卡住以 error.code=CAD_STALLED、超出上限以 CAD_TIMEOUT 结束，status 均为 timed_out，消息中写明所处阶段。cancel / cancel_export_job 可停止排队中或运行中的任务（status=cancelled）。无论哪种情况，都会核对 PID+启动时间后清理独立会话，并按下文恢复 STL 设置；不能把 timed_out/interrupted/cancelled 的输出用于正式模型。

私有会话与本机 SolidWorks 共用用户设置。原生核心启动后立即把 STL 相关设置保存到 output/preferences-snapshot.json；任务超时或失败而未确认恢复时，工具会另起私有会话把设置恢复为该快照，结果写入 result.json 的 preference_restore（changed_keys 为实际改回的项）。worker 意外退出的任务由 get_export_job 标记为待恢复，在下一个 CAD 任务开始前执行。

当前输出是原项目传统 ROS URDF 包。ROS 2 启动脚本、MJCF/USD、自动建模和全自动关节推断不属于本版本功能。模型仍应在实际目标仿真环境验证。

## 回归测试与来源

```powershell
.\.venv\Scripts\python.exe scripts\test_configuration.py
.\.venv\Scripts\python.exe scripts\test_validation.py
.\.venv\Scripts\python.exe scripts\test_jobs.py
.\.venv\Scripts\python.exe scripts\test_maintenance.py
.\.venv\Scripts\python.exe scripts\test_postprocess.py
.\.venv\Scripts\python.exe scripts\test_provenance.py
.\.venv\Scripts\python.exe scripts\test_cad_regressions.py
.\.venv\Scripts\python.exe scripts\test_nested_subassemblies.py
.\.venv\Scripts\python.exe scripts\test_tool_mcp.py
.\.venv\Scripts\python.exe scripts\test_registered_mcp.py
```

test_cad_regressions.py 需要 SolidWorks：它先生成一份含虚拟零件的示例机械臂副本，再覆盖含虚拟零件的 Pack and Go、多个关节共用同一原点、只检查模式、复用准备好的副本、进度记录、mimic/world 设置，以及写明错误组件名的报错。test_nested_subassemblies.py 同样需要 SolidWorks：它把工作区副本嵌进新建的装配体，导出把两层深的子装配体作为整体列出的 Link（示例机械臂；本机有工作站 CAD 时还有带 48 个样品瓶子装配体的料架，路径可用 `SW_URDF_VIAL_RACK` 指定），要求每个零件都在网格里。

原导出源码快照：882169e28952f0d17c87d7eab98826454421aabf，MIT。build/core-source-manifest.json 记录每个上游文件的哈希和适配标记，build/core-source 可审查生成后的源码。源码准备规则在 scripts/prepare-core.py。

此前下载的 solidworks_urdf_exporter2 保留在 vendor 供参考，本版本未使用它执行导出。上游许可证保留在 vendor，并随本地原生构建保留 SW2URDF-LICENSE.txt。

## 许可证

MIT，见 [LICENSE](LICENSE)；上游 ros/solidworks_urdf_exporter 的许可证见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES)。
