# ARTICLE_SEEDS — llterm 記事ストック

> 公開記事 (Qiita / Zenn / LinkedIn / note 等) を書くための素材置き場。
> 1 セッションの作業を「記事の種」に変換して貯める。実装後に angle/hook/事実/コード参照を残し、
> あとから ja/en/zh/ko の 4 言語版に展開できるようにする。honest disclosure を核に、
> 「異常に良い/速い結果は内訳を疑う」「失敗も教訓として残す」を守る。

---

## 種 #1 — 「自走ループの生ログを見たら、注入したタスクが“飢餓”していた」 (2026-06-13)

**コンセプト hook (記事冒頭候補)**:
> AI に「現在の進捗を要約して」と頼んだ。13 分待っても返ってこない。ログを追うと——
> そのタスクは実行されないどころか、構造上**永久に飲み込まれていた**。

### 背景 (かみくだき)
llterm は Claude Code / Codex を **headless (`claude -p --resume` / `codex exec`)** で
ターン駆動し、コンテキストが閾値を超えたら自動 rotate して延々と自走させる「ループエンジン」。
人間は走行中に**タスク注入**で割り込める……はずだった。

### 何が起きたか (事実・production ログ由来)
- orchestra モード (指揮者=Codex 実装 → 複数 AI レビュー → 責任者=Claude 総合判断 → 修正 → sign-off)
  の 1 ターンが実時間で **約 13〜18 分** (≈7 回の AI 呼び出し)。
- そのターン実行中に注入した「進捗を要約できますか？」が、ターン完了の境界まで待たされた。
- さらに致命的: ターン完了時の **ctx 使用率が 2549%** と表示され (cache 再読込の重複加算による過大計上)、
  **毎ターン rotate** が発生。
- 注入の消費点は継続ターン (`_continue_prompt`) だけだったため、**rotate ばかりだと消費点に到達せず**、
  注入は次セッションの再開 prompt に押しのけられて**永久に実行されない (= 飢餓)**。

### 直し方 (技術設計)
- `loop.py`: `_take_injection()` を新セッション **opener でも消費**するよう変更。rotate を挟んでも
  「次のターン境界」で必ず注入を拾う = 高優先・飢餓なし。
- 並び順の話とは別だが、同セッションで「最終更新が日付だけ」問題も是正
  (`progress.py parse_updated_at()` が本文の `最終更新: YYYY-MM-DD HH:MM` を解析し並び順の正に)。

### honest disclosure (記事の核)
- **ctx 2549% の過大計上は未是正**。今回は「実害 (注入飢餓・レビュー二重) 」を潰しただけで、
  使用率の算定 (分子の cache 重複加算) 自体は別課題として残っている。勝った気にならない。

### コード参照
- `src/llterm/host/loop.py` (`_take_injection` / opener での消費 / `error_kind="interrupted"` 処理)
- `src/llterm/host/orchestra_runner.py` (`run_turn` = 実装+レビュー+集約+修正+sign-off)

---

## 種 #2 — 「レビューにレビューを重ねていないか」 — 多 AI オーケストラの過剰レビュー削減

**hook**:
> 5 分の修正に、AI が 7 回レビューした。しかも“記録するだけ”のターンにも、もう一周。

### 事実
- orchestra 1 ターン = 実装(1) + パネル 3 レビュー(3) + 責任者 集約(1) + 修正(1) + 最終 sign-off(1) ≈ **7 AI 呼び出し**。
- rotate のたびの **EXIT 整形 (handoff = SESSION_SUMMARY/next_plan を書くだけ)** が、**フルオーケストラを丸ごと再実行**していた。
- ctx 2549% で毎ターン rotate するため、この二重レビューが恒常的に発生。

### 直し方
- `OrchestraRunner.run_turn_unreviewed()` (指揮者のみ) を追加し、`loop._handoff_run_turn()` が
  handoff/EXIT 整形でそれを使う = 記録ターンに 3-AI レビューを掛けない。
- 最終 sign-off を既定 OFF (`app.py` `final_signoff=False`)。責任者の総合判断 (集約) が既に審判で、
  修正後の再レビューは「レビューにレビューを重ねる」冗長。

### angle 候補
- 「多 AI レビューは“質”だが、無条件に重ねると“時間とレート”を食う」= 適用範囲の設計が肝。
- TRIZ 的: 「レビューを増やすと品質↑だが速度↓」という矛盾 → 分離原理 (記録ターンと実装ターンで掛け分け)。

