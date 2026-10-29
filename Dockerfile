FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUTF8=1

# 中文字体：docx 报告里出现中文时不至于变方块
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY VERSION .
# config/rubric.yaml 是运行时依赖：scripts/rubric.py 读它，漏拷=容器内 import 即崩
COPY config ./config
COPY web ./web
COPY scripts ./scripts
COPY prompts ./prompts
COPY samples ./samples

# 容器里必须监听 0.0.0.0，否则外部访问不到
EXPOSE 8080
# 端口必须取平台注入的 $PORT：Render 会分配一个端口并用它转发流量，
# 写死 8080 的结果是「容器起来了，但平台访问不到 → 502」。
CMD ["sh", "-c", "exec python web/server.py --host 0.0.0.0 --port ${PORT:-8080}"]
