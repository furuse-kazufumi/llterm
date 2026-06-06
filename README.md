# llterm

**Claude Code 専用の自走ターミナルホスト** — Claude Code (または任意の CLI) を縮小 PTY でホストし、IME 安定な分離入力欄と、Claude 自身がセッションを制御できる構造化制御プレーン (llterm-ctl) を提供する。

## なぜ作ったか

Claude Code の TUI に長文・複数行・日本語 IME で入力すると、composition 中の画面再描画で入力が壊れることがある。また、Claude が自分でセッションローテーションや次タスク注入を行う「自走」には、発話文字列のパースではない**帯域外の構造化制御チャネル**が要る。llterm はこの 2 つを 1 つのホストプロセスで解決する。

- **表示は完全互換**: 子 (claude) は実端末より 4 行小さい PTY 上で動き、TUI はそのまま上部に素通し表示される
- **入力は完全分離**: 最下部 4 行は llterm 自前の入力欄。子の再描画はここに届かない (DECSTBM + 縮小 PTY で隔離)
- **制御は fail-closed**: `.llterm/` ディレクトリのファイルプロトコル。未知 action・壊れた JSON は実行されず隔離、全イベントが append-only ledger に残る

## インストール

```
py -3.11 -m pip install -e .[dev]
```

依存は `pywinpty` のみ (Windows 専用)。テスト: `py -3.11 -m pytest tests/ -v`

## 使い方

```
llterm                      # claude を起動してホスト
llterm -- pwsh -NoLogo      # 任意の子コマンドをホスト (デバッグ用)
py -3.11 -m llterm.app      # スクリプト直接起動も同じ
```

### キー操作 (入力欄)

| キー | 動作 |
|---|---|
| 印字キー / IME 確定 | 文字挿入 (composition は入力欄のカーソル位置に表示) |
| Enter | **改行挿入** (送信しない) |
| Ctrl+Enter / Shift+Enter | **送信** (bracketed paste で一括注入) |
| ↑↓←→ | カーソル移動のみ |
| Ctrl+↑ / Ctrl+↓ (Shift も可) | 入力履歴 前 / 次 |
| Backspace | 1 文字削除 (行頭では行結合) |

複数行ペーストは 1 入力として保持される (CRLF/CR は LF に正規化、送信時に TUI 向け CR へ再変換)。

## 制御プレーン (llterm-ctl)

Claude (または人間) は emit CLI で制御コマンドを投函する:

```
py -3.11 -m llterm.ctl emit rotate --reason "context 80%"
py -3.11 -m llterm.ctl emit inject-task --reason "follow-up" --arg "title=do X" --arg "priority=5"
py -3.11 -m llterm.ctl emit query-state --reason "health check"
```

- 許可 action: `rotate` / `set-effort` / `inject-task` / `fork-session` / `query-state` / `shutdown` (v1 実行器は rotate / inject-task / query-state のみ。他は gate が REJECT)
- `--reason` は監査必須。無しは exit 2 で拒否 (fail-closed)
- `shutdown` は `requires_human` が常に強制され、人間承認待ち (`pending_human`) に積まれる
- 結果は `.llterm/results/<id>.json` に書き戻される (Claude が次ターンで読む)
- 全イベント (received / held / rejected / executed / error) は `.llterm/ledger.jsonl` に append-only 記録

Claude 側 CLAUDE.md に書く想定の呼び出し例:

> コンテキストが逼迫したら `py -3.11 -m llterm.ctl emit rotate --reason "<状況>"` を実行し、
> 結果 JSON の id を控えること。rotate は llterm が exit code 75 で再起動ラッパへ通知する。

## 受け入れチェックリスト (spec R10-R13)

自動テスト 60 件 green。実機スモーク (claude / pwsh 実走) での確認項目:

- [ ] **R10**: 入力欄で日本語 IME composition が安定する (確定文字が入力欄に到達する)
- [x] **R11**: 複数行ペーストが 1 入力として保持される (自動テスト + spike で確認)
- [x] **R12**: Enter=改行挿入、Ctrl/Shift+Enter=送信 (自動テスト + spike で修飾キー検出を実証)
- [x] **R13**: 矢印=カーソル移動、Ctrl/Shift+↑↓=履歴 (自動テスト)
- [x] 子 TUI が上部領域に収まり入力欄を侵食しない (spike で pwsh / 大量出力 / claude --help を実証)
- [ ] claude 実走 1 ターンの end-to-end (送信 → 応答表示 → /exit)

## 設計上の既知の教訓 (spike 実証)

- **子終了後の read ハング**: winpty の blocking read は子が exit しても返らない (ConPTY drain 問題)。reader を daemon スレッドへ隔離し EOF フラグで脱出する
- **入力飢餓**: blocking read を単一スレッドでループに置くと、子が無出力のときキー処理が止まる。read は non-blocking 化 (deque 経由) してループは 10ms tick で回す
- **修飾キーリピート洪水**: Ctrl/Shift 単独 keydown はリピートで大量発生する。`Action.NONE` では再描画しない

## 参照

- 仕様: fullsense research `llterm_spec_2026_06_06.md` (R1-R13)
- 実装計画: `docs/plans/2026-06-06-llterm-v1.md`
- spike 実証記録: `docs/spike_results.md`
