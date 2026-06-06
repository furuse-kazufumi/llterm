"""llterm v1 エントリポイント.

    llterm                      # claude を起動してホスト
    llterm -- pwsh -NoLogo      # 任意の子コマンドをホスト (デバッグ用)

メインループ: PTY 出力素通し (上部 scroll region) → 入力欄再描画 → キーイベント処理
→ ctl tick。子プロセス終了 or rotate 要求でループを抜ける。
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from llterm.ctl.queue import CtlQueue
from llterm.host.console import ConsoleInput
from llterm.host.pty_host import PtyHost
from llterm.host.vtbridge import VtResponseFilter
from llterm.host.watcher import CtlWatcher
from llterm.input.buffer import InputBuffer
from llterm.input.keys import Action, decode
from llterm.input.render import render_input_area

RESERVE = 4

# 空欄パススルー (実機知見 2026-06-07): claude の選択 UI (信頼確認・メニュー) は
# 矢印/Enter の直接キーを期待する。入力欄が空のときだけ plain キーを VT で子へ転送。
# 入力欄に内容があるときは R12/R13 (Enter=改行・矢印=カーソル移動) を維持。
_VT_ARROWS = {"up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D"}


class App:
    def __init__(self, child_argv: list[str], *, ctl_root: Path) -> None:
        self.cols, self.rows = shutil.get_terminal_size()
        self.child_argv = child_argv
        self.host = PtyHost(child_argv, rows=self.rows - RESERVE, cols=self.cols)
        self.buf = InputBuffer()
        self.console = ConsoleInput()
        self.vtfilter = VtResponseFilter()
        self.queue = CtlQueue(ctl_root)
        self.watcher = CtlWatcher(self.queue, self, ledger_path=ctl_root / "ledger.jsonl")
        self.rotate_requested: str | None = None
        self._session = 1

    # ---- CtlWatcher が呼ぶ host インターフェース ----
    def request_rotate(self, reason: str) -> None:
        self.rotate_requested = reason

    def state(self) -> dict:
        return {"alive": self.host.isalive(), "session": self._session,
                "child": self.child_argv}

    # ---- メインループ ----
    def run(self) -> int:
        out = sys.stdout
        out.write(f"\x1b[1;{self.rows - RESERVE}r\x1b[H")   # 上部 scroll region
        out.flush()
        self.host.spawn()
        try:
            while self.host.isalive() and self.rotate_requested is None:
                wrote = False
                data = self.host.read(65536)
                if data:
                    out.write(data)                          # 素通し (上部領域)
                    wrote = True
                for ev in self.console.poll_events():
                    action, arg = decode(ev)
                    if action is Action.NONE:
                        # VK_CONTROL/VK_SHIFT 単独 keydown はキーリピートで洪水になる
                        # (spike Task6 実測 + ユーザー指摘 2026-06-06)。NONE で再描画すると
                        # Ctrl 押下中ずっと再描画が走る (R4 に逆行) ため skip する。
                        continue
                    if action is Action.INSERT:
                        self.buf.insert(arg)
                    elif action is Action.NEWLINE:
                        self.buf.newline()
                    elif action is Action.BACKSPACE:
                        self.buf.backspace()
                    elif action is Action.MOVE:
                        self.buf.move(arg)
                    elif action is Action.HISTORY_PREV:
                        self.buf.history_prev()
                    elif action is Action.HISTORY_NEXT:
                        self.buf.history_next()
                    elif action is Action.SUBMIT:
                        text = self.buf.take()
                        if text.strip():
                            self.buf.push_history(text)
                            self.host.send_text(text)
                    wrote = True
                self.watcher.tick()
                if wrote:
                    out.write(render_input_area(self.buf, term_rows=self.rows,
                                                term_cols=self.cols, reserve=RESERVE))
                    out.flush()
                else:
                    time.sleep(0.01)
        finally:
            out.write("\x1b[r\x1b[?25h")                      # scroll region 解除
            out.flush()
            self.host.close()
        if self.rotate_requested is not None:
            print(f"\n[llterm] rotate requested: {self.rotate_requested}")
            return 75                                          # EX_TEMPFAIL: ラッパが再起動
        return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    child = argv[argv.index("--") + 1:] if "--" in argv else ["claude"]
    return App(child, ctl_root=Path(".llterm")).run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
