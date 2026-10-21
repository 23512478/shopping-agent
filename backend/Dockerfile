FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 预下载 ChromaDB 默认嵌入模型（all-MiniLM-L6-v2），避免首个请求冷启动卡在 S3 下载
# chromadb 会在 /root/.cache/chroma/onnx_models/all-MiniLM-L6-v2/ 找 onnx/ 目录，命中就不再下载
RUN mkdir -p /root/.cache/chroma/onnx_models/all-MiniLM-L6-v2 && \
    apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -fsSL --retry 3 --retry-delay 5 "https://chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz" \
      -o /root/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx.tar.gz && \
    tar -xzf /root/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx.tar.gz \
      -C /root/.cache/chroma/onnx_models/all-MiniLM-L6-v2/ && \
    rm /root/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx.tar.gz && \
    apt-get purge -y curl ca-certificates && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
