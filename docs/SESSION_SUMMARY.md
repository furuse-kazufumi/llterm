# llterm Session Summary — 2026-06-12 (リアルタイムストリーミング表示 + カラー化)

## 2026-06-12: 「claude の応答が GUI に表示されない」問題を解消

**根本原因** (ledger 実証): `ClaudeRunner` が `proc.communicate()` でターン完了まで全ブロック
し、GUI はターン完了後に最終 result テキスト 1 個を無着色で出すだけだった。自律 1 ターンは
数分〜数十分かかるため、その間 GUI は完全無表示 → ユーザーは故障と判断し Stop していた
(2026-06-12 03:20 UTC の実走 ledger で session_start 2.5 分後に cancelled を確認)。

**修正内容**:
- `loop.py ClaudeRunner` — stdout を**行単位リアルタイム読み**に変更 (communicate 廃止)。
  stderr 排出スレッド (pipe デッドロック防止) + `threading.Timer` watchdog (timeout 1800→7200s、
  自律長ターンの途中 kill 防止)。`on_stream` コールバックで要約イベントを逐次通知。
  `_build_args()` 分離 (テストが偽の子プロセスを注入する seam)。
- `loop.py summarize_stream_event()` — stream-json 1 イベント → GUI 用軽量 dict 列の純関数。
  **実 claude 2.1.174 の実出力で確認済** (init/assistant text/thinking/tool_use/user tool_result/
  result。hook_started 等の system と rate_limit_event は表示しない)。
- `worker.py` — `stream` シグナル追加。runner が `on_stream` を持てば購読 (duck-typing)。
- `app.py` — **セマンティックカラー描画** (One Dark 系 PALETTE + ダーク背景、html.escape 済み
  appendHtml)。応答=本文色 / セッション境界=黄 / ターン=青 / ツール=シアン / エラー=赤 /
  rotate=マゼンタ / 注入=緑 / 補助=灰。`_streamed_text` カウンタでストリーム済み応答の
  turn 完了時二重表示を防止。stream-json は ANSI を含まないため端末色パススルーではなく
  イベント種別で llterm 自身が着色する設計。
- `virtual.py` — 仮想 claude も同形の stream イベントを発行 (課金ゼロで表示経路を検証可能)。

**実走確認 (2026-06-12)**:
- `--resume <sid>` は**同一 session_id の in-place 継続** (fork しない) — ループの resume 設計は正
- `parse_stream_json` のフィールド名は実出力と一致 (result/total_cost_usd/usage/num_turns)
- 実 claude 1 ターン smoke: 2.9s init → **18.1s 初回テキスト表示** → 38.4s ターン完了
  (旧実装では 38.4s まで無表示)
- テスト 99 passed (新規 10: summarize 5 + ClaudeRunner ストリーミング 2 + GUI 描画 3)、ruff clean

---

# (前回) llterm Session Summary — 2026-06-11 夜 (L2 自走エンジン + L3 Qt GUI)

## このセッションでやったこと
端末 (TUI) を捨てて **GUI 化**する方針転換 + **ループ駆動の心臓 (L2) を新規実装**。
ユーザー指摘「llterm は使いにくい、なぜ GUI でない、Qt 使えば済む」を受け、L3 を端末から
PySide6 GUI へ。会話で壊れ続けた問題は全部 terminal_io 由来 (win32-input-mode / ConPTY /
カーソル競合) で、GUI なら丸ごと消える、という判断。

### 新規実装 (commit 4207e67)
- **L2 `src/llterm/host/loop.py`** — 公式 headless protocol で自走するループ駆動本体。**端末を通らない**。
  - `claude -p <prompt> --output-format stream-json --verbose --session-id/--resume` を回す
    (フラグは `claude --help` で実在確認済)。
  - ループ = `新session → 前回の続き(resume) → 使用率70% → exit準備 → 畳む → 新session`。
  - 安全: circuit breaker / cost cap / **auth(再ログイン)検知で fail-closed 停止** / 監査 ledger /
    `stdin=DEVNULL`(orphan-reader hang を構造的に排除) / 無制限自走は CLI が拒否(課金保護)。
  - GUI 連携: `on_event` 進捗通知 / `should_stop` 協調停止 / `next_prompt` タスク注入。
  - `parse_stream_json` は防御的(フィールド欠落・壊れ行に耐える)。`TurnRunner` Protocol で mock 注入可。
