"""spike: 実端末より 4 行小さい PTY で子プロセスを起動し、
子の全画面描画が上部 (rows-4) に収まり、下 4 行が侵食されないかを確認する。

実行 (実コンソールで手動):
    py -3.11 spikes/spike_ptyrows.py -- pwsh -NoLogo
    py -3.11 spikes/spike_ptyrows.py -- py -3.11 -c "print('x\\n'*100)"
判定基準:
  - 子の出力・全画面再描画が下 4 行を上書きしないこと
  - 下 4 行に描いた [LLTERM INPUT AREA] が残り続けること
終了: 子プロセスを exit させる (pwsh なら `exit`)。
"""
import shutil
import sys

import winpty  # pywinpty


def main() -> None:
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else ["pwsh", "-NoLogo"]
    cols, rows = shutil.get_terminal_size()
    reserve = 4
    pty = winpty.PtyProcess.spawn(args, dimensions=(rows - reserve, cols))

    # 下 4 行に入力欄プレースホルダを描く (CSI 絶対位置)
    def draw_input_area() -> None:
        for i in range(reserve):
            sys.stdout.write(f"\x1b[{rows - reserve + 1 + i};1H\x1b[2K")
        sys.stdout.write(f"\x1b[{rows - reserve + 1};1H\x1b[7m[LLTERM INPUT AREA]\x1b[0m")
        sys.stdout.flush()

    # スクロールを上部領域に制限 (DECSTBM) — 子の出力が下 4 行へ流れ込まない柵
    sys.stdout.write(f"\x1b[1;{rows - reserve}r\x1b[H")
    draw_input_area()
    try:
        while pty.isalive():
            try:
                data = pty.read(4096)
            except EOFError:
                break
            if data:
                # 上部領域の最終カーソル位置に書く (素通し)
                sys.stdout.write(data)
                sys.stdout.flush()
                draw_input_area()  # 毎フレーム入力欄を再描画して侵食を検出しやすく
    finally:
        sys.stdout.write("\x1b[r")  # スクロール領域解除
        print("\n[spike done]")


if __name__ == "__main__":
    main()
