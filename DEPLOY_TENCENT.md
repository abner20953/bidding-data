# 腾讯云轻量应用服务器 (Lighthouse) 部署指南

本指南适用于配置为 **2核 2G 5M**，系统为 **Ubuntu 22.04 LTS**，Docker 版本 **26.1.3** 的环境。

由于通过 GitHub Actions 构建的镜像存储在 GitHub Container Registry (GHCR)，在国内直接拉取可能会遇到网络问题。本指南提供两种方案。

---

## 方案一：直接拉取镜像 (推荐，最简单)

尝试直接从 GHCR 拉取预构建好的镜像。

### 1. 登录服务器
使用 SSH 登录您的腾讯云服务器。

### 2. 拉取镜像
```bash
# 尝试直接拉取
docker pull ghcr.io/abner20953/bidding-data:main

# 如果下载速度极慢或超时失败，请尝试方案二，或者配置 Docker 镜像加速/代理。
```

### 3. 重命名镜像 (可选，为了方便)
```bash
docker tag ghcr.io/abner20953/bidding-data:main bidding-app
```

### 4. 启动容器
我们将容器内部的 `7860` 端口映射到服务器的 `80` 端口（HTTP默认端口），并挂载 `results` 目录以持久化保存数据。

```bash
# 创建数据目录
mkdir -p /home/ubuntu/bidding-data/results
cd /home/ubuntu/bidding-data

# 启动容器
docker run -d \
  --name bidding-app \
  --restart always \
  -p 80:7860 \
  -v $(pwd)/results:/app/results \
  bidding-app
```

### 5. 验证
访问 `http://您的服务器公网IP`，应该能看到系统界面。

---

## 方案二：Gitee 极速构建 (推荐，最快最稳)

由于 GitHub 在国内访问不稳定，我们可以利用 **Gitee (码云)** 作为中转站。这是解决"速度慢"最彻底的方法。

### 1. 准备 Gitee 仓库 (在您的电脑上)
1.  登录 [Gitee](https://gitee.com/)，点击右上角 "+" -> "新建仓库"。
2.  仓库名填 `bidding-data`，设为**公开**或**私有**均可，点击创建。
3.  在您的本地项目目录，添加 Gitee 为远程仓库：

```bash
# 请将 <您的Gitee用户名> 替换为您真实的用户名
git remote add gitee https://gitee.com/lilac111/bidding-data.git

# 推送代码到 Gitee
git push -u gitee main
```

### 2. 在服务器上拉取代码
登录腾讯云服务器：

```bash
# 安装 git (通常已安装)
sudo apt update && sudo apt install -y git

# 克隆代码 (速度飞快)
git clone https://gitee.com/lilac111/bidding-data.git
cd bidding-data
```

### 3. 一键初始化环境
我们利用国内镜像源和本地计算来构建，无需下载 500MB 的模型文件（因为使用的是 BGE-Small，且我们会用国内源安装依赖）。

**关键优化**：
为了让构建更快，我们修改 `Dockerfile` 使用清华源（您可以直接在服务器上编辑，或者在本地改好再 push 到 Gitee）。

**推荐：直接在服务器上修改 Dockerfile**
```bash
nano Dockerfile
```
修改 `pip install` 行，增加清华源：
```dockerfile
# 原来的
RUN pip install --no-cache-dir -r requirements.txt --timeout 100

# 修改为
RUN pip install --no-cache-dir -r requirements.txt --timeout 100 -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 4. 构建并启动
```bash
# 1. 构建镜像 (因为模型只有 95MB，且走了国内源，这步会很快)
docker build -t bidding-app .

# 2. 启动容器
docker run -d \
  --name bidding-app \
  --restart always \
  -p 80:7860 \
  -v $(pwd)/results:/app/results \
  bidding-app
```

---

## 方案三：手动上传 (兜底方案)

如果直接拉取镜像失败，可以使用"传输代码 + 本地构建"的方式。此方案利用了您本地已经下好的模型文件，**无需服务器联网下载模型**，非常适合国内环境。

### 1. 本地准备 (在您的电脑上)
将代码传输到服务器。建议使用 SCP 或 SFTP 工具 (如 WinSCP, FileZilla)。
需要传输的文件/文件夹：
*   `dashboard/`
*   `model_data/` (**关键：包含 BGE 模型**)
*   `scraper.py`
*   `download_model.py`
*   `run.py`
*   `requirements.txt`
*   `Dockerfile`
*   `start.sh`

### 2. 修改 Dockerfile (在服务器上)
为了利用上传的 `model_data` 并加速 pip 安装，我们需要微调 `Dockerfile`。

编辑 `Dockerfile`：
```bash
nano Dockerfile
```

**修改点 1：使用本地模型**
找到：
```dockerfile
# 预下载 AI 模型...
RUN python -c "from sentence_transformers import SentenceTransformer; m = SentenceTransformer('BAAI/bge-small-zh-v1.5'); m.save('/app/model_data')"
```
改为：
```dockerfile
# 直接复制上传的本地模型
COPY model_data /app/model_data
```

**修改点 2：使用国内 pip 源**
找到：
```dockerfile
RUN pip install --no-cache-dir -r requirements.txt --timeout 100
```
改为：
```dockerfile
RUN pip install --no-cache-dir -r requirements.txt --timeout 100 -i https://pypi.tuna.tsinghua.edu.cn/simple
```

保存并退出 (`Ctrl+O`, `Enter`, `Ctrl+X`)。

### 3. 构建镜像
```bash
# 构建镜像 (注意最后有个点)
docker build -t bidding-app .
```
*由于利用了本地模型缓存和清华源，构建速度应该很快。*

### 4. 启动容器
```bash
docker run -d \
  --name bidding-app \
  --restart always \
  -p 80:7860 \
  -v $(pwd)/results:/app/results \
  bidding-app
```

---

## 常用维护命令

**查看日志**
```bash
docker logs -f bidding-app
```

**停止服务**
```bash
docker stop bidding-app
```

**更新版本 (方案一)**
```bash
docker pull ghcr.io/abner20953/bidding-data:main
docker stop bidding-app
docker rm bidding-app
# 重新运行启动命令...
```

**更新版本 (方案二)**
1. 上传新代码。
2. 重新执行 `docker build`。
3. 停止并删除旧容器，启动新容器。
