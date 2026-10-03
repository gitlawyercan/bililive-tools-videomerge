#!/usr/bin/env python3
"""
视频文件合并工具 - Web 版后端（Flask）
默认监听 0.0.0.0:8821，通过环境变量 HOST/PORT 可配置
输入/输出目录可用环境变量 INPUT_DIR/OUTPUT_DIR 覆盖
支持 PyInstaller 打包为 exe：运行后自动打开浏览器
"""
import os
import sys
import threading
import queue
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory

import merger_core as core

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8821"))
DEFAULT_INPUT = Path(os.environ.get("INPUT_DIR", "/data/input"))
DEFAULT_OUTPUT = Path(os.environ.get("OUTPUT_DIR", "/data/output"))
PARALLEL_WORKERS = 3
# 扫描时排除的目录名（输出目录，避免把已合并成品再次扫入）
EXCLUDE_DIRS = tuple({DEFAULT_OUTPUT.name, "merge"})

app = Flask(__name__, static_folder="static", static_url_path="")

# 单例任务锁：同一时间只允许一个合并任务
_merge_lock = threading.Lock()

# 日志环形缓冲（最近 200 条），供前端轮询
_logs = []
_logs_lock = threading.Lock()

# 合并实时进度状态（供前端轮询；服务端持有，刷新/重开页面不丢失）
_merge_state = {
    "running": False,
    "total_groups": 0,
    "done_groups": 0,
    "overall": 0.0,       # 0~1，按各分组时长加权
    "current": [],        # [{name, file, percent}] 正在处理的分组
}
_state_lock = threading.Lock()


def log(msg: str):
    with _logs_lock:
        _logs.append(msg)
        if len(_logs) > 200:
            del _logs[:-200]


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------
@app.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.get_json(silent=True) or {}
    folder = Path(data.get("input_dir") or str(DEFAULT_INPUT))
    if not folder.exists() or not folder.is_dir():
        return jsonify({"ok": False, "error": f"输入目录不存在: {folder}"}), 400
    if not core.FFMPEG_PATH:
        return jsonify({"ok": False, "error": "未找到 ffmpeg，请检查环境变量 FFMPEG_PATH 或 PATH"}), 500
    try:
        groups = core.scan_videos(folder, recursive=True, exclude=EXCLUDE_DIRS)
    except Exception as e:
        return jsonify({"ok": False, "error": f"扫描失败: {e}"}), 500
    result = []
    for name, videos in groups.items():
        total_size = sum(v["file"].stat().st_size for v in videos)
        result.append({
            "name": name,
            "count": len(videos),
            "size": core.format_size(total_size),
            "size_bytes": total_size,
        })
    return jsonify({"ok": True, "groups": result})


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------
@app.route("/api/merge", methods=["POST"])
def api_merge():
    data = request.get_json(silent=True) or {}
    names = data.get("names") or []
    if not names:
        return jsonify({"ok": False, "error": "未选择要合并的分组"}), 400
    if not _merge_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "已有合并任务正在进行"}), 409

    input_dir = Path(data.get("input_dir") or str(DEFAULT_INPUT))
    output_dir = Path(data.get("output_dir") or str(DEFAULT_OUTPUT))
    fix_mode = bool(data.get("fix_mode", True))
    auto_prefix = data.get("auto_prefix", "") or ""
    workers = int(data.get("workers") or PARALLEL_WORKERS)

    try:
        groups = core.scan_videos(input_dir, recursive=True, exclude=EXCLUDE_DIRS)
        # 校验所有选中的分组都存在
        missing = [n for n in names if n not in groups]
        if missing:
            return jsonify({"ok": False, "error": f"以下分组不存在: {missing}"}), 400

        with _logs_lock:
            _logs.clear()

        # 预计算各分组总时长，用于加权整体进度
        group_durations = {}
        for n in names:
            group_durations[n] = sum(core.get_duration(v["file"]) for v in groups[n])
        total_duration = sum(group_durations.values()) or 1.0
        done_set = set()
        active = {}  # name -> {"file": str, "fraction": float}

        def on_progress(gname: str, fraction: float, current_file: str):
            with _state_lock:
                if fraction >= 1.0:
                    active.pop(gname, None)
                    done_set.add(gname)
                else:
                    active[gname] = {"file": current_file, "fraction": fraction}
                weighted = sum(group_durations[g] for g in done_set if g in group_durations)
                weighted += sum(group_durations[g] * a["fraction"]
                                for g, a in active.items() if g in group_durations)
                _merge_state.update({
                    "running": True,
                    "total_groups": len(names),
                    "done_groups": len(done_set),
                    "overall": round(weighted / total_duration, 4),
                    "current": [
                        {"name": g, "file": a["file"],
                         "percent": round(a["fraction"] * 100, 1)}
                        for g, a in active.items()
                    ],
                })

        with _state_lock:
            _merge_state.update({"running": True, "total_groups": len(names),
                                 "done_groups": 0, "overall": 0.0, "current": []})

        def worker(names, groups, output_dir, fix_mode, auto_prefix, workers):
            try:
                result = core.merge_groups(
                    names, groups, output_dir, fix_mode=fix_mode,
                    workers=workers, auto_prefix=auto_prefix, log=log,
                    progress_cb=on_progress)
                log(f"合并完成: 成功 {result['success']} 组, 失败 {len(result['failed'])} 组")
                if result["failed"]:
                    log(f"失败分组: {', '.join(result['failed'])}")
                log(f"输出目录: {output_dir}")
            finally:
                _merge_lock.release()
                with _state_lock:
                    _merge_state.update({"running": False, "overall": 1.0,
                                         "current": []})
                log("任务结束")

        t = threading.Thread(target=worker, args=(
            names, groups, output_dir, fix_mode, auto_prefix, workers), daemon=True)
        t.start()
        return jsonify({"ok": True, "status": "started", "names": names,
                        "output_dir": str(output_dir)})
    except Exception as e:
        _merge_lock.release()
        with _state_lock:
            _merge_state.update({"running": False, "current": []})
        return jsonify({"ok": False, "error": f"启动合并失败: {e}"}), 500


