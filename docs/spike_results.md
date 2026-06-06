# spike 結果記録

## Task 6: 修飾キー検出 (spike_keys.py) — ✅ 完全成立 (2026-06-06 夜, ユーザー実機・完全ログ取得)

- 実行環境: ユーザーの実コンソール (Windows 11, PowerShell)。
- **判定: R12/R13 の技術前提が完全成立** (実ログより):
  - 素の Enter: `vk=13 char='\r' mods=- state=0x0`
  - **Ctrl+Enter: `vk=13 char='\n' mods=CTRL state=0x8`** — char が `\n` に変わる (legacy console 伝統挙動)。
    decoder は vk+mods 判定なので影響なし。
  - **Shift+Enter: `vk=13 char='\r' mods=SHIFT state=0x10`**
  - **Ctrl+↑: `vk=38 char='\x00' mods=CTRL state=0x108`** — 0x100 = ENHANCED_KEY (矢印等の拡張キー
    フラグ)。decoder は SHIFT/CTRL ビットのみ参照するため影響なし。
  - 観察事項: **Ctrl/Shift 押下中は vk=17/16 (VK_CONTROL/VK_SHIFT) 単独 keydown がキーリピートで連続発生**。
    → 本実装 (input/keys.py decode) では非印字 char + 対象外 vk として Action.NONE に落ちるため無害
    (test_control_chars_ignored でカバー)。console.py の wRepeatCount 展開でも同様に NONE。
  - **運用注意 (ユーザー指摘 2026-06-06)**: 実際は「Ctrl を押してから Enter」なので、Enter が来るまで
    char='\x00' の VK_CONTROL イベントが**流れ続ける**。防御 2 段: ①decode が NONE に落とす (バッファ混入
    なし・実装済) ②App ループは **NONE で再描画しない** (洪水中の無駄再描画 = ちらつき種を排除。
    計画 Task 13 に反映済)。
- 未確認 (本実装スモークで再確認): IME 確定文字が `char` にどう届くか (変換確定時のイベント形)。
  R10 の受け入れ確認 (Task 13 Step 5) で必ず見る。

## Task 7: 縮小 PTY + DECSTBM 入力欄分離 (spike_ptyrows.py) — ✅ 判定成立 (2026-06-06 夜, ユーザー実機)

- **判定 OK: `[LLTERM INPUT AREA]` は侵食されず残り続けた** (ユーザー実機確認「残ります」) =
  縮小 PTY (rows-4) + DECSTBM で表示と入力欄の分離が成立。**案(b) の技術前提が全て確定**。
- **既知問題: 子 (pwsh) を exit しても spike が終了しない**。原因 = `pty.read()` がブロッキング読みで、
  子終了後も read が返らず `while pty.isalive()` 判定に戻れない (ConPTY drain 問題 —
  node-pty #375/#1810 と同根、ccr/claude-auto.mjs が同じ罠を踏んだ前歴あり)。
  → **本実装 (Task 11 PtyHost) は reader thread 版に計画修正済**: blocking read を daemon スレッドに
  隔離し、メインループは non-blocking read + EOF フラグで確実に抜ける。spike は throwaway のため
  修正しない (Ctrl+C で抜ける / 画面が乱れたら cls で復旧)。
- **既知問題 2 (ユーザー実機 2026-06-06 夜): `ls` と打っても表示されない = 入力飢餓**。同根の構造欠陥:
  単一スレッドで `pty.read()` がブロック中はキー転送 (`while msvcrt.kbhit()`) に到達できない。
  キーが転送されない → pwsh が何も出力しない → read が返らない → **デッドロック (鶏と卵)**。
  起動直後はプロンプト出力で偶然回っていただけ。
  → **本実装で解消済の設計**: PTY 出力は reader thread が常時 drain (Task 11)、コンソール入力は
  App メインループが ReadConsoleInputW で独立に処理 (Task 13) — 出力と入力が互いをブロックしない。
  spike 3 知見 (侵食なし / 終了ハング / 入力飢餓) はいずれも Task 11/13 設計に反映済。

## 実機スモーク第 1 回 (2026-06-07, claude 実走) — 発見 2 件と対処

ユーザー実機 (デスクトップショートカット経由、子 = claude) で初回起動した結果:

### 発見 1: 端末クエリ応答が入力欄を汚染 + 子に届かない
- 症状: 入力欄に `[?61;4;6;7;...c` `]11;rgb:0c0c/...` が**何も打っていないのに**出現。
- 機序: claude は起動時に端末能力クエリ (DA1 `CSI c` / OSC 11 背景色) を発する。llterm は
  それを素通しで実端末へ書く → 実端末は**応答をコンソール入力に書き戻す** → llterm が
  キー入力と誤認して入力欄に INSERT (ESC は isprintable=False で落ち、残りが入る)。
  さらに応答が子へ転送されないため claude の能力検出も不全。
- 対処: `VtResponseFilter` (host/vtbridge.py) — KeyEvent 列から CSI/OSC 応答シーケンスを
  状態機械で分離し `write_raw()` で子へ転送。分割到着 (バッチ跨ぎ) 対応。8 tests。

### 発見 2: claude の選択 UI が操作不能
- 症状: 初回の信頼確認 (`> 1. Yes, I trust this folder`) を矢印/Enter で選択できない
  (全キーが入力欄に行く設計のため)。
- 対処: **空欄パススルー** (ユーザー承認 2026-06-07) — 入力欄が空のときだけ plain 矢印/Enter
  を VT シーケンス (`CSI A-D` / CR) で子へ直接転送。入力欄に内容があれば R12/R13 を維持。

### 併せて修正 (敵対レビュー confirmed findings 6 件)
- EOF drain: `isalive()` 先行チェックで子の最終出力チャンクを取りこぼす競合 → 「死亡 かつ
  残データなし」までドレインしてから脱出 (app.py)。
- render 縦スクロール窓: 入力 5 行以上でカーソルが scroll region 内へ飛ぶ → カーソル行を
  必ず含む末尾 reserve 行窓で描画 (render.py)。
- inject-task 安全床: caller constraints で no-push/needs-human-judgment が消える →
  強制 union (watcher.py)。
- quarantine 監査: 壊れ JSON の隔離が ledger に痕跡ゼロ → "quarantined" イベント記録 +
  OSError/UnicodeDecodeError 耐性 + 隔離先同名は unique suffix (queue.py)。
