# Third-Party Code and Licenses

The project's own code is released under the MIT License in the root [LICENSE](LICENSE).
The files below contain third-party code or are derived from it. Their original
copyright and license headers are retained in the source files.

## React Bits — Aurora background component

- File: `apps/web/src/ui/Aurora.tsx`
- Source: React Bits registry `@reactbits/Aurora-TS-CSS`
- License: MIT + Commons Clause License Condition v1.0
- Copyright: Copyright (c) 2026 David Haz

This project uses the Aurora component as part of the application UI. The
Commons Clause restricts selling, sublicensing, or redistributing the component
itself; if you want to extract it as a standalone component library, read the
full React Bits license first.

## HiDream-O1-Image inference code

- Directory: `server/app/inference/hidream_o1/`
- Source: <https://github.com/HiDream-ai/HiDream-O1-Image>
- License: MIT License
- Copyright: Copyright (c) 2026 HiDream.ai

Files such as `pipeline.py`, `utils.py`, and `runtime.py` come from or are
adapted from the official HiDream-O1 inference implementation, with local
patches for the current transformers version.

## HuggingFace Diffusers / Qwen / Alibaba Wan files

The following files retain their original copyright and Apache-2.0 headers:

- `server/app/inference/hidream_o1/flash_scheduler.py`
  - Copyright 2024 Stability AI, Katherine Crowson and The HuggingFace Team
  - Apache License 2.0
- `server/app/inference/hidream_o1/qwen3_vl_transformers.py`
  - Copyright 2025 The Qwen Team and The HuggingFace Inc. team
  - Apache License 2.0
- `server/app/inference/hidream_o1/fm_solvers_unipc.py`
  - Adapted from HuggingFace Diffusers `scheduling_unipc_multistep.py`
  - Copyright 2024-2025 The Alibaba Wan Team Authors
  - Apache License 2.0

## Frontend dependencies

The npm dependencies listed in `apps/web/package.json` and
`apps/web/package-lock.json` are maintained by their respective authors. See
the LICENSE file inside each package for its terms (mainly MIT, ISC,
Apache-2.0, and BSD-3-Clause).
