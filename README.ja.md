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
- 永続的なジョブ記録、非同期エクスポート、段階とステップ数付きの進捗問い合わせ、待機、キャンセル、停滞検出、明確な失敗の返却。
- 関節と Link 座標系まで実行して止まるチェック専用モードと、準備済み CAD コピーの再利用による、設定の素早い試行錯誤。
- JSON 設定からの mimic 関節と world ルートフレーム（Z 軸上向き・接地・中心合わせ）。1 回のエクスポートでそのまま使える URDF が得られます。
- ローカル stdio MCP の 9 ツール。Codex に `solidworks_urdf_2026` として登録できます。

検証記録は [ツールレポート](validation/TOOL_REPORT.ja.md) を参照してください。以前のブリッジ実現性テストは [初期レポート](validation/FEASIBILITY.ja.md) に残してあります。

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

# JSON 設定を関節と Link 座標系までだけ検証する（慣性もメッシュも計算しない）
.\.venv\Scripts\python.exe scripts\tool_cli.py check "C:\path\robot.SLDASM" --config "C:\path\robot-config.json"

# 大きなモデルはバックグラウンドでジョブを開始し、完了を待つ（終わり次第戻る）かキャンセルする
.\.venv\Scripts\python.exe scripts\tool_cli.py start-export "C:\path\robot.SLDASM" --package robot_description --config "C:\path\robot-config.json"
.\.venv\Scripts\python.exe scripts\tool_cli.py job "<返された job_id>" --wait 300
.\.venv\Scripts\python.exe scripts\tool_cli.py cancel "<返された job_id>"

# 自己完結した CAD コピーだけを作成する
.\.venv\Scripts\python.exe scripts\tool_cli.py prepare "C:\path\robot.SLDASM"

# SolidWorks を起動せずに設定を検証する
.\.venv\Scripts\python.exe scripts\tool_cli.py validate-config examples\arm-custom-config.json

# 設定がモデルの現在の CAD バージョン用かどうかも報告（ファイルを読むだけ）
.\.venv\Scripts\python.exe scripts\tool_cli.py validate-config "C:\path\robot-config.json" --model "C:\path\robot.SLDASM"

# このコンピューター上の任意の URDF パッケージを検証する（プロジェクトにコピーしたものなど）
.\.venv\Scripts\python.exe scripts\tool_cli.py validate-urdf "C:\project\robot_description\urdf\robot_description.urdf"

