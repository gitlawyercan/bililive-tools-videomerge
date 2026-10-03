# 基于现有镜像叠加修复后的应用代码（无需外网拉取基础镜像）
FROM videomerge:latest

WORKDIR /app
COPY app-src/app.py app-src/merger_core.py ./
COPY app-src/requirements.txt ./
COPY app-src/static ./static
# B站上传已移除，清理旧模块
RUN rm -f /app/bili_client.py

ENV HOST=0.0.0.0 PORT=8821 INPUT_DIR=/data/input OUTPUT_DIR=/data/output
EXPOSE 8821

CMD ["python", "app.py"]
