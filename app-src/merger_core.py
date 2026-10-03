#!/usr/bin/env python3
"""
视频文件合并核心逻辑（无 GUI 依赖）
- 供 Web 版 / CLI 复用
- ffmpeg/ffprobe 发现顺序：环境变量 FFMPEG_PATH/FFPROBE_PATH > PATH 中的 ffmpeg
- 输入/输出目录由调用方传入，不硬编码
"""
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
JUMP_THRESHOLD = 0.5  # 时间戳跳变阈值（秒）
VIDEO_EXTENSIONS = ["*.ts", "*.mp4", "*.flv"]

# 分段文件：人名_中文日期时间.扩展名（严格模式：要求含时分秒）
VIDEO_PATTERN = r"^(.+)_(\d{4}年\d{1,2}月\d{1,2}日\d{1,2}点\d{1,2}分\d{1,2}秒)\.\w+$"
# 成品文件：_合并 后缀，或 日期后无时分秒
FINISHED_PATTERN = r"^(.+?)_(?:合并|(?:\d{4}年\d{1,2}月\d{1,2}日(?!\d{1,2}点)))\.\w+$"

EMOJI_RE = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\u2600-\u27BF"
    "\u2B00-\u2BFF"
    "\u25A0-\u25FF"
    "\uFE00-\uFE0F"
    "\u200D"
    "]+"
)


def find_ffmpeg() -> tuple:
    """返回 (ffmpeg, ffprobe) 可执行路径。优先级：
    环境变量 FFMPEG_PATH > 挂载目录 /opt/ffmpeg > PATH > 本地 FFmpeg 目录
    """
    ff = os.environ.get("FFMPEG_PATH")
    fp = os.environ.get("FFPROBE_PATH")
    # 容器内挂载目录（docker 映射 /mnt/sda/ffmpeg -> /opt/ffmpeg）
    for mount in ("/opt/ffmpeg",):
        if os.path.isdir(mount):
            if not ff:
                cand = Path(mount) / "ffmpeg"
                if cand.exists():
                    ff = str(cand)
            if not fp:
                cand = Path(mount) / "ffprobe"
                if cand.exists():
                    fp = str(cand)
    # PATH
    if not ff:
        ff = shutil.which("ffmpeg")
    if not fp:
        fp = shutil.which("ffprobe")
    if not ff or not fp:
        # 最后兜底：exe 旁 / 脚本旁的 FFmpeg 目录（PyInstaller 打包后 __file__ 在临时目录，需用 exe 所在目录）
        if getattr(sys, "frozen", False):
            local = Path(sys.executable).resolve().parent / "FFmpeg"
        else:
            local = Path(__file__).resolve().parent / "FFmpeg"
        if not ff:
            cand = local / "ffmpeg.exe"
            if cand.exists():
                ff = str(cand)
            else:
                cand = local / "ffmpeg"
                if cand.exists():
                    ff = str(cand)
        if not fp:
            cand = local / "ffprobe.exe"
            if cand.exists():
                fp = str(cand)
            else:
                cand = local / "ffprobe"
                if cand.exists():
                    fp = str(cand)
    return str(ff) if ff else "", str(fp) if fp else ""


FFMPEG_PATH, FFPROBE_PATH = find_ffmpeg()


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def parse_datetime(dt_str: str) -> datetime:
    """解析中文日期时间字符串（兼容无前导零，如 2026年8月4日）"""
    m = re.match(
        r"(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2})点(\d{1,2})分(\d{1,2})秒)?",
        dt_str,
    )
    if not m:
        raise ValueError(f"无法解析日期: {dt_str}")
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    h = int(m.group(4)) if m.group(4) else 0
    mi = int(m.group(5)) if m.group(5) else 0
    s = int(m.group(6)) if m.group(6) else 0
    return datetime(y, mo, d, h, mi, s)


