# 视频扫描合并工具（biliLive-tools-videomerge）

扫描指定目录中的视频分段文件，按 **「前缀_日期」自动分组**，调用 FFmpeg **无损合并**为单个成品文件。提供 Web 界面（Flask），支持实时进度、时间戳跳变自动检测与修复，可 Docker 部署，也支持 PyInstaller 打包为单文件 exe。

## 功能特性

- **自动扫描分组**：按文件名规则自动识别分段文件，相同前缀同一天合为一组（如 `张三_2026年9月28日`），不同前缀同一天分开；递归子目录扫描，自动排除输出目录防止成品被重复扫入
- **无损快速合并**：默认使用 `ffmpeg concat -c copy` 不重编码，速度快、零画质损失
- **时间戳跳变修复**：合并前用 ffprobe 检测 DTS 跳变（阈值 0.5s），发现跳变自动切换重编码模式（`setpts/asetpts` 重建时间戳，x264 CRF18），避免成品卡顿、音画不同步
- **单文件自动改名复制**：组内只有 1 个文件时直接改名复制，不经过 ffmpeg
- **并行处理**：多分组 ThreadPoolExecutor 并行合并（默认 3 并发，可调）
- **实时进度**：整体进度按各分组时长加权，前端轮询展示；服务端持有进度状态，页面刷新不丢失
- **Web 目录浏览器**：前端可视化选择输入/输出目录
- **输出文件名安全化**：自动去除 emoji 与 Windows 非法字符（`:<>/\|?*`）

## 文件命名规则

分段文件（严格模式，必须含时分秒）：

```
{前缀}_{年}年{月}月{日}日{时}点{分}分{秒}秒.{扩展名}
例：张三_2026年9月28日20点30分0秒.ts
```

成品文件（独立成组显示，不再参与合并）：

```
{前缀}_合并.ts                     # 或
{前缀}_{年}年{月}月{日}日.ts        # 日期后无时分秒
```

支持的扩展名：`.ts` / `.mp4` / `.flv`（成品输出统一为 `.ts`）。

## 快速开始

### 方式一：直接运行（Python 3.8+，需已安装 ffmpeg/ffprobe）

```bash
pip install -r app-src/requirements.txt

export INPUT_DIR=/data/input      # 可选，默认 /data/input
export OUTPUT_DIR=/data/output    # 可选，默认 /data/output
export PORT=8821                  # 可选，默认 8821

python app-src/app.py
# 浏览器访问 http://127.0.0.1:8821
```

### 方式二：Docker

本仓库 Dockerfile 基于 `videomerge:latest` 基础镜像（内含 Python + Flask + FFmpeg 环境）叠加应用代码：

```bash
docker build -t videomerge:fixed .
docker run -d --name videomerge \
  -p 8821:8821 \
  -v /your/input:/data/input \
  -v /your/output:/data/output \
  videomerge:fixed
```

FFmpeg 也可通过挂载提供：`-v /mnt/sda/ffmpeg:/opt/ffmpeg`（容器内自动发现 `/opt/ffmpeg/ffmpeg`、`/opt/ffmpeg/ffprobe`）。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8821` | 监听端口 |
| `INPUT_DIR` | `/data/input` | 默认输入目录 |
| `OUTPUT_DIR` | `/data/output` | 默认输出目录 |
| `FFMPEG_PATH` | （自动发现） | ffmpeg 路径，优先级最高 |
| `FFPROBE_PATH` | （自动发现） | ffprobe 路径 |

ffmpeg/ffprobe 发现顺序：环境变量 → `/opt/ffmpeg` 挂载目录 → `PATH` → 程序同目录下 `FFmpeg/`（exe 兜底）。

## Web 界面

访问 `http://<host>:8821`：

1. 选择输入目录，点击**扫描** → 列出所有分组（名称 / 文件数 / 总大小，日期倒序）
2. 勾选要合并的分组（可全选）
3. 可选：设置**输出文件名前缀**（自动补「：」）、调整并行数、开关**修复模式**
4. 点击**开始合并** → 实时查看整体进度、当前处理的分组与文件、滚动日志

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/scan` | 扫描分组。body: `{"input_dir": "..."}`，返回 `groups: [{name, count, size}]` |
| POST | `/api/merge` | 启动合并（同一时间仅允许一个任务）。body: `{"names": [...], "input_dir": "...", "output_dir": "...", "fix_mode": true, "auto_prefix": "", "workers": 3}` |
| GET | `/api/status` | 轮询进度与日志，返回 `{logs, busy, progress: {running, total_groups, done_groups, overall, current}}` |
| GET | `/api/browse?path=/&show_files=1` | 目录浏览器：列出子目录（及视频文件） |
| GET | `/api/config` | 当前配置（ffmpeg/ffprobe 路径、默认目录、并发数） |

## 运行测试

```bash
python test_core.py
```

## 目录结构

```
├── Dockerfile                  # 基于 videomerge:latest 叠加应用代码
├── test_core.py                # 核心逻辑测试
└── app-src/
    ├── app.py                  # Flask 后端（路由 / 任务调度 / 进度状态）
    ├── merger_core.py          # 合并核心（扫描分组 / ffmpeg 调用 / 进度解析）
    ├── requirements.txt        # flask>=3.0,<4
    └── static/
        └── index.html          # Web 前端
```
