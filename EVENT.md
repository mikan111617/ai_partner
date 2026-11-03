# イベント会場で使う（ノートPC・VOICEVOX）

ゲームの横で、四国めたんがプレイを見ながらツッコミを入れます。
声は VOICEVOX（四国めたん）で、GPU のないノートPCでも動きます。マイクは使いません。

## 必要なもの

- Windows
- Python 3.11
- VOICEVOX（アプリ）
- Ollama（任意。無くても、用意したセリフで動きます）

## 準備（1回だけ）

1. VOICEVOX をインストールし、一度起動して、四国めたんの声が出ることを確かめる。
2. このフォルダの `setup.cmd` を実行する。
   Ollama が無い場合は「[NOTE] Ollama was not found.」と出ますが、そのまま使えます。

## 当日の起動順

1. VOICEVOX を起動する（閉じると声が出なくなります。最小化は大丈夫です）。
2. `run_event.cmd` を実行する。
   黒い画面に次の行が出れば準備完了です。
   - `[VOICEVOX] ready speaker=四国めたん ...`
   - `[game-pack] waiting for game events at http://127.0.0.1:8765/event`
3. ゲーム（ランチャー）を起動する。

黒い画面は閉じずに、後ろに置いたままにします。

## 声が出るかの確認

PowerShell で次を実行すると、四国めたんが「被弾したとき」のセリフを話します。

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/event -Body '{"game":"invader","event":{"event_type":"hit","details":{"lives":2}}}'
```

## 反応する出来事（巨大インベーダー）

| 出来事 | event_type |
|---|---|
| 遊び始めた | game_start |
| 次のステージが始まった | stage_start |
| やり直した | retry |
| 被弾した | hit |
| バリアが防いだ | barrier |
| ゲームオーバー | game_over |
| ステージクリア | stage_clear |
| インベーダーが話しかけてきた（平和ルート） | peace_start |
| 平和にクリア | peace_clear |
| エンディング | ending / ending_peace |
| 1機増えた | extend |
| 自機が巨大化した | giant |
| 隠しコマンド | secret_command |

デモプレイ中は送られません。

## セリフを変える

`event_games/invader/game.yaml` の `lines` に、出来事ごとのセリフが並んでいます。
メモ帳で開いて書き換え、`run_event.cmd` を起動し直すと反映されます。

- `{stage}` `{lives}` `{score}` `{goal}` は、ゲームから届いた値に置き換わります。
- `emotion` で声の調子が変わります（neutral / happy / excited / surprise / angry / worry / sad / thinking）。

## 設定（config.event.yaml）

| 項目 | 内容 |
|---|---|
| `game_reaction.mode` | `auto`：Ollama が使えれば LLM で一言を作り、遅い・使えないときは用意したセリフ。`lines`：いつも用意したセリフ（速くて確実） |
| `game_reaction.llm_timeout_seconds` | LLM を待つ秒数。過ぎたら用意したセリフ。3回続けて遅れたら、以後は用意したセリフだけになる |
| `game_reaction.min_gap_seconds` | 話し終わってから次に話すまでの最短の秒数 |
| `voice.voicevox_speaker` | VOICEVOX の話者名 |
| `voice.voicevox_volume` | 声の大きさ（1.0 が標準） |
| `voice.voicevox_speed` | 話す速さ（1.0 が標準） |
| `initiative.enabled` | だれも遊んでいないときの独り言。会場では呼び込みにならないよう `false` |

PC ごとに変えたい値は `config.user.yaml` に書くと、こちらより優先されます。

## クレジット

VOICEVOX の声を使うときは、クレジットの表記が必要です。

```text
VOICEVOX:四国めたん
```


## 実況音声の優先と四国めたん

実況はゲーム好きで穏やかな四国めたんです。LLMの指示・心理の重み・代替セリフを同じ人格に合わせています。
イベント用の保存先は `data_event_metan`、通常起動は `data_metan` です。以前の人格のデータは残し、新しい保存先で始めます。

音量は `config.user.yaml` の `voice.voicevox_volume: 1.5` に設定しています。音が割れる場合は 1.2 などへ下げてください。
対応版のインベーダーは `http://127.0.0.1:8765/voice` を読み、実際の音声再生中にBGM・効果音を25%に下げ、終了後に戻します。
古いexeには音量優先処理がないため、新しいビルドを使用してください。AI停止・接続切断時も通常音量に戻ります。
VOICEVOX自体やWindowsの音量ミキサーでAIの出力音量が小さい場合は、その音量も確認してください。