# 2 つのエクスポートを比較：数値の差は失敗、reference_motion に各メッシュと関節原点の移動量
.\.venv\Scripts\python.exe scripts\tool_cli.py validate-urdf "C:\new\urdf\robot.urdf" --reference "C:\old\urdf\robot.urdf"
```

CAD コマンドはいずれも `--timeout`（総時間の上限、既定 14400 秒）、`--stall-timeout`（既定 900 秒、後述）、`--use-saved-files` を受け付けます。`check`、`export`、`start-export` はさらに `--accept-source-change`、`--kinematics-tolerance-mm`、`--kinematics-tolerance-deg`、`--write-config-source`、`--recompute-preflight` を、`export`/`start-export` は `--no-compare-previous`（後述の「CAD バージョンと前回のエクスポート」を参照）と `--no-reuse-meshes`（後述）を受け付けます。

操作のたびに `validation/jobs/<job_id>/` が作られます。`result.json` には、ステータス、元ファイルの SHA-256、設定の復元、物理検証、エラーが記録され、`stdout.log`/`stderr.log` にはネイティブのログが残ります。操作のすべてのチェックに合格したことを示すのは、`status=succeeded` かつ `passed=true` の場合だけです。`queued`/`running` や「一部のファイルが生成された」状態を成功とみなしてはいけません。

エクスポートの検証では各 Link をゼロ姿勢に配置し、子 Link のメッシュの境界ボックスが親からロボット寸法の 0.5 %（最小 2 mm）より離れていると失敗にします。"Automatically Generate" で回転したルート座標系などの座標系のずれを検出できます。各 Link のワールド境界ボックスは `validation.link_world_bounds_m` にあります。また、エクスポートごとにパッケージの `previews/` に `front.png`、`side.png`、`top.png`（+X、-Y、+Z から見た図、Link ごとに色分け）を書き出し、パスは result.json の `previews.images` に記録されます。親の内部の点まわりの回転では境界ボックスが接したままのことがあるため、使う前に必ず画像を確認してください。`validate-urdf --previews` と `validate_urdf(render_previews=true)` で任意の URDF の画像も作れます。

質量はメッシュではなく CAD コンポーネントから取るため、STL から部品が抜けても質量は正しいままです。そこでエクスポートごとに各 Link のメッシュを CAD 形状と比較します（`validation.cad_geometry`）。列挙された各部品と、サブアセンブリごと列挙された場合はその下の各部品について、CAD 境界ボックス内にメッシュ頂点がなければならず、メッシュ体積は CAD 体積の 85–115 % に収まる必要があります（95–105 % を外れると警告。多くはテッセレーション誤差）。満たさない場合はエクスポートを失敗とし、Link と欠けている可能性の高い部品を示します。サブアセンブリごと列挙すると、抑制・エンベロープ以外の全部品（質量と同じく非表示部品も含む）をエクスポートします。旧バージョンでは別のサブアセンブリの中にあるサブアセンブリ（ワークステーションのラック上のバイアルなど）の部品が一つも出力されませんでした。そのような設定で作ったエクスポートはやり直してください。結果の `subassembly_components` にこうしたコンポーネントを警告付きで示し、`expanded_config_path` はその部品を列挙した設定です（`recompute_kinematics` が true のとき、子 Link の先頭にあるサブアセンブリは関節の検出に使うためそのまま残します）。

ディレクトリには、元のスナップショット、準備済みの CAD、エクスポートパッケージが同居することがあります。最終的な URDF の絶対パスは `bridge.urdf` にあります。既定では過去のジョブや出力を上書きしません。

### 試行錯誤を速くする

- **チェック専用。** `check`（MCP：`check_only=true`）はエクスポートを Link 座標系と関節まで進めて止まり、慣性・質量特性・STL は扱いません。結果には `resolved_joints`（各関節の種類・原点・軸）、`link_frames`、`recompute_kinematics=true` なら `detected_joint_types` が入ります。コンポーネント名の誤り、関節原点と合わない座標系、関節種類の誤検出などの設定ミスが、完全なエクスポートを待たずに数分で分かります。
- **準備済みコピーの再利用。** Pack and Go を実行した inspect・prepare・check・export はすべて `reusable_model` を返します。ファイルのハッシュ一覧付きの自己完結した CAD コピーで、コピーが変更されていなければエクスポート失敗後も使えます。次回のモデルパスに渡すと、ハッシュを照合してフォルダーを複製し直接開くため、元モデルの読み取り専用オープンと Pack and Go を省略できます。
- **進捗。** `job` / `get_export_job` は `progress` を返します：段階（例 `export_meshes`）、`Exporting STL meshes: 7/18 (gripper_z)` のような要約、ステップ数、経過秒数、現段階の秒数、SolidWorks の CPU 秒数、最後に作業の兆候があってからの `idle_seconds`、ログの末尾数行。`stderr.log` にはタイムスタンプが付きます。
- 失敗時は `error.stage` が発生した段階を示します。
- **メッシュの再利用。** 合格したエクスポートはジョブフォルダーに `mesh-keys.json` を書き出します。各 Link メッシュの指紋で、表示される各部品ファイルのハッシュと構成、アセンブリ内での位置、メッシュ品質、ドキュメント全体の大きさ（SolidWorks は粗・細の公差をここから決めます）、Link の CAD 体積（アセンブリでのカット形状でも変わります）から作ります。以後のエクスポートでは指紋が同じ Link の STL をそのまま使い、コンポーネントの表示と再メッシュ化を省きます。関節原点だけを変えた場合は STL を新しい Link 座標系へ剛体移動します。再利用したメッシュも CAD との比較を同じように通り、`reused_meshes` に示されます。`--no-reuse-meshes`（`reuse_meshes=false`）で無効にできます。
- **大規模アセンブリの所要時間。** SolidWorks ではコンポーネントを 1 つずつ選択に追加すると、既に選択した数に比例して時間がかかります（800 部品で 194 秒）。メッシュ出力と関節再計算での表示・非表示・固定・固定解除は、一度の呼び出しで選択するようにしました（0.9 秒）。コンポーネントの質量特性の読み取りも部品数とともに急に増える（800 部品で 106 秒、同じピペットチップ 1434 個は 100 個ごとに 25 秒）ため、各部品ファイルを自身のドキュメントで 1 回だけ計量し（部品内のオーバーライドと非表示ボディを含む）、各インスタンスの Link 座標系での姿勢へ回転・移動して平行軸の定理で正確に合成します。各ファイルはまず 1 つのインスタンスでアセンブリ内の SolidWorks の計量結果と照合し（アセンブリでコンポーネントに材料を割り当てた場合などは一致しません）、一致しないファイル、オーバーライドのあるサブアセンブリ、非アクティブ構成のコンポーネントは引き続き SolidWorks が計量します（`bridge.partMassCheck`）。質量のオーバーライドは各ドキュメントのオーバーライド設定を 1 回ずつ読んで見つけ、該当するコンポーネントだけを計量します。

### CAD バージョンと前回のエクスポート

`recompute_kinematics=false` の設定は、ある版の CAD から写した絶対的な関節原点を持っています。その後アセンブリが原点に対してモデルを動かした状態で保存されると、メッシュは CAD に追従しますが関節原点は追従せず、新しい URDF は単体ではすべての検査に合格してしまいます。これを次の 2 つの検査で検出します。

- **設定の CAD バージョン。** JSON 設定を使った `check` または `export` が合格すると、検証した CAD バージョンを記録します。ジョブの `config_with_source_path` は `source` ブロック（`model_sha256`、`model_path`、`exported_at`、`job_id`、`operation`、ルート Link 座標系での各 Link 座標系の姿勢 `link_frames`、各 Link の最初のコンポーネントのアセンブリ内の姿勢 `component_poses`）付きの設定で、`--write-config-source`（MCP/Python：`write_config_source=true`）を指定すると、このブロックを `--config` のファイル自体に書き込みます（ジョブ中にファイルが変更された場合は書き込みません）。`source` ブロックのない設定は、同じモデルで同じ関節原点を使った過去のジョブから出所を推定します（`config_source.origin=inferred_from_job`。そのジョブがコンポーネントの姿勢を記録していればそれも使います）。そのバージョンがモデルの SHA-256（再利用コピーでは元のモデル）と異なると、結果の `warnings` に警告が出て `config_stale=true` になります。`validate-config --model` は SolidWorks を起動せずに同じ判定を返します。続いてエクスポートは同じ SolidWorks セッション内で、設定の解決直後、Link 座標系やメッシュを作る前に、各 Link の最初のコンポーネントの姿勢を記録値と比較します（数秒）。いずれかのコンポーネントが `--kinematics-tolerance-mm` / `--kinematics-tolerance-deg`（既定 0.1 mm / 0.1°）を超えて移動・回転していると `CONFIG_OUTDATED` で失敗し、Link ごとに新旧の位置を示し、すべてが同じ剛体並進ならそのベクトルを示します（明細は `component_pose_check`）。姿勢が記録されていない設定（この検査より前の設定）は警告のみで、下の前回のエクスポートとの比較に頼ります。`--recompute-preflight`（`recompute_preflight=true`）は代わりに別のチェック専用セッション（`preflight/`）で現在の CAD から "Automatically Generate" の関節を再計算して原点と軸を比較し、`CONFIG_OUTDATED` または `KINEMATICS_PREFLIGHT_FAILED` で失敗します。数百部品のアセンブリでは必要な拘束解析に数時間かかるため、既定ではオフです。`--accept-source-change`（`accept_source_change=true`）はこれらの検査を省き、設定どおりの関節でエクスポートします。
- **前回のエクスポート。** `--reference` を指定せず `--no-compare-previous`（`compare_previous=false`）も指定しない限り、エクスポートは同じモデル（なければ同じパッケージ名）の最新の成功したエクスポートと比較されます（result.json の `previous_export`）。差は `validation.reference_changes` に列挙され `warnings` に要約されますが、失敗にはなりません。`validation.reference_motion` には各メッシュ（バウンディングボックス中心と重心）と各関節原点の移動量が、メッシュを持つ最上位 Link の座標系で入ります。すべてのメッシュが同じベクトルだけ動いた場合、revolute と continuous の各関節原点も一緒に動いている必要があります。自分の軸方向へのずれは運動を変えないので数えません（`reference_motion.rotation_axis_offset_mm` に関節ごとの軸に垂直な残差が入ります）。0.5 mm を超えて取り残された軸があると、エクスポートは「Mesh/joint-origin mismatch」エラーで失敗し、そのベクトル、取り残された各軸と部品からのずれ、追従した軸を示します。一部の軸だけ修正した設定も失敗します。動いていない prismatic と fixed の関節原点は Link 座標系の位置を変えるだけなので、変更として列挙します。`--accept-source-change` では警告になります。明示的な `--reference` は従来どおり厳密で、差があれば失敗します。

待機中のジョブはスリープやスタンバイで誤って停止されません。2 回のポーリングの間隔が 60 秒を超えた分は中断時間とみなし、`--stall-timeout` のアイドル時間にも `--timeout` にも数えません。任意の再計算事前チェックは 2 つ目の SolidWorks セッションで、`--timeout` は別に適用されます。

## JSON 設定

まず `inspect` を実行し、返された `configuration_path` のファイルを編集します。実行できるサンプルは [arm-config.json](examples/arm-config.json) と [リミットと固定フレームを含む設定](examples/arm-custom-config.json) です。

形式は `schema_version=1`、`robot_name`、`recompute_kinematics`、`links` と、任意の `world` および `source`（設定を検証した CAD バージョン。ツールが書き込みます。「CAD バージョンと前回のエクスポート」を参照）です。各 Link には `name`、`parent`、`components`、`coordinate_system`、`mesh_quality`、`frame_only`、`joint` を指定します。コンポーネント名は inspect が返す完全なインスタンス名を使います。

- `recompute_kinematics=true`：設定で指定した CAD の座標系/軸からジョイントの変換を再計算します。座標系または軸が "Automatically Generate" の場合は、元のエクスポーターが合致から推定します。推定は、親 Link のコンポーネントを固定したときの各子 Link の**先頭コンポーネント**の残り自由度だけを見るため、フレキシブルなサブアセンブリ内部や他の Link を経由する合致は認識されません。推定したジョイント種別が JSON の `type` と異なる場合は、`fixed` を黙って出力せず、各ジョイントを列挙して失敗します。
- `recompute_kinematics=false`：JSON のローカル xyz、rpy、axis（axis は子 Link 座標系）を使います。`coordinate_system` が "Automatically Generate" の Link には、ジョイントの連鎖から CAD コピー内に座標系 `Origin_<ジョイント名>` を作成し、メッシュと慣性をその Link 自身の座標系で出力します。既存の座標系を指定した Link は JSON の原点と一致している必要があり、一致しない場合はその座標系の xyz/rpy を示して失敗します。continuous/revolute ジョイントの xyz は回転軸上に置いてください。
- ルート Link の `coordinate_system` が "Automatically Generate" の場合は元のエクスポーターの `Origin_global` を使います。これは Y 軸上向きのモデルを前提とし、CAD の +Y を URDF の +Z に変換します。Z 軸上向きでモデリングしたアセンブリでは "Assembly Origin" を指定すると、アセンブリ自身の原点と軸をそのまま使います。
- 質量・重心・慣性は SolidWorks のコンポーネント質量特性から取ります。元のエクスポーターは個々のボディの慣性を足し合わせるため、複数部品からなる Link では出力メッシュから求めた慣性より 3～5 倍小さくなり、また形状と密度から質量を計算するので SolidWorks で上書きした質量が失われます。コンポーネント質量特性はメッシュと一致し、上書きも反映します。上書きのない全 Link で質量と重心が一致し、Link の質量の合計がアセンブリ質量と等しい場合に限って置き換え、そうでなければ元の値を残して `warnings` に記録します。質量を上書きした Link とコンポーネントは `warnings` と `bridge.massOverrides` に、元のエクスポーターの慣性は `bridge.exporterInertia` に残ります。
- revolute/prismatic には有限の `lower`/`upper` と正の `effort`/`velocity` が必須です。角度はラジアン、並進はメートルです。
- `frame_only=true` の Link は質量・ビジュアル・コリジョンのジオメトリを出力しませんが、その子孫は通常どおり処理されます。
- 座標系と基準軸は、モデルにすでにある基準ジオメトリに基づくべきです。ツールが実際のジョイントの意図やアクチュエータのパラメータを推測することはありません。

次の 2 つは、CAD エクスポーターが URDF を書き出した後にツールが加えます。エクスポーター自身のファイルはジョブディレクトリの `native.urdf` として残り、変更内容は `postprocess` に記録されます。

- `joint.mimic`：`{"joint": "<追従する関節>", "multiplier": 1, "offset": 0}` で URDF の `<mimic>` 要素を追加します（対称グリッパーのもう一方の指など）。両方とも可動関節である必要があり、mimic 関節が別の mimic 関節に追従することはできません。
- `world`（トップレベル）：ルート Link の上にルート Link（既定 `world`）と固定関節（既定 `world_to_base`）を追加します。`up_axis`（`+z`、`-z`、`+y`、`-y`、`+x`、`-x`）は URDF の +Z になるモデルの軸（Y 軸上向きのモデルなら `+y`）、または `rpy` で回転を直接指定します。`ground: true` はメッシュの最下点を z=0 に、`center_xy: true` はメッシュの外接箱を x=y=0 の中心に合わせます。どちらもエクスポートしたメッシュから全関節ゼロの姿勢で計算し、`xyz` はさらにオフセットを加えます。例：`"world": {"up_axis": "+y", "ground": true, "center_xy": true}`。

モデルに保存済みの設定がない（または存在しないコンポーネントを参照する古い設定しかない）場合、inspect は `configuration_path` にテンプレート（`configuration_source=template`）を返します。最上位のコンポーネントをすべて 1 つの `base_link` に入れたもので、チェックを通り単一の剛体としてエクスポートでき、可動 Link を切り出す出発点になります。

チェッカーは、循環または切断された Link ツリー、コンポーネントの二重割り当て、軸の長さの誤り、無効なリミット、有限でない数値を拒否し、各メッセージは対象の Link または関節と、期待値・実際の値を示します。inspect の結果では、`unassigned_components` がどの Link にも割り当てられていない実体パーツを、`overlapping_components` が直接割り当てと親子アセンブリ経由の割り当ての両方に該当するコンポーネントを列挙します。どちらかが空でないと `configuration_ready=false` となり、エクスポートはメッシュ生成前にコンポーネント名を示して失敗します。JSON モードが扱うのは上記の Link/Joint フィールドで、旧設定の外観設定は JSON 編集インターフェースに公開されていません。

## Python

```python
import sys
sys.path.insert(0, r"C:\path\to\solidworks-urdf-tool\scripts")
from tool_service import inspect_model, check_configuration, export_urdf, start_export, wait_job, cancel_job