@app.route("/api/status", methods=["GET"])
def api_status():
    with _logs_lock:
        logs = list(_logs)
    with _state_lock:
        progress = dict(_merge_state)
        progress["current"] = list(_merge_state["current"])
    return jsonify({"ok": True, "logs": logs, "busy": _merge_lock.locked(),
                    "progress": progress})


@app.route("/api/browse", methods=["GET"])
def api_browse():
    """列出指定路径下的子目录（供前端目录选择器使用）；show_files=1 时同时列出视频文件"""
    path = request.args.get("path") or "/"
    show_files = request.args.get("show_files") == "1"
    p = Path(path)
    if not p.exists() or not p.is_dir():
        return jsonify({"ok": False, "error": f"目录不存在: {path}"}), 400
    try:
        dirs = sorted(
            (c.name for c in p.iterdir() if c.is_dir()),
            key=lambda n: n.lower(),
        )
        files = []
        if show_files:
            files = sorted(
                (c.name for c in p.iterdir()
                 if c.is_file() and c.suffix.lower() in (".ts", ".mp4", ".flv", ".mkv")),
                key=lambda n: n.lower(),
            )
    except PermissionError:
        return jsonify({"ok": False, "error": f"无权限读取: {path}"}), 403
    parent = str(p.parent) if p.parent != p else None
    return jsonify({"ok": True, "path": str(p), "parent": parent,
                    "dirs": dirs, "files": files})


@app.route("/api/config", methods=["GET"])
def api_config():
    return jsonify({
        "ok": True,
        "ffmpeg": core.FFMPEG_PATH,
        "ffprobe": core.FFPROBE_PATH,
        "input": str(DEFAULT_INPUT),
        "output": str(DEFAULT_OUTPUT),
        "workers": PARALLEL_WORKERS,
    })


if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        # exe 模式：默认仅本机访问，启动后自动打开浏览器
        import webbrowser
        _port = int(os.environ.get("PORT", "8821"))
        threading.Timer(
            1.5, lambda: webbrowser.open(f"http://127.0.0.1:{_port}")
        ).start()
    app.run(host=HOST, port=PORT, threaded=True)