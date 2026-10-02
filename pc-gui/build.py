# -*- coding: utf-8 -*-
"""
把 GUI 打包成单文件 exe。

用法：
    .venv\\Scripts\\python.exe build.py

产物：
    dist\\VpnShare.exe
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "app", "app.py")
ICON = os.path.join(HERE, "icon.ico")
MANIFEST = os.path.join(HERE, "app.manifest")
NAME = "VpnShare"


def main():
    if not os.path.exists(APP):
        print(f"[FAIL] 找不到 {APP}")
        return 1

    # 每次构建用一个全新的带时间戳的 build 目录：
    # 这样 PyInstaller 不需要删除任何旧文件，避开删除保护导致的 --clean 失败。
    stamp = time.strftime("%Y%m%d-%H%M%S")
    workdir = os.path.join(HERE, "build", stamp)
    # 输出目录同样带时间戳：固定的 dist\ 下已有旧的 VpnShare.exe，
    # PyInstaller 覆盖前会删它，同样会撞上删除确认保护。
    distdir = os.path.join(HERE, "dist", stamp)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        # 不加 --clean：它会清理 build 缓存（几十个文件），
        # 在部分环境下会触发删除确认而中断。增量构建同样正确。
        "--onefile",              # 单文件 exe
        "--windowed",             # 不带黑窗口（GUI 程序）
        "--name", NAME,
        # tkinter 会被自动收集，这里显式声明避免漏
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--hidden-import", "winreg",
        # local_relay 是同目录模块，显式声明保证一定被收集
        "--hidden-import", "local_relay",
        "--hidden-import", "proxy_core",
        # v3.4：实时流量折线图控件（同目录模块）
        "--hidden-import", "traffic_chart",
        # 保险：把可能被误收集的大库排掉，减小体积
        "--exclude-module", "numpy",
        "--exclude-module", "pandas",
        "--exclude-module", "matplotlib",
        "--exclude-module", "PIL",
        "--exclude-module", "pytest",
        "--exclude-module", "unittest",
        "--distpath", distdir,
        "--workpath", workdir,
        "--specpath", workdir,
    ]

    if os.path.exists(ICON):
        # 必须用绝对路径：PyInstaller 以 --specpath 目录为基准解析相对路径
        args += ["--icon", os.path.abspath(ICON)]
    else:
        print("[注意] 没找到 icon.ico，将使用默认图标")

    if os.path.exists(MANIFEST):
        # 同上，绝对路径
        args += ["--manifest", os.path.abspath(MANIFEST)]
    else:
        print("[注意] 没找到 app.manifest，将不带 UAC / 高 DPI 声明")

    args.append(APP)

    print("执行:", " ".join(args))
    print("-" * 60)
    rc = subprocess.run(args, cwd=HERE).returncode
    if rc != 0:
        print(f"[FAIL] PyInstaller 退出码 {rc}")
        return rc

    exe = os.path.join(distdir, f"{NAME}.exe")
    if os.path.exists(exe):
        size = os.path.getsize(exe) / 1024 / 1024
        print("-" * 60)
        print(f"[OK] 打包完成：{exe}")
        print(f"     体积：{size:.1f} MB")
        # 顺手复制到 release\ 供分发（先用新名字避免覆盖冲突）
        rel_dir = os.path.join(HERE, "release")
        os.makedirs(rel_dir, exist_ok=True)
        rel_exe = os.path.join(rel_dir, f"{NAME}.exe")
        try:
            import shutil as _sh
            _sh.copy2(exe, rel_exe)
            print(f"[OK] 已复制到：{rel_exe}")
        except Exception as e:
            print(f"[注意] 复制到 release 失败：{e}")
            print(f"       请手动复制 {exe} 到 {rel_dir}")
    else:
        print("[FAIL] 没找到产物 exe")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
