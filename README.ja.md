# SolidWorks URDF 2026 Tool

[English](README.md) | [简体中文](README.zh-CN.md) | **日本語**

コマンドライン、Python、MCP から SolidWorks 2026 を操作し、モデル構成の検査、CAD ファイルの分離コピーの作成、URDF/STL のエクスポートを行うローカルツールです。モデルの操作はすべて独立した SolidWorks プロセス内で実行されるため、computer-use やマウス操作、旧アドインのウィザードは不要です。

エクスポートのコアは、元の [ros/solidworks_urdf_exporter](https://github.com/ros/solidworks_urdf_exporter) のソースからビルドし、ヘッドレス実行、2026 API、エラー処理、座標フレームに対応するよう改造しています。現在のコアのバージョンは 2.0.0 で、インストールフォルダにある旧 SW2URDF.dll はもう読み込みません。

## 主な機能

- SolidWorks 2026 ネイティブ COM（x64、STA）。PID を指定して独立セッションに接続します。
- アセンブリと単一パーツのエクスポート。単一パーツは Link 1 個、ジョイント 0 個になります。
- 旧アドインが保存した Link/Joint 設定を読み込み、編集可能な JSON に変換します。
- JSON で Link ツリー、コンポーネントの割り当て、座標系、ジョイントの種類・軸、リミット、ダンピング/摩擦を設定できます。
- fixed、continuous、revolute、prismatic のジョイントと、ジオメトリを持たない固定座標フレームに対応します。
- ディレクトリをまたぐ CAD 依存関係のスナップショット、参照の書き換え、Pack and Go。同名の依存ファイルには別のファイル名を割り当てます。
- 実ファイルのハッシュ監査、質量・慣性・メッシュの検証、STL 設定の復元、独立プロセスのクリーンアップ。
- 永続的なジョブ記録、非同期エクスポート、状態の問い合わせ、明確なタイムアウト/失敗の返却。
- ローカル stdio MCP の 8 ツール。Codex に `solidworks_urdf_2026` として登録できます。

検証記録は [ツールレポート](validation/TOOL_REPORT.md) を参照してください。以前のブリッジ実現性テストは [初期レポート](validation/FEASIBILITY.md) に残してあります（どちらのレポートも中国語です）。

## 環境の準備

Windows x64、SolidWorks 2026、.NET Framework が必要です。

環境を（再）準備するには次を実行します。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
```

`setup.ps1` は、ワークスペース用の Python 環境の作成、固定バージョンの依存パッケージのインストール、Microsoft Roslyn コンパイラのダウンロード、ツールのビルドを行います。ソースのビルドには、ローカルの SolidWorks 2026 API の DLL と、インストールフォルダの URDFExporter にある MathNet/CsvHelper/log4net を使います。場所を変えるには `build-tool.ps1` の `-SolidWorksDir` と `-ExporterDir` を指定してください。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\build-tool.ps1
```

成果物は `build/bin/SolidWorksUrdf.exe` と、各ジョブの前に実行される読み取り専用のセッションプローブ `build/bin/SolidWorksProbe.exe` です。上流ソースへの適合パッチは、それぞれ一致回数を検証しており、上流の変更でパッチが合わなくなるとビルドは即座に失敗します。実行ファイルはリクエスト JSON を受け取りますが、通常は下記の Python/CLI/MCP インターフェースを使い、内部のジョブリクエストを手で管理しないでください。

## コマンドライン

ワークスペースのルートで実行します。

```powershell
# コンポーネントと既存の設定を検査する（結果に configuration_path が含まれる）
.\.venv\Scripts\python.exe scripts\tool_cli.py inspect "C:\path\robot.SLDASM"

# モデルに保存されている旧設定でエクスポートする
.\.venv\Scripts\python.exe scripts\tool_cli.py export "C:\path\robot.SLDASM" --package robot_description

# JSON 設定でエクスポートする
.\.venv\Scripts\python.exe scripts\tool_cli.py export "C:\path\robot.SLDASM" --package robot_description --config "C:\path\robot-config.json"

# 単一パーツ
.\.venv\Scripts\python.exe scripts\tool_cli.py export "C:\path\part.SLDPRT" --package part_description

# 大きなモデルはバックグラウンドでジョブを開始し、あとで問い合わせる
.\.venv\Scripts\python.exe scripts\tool_cli.py start-export "C:\path\robot.SLDASM" --package robot_description --timeout 1200
.\.venv\Scripts\python.exe scripts\tool_cli.py job "<返された job_id>"

# 自己完結した CAD コピーだけを作成する
.\.venv\Scripts\python.exe scripts\tool_cli.py prepare "C:\path\robot.SLDASM"

# SolidWorks を起動せずに設定を検証する
.\.venv\Scripts\python.exe scripts\tool_cli.py validate-config examples\arm-custom-config.json
```

操作のたびに `validation/jobs/<job_id>/` が作られます。`result.json` には、ステータス、元ファイルの SHA-256、設定の復元、物理検証、エラーが記録され、`stdout.log`/`stderr.log` にはネイティブのログが残ります。操作のすべてのチェックに合格したことを示すのは、`status=succeeded` かつ `passed=true` の場合だけです。`queued`/`running` や「一部のファイルが生成された」状態を成功とみなしてはいけません。

ディレクトリには、元のスナップショット、準備済みの CAD、エクスポートパッケージが同居することがあります。最終的な URDF の絶対パスは `bridge.urdf` にあります。既定では過去のジョブや出力を上書きしません。

## JSON 設定

まず `inspect` を実行し、返された `configuration_path` のファイルを編集します。実行できるサンプルは [arm-config.json](examples/arm-config.json) と [リミットと固定フレームを含む設定](examples/arm-custom-config.json) です。

形式は `schema_version=1`、`robot_name`、`recompute_kinematics`、`links` です。各 Link には `name`、`parent`、`components`、`coordinate_system`、`mesh_quality`、`frame_only`、`joint` を指定します。コンポーネント名は inspect が返す完全なインスタンス名を使います。

- `recompute_kinematics=true`：設定で指定した CAD の座標系/軸からジョイントの変換を再計算します。座標系または軸が "Automatically Generate" の場合は、元のエクスポーターが合致から推定します。推定は、親 Link のコンポーネントを固定したときの各子 Link の**先頭コンポーネント**の残り自由度だけを見るため、フレキシブルなサブアセンブリ内部や他の Link を経由する合致は認識されません。推定したジョイント種別が JSON の `type` と異なる場合は、`fixed` を黙って出力せず、各ジョイントを列挙して失敗します。
- `recompute_kinematics=false`：JSON のローカル xyz、rpy、axis（axis は子 Link 座標系）を使います。`coordinate_system` が "Automatically Generate" の Link には、ジョイントの連鎖から CAD コピー内に座標系 `Origin_<ジョイント名>` を作成し、メッシュと慣性をその Link 自身の座標系で出力します。既存の座標系を指定した Link は JSON の原点と一致している必要があり、一致しない場合はその座標系の xyz/rpy を示して失敗します。continuous/revolute ジョイントの xyz は回転軸上に置いてください。
- ルート Link の `coordinate_system` が "Automatically Generate" の場合は元のエクスポーターの `Origin_global` を使います。これは Y 軸上向きのモデルを前提とし、CAD の +Y を URDF の +Z に変換します。Z 軸上向きでモデリングしたアセンブリでは "Assembly Origin" を指定すると、アセンブリ自身の原点と軸をそのまま使います。
- 質量・重心・慣性は SolidWorks のコンポーネント質量特性から取ります。元のエクスポーターは個々のボディの慣性を足し合わせるため、複数部品からなる Link では出力メッシュから求めた慣性より 3～5 倍小さくなり、また形状と密度から質量を計算するので SolidWorks で上書きした質量が失われます。コンポーネント質量特性はメッシュと一致し、上書きも反映します。上書きのない全 Link で質量と重心が一致し、Link の質量の合計がアセンブリ質量と等しい場合に限って置き換え、そうでなければ元の値を残して `warnings` に記録します。質量を上書きした Link とコンポーネントは `warnings` と `bridge.massOverrides` に、元のエクスポーターの慣性は `bridge.exporterInertia` に残ります。
- revolute/prismatic には有限の `lower`/`upper` と正の `effort`/`velocity` が必須です。角度はラジアン、並進はメートルです。
- `frame_only=true` の Link は質量・ビジュアル・コリジョンのジオメトリを出力しませんが、その子孫は通常どおり処理されます。
- 座標系と基準軸は、モデルにすでにある基準ジオメトリに基づくべきです。ツールが実際のジョイントの意図やアクチュエータのパラメータを推測することはありません。

チェッカーは、循環または切断された Link ツリー、コンポーネントの二重割り当て、軸の長さの誤り、無効なリミット、有限でない数値を拒否します。inspect の結果では、`unassigned_components` がどの Link にも割り当てられていない実体パーツを、`overlapping_components` が直接割り当てと親子アセンブリ経由の割り当ての両方に該当するコンポーネントを列挙します。どちらかが空でないと `configuration_ready=false` となり、エクスポートはメッシュ生成前にコンポーネント名を示して失敗します。JSON モードが扱うのは上記の Link/Joint フィールドで、mimic や外観といった旧設定の複雑なフィールドは、まだすべては JSON 編集インターフェースに公開されていません。

## Python

```python
import sys
sys.path.insert(0, r"C:\path\to\solidworks-urdf-tool\scripts")
from tool_service import inspect_model, export_urdf, start_export, get_job

result = export_urdf(r"C:\path\robot.SLDASM", "robot_description")
# config_path / reference_urdf / timeout_seconds はキーワード引数のみ
result = export_urdf(r"C:\path\robot.SLDASM", "robot_description", config_path=r"C:\path\robot-config.json")
assert result["passed"], result.get("error")
print(result["bridge"]["urdf"])
```

## MCP

サーバーは `scripts/mcp_server.py` で、ワークスペースの Python で起動します。ほかの stdio クライアントは [mcp-connection.example.json](mcp-connection.example.json) をテンプレートとして使い、`<REPO_ROOT>` をこのリポジトリのパスに置き換えてください。開発時の Codex 設定では、起動タイムアウトは 60 秒、ツールのタイムアウトは 1200 秒で、登録で追加されるのはこのサーバーだけです（ほかのサーバーやセキュリティ設定は変更しません）。

8 つのツール：`inspect_solidworks`、`inspect_model_configuration`、`prepare_model`、`validate_configuration`、`export_urdf`、`start_urdf_export`、`get_export_job`、`validate_urdf`。大きなモデルでは `start_urdf_export` を使い、`get_export_job` で最終状態を確認してください。

ブロッキング型のツール（`export_urdf`、`inspect_model_configuration`、`prepare_model`）もバックグラウンドジョブとして実行され、最大 1000 秒待機します（環境変数 `SW_URDF_SYNC_WAIT_SECONDS` で調整できます。クライアントのツールタイムアウトより小さくしてください）。その時点で完了していなければ `still_running=true` と `job_id` が返り、ジョブは実行を続けるので、`get_export_job` で問い合わせます。

新しいサーバーが Codex のツールカタログに加わるには、クライアントが MCP 設定を再読み込みする必要があります。設定の MCP servers で接続を再起動するか、クライアントを再起動して `/mcp` を確認してください。[公式の MCP 設定ドキュメント](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) を参照してください。公開ポート、OAuth、OpenAI API キーは不要です。

## モデルの保護と制約

元ファイルは読み取りとハッシュ計算のみに使います。ツールはまず元のモデルと依存ファイルをコピーし、参照の書き換え、Pack and Go、エクスポートはスナップショットに対してのみ行います。パッケージ化の際に API がモデルを保存しても、保存されるのはスナップショットだけです。

保存された参照がこのコンピューターにないファイル（旧構成、インポート元ファイル、ライブラリ部品など）を指していても、先に SolidWorks でモデルを開く必要はありません。ツールはスナップショット作成時にそのパスをスキップし、開いたスナップショットで抑制されていないすべてのコンポーネントが読み込まれたことを確認します。アクティブなコンポーネントのファイルが欠けている場合だけ、コンポーネント名とパスを示して失敗します。スキップした参照とファイルのない抑制コンポーネントは result.json の `warnings` に記録されます。

保存済みのアセンブリが SolidWorks で開かれている場合は、さらに読み取り専用のアクティブモデル一覧を読み、スナップショットの構成名、コンポーネント数、ネイティブの質量が一致することを求めます。未保存の変更があるアクティブモデルは拒否されるため、ユーザーが明示的に保存してからエクスポートしてください。

操作は直列に実行され、複数のエクスポートがグローバルな STL 設定を上書きし合うのを防ぎます。後から来たジョブは最大 3600 秒 `queued`（`result.json` の `queue=waiting_for_cad_lock`）のまま待ち、超過すると `CAD_BUSY` を返します。先着順は保証されません。実行プロセスは PID と開始時刻を登録し、プロセスが消えた、または 120 秒以内に登録されなかったジョブは `get_export_job` が `interrupted` とマークします。タイムアウト時はジョブが失敗を返し、PID と開始時刻を照合したうえで独立セッションをクリーンアップします。`timed_out`/`interrupted` ジョブの出力を正式なモデルに使ってはいけません。

プライベートセッションは、ローカルの SolidWorks とユーザー設定を共有します。ネイティブコアは起動直後に STL 関連の設定を `output/preferences-snapshot.json` に保存します。ジョブがタイムアウトまたは失敗して復元が確認できなかった場合、ツールは別のプライベートセッションを起動して設定をそのスナップショットの状態に戻し、結果を `result.json` の `preference_restore` に記録します（`changed_keys` は実際に元へ戻した項目です）。ワーカーが予期せず終了したジョブは、`get_export_job` により復元待ちとマークされ、次の CAD ジョブの開始前に復元が実行されます。

出力は、元プロジェクトと同じ従来型の ROS URDF パッケージです。ROS 2 の起動スクリプト、MJCF/USD、自動モデリング、完全自動のジョイント推定は、このバージョンの対象外です。モデルは実際のターゲットのシミュレータで必ず検証してください。

## 回帰テストと出典

```powershell
.\.venv\Scripts\python.exe scripts\test_configuration.py
.\.venv\Scripts\python.exe scripts\test_validation.py
.\.venv\Scripts\python.exe scripts\test_jobs.py
.\.venv\Scripts\python.exe scripts\test_tool_mcp.py
.\.venv\Scripts\python.exe scripts\test_registered_mcp.py
```

元のエクスポートソースのスナップショット：882169e28952f0d17c87d7eab98826454421aabf（MIT）。`build/core-source-manifest.json` に上流の各ファイルのハッシュと改造マーカーが記録され、`build/core-source` で生成後のソースを確認できます。ソース準備のルールは `scripts/prepare-core.py` にあります。

以前ダウンロードした `solidworks_urdf_exporter2` は参考用として `vendor` に置いてありますが、このバージョンではエクスポートに使っていません。上流のライセンスは `vendor` に保存され、ローカルのネイティブビルドには `SW2URDF-LICENSE.txt` が同梱されます。

## ライセンス

MIT。[LICENSE](LICENSE) を参照してください（上流の ros/solidworks_urdf_exporter のライセンスは [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES) にあります）。
