# 使用官方轻量级 Python 镜像
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
# 防止 Python 生成pyc文件
ENV PYTHONDONTWRITEBYTECODE=1
# 保持标准输出不被缓冲
ENV PYTHONUNBUFFERED=1
# 设置 Hugging Face 模型缓存目录 (方便挂载)
ENV HF_HOME=/app/cache

# 安装依赖
COPY requirements.txt .
# 增加 timeout 防止下载大包超时
RUN pip install --no-cache-dir -r requirements.txt --timeout 100

# 复制项目文件
COPY . .

# 创建必要的目录
RUN mkdir -p results logs cache

# 预下载 AI 模型并保存到固定目录 (彻底避开缓存机制)
RUN python -c "from sentence_transformers import SentenceTransformer; m = SentenceTransformer('shibing624/text2vec-base-chinese'); m.save('/app/model_data')"

# 赋予权限 (Hugging Face Spaces 需要非 root 用户权限 1000)
RUN chown -R 1000:1000 /app

# 切换用户
USER 1000

# 只需要暴露端口，实际由平台的环境变量决定运行端口
EXPOSE 7860

# 启动命令 (使用 Gunicorn)
# Hugging Face Spaces 默认端口 7860
# --timeout 300 防止模型加载超时
CMD ["sh", "-c", "gunicorn -w 1 -b 0.0.0.0:${PORT:-7860} --timeout 300 dashboard.app:app"]
