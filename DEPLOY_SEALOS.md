# Sealos Cloud 部署指南 (极简版)

本文档将指导您在 [Sealos Cloud](https://sealos.io/) (国内版域名通常是 `cloud.sealos.run` 或 `sealos.top`，请搜索 "Sealos 公有云") 上部署您的应用。

鉴于您可能不想折腾 Docker 镜像构建，我们采用**“运行时拉取代码”**的懒人方案。

## 📋 准备工作

1.  注册 Sealos 账号 (支持微信/手机号登录)。
2.  确保您的 GitHub 仓库是**公开 (Public)** 的 (目前是 `https://github.com/abner20953/bidding-data`)，否则服务器无法下载代码。

---

## 🚀 部署步骤

### 1. 进入 "应用管理" (App Launchpad)

登录 Sealos 桌面后，点击图标类似 "火箭" 🚀 的应用 (**App Launchpad** / **应用管理**)。

### 2. 新建应用

点击右上角的 **"新建应用" (New Application)**。

### 3. 填写基本配置

请严格按照以下内容填写：

*   **应用名称 (Name)**: `bidding-monitor`
*   **镜像名 (Image Name)**: `python:3.9`
*   **CPU**: 推荐 `0.5 Core` (够用了)
*   **Memory**: 推荐 `512 MB` 或 `1 GB` (AI 模型加载需要内存，建议先选 1G，跑通后再降)
*   **多副本**: `1`

### 4. 关键配置：启动命令 (Command)

开启 **"高级配置"** 或找到 **"运行命令" (CMD/Args)** 设置项。

在 **"命令 (CMD)"** 或 **"启动参数"** 中，开启 `Shell` 模式，并填入以下**整段**脚本（这是一个自动下载代码并运行的魔法脚本）：

```bash
rm -rf bidding-data && git clone https://github.com/abner20953/bidding-data.git && cd bidding-data && pip install -r requirements.txt && gunicorn -w 1 -b 0.0.0.0:8080 --timeout 120 dashboard.app:app
```

**终极修正填法 (必看)**：

1.  **Command (CMD)** 输入框：直接填上面那一整行代码。
2.  **Arguments (Args)** 输入框：**留空**。
3.  **Shell 开关**：**必须开启 (ON)**。

*如果开启 Shell 后还是报错，尝试下面这个备选方案（不开启 Shell）：*

*   **CMD**: `/bin/sh`
*   **Args**: `-c,rm -rf bidding-data && git clone https://github.com/abner20953/bidding-data.git && cd bidding-data && pip install -r requirements.txt && gunicorn -w 1 -b 0.0.0.0:8080 --timeout 120 dashboard.app:app`
    *(注意：Args 里用逗号 `,` 分隔 `-c` 和后面的长命令，或者直接回车换行让它们变成两个参数块)*

### 5. 网络配置 (Network)

*   **容器端口 (Container Port)**: `8080` (注意这里必须填 8080，因为上面的启动命令里指定了 8080)
*   **公网访问 (Public Access)**: **开启**。
    *   开启后，系统会分配一个随机域名给你（例如 `bidding-monitor.xf3s2.sealos.run`）。

### 6. 点击部署 (Deploy)

点击右上角的 **"部署" (Deploy)** 按钮。

---

## 👀 查看状态

1.  部署后，列表里会出现您的应用，状态通常是 `Pending` -> `Creating` -> `Running`。
2.  点击应用名称进入详情页。
3.  点击右侧的 **"日志" (Logs)** 图标。
    *   **关键点**：您会看到 pip 正在安装依赖 (`Installing collected packages...`)。
    *   **耐心等待**：因为需要现场安装 `sentence-transformers` 和 `torch`，并且下载 AI 模型，**首次启动可能需要 3-5 分钟**。
    *   看到 `[INFO] Listening at: http://0.0.0.0:8080` 字样时，说明成功了！

### 7. 访问应用

点击详情页中 **"公网地址"** 旁边的链接，即可打开您的监控系统。

---

## 💾 如何更新代码？

当您在 GitHub 上更新了代码后：

1.  回到 Sealos 的应用详情页。
2.  点击右上角 **"重启" (Restart)**。
3.  容器重启不仅会重启服务，还会重新执行 `git clone` 拉取您最新的代码。

---

## 💰 费用说明

*   Sealos 是**按量计费**。
*   我们配置了 0.5核/1G内存，大概每小时花费不到￥0.05（具体看运行占用）。
*   **省钱技巧**：如果不看的时候，可以将副本数调整为 `0` (暂停)，需要用时再调回 `1`。这样暂停期间不扣计算费（只扣很少的硬盘存储费）。

祝部署顺利！
