---
title: Bidding Data Monitor
emoji: 📊
colorFrom: blue
colorTo: indigo
sdk: docker
pinned: false
---

# 山西省信息化项目采集监控系统

针对 [中国政府采购网](http://www.ccgp.gov.cn/) 的自动化数据采集与监控工具，专用于筛选**山西省**范围内的**信息化建设**相关项目。

## ✨ 功能特性

- **双重搜索机制**：自动通过 "开标时间" 和 "开启时间" 双维度搜索，确保数据零遗漏。
- **智能语义分析**：使用 `Sentence-Transformers` 模型对项目标题进行语义匹配，精准识别信息化相关项目。
- **深度数据提取**：自动进入详情页提取开标地点、开标时间、采购人、代理机构等关键信息。
    - 支持 "开标地点"、"投标地点"、"提交投标文件地点"、"响应文件开启地点" 等多种字段格式。
- **可视化看板**：提供 Web 界面日历，支持可视化选择日期进行采集、查看进度、管理数据。
- **数据管理**：支持导出 Excel，并提供界面化的历史数据清除功能。

## 🛠️ 技术栈

- **后端**: Python, Flask
- **爬虫**: Requests, BeautifulSoup
- **数据处理**: Pandas, OpenPyXL
- **AI 模型**: Sentence-Transformers (Hugging Face)
- **前端**: HTML5, CSS3 (Glassmorphism UI), Vanilla JS

## 🚀 快速开始

### 1. 安装依赖

确保已安装 Python 3.8+。

```bash
pip install -r requirements.txt
```

### 2. 启动服务

```bash
python run.py
```

服务启动后，浏览器访问 [http://localhost:5000](http://localhost:5000)。

### 3. 使用说明

1.  在网页日历中点击要采集的日期（支持多选）。
2.  点击 **"🚀 采集数据"** 按钮。
3.  系统将自动在后台进行搜索、爬取、分析和保存。
4.  采集完成后，日期下方会显示绿色圆点。
5.  点击 **"开标时间"** 标题旁的链接可下载生成的 Excel 文件。

## 📂 目录结构

```
.
├── dashboard/          # Web 前端与 API 代码
│   ├── static/         # CSS, JS 资源
│   ├── templates/      # HTML 模板
│   └── app.py          # Flask 应用入口
├── results/            # 采集结果 (Excel) 存放目录 (自动忽略)
├── scraper.py          # 核心爬虫逻辑

├── run.py              # 项目启动脚本
├── requirements.txt    # 项目依赖
└── README.md           # 项目说明
```

## ⚠️ 注意事项

- 本工具仅供学习研究使用，通过公开渠道获取数据。
- 请遵守目标网站的 `robots.txt` 规则（代码中已内置延时机制以降低负载）。
