# -*- coding: utf-8 -*-
"""端到端测试：前缀+日期分组 / 输出命名 / 进度回调"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "app-src"))
import merger_core as core


def make_ts(path: Path, dur=2):
    subprocess.run([
        core.FFMPEG_PATH, "-y",
        "-f", "lavfi", "-i", f"testsrc=duration={dur}:size=128x96:rate=10",
        "-f", "lavfi", "-i", f"sine=duration={dur}",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path),
    ], capture_output=True, check=True)


tmp = Path(tempfile.mkdtemp(prefix="vmtest_"))
try:
    # 同前缀同日 -> 一组；不同前缀同日 -> 分开；跨日 -> 分开
    make_ts(tmp / "张三_2026年9月28日10点00分00秒.ts")
    make_ts(tmp / "李四_2026年9月28日11点30分00秒.ts")
    make_ts(tmp / "张三_2026年9月28日08点00分00秒.ts")
    make_ts(tmp / "张三_2026年9月29日09点00分00秒.ts")

    groups = core.scan_videos(tmp)
    print("分组顺序:", list(groups.keys()))
    assert list(groups.keys()) == [
        "张三_2026年9月29日",       # 日期倒序
        "张三_2026年9月28日",       # 同日期内按前缀升序
        "李四_2026年9月28日",
    ], list(groups.keys())
    assert len(groups["张三_2026年9月28日"]) == 2, "同前缀同日应合为一组"
    assert len(groups["李四_2026年9月28日"]) == 1, "不同前缀同日应分开"
    g28 = groups["张三_2026年9月28日"]
    assert [v["file"].name for v in g28][0].startswith("张三_2026年9月28日08点"), "组内时间排序错误"

    # 命名：分组名即输出主干
    base = core.build_output_base("张三_2026年9月28日", g28, "")
    print("输出主干:", base)
    assert base == "张三_2026年9月28日", base
    base_p = core.build_output_base("张三_2026年9月28日", g28, "萌妹精选")
    print("带前缀主干:", base_p)
    assert base_p == "萌妹精选：张三_2026年9月28日", base_p

    # 合并 + 进度回调
    out = tmp / "out"
    events = []
    result = core.merge_groups(
        list(groups.keys()), groups, out,
        fix_mode=False, workers=2, auto_prefix="",
        log=lambda m: print("LOG:", m),
        progress_cb=lambda n, f, label: events.append((n, round(f, 2), label)))
    print("合并结果:", result)
    assert result["success"] == 3, result
    files = sorted(p.name for p in out.glob("*.ts"))
    print("输出文件:", files)
    assert "张三_2026年9月28日.ts" in files, files
    assert "张三_2026年9月29日.ts" in files, files
    assert "李四_2026年9月28日.ts" in files, files
    finals = {(n, f) for n, f, _ in events if f >= 1.0}
    print("完成事件:", sorted(finals))
    assert len(finals) >= 3, "三组都应有完成事件"
    print("\n=== 全部断言通过 ===")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
