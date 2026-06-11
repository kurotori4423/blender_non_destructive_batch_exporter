# Kurotori Non-Destructive Batch Exporter

Blender用の非破壊バッチFBXエクスポーターです。

UIで設定したジョブを、現在開いているBlender内では直接処理せず、一時コピーした `.blend` を別プロセスの `blender --background` で開いて前処理とFBX出力を行います。

## 対応バージョン

Blender 4.5.7 LTS 以降。

## 主な機能

- 複数のエクスポートジョブ管理
- ジョブ対象メッシュの指定
- 複数メッシュを指定名のメッシュへ結合
- Action + Frame をレストポーズとして出力
- Armature以外のモディファイアをShape Key保持付きで適用
- 出力対象だけに全トランスフォーム適用
- 主要なFBX設定のUI化

## Headless方式

エクスポート実行時、アドオンは現在の `.blend` を一時フォルダへコピーし、以下のような形で別Blenderプロセスを起動します。

```powershell
blender --background source.blend --python export_worker.py -- --job-json job.json
```

前処理は一時コピー側で行われるため、元のBlenderセッションのオブジェクト、Shape Key、モディファイア、ポーズ、選択状態は変更されません。

## 使い方

1. `.blend` を保存します。
2. 3D Viewportのサイドバーから `ND Exporter` タブを開きます。
3. `Add Job` でジョブを追加します。
4. 出力先、ファイル名、アーマチュア、Action、Frameを設定します。
5. 出力したいメッシュを選択し、`Export Meshes > Set Selected` を押します。
6. メッシュ結合が必要な場合は `Merge Groups` を追加し、結合後名と対象メッシュを設定します。
7. ジョブ右上のエクスポートボタンを押します。

## 制限事項

- 未保存の `.blend`、または未保存変更がある状態ではエクスポートを開始しません。
- Shape Keyごとの評価結果で頂点数が一致しない場合、そのジョブは失敗します。
- Armatureモディファイアは適用せず、スキニングとしてFBXに残します。
- Shape Key保持付きモディファイア適用では、Viewportで有効な非Armatureモディファイアだけを対象にします。
