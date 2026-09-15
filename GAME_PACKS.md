# Game Packs

Game Pack は、AI Partner にゲーム中の「現在の出来事」を伝えるためのローカル設定です。

設定は `local_games/<id>/` に置きます。

```text
local_games/
  my_game/
    game.yaml
    runtime_state.json
```

`local_games/` は Git 管理対象外です。

## 画面の文字から出来事を検出する

`tracking.method: screen_ocr` を指定すると、Windows OCR で現在画面に表示されている文字を読み取れます。

`gameplay_rules` に文字パターンを設定すると、その文字が画面に出た時にゲーム中の出来事として扱えます。

例:

```yaml
tracking:
  method: screen_ocr
  interval_seconds: 0.9

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

  - pattern: 'VICTORY|勝利|戦闘に勝利'
    event_type: victory
    actor_scope: user
    summary: プレイヤーが戦闘に勝利した
    importance: 0.65
    cooldown_seconds: 8
    signals:
      victory: 1.0
```

同じ文字が連続して表示された時に何度も反応しないよう、`cooldown_seconds` で待ち時間を設定できます。

## ゲーム側から直接出来事を通知する

ゲーム側から `runtime_state.json` を更新できる場合は、現在の出来事やプレイヤー行動を AI Partner へ直接通知できます。

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

長期的な `trust` / `affection` / `respect` / `antipathy` は、プレイヤー自身の行動として扱われたイベントから変化します。

## game.yaml の例

`examples/game_pack/game.yaml` を `local_games/my_game/game.yaml` にコピーして使えます。

ゲームを判定するには、`match` に対象ウィンドウや実行ファイル名を設定します。

```yaml
match:
  process_names:
    - game.exe
  window_patterns: []
```

まずは `examples/game_pack/` をコピーし、対象ゲームに合わせて `game.yaml` を変更するのが簡単です。
