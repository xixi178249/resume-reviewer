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
COPY web ./web
COPY scripts ./scripts
COPY prompts ./prompts
COPY samples ./samples

# 容器里必须监听 0.0.0.0，否则外部访问不到
EXPOSE 8080
CMD ["python", "web/server.py", "--host", "0.0.0.0", "--port", "8080"]
