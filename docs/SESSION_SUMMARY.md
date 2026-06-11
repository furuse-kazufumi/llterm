# llterm Session Summary — 2026-06-11 夜 (L2 自走エンジン + L3 Qt GUI)

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
- GUI = プロジェクト選択コンボボックス(D:/projects 探索)+ 実/仮想トグル + 最大 session。
- **実 claude は claude.ai サブスク認証(`ClaudeRunner` が ANTHROPIC_API_KEY 系 env を外す)→ 従量課金なし**
  (Max 定額の範囲。制約は $ でなくレート制限)。仮想 claude(トグル off=既定)は課金ゼロのプレビュー。
- CLI 単体: `llterm-loop --workdir <対象> --dry-run --max-sessions 2`(仮想)。

## 次にやるべきこと
1. ~~課金モデルの確定~~ **解決済 (2026-06-12)**: claude は OAuth(claude.ai サブスク)認証 +
   env に ANTHROPIC_API_KEY 有り。`ClaudeRunner(use_subscription=True)` が API キー env を外して
   サブスク認証を強制 → **新たな従量課金なし**(レート制限内)。
2. **実 claude 初回走 (1回)**: stream-json の実イベント・フィールド名(`usage.input_tokens`/`total_cost_usd`
   等)を実出力で確認。`parse_stream_json` は防御的だが、フィールド名が違えば数行調整。サブスクなので追加課金なし。
3. **v2**: L1 制御プレーン(`ctl/schema`+`gate`)をループに結線し rotate/inject を**監査・HITL ゲート**化 /
   token 級ストリーミング表示 / GUI に Approve ボタン / Stop の即時性(現状は次ターン境界で停止)。

## 設計の要点(なぜこの形か)
- **層分離**: L1 ctl(既存)+ L2 loop(新・headless・表示非依存)+ L3 Qt GUI(新)。L2 はGUIが無くても回る。
- prior art(GitHub調査): `frankbria/ralph-claude-code`(--resume+EXIT_SIGNAL+circuit breaker)、
  `claude-resurrect`(summary→self-exit→--resume)が機構的に近い。Ralph 本家は「毎回 fresh context で
  捨て切る」、本実装は「70% まで resume で引き継ぎ→exit準備→新session」のハイブリッド。
- 唯一の人間介在点 = **再ログイン**(構造的上限。それ以外は自動)。
