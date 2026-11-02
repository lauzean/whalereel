# 第三方代码与许可说明

本项目的自有代码以根目录 [LICENSE](LICENSE) 中的 MIT 许可证发布。
以下文件包含第三方代码或由其改编而来，分别遵循原始许可证；相关版权与许可头已保留在源文件中。

## React Bits — Aurora 背景组件

- 文件：`apps/web/src/ui/Aurora.tsx`
- 来源：React Bits 注册表 `@reactbits/Aurora-TS-CSS`
- 许可证：MIT + Commons Clause License Condition v1.0
- 版权：Copyright (c) 2026 David Haz

本项目将 Aurora 组件作为应用界面的一部分使用。Commons Clause 限制的是单独销售、再许可或 redistributing 组件本身；如需把该组件单独抽出作为组件库分发，请先阅读 React Bits 的完整许可条款。

## HiDream-O1-Image 推理代码

- 目录：`server/app/inference/hidream_o1/`
- 来源：<https://github.com/HiDream-ai/HiDream-O1-Image>
- 许可证：MIT License
- 版权：Copyright (c) 2026 HiDream.ai

其中 `pipeline.py`、`utils.py`、`runtime.py` 等文件来自或改编自 HiDream-O1 官方推理实现，并包含为适配当前 transformers 版本所做的本地补丁。

## HuggingFace Diffusers / Qwen / Alibaba Wan 相关文件

以下文件保留原始版权与 Apache-2.0 许可头：

- `server/app/inference/hidream_o1/flash_scheduler.py`
  - Copyright 2024 Stability AI, Katherine Crowson and The HuggingFace Team
  - Apache License 2.0
- `server/app/inference/hidream_o1/qwen3_vl_transformers.py`
  - Copyright 2025 The Qwen Team and The HuggingFace Inc. team
  - Apache License 2.0
- `server/app/inference/hidream_o1/fm_solvers_unipc.py`
  - 改编自 HuggingFace Diffusers `scheduling_unipc_multistep.py`
  - Copyright 2024-2025 The Alibaba Wan Team Authors
  - Apache License 2.0

## 前端依赖

`apps/web/package.json` 与 `apps/web/package-lock.json` 中列出的 npm 依赖由各自的作者维护，许可证以各依赖包内的 LICENSE 文件为准（主要包括 MIT、ISC、Apache-2.0、BSD-3-Clause 等）。
