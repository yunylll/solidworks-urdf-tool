# SolidWorks URDF 2026 Tool

[English](README.md) | [简体中文](README.zh-CN.md) | **日本語**

SolidWorks 2026 をバックグラウンドで操作し、アセンブリや部品を、そのまま使えて独立に検証済みの ROS URDF パッケージとしてエクスポートするツールです。コマンドライン、Python、MCP から使えます。CAD 操作はすべて非表示のプライベート SolidWorks セッションで、ソースモデルのコピーに対して行います。旧アドインのウィザード、マウス操作、computer-use は不要です。

エクスポートのコアは [ros/solidworks_urdf_exporter](https://github.com/ros/solidworks_urdf_exporter) のソースからビルドしています（コアバージョン 2.0.0）。ヘッドレス実行、SolidWorks 2026 API、エラー処理、座標系、大規模アセンブリに合わせて改修しており、インストール先の旧 SW2URDF.dll は読み込みません。

## 目次

- [主な機能](#主な機能)
- [動作環境とインストール](#動作環境とインストール)
- [クイックスタート](#クイックスタート)
- [コマンドリファレンス](#コマンドリファレンス)
- [JSON 設定](#json-設定)
- [結果と検証](#結果と検証)
- [CAD バージョンと前回のエクスポート](#cad-バージョンと前回のエクスポート)
- [大規模アセンブリ](#大規模アセンブリ)
- [ジョブの実行と復旧](#ジョブの実行と復旧)
- [公開とクリーンアップ](#公開とクリーンアップ)
- [ソースモデルの保護](#ソースモデルの保護)
- [Python](#python)
- [MCP](#mcp)
- [テスト](#テスト)
- [制限事項](#制限事項)
- [出典とライセンス](#出典とライセンス)

## 主な機能

| 項目 | 内容 |
|---|---|
| エクスポート | アセンブリと単一部品（1 Link・関節なし）。fixed、continuous、revolute、prismatic 関節と、形状を持たない座標フレーム Link。mimic 関節。world ルート変換（Z 上向き、接地、中央配置）。 |
| 設定 | 旧アドインがモデルに保存した Link/Joint 設定、または編集可能な JSON 設定：Link ツリー、コンポーネントの割り当て、座標系、関節の種類と軸、リミット、減衰と摩擦。 |
| 検証 | 質量、慣性、URDF の構造、ゼロ姿勢での Link 配置、メッシュと CAD 形状の一致、前回エクスポートとの差分。正面・側面・上面のプレビュー画像。 |
| ソースの保護 | ソースファイルは読み取りとハッシュ計算のみ。フォルダーをまたぐ依存ファイルをスナップショットし、参照を書き換えて Pack and Go。最後にハッシュを再確認し、SolidWorks の STL 設定を復元。 |
| ジョブ | 永続的なジョブ記録、バックグラウンド実行、段階ごとの進捗、待機、キャンセル、停滞検出、段階ごとの所要時間、古いジョブのクリーンアップ、プロジェクトフォルダーへの公開。 |
| 大規模アセンブリ | コンポーネントの一括選択、部品ファイルごとの質量特性、形状が変わらない場合のメッシュ再利用。3250 コンポーネントのワークステーションの初回エクスポートは約 24 分。 |
| インターフェース | コマンドライン `tool_cli.py`、Python `tool_service`、ローカル stdio MCP サーバー（10 ツール）。 |

検証記録は[ツールレポート](validation/TOOL_REPORT.ja.md)、初期のブリッジ方式の実現性テストは[初期レポート](validation/FEASIBILITY.ja.md)にあります。

## 動作環境とインストール

- Windows x64、SolidWorks 2026、.NET Framework 4.7.2 以降（Roslyn コンパイラーが必要とします）。
- Python 環境はインストールスクリプトがワークスペース内（`.venv`）に作成します。

初回インストール（Python 環境の作成、固定バージョンの依存パッケージのインストール、Microsoft Roslyn コンパイラーのダウンロード、ツールのビルド）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
```

C# のコードを変更した後は再ビルドのみ：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\build-tool.ps1
```

ビルドにはこのコンピューターの SolidWorks 2026 API DLL と、URDFExporter インストールフォルダーの MathNet、CsvHelper、log4net を使います。場所が異なる場合は `-SolidWorksDir` と `-ExporterDir` で指定してください。旧アドインがないコンピューターでは、以前のビルドで `build\bin` に残ったこの 3 つの DLL を使います。

ビルド結果は `build\bin\SolidWorksUrdf.exe`（エクスポートのコア）と `build\bin\SolidWorksProbe.exe`（各ジョブの前に使う読み取り専用のセッションプローブ）です。上流ソースへの各改修は一致回数を確認し、上流の変更で改修が合わなくなるとビルドが失敗します。コアは内部用のリクエスト JSON を受け取るので、通常は以下のコマンドライン、Python、MCP を使ってください。

## クイックスタート

ワークスペースのルートで実行します。

```powershell
# 1. モデルを調べる：コンポーネントを一覧し、編集可能な設定（configuration_path）を返す
.\.venv\Scripts\python.exe scripts\tool_cli.py inspect "C:\path\robot.SLDASM"

# 2. 設定を編集したら、まずチェックだけ行う：Link 座標系と関節で止まり、設定の誤りが数分でわかる
.\.venv\Scripts\python.exe scripts\tool_cli.py check "C:\path\robot.SLDASM" --config "C:\path\robot-config.json"

# 3. エクスポート。大きなモデルはバックグラウンドで実行して結果を待つ。--publish-to（任意）は合格後にパッケージをプロジェクトフォルダーへコピーする
.\.venv\Scripts\python.exe scripts\tool_cli.py start-export "C:\path\robot.SLDASM" --package robot_description --config "C:\path\robot-config.json" --publish-to "D:\robots\my_robot"
.\.venv\Scripts\python.exe scripts\tool_cli.py job <job_id> --wait 300
```

すべての検査に合格したことを示すのは `status=succeeded` かつ `passed=true` の場合だけです。`queued`、`running`、`timed_out`、`interrupted`、`cancelled`、「一部のファイルができた」状態は、いずれも成功ではありません。

## コマンドリファレンス

使い方：`.\.venv\Scripts\python.exe scripts\tool_cli.py <コマンド> [オプション]`

| コマンド | 内容 |
|---|---|
| `inspect <モデル>` | コンポーネントと保存済みの設定を一覧し、編集可能な `configuration_path` を返す |
| `prepare <モデル>` | 自己完結した CAD コピー（`reusable_model`）だけを作る |
| `check <モデル> --config <設定>` | チェックのみ：Link 座標系と関節で止まり、慣性もメッシュも出力しない |
| `export <モデル> [--package 名前] [--config 設定]` | エクスポートして完了を待つ。`--config` がなければモデルに保存された設定を使う |
| `start-export …` | `export` と同じだがバックグラウンドで実行し、すぐに `job_id` を返す。`--check-only` でチェックのみ |
| `job <job_id> [--wait 秒]` | ジョブを表示。`--wait` は終了または時間切れで戻る |
| `cancel <job_id>` | 待機中または実行中のジョブをキャンセル |
| `validate-config <設定> [--model モデル]` | SolidWorks を起動せずに設定を検査。`--model` を付けると、モデルの現在の CAD バージョン用の設定かも判定 |
| `validate-urdf <urdf> [--reference 参照] [--previews]` | 任意の場所の URDF パッケージを検証。`--reference` は 2 つのエクスポートを厳密に比較、`--previews` は 3 方向の画像を作成 |
| `cleanup-jobs …` | 古いジョブの CAD スナップショットを一覧（`--delete` で削除）。[公開とクリーンアップ](#公開とクリーンアップ)を参照 |

CAD コマンド（inspect、prepare、check、export、start-export）共通のオプション：

| オプション | 既定値 | 内容 |
|---|---|---|
| `--timeout` | 14400 | 総時間の上限（秒） |
| `--stall-timeout` | 900 | 進捗も CPU 活動もないまま何秒で停滞とみなすか。[ジョブの実行と復旧](#ジョブの実行と復旧)を参照 |
| `--use-saved-files` | オフ | モデルが SolidWorks で開かれて未保存の変更があっても、ディスクに保存されたファイルを使う |

check、export、start-export の追加オプション：

| オプション | 内容 |
|---|---|
| `--config` | JSON 設定ファイル |
| `--accept-source-change` | 設定が別の CAD バージョン用でも、設定どおりの関節でエクスポートする |
| `--kinematics-tolerance-mm` / `--kinematics-tolerance-deg` | コンポーネント位置や再計算した関節を比較する許容差。既定 0.1 mm / 0.1° |
| `--write-config-source` | 合格後、検証した CAD バージョン（`source` ブロック）を設定ファイルに書き戻す |
| `--recompute-preflight` | 設定が古い場合、まず別セッションで現在の CAD から関節を再計算して比較する |

export、start-export の追加オプション：

| オプション | 内容 |
|---|---|
| `--package` | ROS パッケージ名（小文字・数字・アンダースコア）。既定 `robot_description` |
| `--reference` | 一致しなければならない参照 URDF。差があれば失敗 |
| `--no-compare-previous` | 前回成功したエクスポートと比較しない |
| `--no-reuse-meshes` | すべての Link メッシュを出力し直す |
| `--publish-to` | 合格後にこのフォルダーへ公開する。[公開とクリーンアップ](#公開とクリーンアップ)を参照 |

## JSON 設定

まず inspect を実行し、返された `configuration_path` を編集します。実行可能な例は [arm-config.json](examples/arm-config.json) と[リミットと座標フレームを含む設定](examples/arm-custom-config.json)です。

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

トップレベルのフィールド：

| フィールド | 内容 |
|---|---|
| `schema_version` | 1 であること |
| `robot_name` | ロボット名 |
| `recompute_kinematics` | true：CAD から関節を再計算、false：JSON の関節原点と軸を使う（後述） |
| `links` | Link の一覧。ルート（`parent` が null）はちょうど 1 つ |
| `world` | 任意。ルートの上に world Link を追加（後述） |
| `exclude_hidden_bodies` | 任意。非表示ボディを質量に含めない部品ファイル名（後述） |
| `source` | ツールが書き込む：設定を検証した CAD バージョン。[CAD バージョンと前回のエクスポート](#cad-バージョンと前回のエクスポート)を参照 |

Link のフィールド：`name`、`parent`、`components`（inspect が示す完全なインスタンス名。例 `gantry_asm-1/gantry_x1_asm-1/gantry_x1_p03-2`）、`coordinate_system`、`mesh_quality`（`coarse` または `fine`）、`frame_only`、`joint`。関節のフィールド：`name`、`type`、`axis_name`、`axis`、`xyz`、`rpy`、`lower`、`upper`、`effort`、`velocity`、`damping`、`friction`、任意の `mimic`。

### 関節と座標系

- **`recompute_kinematics=true`**：設定で指定した CAD の座標系と軸から関節変換を再計算します。`"Automatically Generate"` の場合は元のエクスポーターが合致から推定します。推定は親 Link を固定したときの各子 Link の**最初のコンポーネント**の残り自由度だけを見るため、フレキシブルなサブアセンブリ内部や他の Link を経由する合致は考慮されません。推定した関節の種類が JSON の `type` と異なると、黙って fixed にはせず、全関節を示してエクスポートを失敗させます。
- **`recompute_kinematics=false`**：JSON のローカルな `xyz`、`rpy`、`axis` を使います（`axis` は子 Link の座標系）。`coordinate_system` が `"Automatically Generate"` の Link では、関節チェーンから CAD コピー内に座標系 `Origin_<関節名>` を作り、その Link 自身の座標系でメッシュと慣性を出力します。既存の座標系を指定した Link は JSON の原点と一致している必要があり、一致しなければその座標系の xyz/rpy を示して失敗します。continuous と revolute 関節の `xyz` は回転軸上に置いてください。
- **ルート Link**：`coordinate_system` が `"Automatically Generate"` の場合は元のエクスポーターの `Origin_global` を使います。これは Y 上向きのモデルを想定し、CAD の +Y を URDF の +Z に変換します。Z 上向きでモデリングしたアセンブリでは `"Assembly Origin"` を使い、アセンブリの原点と軸をそのまま使ってください。
- **リミット**：revolute と prismatic 関節には有限の `lower`/`upper` と正の `effort`/`velocity` が必要です。角度はラジアン、並進はメートルです。
- **`frame_only=true`**：その Link は質量、ビジュアル、コリジョンの形状を出力しません。子孫は通常どおり処理します。
- 座標系と参照軸はモデルにある参照ジオメトリから取ってください。ツールが実機の関節の意図やアクチュエーターのパラメーターを推測することはありません。

### 質量と慣性

- 質量、重心、慣性は SolidWorks のコンポーネント質量特性から取ります。元のエクスポーターは各ボディの慣性を単純に足すため、複数部品からなる Link では出力メッシュから計算した慣性より 3〜5 倍小さくなり、質量も形状と密度から計算するため SolidWorks でのオーバーライドが失われます。コンポーネント質量特性はメッシュと一致し、オーバーライドも含みます。
- ツールは、オーバーライドのない Link で両方の計算の質量と重心が一致し、Link の質量の合計がアセンブリ質量に等しいことを確認してから、エクスポーターの値を置き換えます。そうでなければエクスポーターの値を残し、`warnings` で知らせます。オーバーライドした質量を使った Link とコンポーネントは `warnings` と `bridge.massOverrides` に、エクスポーター自身の慣性は `bridge.exporterInertia` に残ります。
- **`exclude_hidden_bodies`**（トップレベル）：部品ファイル名のリスト（例 `["gantry_z1_p05.SLDPRT"]`）。これらの部品の非表示ソリッドボディを質量・慣性とメッシュ検査の CAD 体積から除きます。合致が参照するミラー元ボディなど、CAD に必要な非表示ボディに使います（削除すると合致が壊れます）。SolidWorks はアセンブリ質量に非表示ボディを含めるため、質量の検査では除いた分を差し引きます。`warnings` と `bridge.excludedHiddenBodies` に Link ごとの除外質量を示します。どのコンポーネントのファイルでもない名前は直ちにエラーとなり、似た名前を示します。SolidWorks がアセンブリ内で計量するインスタンス（`bridge.partMassCheck` を参照）は非表示ボディを含んだままで、未適用として示します。

### エクスポート後の処理

次の 2 つは、エクスポーターが URDF を書き出した後にツールが加えます。エクスポーターの元ファイルはジョブフォルダーの `native.urdf` に残り、変更内容は `postprocess` に記録されます。

- **`joint.mimic`**：`{"joint": "<追従する関節>", "multiplier": 1, "offset": 0}` が URDF の `<mimic>` 要素になります（対称グリッパーのもう一方の指など）。両方の関節が可動である必要があり、mimic 関節が別の mimic 関節に追従することはできません。
- **`world`**（トップレベル）：ルート Link の上にルート Link（既定 `world`）と固定関節（既定 `world_to_base`）を追加します。`up_axis`（`+z`、`-z`、`+y`、`-y`、`+x`、`-x`）でモデルのどの軸を URDF の +Z にするかを指定するか（Y 上向きのモデルは `+y`）、`rpy` で回転を直接与えます。`ground: true` はメッシュの最下点を z=0 に、`center_xy: true` はメッシュの範囲を x=y=0 の中央に置き、どちらも全関節ゼロでの出力メッシュから計算します。`xyz` でさらにオフセットを加えます。例：`"world": {"up_axis": "+y", "ground": true, "center_xy": true}`。

### 設定の検査

- モデルに保存された設定がない（または保存された設定が存在しないコンポーネントを参照している）場合、inspect はすべてのトップレベルコンポーネントを 1 つの `base_link` にまとめたスケルトン設定（`configuration_source=template`）を返します。検査に合格し、1 つの剛体としてエクスポートできるので、ここから可動 Link を分けていきます。
- 検査では、循環または切断された Link ツリー、コンポーネントの重複割り当て、誤った軸の長さ、無効なリミット、有限でない数値を拒否し、Link または関節、期待値、実際の値を示します。
- inspect 結果の `unassigned_components` はどの Link にも割り当てられていないソリッド部品、`overlapping_components` は直接と親サブアセンブリ経由の両方で割り当てられたコンポーネントです。どちらかが空でなければ `configuration_ready=false` となり、エクスポートはメッシュを作る前に、コンポーネント名を示して失敗します。
- サブアセンブリを丸ごと Link に列挙することもできます。その場合、抑制とエンベロープ以外の全部品を（質量と同じく非表示部品も含めて）出力・計量します。結果の `subassembly_components` にこうしたコンポーネントを警告付きで示し、`expanded_config_path` はその部品を個別に列挙した設定です。`recompute_kinematics=true` のとき、子 Link の先頭にあるサブアセンブリは関節の検出に使うため、そのまま残します。
- JSON 設定は上記の Link/Joint フィールドを扱います。旧設定の外観設定はまだ JSON から編集できません。

## 結果と検証

各操作は `validation\jobs\<job_id>\` を作成します：

| ファイル | 内容 |
|---|---|
| `result.json` | 状態、`passed`、エラー、警告、ソースのハッシュ、各検査、開始・終了時刻、`duration_seconds` と `stage_durations` |
| `request.json`、`robot-config.json` | ジョブのリクエストと使用した設定 |
| `native.urdf` | エクスポーターの元の URDF（後処理の前） |
| `output\<package>\` | URDF パッケージ：`urdf\`、`meshes\`、`previews\`。URDF の絶対パスは `bridge.urdf` |
| `output\cad\` | 自己完結した CAD コピー（`reusable_model`） |
| `mesh-keys.json` | 合格したエクスポートの各 Link メッシュの指紋（以後のエクスポートで再利用） |
| `stdout.log`、`stderr.log` | ネイティブのログ。`stderr.log` には段階ごとのタイムスタンプ |

古いジョブや出力は上書きしません。エクスポート後に次の検査を行い、どれかが失敗すると `passed=false` になります：

| 検査 | 失敗の条件 |
|---|---|
| URDF の構造 | ルートを持つ木になっていない、関節や Link の名前の重複、単位ベクトルでない軸、無効なリミット、有限でない数値 |
| 質量と慣性 | 正でない質量や物理的でない慣性。Link の質量の合計が CAD のアセンブリ質量と一致しない |
| メッシュと CAD（`validation.cad_geometry`） | 列挙した部品、または丸ごと列挙したサブアセンブリの部品の CAD 境界ボックス内にメッシュ頂点がない。メッシュ体積が CAD 体積の 85〜115 % の外（95〜105 % の外は警告。多くはテッセレーション誤差） |
| ゼロ姿勢での Link 配置 | 子 Link のメッシュの範囲が親からロボット寸法の 0.5 %（最小 2 mm）より離れている。`"Automatically Generate"` で回転したルート座標系などを検出。各 Link のワールド範囲は `validation.link_world_bounds_m` |
| 参照との比較 | `--reference` 使用時の数値の差。メッシュと関節原点の不一致は次節を参照 |

メッシュ検査が必要なのは、質量をメッシュではなく CAD コンポーネントから取るため、STL から部品が抜けても質量は正しいままだからです。失敗時には、Link と欠けている可能性が高い部品を示します。体積の差が部品内の非表示ボディ（質量には入るが STL には入らない）で説明できる場合は、ボディ名と体積を個別に示します。旧バージョンでは、別のサブアセンブリの中にあり丸ごと列挙されたサブアセンブリ（ワークステーションのラック上のバイアルなど）の部品が一つも出力されませんでした。そのような設定で作ったエクスポートはやり直してください。

各エクスポートは、パッケージの `previews\` に `front.png`、`side.png`、`top.png`（+X、-Y、+Z から見た図、Link ごとに色分け）も書き出し、パスは `previews.images` に記録されます。使う前に必ず確認してください。親の内部の点まわりの回転では、2 つの範囲が接したまま気づかれないことがあります。`validate-urdf --previews` と MCP の `validate_urdf(render_previews=true)` で任意の URDF の画像も作れます。

## CAD バージョンと前回のエクスポート

`recompute_kinematics=false` の設定は、ある CAD バージョンから写した絶対的な関節原点を持っています。後でアセンブリが、モデルをアセンブリ原点に対して動かした状態で保存されると、メッシュは CAD に従って動き関節原点は動きませんが、新しい URDF は単独ではすべての検査に合格してしまいます。次の 2 つの検査でこれを見つけます。

**設定の CAD バージョン。** JSON 設定を使った check または export が合格すると、検証した CAD バージョンを記録します。ジョブの `config_with_source_path` は `source` ブロック付きの設定で、`model_sha256`、`model_path`、`exported_at`、`job_id`、`operation`、`link_frames`（ルート Link 座標系での各 Link 座標系）、`component_poses`（各 Link の最初のコンポーネントのアセンブリ内の姿勢）を含みます。`--write-config-source` はこのブロックを設定ファイル自体に書き戻します（ジョブ中にファイルが変更されていれば書きません）。

- `source` ブロックのない設定は、同じモデルで同じ関節原点を使った過去のジョブから推定します（`config_source.origin=inferred_from_job`）。
- そのバージョンがモデルの現在の SHA-256（再利用コピーでは元のモデル）と異なると、`warnings` に示し `config_stale=true` になります。`validate-config --model` は SolidWorks なしで同じ判定をします。
- その後エクスポートは、同じセッション内で Link 座標系やメッシュを作る前に、各 Link の最初のコンポーネントの姿勢を記録値と比較します（数秒）。並進または回転が `--kinematics-tolerance-mm` / `--kinematics-tolerance-deg` を超えると `CONFIG_OUTDATED` で失敗し、各 Link の新旧の位置と、すべてが同じ剛体並進ならそのベクトルを示します（詳細は `component_pose_check`）。姿勢を記録していない設定には警告のみを出し、下記の前回エクスポートとの比較に任せます。
- `--recompute-preflight` は代わりに、まず別のチェック専用セッション（`preflight\`）で現在の CAD から `"Automatically Generate"` の関節を再計算して原点と軸を比較し、`CONFIG_OUTDATED` または `KINEMATICS_PREFLIGHT_FAILED` で失敗します。
- `--accept-source-change` はこれらの検査を省き、設定どおりの関節でエクスポートします。

**前回のエクスポート。** `--reference` も `--no-compare-previous` も指定しない限り、エクスポートは同じモデル（なければ同じパッケージ名）の最新の成功したエクスポート（`previous_export`）と比較されます。差は `validation.reference_changes` に列挙して `warnings` に要約し、失敗にはしません。`validation.reference_motion` は各メッシュ（範囲の中心と重心）と各関節原点の移動量を、メッシュを持つ最上位の Link の座標系で示します。

- すべてのメッシュが同じベクトルだけ動いた場合、revolute と continuous 関節の原点もそれに合わせて動いていなければなりません。自身の軸方向のずれは運動に影響しないので数えません（軸に垂直な残差は `reference_motion.rotation_axis_offset_mm`）。0.5 mm を超えて取り残された軸があると `Mesh/joint-origin mismatch` で失敗し、そのベクトル、取り残された各軸とその距離、追従した軸を示します。したがって一部の軸だけ直した設定も失敗します。
- 動いていない prismatic と fixed 関節の原点は Link 座標系の位置を変えるだけなので変更として列挙し、`--accept-source-change` では警告になります。
- 明示的な `--reference` は常に厳密な比較で、差があれば失敗します。

## 大規模アセンブリ

lab_workstation ワークステーション（3250 コンポーネント、19 Link）での参考値：

| 操作 | 所要時間 |
|---|---|
| 初回の完全なエクスポート | 約 24 分（改善前は約 57 分） |
| 全メッシュを再利用した再エクスポート | 約 17 分 |
| チェックのみ（`check`、`recompute_kinematics=true`、19 関節を再計算） | 約 8 分 |

- **チェックのみ。** `check`（MCP：`check_only=true`）はエクスポートと同じ手順で Link 座標系と関節まで進んで止まり、質量特性も STL も作りません。結果には `resolved_joints`（各関節の種類、原点、軸）、`link_frames`、`recompute_kinematics=true` なら `detected_joint_types` が含まれます。コンポーネント名の誤り、関節原点と合わない座標系、想定外の関節の種類が数分でわかります。
- **準備済みコピーの再利用。** Pack and Go を実行した inspect、prepare、check、export はすべて `reusable_model` を返します。ファイルハッシュの一覧付きの自己完結した CAD コピーで、エクスポートが失敗しても使えます。次回これをモデルのパスとして渡すと、ハッシュを確認しフォルダーをコピーしてそのまま開き、元モデルの読み取り専用での読み込みと Pack and Go を省きます。
- **メッシュの再利用。** 合格したエクスポートは `mesh-keys.json` に各 Link メッシュの指紋を記録します。表示される各部品ファイルのハッシュと構成（インポート部品と仮想部品はその形状）、アセンブリ内での位置、メッシュ品質、ドキュメント全体の大きさ（SolidWorks は粗・細の公差をここから決めます）、Link の CAD 体積（アセンブリでのカット形状でも変わります）から作ります。以後のエクスポートでは指紋が同じ Link の STL をそのまま使い、関節原点だけを変えた場合は STL を新しい Link 座標系へ剛体移動します。再利用したメッシュもメッシュと CAD の検査を同じように通り、`reused_meshes` に示されます。`--no-reuse-meshes`（`reuse_meshes=false`）で無効にできます。
- **コンポーネントの選択。** SolidWorks ではコンポーネントを 1 つずつ選択に追加すると、既に選択した数に比例して時間がかかります（800 部品で 194 秒）。メッシュ出力での表示・非表示と、関節再計算での固定・固定解除は、一度の呼び出しで選択します（0.9 秒）。
- **質量特性。** 各部品ファイルを自身のドキュメントで 1 回だけ計量し（部品内のオーバーライドと非表示ボディを含む）、各インスタンスの Link 座標系での姿勢へ回転・移動して平行軸の定理で正確に合成します。各ファイルはまず 1 つのインスタンスでアセンブリ内の SolidWorks の計量結果と照合し（アセンブリでコンポーネントに材料を割り当てた場合などは一致しません）、一致しないファイル、オーバーライドのあるサブアセンブリ、非アクティブ構成のコンポーネントは引き続き SolidWorks が計量します（`bridge.partMassCheck`）。質量のオーバーライドは各ドキュメントのオーバーライド設定から見つけ、該当するコンポーネントだけを計量し直します。
- **進捗と所要時間。** `job` / `get_export_job` は `progress` を返します：現在の段階（`export_meshes` など）、要約（`Exporting STL meshes: 7/18 (gripper_z)` など）、ステップ数、経過時間、段階内の経過時間、SolidWorks の CPU 時間、最後の活動からの `idle_seconds`、ログの末尾数行。終了したジョブの `result.json` には `duration_seconds` と `stage_durations`（コンピューターが一時停止していた時間を除く）があります。失敗時は `error.stage` が段階を示します。

## ジョブの実行と復旧

- **キュー。** CAD ジョブは 1 つずつ実行し、エクスポート同士がグローバルな STL 設定を上書きし合うのを防ぎます。後から来たジョブは最大 3600 秒 `queued`（`queue=waiting_for_cad_lock`）で待ち、超えると `CAD_BUSY` を返します。先着順は保証されません。
- **ワーカー。** ワーカーは PID と開始時刻を登録します。ワーカーが消えた、または 120 秒以内に登録されなかったジョブは、`get_export_job` が `interrupted` にします。
- **停滞とタイムアウト。** 実行中のジョブを止めるのは停滞したときだけです：ワーカーとその SolidWorks プロセスに `stall_timeout_seconds`（既定 900 秒）の間、進捗の更新も CPU 時間の増加もない場合です。元のエクスポーター自身のログ（`bridge-export.log`）の増加も活動とみなし、大きな STL 1 つの出力のような長い処理の間 SolidWorks が計算し続けていれば停滞ではありません。`timeout_seconds`（既定 14400 秒）は総時間の上限にすぎません。停滞は `error.code=CAD_STALLED`、上限超過は `CAD_TIMEOUT` で終わり、いずれも `status=timed_out` で、メッセージに段階を示します。
- **スリープとスタンバイ。** ワーカーが一時停止（スリープ、スタンバイ、マシンのフリーズ）から再開したとき、その時間はアイドル時間にも上限にも数えず、`paused_seconds` と `pauses` に記録します。任意の再計算プリフライトは 2 つ目の SolidWorks セッションで、別に `--timeout` で計時します。
- **キャンセル。** `cancel` / `cancel_export_job` で待機中または実行中のジョブを止めます（`status=cancelled`）。
- **後片付けと設定。** ジョブがどう終わっても、PID と開始時刻を確認してプライベートセッションを閉じます。プライベートセッションはこのコンピューターの SolidWorks とユーザー設定を共有します。ネイティブのコアは起動直後に STL 関連の設定を `output\preferences-snapshot.json` に保存し、ジョブがタイムアウトや失敗で設定の復元を確認できなかった場合は、別のプライベートセッションで復元して `preference_restore`（`changed_keys` は実際に戻した項目）に記録します。ワーカーが異常終了したジョブは復元待ちとしてマークし、次の CAD ジョブの開始前に復元します。
- `timed_out`、`interrupted`、`cancelled` のジョブの出力は使わないでください。

## 公開とクリーンアップ

**公開。** `--publish-to <フォルダー>`（`publish_to`）は、エクスポートが `status=succeeded` かつ `passed=true` で終わったときだけ動作します：

- URDF パッケージを `<フォルダー>\urdf\<package>\` にコピー。
- ジョブの `robot-config.json`（なければ `configuration.json`）、`request.json`、`result.json` を `<フォルダー>\export\export-config.json`、`export-request.json`、`export-result.json` としてコピー。
- 既にあるファイルやパッケージフォルダーは、先に `<フォルダー>\export\history\<タイムスタンプ>_job-<job_id の先頭 12 文字>\` へ移動し、決して上書きしない。
- コピーごとにハッシュを確認し、`result.json` の `publish` にパス、ハッシュ、履歴へ移したファイルを記録。

公開に失敗しても `passed` は変わらず、理由は `publish.error` に残ります。

**クリーンアップ。** ワークステーション全体のジョブ 1 つは約 650 MB で、ほとんどが `output\cad` の CAD スナップショットです。

```powershell
# 一覧のみ（dry run）：30 日より古いジョブ、またはモデルとパッケージごとに最新 3 つ以外
.\.venv\Scripts\python.exe scripts\tool_cli.py cleanup-jobs --older-than-days 30
.\.venv\Scripts\python.exe scripts\tool_cli.py cleanup-jobs --keep-latest 3 --delete
```

- `--older-than-days`、`--keep-latest`、`--job` のいずれかでジョブを選ぶ必要があります。どれもないとコマンドは `INVALID_INPUT` で拒否されます。
- `--delete`（MCP / Python：`dry_run=false`）がなければ、削除するスナップショット（`output\cad`、`preflight\output\cad`、`--include-logs` ならログ）と解放できるバイト数を一覧するだけです。
- 常に残すもの：`result.json`、`request.json`、各設定、`native*.json`、`mesh-keys.json`、URDF パッケージ（以後のエクスポートがメッシュを再利用します）。
- 対象外：待機中または実行中のジョブ。残すジョブが `model_path`、`reusable_model`、ソースの一覧として使っているスナップショット。各モデルの最新の成功したエクスポートのスナップショット（`--no-keep-latest-success` を除く）。
- クリーンアップしたジョブは `result.json` に `cleaned_at` と `cleanup` を記録し、`reusable_model` は `reusable_model_removed` になります。残すジョブに同じモデル・同じ CAD バージョン（`cad_version_sha256`）のスナップショットがあれば、`equivalent_snapshot` と `reusable_model_hint` がそれを指します。

## ソースモデルの保護

- **ソースは読み取りのみ。** ソースファイルは読み取りとハッシュ計算だけを行います。ツールはまずソースモデルと依存ファイルをコピーし、参照の書き換え、Pack and Go、エクスポートはスナップショットに対してだけ行います。API がパック時にモデルを保存しても、保存されるのはスナップショットです。最後にすべてのソースのハッシュを再確認します（`source_unchanged`）。
- **見つからない参照。** 保存された参照がこのコンピューターにないファイル（古い構成、インポート元ファイル、ライブラリ部品など）を指していても、先に SolidWorks でモデルを開く必要はありません。スナップショットはそれらのパスを飛ばして作り、開いた後で抑制されていないすべてのコンポーネントが読み込まれたかを確認します。ファイルがないのが有効なコンポーネントの場合だけ、コンポーネント名とパスを示して失敗します。飛ばした参照とファイルのない抑制コンポーネントは `warnings` に記録します。
- **SolidWorks で開いているモデル。** アセンブリが SolidWorks で開かれている場合、アクティブなモデルの読み取り専用の一覧を読み、スナップショットの構成名、コンポーネント数、質量が一致することを確認します。開いているモデルに未保存の変更があるとジョブは `UNSAVED_CHANGES` で失敗します。保存するか保存せずに閉じてから再実行するか、`--use-saved-files` でディスク上の保存済みファイルをエクスポートしてください（`warnings` にその旨が記録されます）。

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

パッケージ名より後のオプション（`config_path`、`reference_urdf`、`timeout_seconds`、`stall_timeout_seconds`、`use_saved_files`、`accept_source_change`、`kinematics_tolerance_mm`、`kinematics_tolerance_deg`、`compare_previous`、`write_config_source`、`recompute_preflight`、`reuse_meshes`、`publish_to`）はキーワード引数のみです。2 つのパス引数を位置で取り違えないためです。

## MCP

サーバーは `scripts\mcp_server.py` で、ワークスペースの Python で起動します。他の stdio クライアントは [mcp-connection.example.json](mcp-connection.example.json) を参考に、`<REPO_ROOT>` をこのリポジトリのパスに置き換えてください。起動タイムアウト 60 秒、ツールのタイムアウト 1200 秒を推奨します。公開ポート、OAuth、API キーは不要です。Codex に登録した後は MCP 設定の再読み込みが必要です（設定の MCP servers で接続を再起動するか、クライアントを再起動して `/mcp` を確認）。[MCP 設定のドキュメント](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)を参照してください。

| ツール | 内容 |
|---|---|
| `inspect_solidworks` | SolidWorks のバージョンとアクティブなドキュメントを変更せずに読み取る |
| `inspect_model_configuration` | モデルを調べ、コンポーネント、編集可能な設定、未割り当てのコンポーネントを返す |
| `prepare_model` | 自己完結した CAD コピーを作る |
| `validate_configuration` | SolidWorks なしで設定を検査。CAD バージョンの確認用に任意の `model_path` |
| `export_urdf` | エクスポート（`check_only=true` ならチェックのみ）して結果を待つ |
| `start_urdf_export` | バックグラウンドでエクスポートを始め、すぐに `job_id` を返す |
| `get_export_job` | ジョブを表示。`wait_seconds` は終了すると直ちに戻る |
| `cancel_export_job` | ジョブをキャンセル |
| `validate_urdf` | 任意の URDF を検証。任意で `reference_urdf` と `render_previews` |
| `cleanup_jobs` | 古いジョブのスナップショットをクリーンアップ。既定は `dry_run=true` |

`export_urdf` と `start_urdf_export` はコマンドラインと同じオプションを受け付けます：`check_only`、`use_saved_files`、`timeout_seconds`、`stall_timeout_seconds`、`compare_previous`（既定 true）、`accept_source_change`、`kinematics_tolerance_mm`、`kinematics_tolerance_deg`（既定 0.1）、`write_config_source`、`recompute_preflight`、`reuse_meshes`（既定 true）、`publish_to`。

大きなモデルでは `start_urdf_export` を使い、`wait_seconds`（例 300）付きで `get_export_job` を呼びます。呼び出しはジョブが終わるまで待ち、終端状態になると直ちに戻るので、クライアントは shell の sleep も頻繁なポーリングも不要です。ブロッキングするツール（`export_urdf`、`inspect_model_configuration`、`prepare_model`）もバックグラウンドジョブとして動き、最大 1000 秒待ちます（環境変数 `SW_URDF_SYNC_WAIT_SECONDS` で調整でき、クライアントのツールタイムアウトより短くしてください）。その時点で終わっていなければ `still_running=true` と `job_id` を返し、ジョブは続行します。

## テスト

```powershell
.\.venv\Scripts\python.exe scripts\test_configuration.py
```

| テスト | SolidWorks | 内容 |
|---|---|---|
| `test_configuration.py` | 不要 | 設定の検査、後処理のフィールド、サブアセンブリの展開、`exclude_hidden_bodies` |
| `test_validation.py` | 不要 | 検証が壊れたエクスポートを見つけること：メッシュの欠落、単位の誤り、関節の向き、ゼロ姿勢での配置、メッシュと CAD、非表示ボディ |
| `test_postprocess.py` | 不要 | mimic と world の後処理 |
| `test_jobs.py` | 不要 | キュー、ワーカーの生存確認、停滞と一時停止、キャンセル、進捗、読み取り中の JSON ファイル、メッシュ再利用の候補、所要時間 |
| `test_provenance.py` | 不要 | 設定の由来と CAD バージョン。`scripts\testdata\provenance-jobs` の合成ジョブで実行し、`private-fixture.json` があれば実際のジョブでも実行 |
| `test_maintenance.py` | 不要 | 公開とクリーンアップ |
| `test_cad_regressions.py` | 必要 | 仮想部品を含む Pack and Go、原点を共有する関節、チェック専用モード、準備済みコピーの再利用、進捗、mimic/world、誤ったコンポーネント名を示すエラー |
| `test_nested_subassemblies.py` | 必要 | 2 階層下のサブアセンブリを丸ごと列挙したときのメッシュの完全性、分割と一括の質量特性の一致、メッシュの再利用と移動。ワークステーションの CAD があればラックと `exclude_hidden_bodies` も（パスは `SW_URDF_VIAL_RACK` で指定） |
| `test_tool_mcp.py`、`test_mcp.py` | 必要 | MCP ツールのエンドツーエンド |
| `test_registered_mcp.py`、`test_mcp_smoke.py` | 必要 | 登録済みの MCP サーバー。`test_mcp_smoke.py` は開いている SolidWorks セッションが必要 |

## 制限事項

- 出力は従来の ROS URDF パッケージです。ROS 2 の起動ファイル、MJCF/USD、自動モデリング、完全自動の関節推定は対象外です。エクスポートしたモデルは対象のシミュレーターでも検証してください。
- 関節の自動推定は各子 Link の最初のコンポーネントだけを見ます（[関節と座標系](#関節と座標系)を参照）。
- 旧設定の外観設定はまだ JSON から編集できません。

## 出典とライセンス

- 上流ソースのスナップショット：ros/solidworks_urdf_exporter `882169e28952f0d17c87d7eab98826454421aabf`（MIT）。`build\core-source-manifest.json` に各上流ファイルのハッシュと改修の有無を記録し、生成したソースは `build\core-source` で確認できます。改修の規則は `scripts\prepare-core.py` にあります。
- `vendor` の solidworks_urdf_exporter2 は参考用に残しているだけで、エクスポートには使いません。上流のライセンスは `vendor` に保持し、ローカルビルドごとに `SW2URDF-LICENSE.txt` としてコピーします。
- 本ツールは MIT ライセンスです（[LICENSE](LICENSE)）。上流のライセンスは [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES) を参照してください。