def get_video_fps(file_path: Path) -> float:
    """读取视频平均帧率（如 30/1 -> 30.0），失败返回 30.0"""
    try:
        r = subprocess.run(
            [FFPROBE_PATH, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate", "-of", "csv=p=0",
             str(file_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        rate_str = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
        if "/" in rate_str:
            num, den = rate_str.split("/")
            num, den = float(num), float(den)
            if den > 0 and num > 0:
                return num / den
        elif rate_str:
            v = float(rate_str)
            if v > 0:
                return v
    except Exception:
        pass
    return 30.0


def detect_timestamp_jumps(file_path: Path, threshold: float = JUMP_THRESHOLD):
    """
    检测视频流内的时间戳跳变。
    返回 (跳变处数, 最大跳变秒数, 状态)：
      状态 "ok"    正常
      状态 "jump"  存在跳变
      状态 "error" 检测失败（ffprobe 报错/无输出）
    """
    try:
        r = subprocess.run(
            [FFPROBE_PATH, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "packet=dts_time", "-of", "csv=p=0",
             str(file_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300,
        )
        jumps = 0
        max_gap = 0.0
        prev = None
        for line in r.stdout.splitlines():
            line = line.strip().rstrip(",")
            if not line:
                continue
            try:
                t = float(line)
            except ValueError:
                continue
            if prev is not None:
                gap = t - prev
                if gap > threshold:
                    jumps += 1
                    if gap > max_gap:
                        max_gap = gap
            prev = t
        if prev is None:
            return -1, 0.0, "error"  # 无任何 dts 输出
        if jumps > 0:
            return jumps, max_gap, "jump"
        return 0, 0.0, "ok"
    except Exception:
        return -1, 0.0, "error"


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------
def _norm_date(dt: datetime) -> str:
    """归一化日期分组名：2026年9月28日（去前导零）"""
    return f"{dt.year}年{dt.month}月{dt.day}日"


def get_duration(file_path: Path) -> float:
    """读取视频时长（秒），失败返回 0.0"""
    try:
        r = subprocess.run(
            [FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(file_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        return float(r.stdout.strip().splitlines()[0])
    except Exception:
        return 0.0


def _parse_clock(s: str) -> float:
    """解析 ffmpeg -progress 的 out_time=HH:MM:SS.micro 为秒；失败返回 None"""
    try:
        parts = s.split(":")
        if len(parts) != 3:
            return None
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except Exception:
        return None


def _run_ffmpeg_progress(cmd: list, total_sec: float,
                         progress_cb=None, group_name: str = "",
                         label: str = "") -> int:
    """执行 ffmpeg 并通过 -progress pipe:1 解析实时进度。
    progress_cb(group_name, fraction(0~1, 相对 total_sec), label)
    返回 returncode。"""
    cmd = list(cmd) + ["-progress", "pipe:1", "-nostats"]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time=") and total_sec > 0:
                t = _parse_clock(line[len("out_time="):])
                if t is not None and progress_cb:
                    progress_cb(group_name, min(t / total_sec, 1.0), label)
        proc.wait()
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
    return proc.returncode


def _match_video(file: Path, groups: dict):
    """单个文件匹配：分段文件按「前缀_日期」分组（相同前缀同一天一组），成品文件独立成组"""
    m = re.match(VIDEO_PATTERN, file.name)
    if m:
        prefix = m.group(1)
        datetime_str = m.group(2)
        dt = parse_datetime(datetime_str)
        key = f"{prefix}_{_norm_date(dt)}"
        groups[key].append({
            "file": file,
            "datetime": dt,
            "datetime_str": datetime_str,
            "prefix": prefix,
        })
    elif re.match(FINISHED_PATTERN, file.name):
        key = file.stem
        groups[key].append({
            "file": file,
            "datetime": None,
            "datetime_str": "",
            "prefix": "",
        })


def scan_videos(folder: Path, recursive: bool = False,
                exclude: tuple = ()) -> dict:
    """扫描视频文件并按「前缀_日期」分组（组名如 张三_2026年9月28日，
    相同前缀同一天合为一组，不同前缀同一天分开）；成品文件独立成组显示。
    recursive=True 时递归子目录；exclude 为要跳过的相对目录名（如输出目录）。
    返回有序 dict：日期组按日期倒序（最新在前，同日按前缀排序），成品组排最后。
    """
    groups = defaultdict(list)
    if recursive:
        for file in folder.rglob("*"):
            if file.is_dir():
                continue
            rel = file.relative_to(folder)
            if any(part in exclude for part in rel.parts):
                continue
            if file.suffix.lower() in {".ts", ".mp4", ".flv"}:
                _match_video(file, groups)
    else:
        for ext in VIDEO_EXTENSIONS:
            for file in folder.glob(ext):
                _match_video(file, groups)
    # 组内按时间排序
    for name in groups:
        groups[name].sort(key=lambda x: x["datetime"] or datetime.min)
    # 组间排序：日期组按日期倒序（最新在前），同日期内按前缀升序；成品组排最后
    def gdate(name: str):
        m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", name)
        if m:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return None
    date_groups = [k for k in groups if gdate(k)]
    finished_groups = sorted([k for k in groups if not gdate(k)])
    date_groups.sort()                      # 先按名称（同日期内前缀升序）
    date_groups.sort(key=gdate, reverse=True)  # 稳定排序：日期倒序
    return {k: groups[k] for k in date_groups + finished_groups}


# ---------------------------------------------------------------------------
# 输出命名
# ---------------------------------------------------------------------------
def build_output_base(name: str, videos: list = None,
                      auto_prefix: str = "") -> str:
    """生成输出文件名主干。
    分组名即「前缀_日期」（如 张三_2026年9月28日），直接作为输出主干。
    auto_prefix: 输出名不含冒号时补加此前缀（自动补「：」）；已含冒号保留原样。
    输出文件名中去除 emoji 与非法字符。
    """
    base = EMOJI_RE.sub("", name)
    base = re.sub(r'[<>:"/\\|?*]', "_", base)
    if auto_prefix and "：" not in base and ":" not in base:
        if not auto_prefix.endswith("：") and not auto_prefix.endswith(":"):
            auto_prefix = auto_prefix + "："
        base = auto_prefix + base
    return base


def format_size(size_bytes) -> str:
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------
def _safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def copy_single(name: str, video: dict, output_path: Path,
                auto_prefix: str = "", log=None, progress_cb=None) -> bool:
    """单个视频：改文件名后直接复制（不经过 ffmpeg）"""
    if progress_cb:
        progress_cb(name, 0.0, video["file"].name)
    ext = video["file"].suffix if video["file"].suffix else ".ts"
    base = build_output_base(name, [video], auto_prefix)
    output_file = output_path / f"{base}{ext}"
    n = 1
    while output_file.exists():
        n += 1
        output_file = output_path / f"{base}_{n}{ext}"
    try:
        shutil.copy2(video["file"], output_file)
        if log:
            log(f"✅ {name} -> {output_file.name} ({format_size(output_file.stat().st_size)})")
        if progress_cb:
            progress_cb(name, 1.0, "")
        return True
    except Exception as e:
        if log:
            log(f"❌ {name}: {e}")
        if progress_cb:
            progress_cb(name, 1.0, "")
        return False


def _concat_path(p: Path) -> str:
    """转成 concat 列表可用的绝对路径（concat 相对路径按列表文件所在目录解析，必须绝对）"""
    return str(p.resolve()).replace("\\", "/").replace("'", "'\\''")


def merge_single(name: str, videos: list, output_path: Path,
                 auto_prefix: str = "", log=None, progress_cb=None) -> bool:
    """快速合并（-c copy 不重编码），带实时进度"""
    safe_name = _safe_name(name)
    output_file = output_path / (build_output_base(name, videos, auto_prefix) + ".ts")
    concat_file = (output_path / f"{safe_name}_concat.txt").resolve()
    try:
        total_dur = sum(get_duration(v["file"]) for v in videos)
        if progress_cb:
            progress_cb(name, 0.0, output_file.name)
        with open(concat_file, "w", encoding="utf-8") as f:
            for video in videos:
                f.write(f"file '{_concat_path(video['file'])}'\n")
        cmd = [FFMPEG_PATH, "-y", "-f", "concat", "-safe", "0",
               "-i", str(concat_file), "-c", "copy", str(output_file)]
        rc = _run_ffmpeg_progress(cmd, total_dur, progress_cb, name,
                                  output_file.name)
        if rc == 0:
            if log:
                log(f"✅ {name} -> {output_file.name} ({format_size(output_file.stat().st_size)})")
            if progress_cb:
                progress_cb(name, 1.0, "")
            return True
        else:
            if log:
                log(f"❌ {name}: ffmpeg 错误")
            if progress_cb:
                progress_cb(name, 1.0, "")
            return False
    except Exception as e:
        if log:
            log(f"❌ {name}: {e}")
        if progress_cb:
            progress_cb(name, 1.0, "")
        return False
    finally:
        try:
            concat_file.unlink()
        except OSError:
            pass


def merge_single_fixed(name: str, videos: list, output_path: Path,
                       auto_prefix: str = "", log=None,
                       progress_cb=None) -> bool:
    """修复时间戳跳变后合并（重编码 setpts），带实时进度"""
    safe_name = _safe_name(name)
    output_file = output_path / (build_output_base(name, videos, auto_prefix) + ".ts")
    tmp_dir = output_path / f".tmp_fix_{safe_name}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    concat_file = (output_path / f"{safe_name}_fix_concat.txt").resolve()

    def report(done_dur: float, cur: float, total: float, label: str):
        if progress_cb and total > 0:
            progress_cb(name, min((done_dur + cur) / total, 0.99), label)

    try:
        durations = [get_duration(v["file"]) for v in videos]
        total_dur = sum(durations)
        if progress_cb:
            progress_cb(name, 0.0, videos[0]["file"].name)
        fixed_files = []
        done_dur = 0.0
        for i, video in enumerate(videos):
            fps = get_video_fps(video["file"])
            fixed = tmp_dir / f"seg_{i:03d}.ts"
            cmd = [FFMPEG_PATH, "-y", "-i", str(video["file"]),
                   "-vf", f"setpts=N/({fps}*TB)",
                   "-af", "asetpts=N/SR/TB",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                   "-c:a", "aac", "-b:a", "192k",
                   str(fixed)]
            rc = _run_ffmpeg_progress(
                cmd, durations[i],
                lambda g, frac, label, d=done_dur, t=total_dur:
                    report(d, frac * durations[i], t, label),
                name, video["file"].name)
            if rc != 0 or not fixed.exists():
                raise RuntimeError(f"重编码失败: {video['file'].name}")
            fixed_files.append(fixed)
            done_dur += durations[i]
        with open(concat_file, "w", encoding="utf-8") as f:
            for fixed in fixed_files:
                f.write(f"file '{_concat_path(fixed)}'\n")
        cmd = [FFMPEG_PATH, "-y", "-f", "concat", "-safe", "0",
               "-i", str(concat_file), "-c", "copy", str(output_file)]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode == 0 and output_file.exists():
            if log:
                log(f"✅ {name} -> {output_file.name} ({format_size(output_file.stat().st_size)})")
            if progress_cb:
                progress_cb(name, 1.0, "")
            return True
        else:
            if log:
                log(f"❌ {name}: 修复合并失败")
            if progress_cb:
                progress_cb(name, 1.0, "")
            return False
    except Exception as e:
        if log:
            log(f"❌ {name}: {e}")
        if progress_cb:
            progress_cb(name, 1.0, "")
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        try:
            concat_file.unlink()
        except OSError:
            pass


def process_one(name: str, videos: list, output_path: Path, fix_mode: bool,
                jump_cache: dict, auto_prefix: str = "",
                log=None, progress_cb=None) -> bool:
    """处理单个分组（供并行调用）。fix_mode=True 时按跳变检测结果决定是否重编码"""
    if len(videos) < 2:
        return copy_single(name, videos[0], output_path, auto_prefix, log,
                           progress_cb)
    need_fix = fix_mode
    if need_fix:
        for v in videos:
            key = str(v["file"])
            if key in jump_cache:
                j = jump_cache[key][0]
            else:
                j, _, _ = detect_timestamp_jumps(v["file"])
                jump_cache[key] = (j, 0.0, "")
            if j > 0:
                need_fix = True
                break
        else:
            need_fix = False  # 所有文件都无跳变 -> 快速合并
    if need_fix:
        if log:
            log(f"🛠 修复时间戳跳变: {name}")
        return merge_single_fixed(name, videos, output_path, auto_prefix, log,
                                  progress_cb)
    else:
        return merge_single(name, videos, output_path, auto_prefix, log,
                            progress_cb)


def merge_groups(selected: list, videos_by_name: dict, output_path: Path,
                 fix_mode: bool = True, workers: int = 3,
                 auto_prefix: str = "", log=None,
                 progress_cb=None) -> dict:
    """并行合并多个分组。返回 {success: int, failed: [names]}
    log: 回调函数 log(str)，在调用方线程上下文执行（线程安全由调用方保证）
    progress_cb(group_name, fraction(0~1), current_file): 分组级实时进度回调
    """
    output_path.mkdir(parents=True, exist_ok=True)
    jump_cache = {}
    total = len(selected)
    done = 0
    success = 0
    failed = []

    def process(name):
        return process_one(name, videos_by_name[name], output_path, fix_mode,
                           jump_cache, auto_prefix, log, progress_cb)

    w = min(workers, max(1, total))
    with ThreadPoolExecutor(max_workers=w) as executor:
        futures = {executor.submit(process, name): name for name in selected}
        for future in as_completed(futures):
            name = futures[future]
            try:
                ok = future.result()
            except Exception as e:
                ok = False
                if log:
                    log(f"❌ {name}: {e}")
            if progress_cb:
                progress_cb(name, 1.0, "")  # 兜底：确保该组标记完成
            if ok:
                success += 1
            else:
                failed.append(name)
            done += 1
            if log:
                log(f"进度: {done}/{total}")
    return {"success": success, "failed": failed, "total": total}