# 鲸影 WhaleReel

<img src="apps/web/public/brand/logo.png" width="88" alt="WhaleReel">

无限画布 AI 视频创作工作台：把文本、图片、视频、音频放进一张可自由连线的画布，覆盖 AI 短剧/漫剧的剧本解析、逐集审核、资产提取、分镜、首帧、视频生成与合成全链路。

> 本仓库是开源代码版本，**不包含**内部设计文档（`docs/`）和本地运行数据。代码注释里出现的 `docs/design/...` 是内部设计文档编号，仅作历史说明。

## 功能特性

- **无限画布**：节点化编排素材与生成任务，支持拖拽连线、分组、版本回溯；WebSocket 实时推送任务进度。
- **短剧/漫剧流水线**：剧本解析 → 逐集审核 → 角色/场景/道具资产 → 分镜 → 首帧 → 视频 → 审片 → 合成导出。
- **多模型服务端点**：OpenAI 兼容接口、火山方舟（Seedream/Seedance）、Vidu、ComfyUI 本地端点等；可在管理界面在线配置与探测模型列表，API Key 脱敏展示。
- **本地推理（可选）**：支持 MiniMax H3、Wan 2.2、Flux.1、Qwen-Image 等本地模型的资源池调度；未安装推理依赖时可使用 mock 模式跑通全链路。
- **多用户与数据隔离**：内置管理员账号，业务数据按 `owner_user_id` 隔离，越权访问统一返回 404。
- **积分计费（可选）**：预扣/结算、按调用计费、失败自动退回。
- **移动端适配**：桌面、平板、手机三档布局。

## 技术栈

| 层 | 技术 |
| --- | --- |
| 后端 | Python 3.11+、FastAPI、SQLAlchemy、SQLite、WebSocket |
| 前端 | React 19、TypeScript、Vite 6、React Flow（@xyflow/react）、zustand |
| 生成 | OpenAI 兼容端点 / 火山方舟 / Vidu / ComfyUI / 本地 Diffusers 推理 |

## 目录结构

```text
apps/web/                 前端（React + Vite）
  src/api/                REST 与 WebSocket 客户端
  src/canvas/             无限画布
  src/drama/              短剧/漫剧工作区
  src/pages/              登录、系统管理等页面
  src/styles.css          桌面与平板样式
  src/styles-mobile.css   手机样式

server/
  app/
    main.py               FastAPI 入口
    api/                  REST 路由
    agent/                LLM 提示词、技能包、编排
    core/                 配置、模型服务、任务引擎、缓存、计费
    db/                   ORM 模型与会话
    runners/              任务执行器
    script/               剧本解析与提示词组装
    inference/            本地推理管线
  test_*.py               独立测试脚本
  pyproject.toml          后端依赖
  .env.example            配置模板
```

## 快速开始

### 1. 启动后端

需要 Python 3.11 或更高版本。

```bash
cd server
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e .

# 复制配置模板（Windows PowerShell 用 Copy-Item .env.example .env）
cp .env.example .env

uvicorn app.main:app --host 127.0.0.1 --port 8800
```

首次启动会自动创建 SQLite 数据库（`server/data/studio.db`）和内置管理员账号：

- 用户名：`admin`
- 密码：`admin123`
- 首次登录后系统会要求修改密码。

接口文档：<http://127.0.0.1:8800/docs>

### 2. 启动前端

需要 Node.js 18 或更高版本。

```bash
cd apps/web
npm install
npm run dev
```

浏览器打开 <http://localhost:5173>。Vite 已把 `/api`、`/files`、`/ws` 代理到 `127.0.0.1:8800`。

### 3. 没有 GPU 或模型时先跑通链路

未配置第三方模型、也没有安装本地推理依赖时，可以强制使用 mock 模式：

```bash
# Windows
set AVS_MOCK=1
# macOS / Linux
export AVS_MOCK=1

uvicorn app.main:app --host 127.0.0.1 --port 8800
```

mock 模式会生成占位图片/视频，用于验证画布、任务队列、短剧流水线等完整交互。

## 配置模型服务

两种方式，任选其一：

1. 复制 `server/.env.example` 为 `server/.env`，填写对应变量；
2. 登录后进入「系统管理 → 模型服务」，在线新增 Key 与端点。

主要环境变量（每类任务独立配置）：

```text
AVS_LLM_PROVIDER / AVS_LLM_BASE_URL / AVS_LLM_API_KEY / AVS_LLM_MODEL
AVS_T2I_PROVIDER / AVS_T2I_BASE_URL / AVS_T2I_API_KEY / AVS_T2I_MODEL
AVS_T2V_PROVIDER / AVS_T2V_BASE_URL / AVS_T2V_API_KEY / AVS_T2V_MODEL
AVS_I2V_PROVIDER / AVS_I2V_BASE_URL / AVS_I2V_API_KEY / AVS_I2V_MODEL
AVS_TTS_PROVIDER / AVS_TTS_BASE_URL / AVS_TTS_API_KEY / AVS_TTS_MODEL
```

其他常用变量见 `server/.env.example`。API Key 只保存在本地 `.env` 或本地 SQLite 中，列表接口只返回前 4 位和后 4 位。

### 本地推理（可选）

```bash
cd server
pip install -e ".[inference]"
# 需要配音时
pip install -e ".[tts]"
```

本地推理需要 NVIDIA GPU 和对应 CUDA 版本的 PyTorch，模型权重会下载到 `server/models/`（体积较大，请先确认磁盘与显存）。不安装也能使用远程模型服务或 mock 模式。

## 运行测试

测试是独立脚本，建议在仓库根目录执行。以 Windows 为例：

```bash
server/.venv/Scripts/python server/test_auth_multitenant.py
server/.venv/Scripts/python server/test_billing.py
server/.venv/Scripts/python server/test_calls_log.py
server/.venv/Scripts/python server/test_comfyui.py
server/.venv/Scripts/python server/test_media_signature.py
server/.venv/Scripts/python server/test_model_skills.py
server/.venv/Scripts/python server/test_reliability_fixes.py
server/.venv/Scripts/python server/test_stream_events.py
server/.venv/Scripts/python server/test_version_trace.py
server/.venv/Scripts/python server/test_vidu.py
```

macOS / Linux 把 `server/.venv/Scripts/python` 换成 `server/.venv/bin/python`。测试使用临时数据目录和 mock 模式，不会污染 `server/data/`，也不需要外部 API Key。

## 部署与安全提醒

- 首次登录后立即修改默认管理员密码。
- `server/.env`、`server/data/`、`server/models/` 已被 `.gitignore` 排除，不要提交密钥或运行数据。
- 对外部署时建议配置 HTTPS 和反向代理，并通过 `AVS_CORS_ORIGINS` 收紧跨域来源。
- 生产环境请按实际用户规模配置数据库备份、日志轮转和访问控制。

## 第三方代码与许可

本项目以 MIT 许可证开源。仓库中包含少量第三方代码，其许可与版权见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)：

- `apps/web/src/ui/Aurora.tsx`：来自 React Bits，遵循 MIT + Commons Clause v1.0。
- `server/app/inference/hidream_o1/`：部分文件来自 HiDream-O1-Image（MIT）、HuggingFace Diffusers、Qwen、Alibaba Wan（Apache-2.0），原始版权与许可头已保留。
- 前端 npm 依赖的许可见 `apps/web/package-lock.json`。

## 许可证

[MIT](LICENSE)

## 贡献

欢迎提交 Issue 和 Pull Request。提交前请先运行上面的测试脚本，并确保没有提交任何密钥、本地数据库或生成产物。
