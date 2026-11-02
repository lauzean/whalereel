"""路由清单收集（33 号计费方案共用）：把 app 的全部路由收成 (方法, 路径模板) 清单。

**为什么不能直接枚举 `app.routes`**：这个 FastAPI 版本用惰性包含，被 `include_router`
挂进来的子路由藏在 `_IncludedRouter.original_router` 上——直接枚举顶层只看到 22 条，
其中仅 6 条带 path（实测，同 main.py 归属识别的踩坑记录）。必须递归下钻。

与 main.py 的 `_path_params_from_routes` 的关系：那边只收「带 { 参数」的模板（归属
判定用）；这里收**全量**（含静态路径，如 /api/tasks），供计费价目同步等清单类需求用。
"""

from __future__ import annotations


def collect_route_entries(app) -> list[tuple[str, str]]:  # noqa: ANN001
    """收集全部 HTTP 路由 (方法, 路径模板) 清单（同路径多方法分别成行）。

    跳过 HEAD/OPTIONS（浏览器自动发的，不是业务动作）与 WebSocket 路由
    （没有 methods 属性，天然被过滤）。
    """
    entries: list[tuple[str, str]] = []

    def visit(routes) -> None:  # noqa: ANN001
        for r in routes:
            original = getattr(r, "original_router", None)
            if original is not None and getattr(original, "routes", None):
                visit(original.routes)
                continue
            sub = getattr(r, "routes", None)
            if sub and not getattr(r, "path", None):
                visit(sub)
                continue
            path = getattr(r, "path", "") or ""
            methods = getattr(r, "methods", None)
            if not path or not methods:
                continue  # WebSocket / Mount 等无 methods 的项
            for m in sorted(methods):
                if m in ("HEAD", "OPTIONS"):
                    continue
                entries.append((m, path))

    visit(getattr(app.router, "routes", []))
    return entries