result = export_urdf(r"C:\path\robot.SLDASM", "robot_description")
# config_path / reference_urdf / timeout_seconds / stall_timeout_seconds / use_saved_files と
# accept_source_change / kinematics_tolerance_mm / kinematics_tolerance_deg / compare_previous / write_config_source / recompute_preflight / reuse_meshes はキーワード引数のみ
check = check_configuration(r"C:\path\robot.SLDASM", r"C:\path\robot-config.json")
result = export_urdf(check.get("reusable_model") or r"C:\path\robot.SLDASM", "robot_description", config_path=r"C:\path\robot-config.json")
assert result["passed"], result.get("error")
print(result["bridge"]["urdf"])
```

## MCP

サーバーは `scripts/mcp_server.py` で、ワークスペースの Python で起動します。ほかの stdio クライアントは [mcp-connection.example.json](mcp-connection.example.json) をテンプレートとして使い、`<REPO_ROOT>` をこのリポジトリのパスに置き換えてください。開発時の Codex 設定では、起動タイムアウトは 60 秒、ツールのタイムアウトは 1200 秒で、登録で追加されるのはこのサーバーだけです（ほかのサーバーやセキュリティ設定は変更しません）。

9 つのツール：`inspect_solidworks`、`inspect_model_configuration`、`prepare_model`、`validate_configuration`、`export_urdf`、`start_urdf_export`、`get_export_job`、`cancel_export_job`、`validate_urdf`。`export_urdf` と `start_urdf_export` は `check_only`、`use_saved_files`、`timeout_seconds`、`stall_timeout_seconds`、`compare_previous`（既定 true）、`accept_source_change`、`kinematics_tolerance_mm`、`kinematics_tolerance_deg`（既定はいずれも 0.1）、`write_config_source`、`recompute_preflight`、`reuse_meshes`（既定 true）を受け付けます。`validate_configuration` は CAD バージョンの確認用に任意の `model_path` を受け付けます。大きなモデルでは `start_urdf_export` を使い、`wait_seconds`（例 300）付きで `get_export_job` を呼んでください。呼び出しはジョブの完了を待ち、終了状態になった時点ですぐ戻るので、クライアントはシェルの `sleep` も頻繁なポーリングも不要です。`validate_urdf` はこのコンピューター上の任意の URDF と、任意の `reference_urdf` を受け付けます。

ブロッキング型のツール（`export_urdf`、`inspect_model_configuration`、`prepare_model`）もバックグラウンドジョブとして実行され、最大 1000 秒待機します（環境変数 `SW_URDF_SYNC_WAIT_SECONDS` で調整できます。クライアントのツールタイムアウトより小さくしてください）。その時点で完了していなければ `still_running=true` と `job_id` が返り、ジョブは実行を続けるので、`get_export_job` で問い合わせます。

新しいサーバーが Codex のツールカタログに加わるには、クライアントが MCP 設定を再読み込みする必要があります。設定の MCP servers で接続を再起動するか、クライアントを再起動して `/mcp` を確認してください。[公式の MCP 設定ドキュメント](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) を参照してください。公開ポート、OAuth、OpenAI API キーは不要です。

## モデルの保護と制約

元ファイルは読み取りとハッシュ計算のみに使います。ツールはまず元のモデルと依存ファイルをコピーし、参照の書き換え、Pack and Go、エクスポートはスナップショットに対してのみ行います。パッケージ化の際に API がモデルを保存しても、保存されるのはスナップショットだけです。

保存された参照がこのコンピューターにないファイル（旧構成、インポート元ファイル、ライブラリ部品など）を指していても、先に SolidWorks でモデルを開く必要はありません。ツールはスナップショット作成時にそのパスをスキップし、開いたスナップショットで抑制されていないすべてのコンポーネントが読み込まれたことを確認します。アクティブなコンポーネントのファイルが欠けている場合だけ、コンポーネント名とパスを示して失敗します。スキップした参照とファイルのない抑制コンポーネントは result.json の `warnings` に記録されます。

保存済みのアセンブリが SolidWorks で開かれている場合は、さらに読み取り専用のアクティブモデル一覧を読み、スナップショットの構成名、コンポーネント数、ネイティブの質量が一致することを求めます。開いているモデルに未保存の変更があると、ジョブは `UNSAVED_CHANGES` で失敗します。SolidWorks で保存するか、保存せずに閉じてから再実行してください。または `use_saved_files=true`（CLI：`--use-saved-files`）で、ディスクに保存済みの版をエクスポートし未保存の変更を無視できます。その旨は結果の `warnings` に記録されます。

操作は直列に実行され、複数のエクスポートがグローバルな STL 設定を上書きし合うのを防ぎます。後から来たジョブは最大 3600 秒 `queued`（`result.json` の `queue=waiting_for_cad_lock`）のまま待ち、超過すると `CAD_BUSY` を返します。先着順は保証されません。実行プロセスは PID と開始時刻を登録し、プロセスが消えた、または 120 秒以内に登録されなかったジョブは `get_export_job` が `interrupted` とマークします。実行中のジョブが止められるのは停滞したときです：`stall_timeout_seconds`（既定 900 秒）の間、進捗も、ツールとその SolidWorks プロセスの CPU 時間も増えなかった場合です（大きな STL 1 つのような長いステップの間は SolidWorks が計算し続けるため停滞とはみなしません）。`timeout_seconds`（既定 14400 秒）は総時間の上限にすぎません。元のエクスポーター自身のログ（`bridge-export.log`）の増加も活動とみなします。ワーカーが一時停止（スリープ、スタンバイ、マシンのフリーズ）から再開したとき、その時間はアイドル時間に数えず、result.json の `paused_seconds` と `pauses` に記録します。停滞は `error.code=CAD_STALLED`、上限超過は `CAD_TIMEOUT` で終わり、いずれも `status=timed_out` で、メッセージに段階が示されます。`cancel` / `cancel_export_job` は待機中または実行中のジョブを止めます（`status=cancelled`）。どの場合も PID と開始時刻を照合して独立セッションをクリーンアップし、後述のとおり STL 設定を復元します。`timed_out`/`interrupted`/`cancelled` ジョブの出力を正式なモデルに使ってはいけません。

プライベートセッションは、ローカルの SolidWorks とユーザー設定を共有します。ネイティブコアは起動直後に STL 関連の設定を `output/preferences-snapshot.json` に保存します。ジョブがタイムアウトまたは失敗して復元が確認できなかった場合、ツールは別のプライベートセッションを起動して設定をそのスナップショットの状態に戻し、結果を `result.json` の `preference_restore` に記録します（`changed_keys` は実際に元へ戻した項目です）。ワーカーが予期せず終了したジョブは、`get_export_job` により復元待ちとマークされ、次の CAD ジョブの開始前に復元が実行されます。

出力は、元プロジェクトと同じ従来型の ROS URDF パッケージです。ROS 2 の起動スクリプト、MJCF/USD、自動モデリング、完全自動のジョイント推定は、このバージョンの対象外です。モデルは実際のターゲットのシミュレータで必ず検証してください。

## 回帰テストと出典

```powershell
.\.venv\Scripts\python.exe scripts\test_configuration.py
.\.venv\Scripts\python.exe scripts\test_validation.py
.\.venv\Scripts\python.exe scripts\test_jobs.py
.\.venv\Scripts\python.exe scripts\test_postprocess.py
.\.venv\Scripts\python.exe scripts\test_provenance.py
.\.venv\Scripts\python.exe scripts\test_cad_regressions.py
.\.venv\Scripts\python.exe scripts\test_nested_subassemblies.py
.\.venv\Scripts\python.exe scripts\test_tool_mcp.py
.\.venv\Scripts\python.exe scripts\test_registered_mcp.py
```

`test_cad_regressions.py` には SolidWorks が必要です。仮想部品を含むサンプルアームのコピーを作成し、仮想部品を含む Pack and Go、同じ原点を共有する関節、チェック専用モード、準備済みコピーの再利用、進捗記録、mimic/world 設定、誤ったコンポーネント名を示すエラーメッセージを検証します。`test_nested_subassemblies.py` も SolidWorks が必要です。ワークスペースのコピーを新しいアセンブリに入れ子にし、2 階層下のサブアセンブリを丸ごと列挙した Link をエクスポートして、全部品がメッシュに含まれることを確認します（サンプルアームと、ワークステーションの CAD がある場合は 48 個のバイアルのサブアセンブリを持つラック。パスは `SW_URDF_VIAL_RACK` で指定可能）。

元のエクスポートソースのスナップショット：882169e28952f0d17c87d7eab98826454421aabf（MIT）。`build/core-source-manifest.json` に上流の各ファイルのハッシュと改造マーカーが記録され、`build/core-source` で生成後のソースを確認できます。ソース準備のルールは `scripts/prepare-core.py` にあります。

以前ダウンロードした `solidworks_urdf_exporter2` は参考用として `vendor` に置いてありますが、このバージョンではエクスポートに使っていません。上流のライセンスは `vendor` に保存され、ローカルのネイティブビルドには `SW2URDF-LICENSE.txt` が同梱されます。

## ライセンス

MIT。[LICENSE](LICENSE) を参照してください（上流の ros/solidworks_urdf_exporter のライセンスは [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES) にあります）。
