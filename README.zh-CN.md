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
- 持久任务记录、异步导出、状态查询和明确的超时/失败返回。
- 本地 stdio MCP 的 8 个工具；已注册到本机 Codex，服务名 solidworks_urdf_2026。

验证记录见 [工具报告](validation/TOOL_REPORT.md)；旧桥接可行性测试保留在 [早期报告](validation/FEASIBILITY.md)。

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

# 为大型模型后台启动任务，再查询它
.\.venv\Scripts\python.exe scripts\tool_cli.py start-export "C:\path\robot.SLDASM" --package robot_description --timeout 1200
.\.venv\Scripts\python.exe scripts\tool_cli.py job "返回的job_id"

# 仅生成自包含 CAD 副本
.\.venv\Scripts\python.exe scripts\tool_cli.py prepare "C:\path\robot.SLDASM"

# 不启动 SolidWorks 的配置检查
.\.venv\Scripts\python.exe scripts\tool_cli.py validate-config examples\arm-custom-config.json
```

每次操作生成 validation/jobs/<job_id>/。result.json 记录状态、原文件 SHA-256、设置恢复、物理校验和错误；stdout.log/stderr.log 保留原生日志。status=succeeded 且 passed=true 才表示该操作所有检查通过。queued/running 和“已生成部分文件”均不能视为成功。

目录中可能同时存在源快照、准备后的 CAD 和导出包；最终 URDF 的绝对路径在 bridge.urdf 中。默认不覆盖旧任务或旧输出。

## JSON 配置

先 inspect，再编辑它返回的 configuration_path。可运行样例见 [arm-config.json](examples/arm-config.json) 和 [带限位及固定框架的配置](examples/arm-custom-config.json)。

配置格式为 schema_version=1、robot_name、recompute_kinematics 和 links。每个 Link 指定 name、parent、components、coordinate_system、mesh_quality、frame_only 和 joint；组件名采用检查返回的完整实例名。

- recompute_kinematics=true：从配置中指定的 CAD 坐标系/轴重算关节变换；坐标系或轴为 "Automatically Generate" 时由原导出器根据配合推断。推断只看每个子 Link 的**第一个组件**在父 Link 固定时剩余的自由度，柔性子装配内部或经过其他 Link 的配合看不到。推断出的关节类型与 JSON 的 type 不一致时，导出直接失败并列出每个关节，不会静默生成 fixed。
- recompute_kinematics=false：采用 JSON 中的局部 xyz、rpy 和 axis（axis 在子 Link 坐标系中）。coordinate_system 为 "Automatically Generate" 的 Link，工具按关节链在 CAD 副本中创建坐标系 Origin_<关节名>，网格和惯量都按该 Link 自己的坐标系导出；指定了已有坐标系的 Link 必须与 JSON 原点一致，否则报错并给出该坐标系对应的 xyz/rpy。continuous/revolute 关节的 xyz 应位于转轴上。
- 根 Link 的 coordinate_system 取 "Automatically Generate" 时沿用原导出器的 Origin_global：它假定模型 Y 轴朝上，把 CAD 的 +Y 转成 URDF 的 +Z。按 Z 轴朝上建模的装配体应改用 "Assembly Origin"，直接使用装配体原点和坐标轴。
- 质量、质心和惯量取自 SolidWorks 组件质量属性。原导出器把各实体的惯量直接相加，由多个零件组成的 Link 惯量比导出网格实际算得的小 3–5 倍；它还按几何和密度计算质量，丢失 SolidWorks 中覆盖的质量。组件质量属性与网格一致，也计入覆盖值。工具先确认未覆盖的 Link 两种算法的质量和质心一致、各 Link 质量之和等于装配体质量，才替换；否则保留原导出器的值并在 warnings 中说明。用到覆盖值的 Link 和组件列在 warnings 与 bridge.massOverrides 中，原导出器的惯量保留在 bridge.exporterInertia。
- revolute/prismatic 必须给出有限的 lower/upper 与正的 effort/velocity；角度使用弧度、平移使用米。
- frame_only=true 的 Link 不导出质量、视觉或碰撞几何；它的子树仍正常处理。
- 坐标系和参考轴应来自模型已有参考几何。工具不会凭空推断真实机器人关节意图或执行器参数。

检查器会拒绝循环/断开的 Link 树、重复组件归属、错误轴长度、无效限位和非有限数值。inspect 结果中的 unassigned_components 列出未归属任何 Link 的实体零件，overlapping_components 列出既直接分配又随父子装配体分配的组件；两者非空时 configuration_ready=false，导出会在生成网格前直接失败并给出组件名。JSON 模式覆盖的是上述 Link/Joint 字段；复杂 mimic/外观等旧配置字段尚未全部暴露到 JSON 编辑接口。

## Python

```python
import sys
sys.path.insert(0, r"C:\path\to\solidworks-urdf-tool\scripts")
from tool_service import inspect_model, export_urdf, start_export, get_job