- **L3 `src/llterm/gui/`** — PySide6 GUI(`app.py` 窓 / `worker.py` QThread / `virtual.py` 仮想claude)。
  出力(リングバッファ)/ctx% バー/cost/session 番号/Start・Stop/注入欄(Ctrl+Enter 送信)。
- **仮想 claude `gui/virtual.py`** — 実 claude 不要・課金ゼロ。使用率が増え 70% で rotate する擬似。
  fail_every / auth_after でエラー・認証切れ経路も模擬。

### 検証 (仮想 claude + offscreen, 課金ゼロ)
- 新規 **27 tests green**(`test_loop.py` 21 + `test_gui.py` 6)。フル 107 passed。
  - 失敗 1 = `test_pty_host::test_pty_roundtrip_with_python_child` は **既知 flaky**(全体実行時のみの
    winpty PermissionError。単体では 4 passed)。今回捨てる旧 PTY 端末側で本作業と無関係。
- GUI は **offscreen で main() rc=0**(`show()` まで起動確認)。`--real` 課金ガード rc=2 確認。
- entry points: `llterm-loop` / `llterm-gui` 登録済(editable 再インストール済)。

## 起動方法
```powershell
llterm-gui            # GUI 起動 → コンボボックスでプロジェクトを選び Start
llterm-gui --real     # 起動時に「実 claude (サブスク認証)」を選択状態にする
#   py -3.11 -m llterm.gui でも可
```
- GUI = プロジェクト選択コンボボックス(D:/projects 探索)+ 実/仮想トグル + 最大 session +
  **設定行(rotate閾値/窓tokens/コスト上限)** + **テンプレ選択(用途ツールチップ付き)** + 公開ボタン。
- **実 claude は claude.ai サブスク認証(`ClaudeRunner` が ANTHROPIC_API_KEY 系 env を外す)→ 従量課金なし**
  (Max 定額の範囲。制約は $ でなくレート制限)。仮想 claude(トグル off=既定)は課金ゼロのプレビュー。
- **テンプレ(機能別)**: `general` / `rad_expand`(RAD 拡張) / `green_keeper` / `doc_update`。
  GUI のコンボボックスで選択(各項目に用途ツールチップ)、`--template <key>` で CLI からも。
- **RAD 連携**: 参照=「RAD 参照」トグル(`--rad`、作業前に `D:/docs/*_corpus_v2` を grep)。
  拡張=`rad_expand` テンプレで分野を **staging** に生成 → **「公開」ボタン(人間ゲート)で live へ昇格**
  (`llterm-rad publish <domain>` でも可)。**自走ループは live を絶対に上書きしない**(共有 RAD 保護)。
- CLI 単体: `llterm-loop --workdir <対象> --dry-run --max-sessions 2`(仮想)。

## 次にやるべきこと
1. ~~課金モデルの確定~~ **解決済 (2026-06-12)**: claude は OAuth(claude.ai サブスク)認証 +
   env に ANTHROPIC_API_KEY 有り。`ClaudeRunner(use_subscription=True)` が API キー env を外して
   サブスク認証を強制 → **新たな従量課金なし**(レート制限内)。
2. ~~実 claude 初回走 (1回)~~ **解決済 (2026-06-12)**: stream-json 実フォーマット確認、
   `parse_stream_json` 一致、`--resume` は同一 ID in-place 継続を実証。
3. ~~応答のリアルタイム表示~~ **解決済 (2026-06-12)**: メッセージ級ストリーミング + カラー表示
   (上記参照)。token 級 (`--include-partial-messages`) は v2 候補のまま。
4. **v2**: L1 制御プレーン(`ctl/schema`+`gate`)をループに結線し rotate/inject を**監査・HITL ゲート**化 /
   token 級ストリーミング表示 / GUI に Approve ボタン / Stop の即時性(現状は実行中ターンの kill まで)。

## 設計の要点(なぜこの形か)
- **層分離**: L1 ctl(既存)+ L2 loop(新・headless・表示非依存)+ L3 Qt GUI(新)。L2 はGUIが無くても回る。
- prior art(GitHub調査): `frankbria/ralph-claude-code`(--resume+EXIT_SIGNAL+circuit breaker)、
  `claude-resurrect`(summary→self-exit→--resume)が機構的に近い。Ralph 本家は「毎回 fresh context で
  捨て切る」、本実装は「70% まで resume で引き継ぎ→exit準備→新session」のハイブリッド。
- 唯一の人間介在点 = **再ログイン**(構造的上限。それ以外は自動)。
