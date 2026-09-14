# Game Packs

Game Pack は、AI Partner にゲーム固有の「現在の出来事」を伝えるためのローカル設定です。

公開版では、既存ゲームのシナリオファイルや抽出データを読み込ませる方式は提供していません。

利用するゲームやデータは、正規に入手し、利用権限のあるものを使用してください。各ゲーム、配信プラットフォーム、サービスの利用規約や権利者の条件にも従ってください。

## 配置場所

ローカル設定は `local_games/<id>/` に置きます。

```text
local_games/
  my_game/
    game.yaml
    runtime_state.json   # 自作ゲーム・許諾済みゲームで使用する場合
```

`local_games/` は `.gitignore` 対象です。

## 自作ゲーム・許諾済みゲーム: state_file

ゲーム側から `runtime_state.json` を更新できる場合は、画面認識に頼らず現在の出来事を直接通知できます。

例:

```json
{
  "events": [
    {
      "event_id": "choice-001",
      "event_type": "choice",
      "actor_scope": "user",
      "summary": "危険を承知で扉を開けた",
      "importance": 0.8,
      "signals": {
        "boldness": 0.9,
        "adventure": 0.8,
        "active_behavior": 0.7
      }
    }
  ]
}
```

`actor_scope: user` は、プレイヤー自身の行動であることを表します。

長期的な `trust` / `affection` / `respect` / `antipathy` は、原則としてプレイヤー自身の行動として明示されたイベントから変化します。

## 既存ゲーム: screen_ocr

正規に入手・利用しているゲームでは、`tracking.method: screen_ocr` を指定すると、Windows OCR で現在画面に表示されている文字を読み取れます。

公開版の `screen_ocr` は、シナリオデータとの照合には使用しません。

代わりに、`gameplay_rules` に「勝利」「敗北」「戦闘不能」など、現在画面に表示される文字パターンを設定し、ゲーム中の出来事として扱います。

```yaml
gameplay_rules:
  - pattern: 'GAME OVER|敗北|戦闘不能'
    event_type: defeat
    actor_scope: user
    summary: プレイヤーが敗北した
    importance: 0.7
    cooldown_seconds: 10
    signals:
      player_defeated: 1.0
      heavy_damage: 0.4
```

敗北だけで「信用できない」とは扱いません。標準設定では、敗北は主に実力評価や心配に影響し、大胆さ・一貫性・臆病さなどの行動シグナルは関係値へより直接影響します。

## game.yaml の例

`examples/game_pack/game.yaml` を参考に、対象ゲームのウィンドウやプロセスを設定してください。

Game Pack はゲーム本体やゲームデータを配布する仕組みではありません。ゲームファイルの抽出、不正入手したデータの利用、アクセス制限の回避を前提とした設定は、このプロジェクトの対象外です。