---

## 種 #3 — 「headless CLI を回すループに“緊急割り込み”を入れる」

**hook**:
> `claude -p` は実行中に新しい入力を差し込めない。では走っているターンをどう“今すぐ”止める？

### 設計
- **恒久 cancel (Stop)** と **一発 interrupt (緊急注入)** を分離。
  - `cancel()`: `_cancelled=True` (sticky) → 以後 run_turn は起動しない (Stop 用)。
  - `interrupt()`: `_interrupted=True` (一発) → 現プロセスを kill するが**次の run_turn は起動できる**。
- run_turn は中断時に `error_kind="interrupted"` を返す。loop はこれを **停止ではなく継続**として扱い、
  注入タスクを次ターンで**必ず消費 (スキップ防止)**。
- worker: `inject(text, emergency=True)` は**キュー先頭へ挿入 (最優先)** + 全 runner へ interrupt。
- GUI: 「⚡緊急注入」ボタン。通常 Send はターン境界でキュー、緊急は現ターンを切って即実行。

### 落とし穴 (教訓・honest)
- cancel が sticky なのを忘れて interrupt に流用すると「以後ずっと起動しない」死に方をする。
  → 別フラグ (`_interrupted`、一発リセット) で分離する必要があった。
- 中断 = in-progress 作業の破棄。だから既定は「ターン境界キュー」、緊急は明示ボタンで opt-in。

### コード参照
- `loop.py` / `codex_runner.py` / `gemini_runner.py` の `interrupt()` + `_interrupted`
- `orchestra_runner.py` の `interrupt()` (メンバ委譲) と run_turn の interrupted 伝播
- `gui/worker.py` `inject(emergency=)` / `request_interrupt()`、`gui/app.py` `emergency_inject()`

---

## 種 #4 — 「トレーサビリティ: 全行タイムスタンプ + 1 時間ローテログ」

**hook**:
> 自走 AI が何をいつやったか後から追えないなら、それは“監督”ではない。

### 事実 / 設計
- 出力ログの**各行の先頭に必ず `[HH:MM:SS]`** (唯一のファネル `_append` で一括付与・空行は素通し)。
- ターミナル表示を **1 時間単位ファイル (`YYYY-MM-DD_HH.log`) に行単位で append**、**1 週間 (168h) 保持**、
  古いものから自動削除。失敗 (ディスク/権限) でも GUI を殺さない fail-safe。
- 置き場所 = `~/.llterm/logs/` (設定と同じ stable な per-user ディレクトリ)。

### angle
- 「責任あるおせっかい AI」(FullSense 哲学) を **architecture level** で担保する = HITL + 監査可能性。
- 純ロジック (`gui/termlog.py`) を Qt 非依存にして単体テスト可能にした設計の話も書ける。

### コード参照
- `src/llterm/gui/termlog.py` (`TerminalLog` / `_prune` / `_parse_stem`)、`gui/app.py` `_append`

---

## 種 #5 (教訓) — 「自分の変更が flaky test を顕在化させた」

**hook**:
> テストは緑だった。でも“たまたま”緑だっただけだった。

### 事実
- choice→inject テストは `start_loop()` で**実ループをスレッド駆動**し、注入キューを直接 assert していた。
- `VirtualClaudeRunner(delay=0.0)` で worker が**キューを race 消費**するため、本質的に flaky だった
  (今回の出力ログ I/O 追加で main スレッドが少し遅くなり、レースが顕在化 = 失敗側へ傾いた)。
- 決定論化: `delay>0` で worker を turn 1 にブロック → 注入が消費されない窓を作り、検証後に force stop。

### 教訓 (記事 angle)
- 「並行テストの“たまたま緑”を疑う」「タイミング依存の assert はブロック点を作って決定論化する」。
- honest disclosure: 既存の lint 2 件 (uuid セミコロン / QThread.event シグナル名衝突) は本変更外として明示。

---

## 多言語展開メモ
- ja を正本に、en/zh/ko へ。技術用語 (headless / rotate / orchestra / starvation / fail-safe) は原語維持。
- 技術者向け = QIITA 系、非エンジニア向けは「監督できる自走 AI」「割り込めるからこそ任せられる」の比喩で。
