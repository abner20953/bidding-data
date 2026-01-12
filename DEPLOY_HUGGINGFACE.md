# Hugging Face Spaces 部署指南

本指南将指导您将招标采集系统部署到 Hugging Face Spaces。这是一个**永久免费**且**适合 AI 应用**的云托管平台。

## 📋 准备工作

1.  **注册账号**: 访问 [huggingface.co/join](https://huggingface.co/join) 注册一个账号。
2.  **准备代码**: 您的本地项目已经包含部署所需的所有文件：
    - `Dockerfile` (容器配置文件)
    - `requirements.txt` (依赖列表)
    - `dashboard/app.py` (已适配生产环境配置)

---

## 🚀 步骤一：创建 Space

1.  登录 Hugging Face。
2.  点击页面右上角的头像，选择 **"New Space"**。
3.  填写配置表单：
    *   **Space name**: 输入项目名称，例如 `shanxi-bidding-monitor`。
    *   **License**: 选择 `Apache 2.0` 或 `MIT`。
    *   **Select the Space SDK**: ⚠️ **必须选择 Docker** (不要选 Streamlit/Gradio)。
    *   **Docker Template**: 选择 `Blank`。
    *   **Space Hardware**: 保持默认的 `Free` (2vCPU · 16GB · CPU basic)。
    *   **Visibility**:为了保护数据安全，建议选择 **Private** (私有)。
4.  点击 **"Create Space"** 按钮。

---

## 📤 步骤二：上传代码

Space 创建成功后，您会看到一个类似 GitHub 的仓库页面。我们需要把本地代码推送到这里。

### 方法 A：使用 Git 命令行 (推荐)

在您的 VS Code 终端中执行以下命令（请将 `<您的用户名>` 和 `<Space名称>` 替换为实际值）：

```bash
# 1. 登录 Hugging Face (如果尚未登录)
# 会提示输入 Token，去 https://huggingface.co/settings/tokens 创建一个 Write 权限的 token
git config --global credential.helper store

# 2. 添加远程仓库
# 注意：Space 的 Git 地址通常是 https://huggingface.co/spaces/您的用户名/Space名称
git remote add hf https://huggingface.co/spaces/您的用户名/shanxi-bidding-monitor

# 3. 推送代码
# -f 参数用于强制覆盖 Space 默认生成的 README
git push -f hf main
```

### 方法 B：网页上传 (超简单)

如果不熟悉命令行，可以直接在网页操作：

1.  在 Space 页面点击 **"Files"** 标签页。
2.  点击右上角的 **"Add file"** -> **"Upload files"**。
3.  直接将本地项目文件夹中的**所有文件**拖进去（除了 `.venv`, `.git`, `__pycache__` 目录）。
4.  在底部 "Commit changes" 处点击 **"Commit changes to main"**。

---

## ⏳ 步骤三：等待构建

代码上传后，Hugging Face 会自动开始构建：

1.  点击页面顶部的 **"App"** 标签页。
2.  您会看到状态显示为 **"Building"** (蓝色)。
3.  点击 "Logs" 可以查看构建进度。
    *   *注意：首次构建需要下载 Python 镜像和安装依赖，可能需要 3-5 分钟，请耐心等待。*
4.  当状态变为 **"Running"** (绿色) 时，应用就部署成功了！

您可以直接在网页中看到您的采集系统界面。

---

## 💾 关键说明：关于数据保存

**⚠️ 重要提示**：Hugging Face Spaces 的免费容器是"临时"的。
当 Space 重启（更新代码或休眠唤醒）时，**容器内的所有文件（包括 `results` 目录下的 Excel）都会被重置**。

**解决方案**：

1.  **及时下载**: 每次采集任务完成后，请立即点击列表中的链接下载 Excel 文件到本地。
2.  **避免长时间闲置**: 虽然 Space 会休眠，但只要您处于打开状态，生成的临时文件是存在的。
3.  **(进阶) 持久化存储**:
    *   如果需要长期在云端保存数据，可以进入 Space 的 **Settings**。
    *   找到 **Persistent Storage** 部分。
    *   申请一个小额的存储空间 (如 SSD Tier, 需绑定信用卡付费，约 $5/月)。
    *   申请后，系统会挂载一个 `/data` 目录，您需要修改代码将 Excel 保存到 `/data` 下才能永久保留。

---

## 🛠️ 常见问题

**Q: 为什么打开页面显示 "Runtime Error"?**
A: 请点击 Logs 查看报错。常见原因是依赖安装失败。目前项目配置已包含 `requirements.txt`，通常不会出错。

**Q: 休眠了怎么唤醒？**
A: 免费版 Space 在 48 小时无访问后会进入 Sleep 模式。下次访问时会显示 "Building"，等待 1-2 分钟即可自动唤醒。

**Q: 手机能访问吗？**
A: 可以。直接使用浏览器访问 `https://huggingface.co/spaces/您的用户名/Space名称` 即可。如果设置了 Private，需要先登录 Hugging Face 账号。