result = export_urdf(r"C:\path\robot.SLDASM", "robot_description")
# config_path / reference_urdf / timeout_seconds 只能按关键字传入
result = export_urdf(r"C:\path\robot.SLDASM", "robot_description", config_path=r"C:\path\robot-config.json")
assert result["passed"], result.get("error")
print(result["bridge"]["urdf"])
```

## MCP

本机已注册 solidworks_urdf_2026，启动程序为工作区 Python，服务脚本为 scripts/mcp_server.py；其他 stdio 客户端可参考 mcp-connection.example.json，把 <REPO_ROOT> 换成本仓库路径。启动超时 60 秒，工具超时 1200 秒。Codex 配置仅新增此服务；其他服务器和安全设置保留。

8 个工具：inspect_solidworks、inspect_model_configuration、prepare_model、validate_configuration、export_urdf、start_urdf_export、get_export_job、validate_urdf。大型模型优先使用 start_urdf_export，再用 get_export_job 查询终态。

阻塞式工具（export_urdf、inspect_model_configuration、prepare_model）同样在后台任务中运行，最多等待 1000 秒（环境变量 SW_URDF_SYNC_WAIT_SECONDS 可调，应小于客户端工具超时）。届时未完成会返回 still_running=true 和 job_id，任务继续执行，用 get_export_job 查询即可。

Codex 客户端需要重新加载 MCP 配置后才会把新服务加入当前工具目录；可在设置的 MCP servers 中重启连接，或重新启动客户端后查看 /mcp。依据：[官方 MCP 配置文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。无需公网端口、OAuth 或 OpenAI API key。

## 模型保护与边界

原文件只做读取和哈希。工具先复制原模型和依赖，仅对快照做引用重写、Pack and Go 和导出；API 即使在打包时保存模型，也只会保存快照。

模型保存的引用指向本机不存在的文件时（旧配置、导入源文件、库零件等），无需先在 SolidWorks 中打开模型：工具跳过这些路径建立快照，打开快照后检查每个未压缩组件都已加载。只有活动组件缺文件时才失败，并列出组件名和路径；被跳过的引用和缺文件的压缩组件写入 result.json 的 warnings。

若该装配体已保存且正在 SolidWorks 中打开，工具另外读取只读活动模型清单，要求快照的配置名、组件数和原生质量与之一致。未保存的活动模型会被拒绝，需用户明确保存后再导出。

操作串行执行，避免多个导出相互覆盖全局 STL 设置：后到的任务保持 queued（result.json 中 queue=waiting_for_cad_lock）最多 3600 秒，超时返回 CAD_BUSY；不保证先到先得。执行进程会登记 PID 与启动时间，进程消失或 120 秒内未登记的任务由 get_export_job 标记为 interrupted。任务超时会返回失败并核对 PID+启动时间后清理独立会话；不能把 timed_out/interrupted 的输出用于正式模型。

私有会话与本机 SolidWorks 共用用户设置。原生核心启动后立即把 STL 相关设置保存到 output/preferences-snapshot.json；任务超时或失败而未确认恢复时，工具会另起私有会话把设置恢复为该快照，结果写入 result.json 的 preference_restore（changed_keys 为实际改回的项）。worker 意外退出的任务由 get_export_job 标记为待恢复，在下一个 CAD 任务开始前执行。

当前输出是原项目传统 ROS URDF 包。ROS 2 启动脚本、MJCF/USD、自动建模和全自动关节推断不属于本版本功能。模型仍应在实际目标仿真环境验证。

## 回归测试与来源

```powershell
.\.venv\Scripts\python.exe scripts\test_configuration.py
.\.venv\Scripts\python.exe scripts\test_validation.py
.\.venv\Scripts\python.exe scripts\test_jobs.py
.\.venv\Scripts\python.exe scripts\test_tool_mcp.py
.\.venv\Scripts\python.exe scripts\test_registered_mcp.py
```

原导出源码快照：882169e28952f0d17c87d7eab98826454421aabf，MIT。build/core-source-manifest.json 记录每个上游文件的哈希和适配标记，build/core-source 可审查生成后的源码。源码准备规则在 scripts/prepare-core.py。

此前下载的 solidworks_urdf_exporter2 保留在 vendor 供参考，本版本未使用它执行导出。上游许可证保留在 vendor，并随本地原生构建保留 SW2URDF-LICENSE.txt。

## 许可证

MIT，见 [LICENSE](LICENSE)；其中同时附有上游 ros/solidworks_urdf_exporter 的许可证。
