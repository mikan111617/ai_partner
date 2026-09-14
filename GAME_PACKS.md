# Game Packs

ゲーム固有データは公開リポジトリへ入れず、`local_games/<id>/` に置きます。`local_games/` は `.gitignore` 対象です。

## 自作ゲーム: state_file

最も正確です。ゲーム側から `runtime_state.json` を更新します。

```json
{
  "event_id": "unique-event-id",
  "scenario_key": "scene_003",
  "speaker": "Narrator",
  "text": "現在表示された文章",
  "player_event": {
    "event_id": "choice-001",
    "event_type": "choice",
    "actor_scope": "user",
    "summary": "危険な道を選んだ",
    "importance": 0.8,
    "signals": {"boldness": 0.9, "adventure": 0.8}
  }
}
```

`actor_scope: user` のイベントだけが、原則として長期的な trust / affection / respect / antipathy を変化させます。シナリオ登場人物の行動でユーザーへの信頼が下がらないように分離しています。

## 既存ゲーム: screen_ocr

`game.yaml` で `tracking.method: screen_ocr` を指定します。Windows OCRで画面文字を読み、ローカルのシナリオファイルと曖昧照合します。

シナリオファイルは txt / ks / csv / tsv / json / jsonl に対応します。AIへ渡るのは**実際に画面と照合できた行だけ**です。未到達のシナリオは会話コンテキストへ入れません。

## 戦闘結果などで心理値を動かす

`gameplay_rules` に画面文字の正規表現とシグナルを定義できます。

```yaml
gameplay_rules:
  - pattern: 'GAME OVER|敗北|戦闘不能'
    actor_scope: user
    signals:
      player_defeated: 1.0
      heavy_damage: 0.4
```

敗北だけで「信用できない」とは扱わず、標準のお嬢様設定では主に respect が少し下がり concern が上がります。大胆さ、一貫性、臆病さなどのシグナルは関係値へより直接影響します。

`examples/game_pack/` を `local_games/my_game/` へコピーして編集してください。
