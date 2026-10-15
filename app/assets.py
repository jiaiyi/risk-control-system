# -*- coding: utf-8 -*-
"""静态资源版本号（缓存击穿）

**为什么需要它**

`app/main.py` 给 `/static/**` 加了 `Cache-Control: no-cache`，意思是「每次都要
回源校验」。但 `no-cache` 只保证**以后**不再盲缓存 —— 浏览器里**已经**存下的旧副本
（在没这个响应头的时候缓存的）仍可能继续生效。真实踩到的现象是：改了 `static/js/app.js`
加了 `qs()` 函数，模板已经引用它，可用户的浏览器还在跑旧 JS，于是
`ReferenceError: qs is not defined`，规则页表格永远转圈 —— 而服务端和 headless
浏览器实测都完全正常，极难自查。

所以这里给 URL 加一个随文件内容变化的查询串（`?v=<hash>`）。
URL 一变，浏览器就不可能命中旧缓存，且**不需要用户清缓存**。

用文件 mtime+size 派生，而不是读全文算哈希：模板每次渲染都要取值，
读全文会把每个页面请求都变成一次磁盘 IO。mtime+size 对「改代码」这个场景
足够敏感，成本几乎为零。
"""
from __future__ import annotations

import functools
from pathlib import Path

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@functools.lru_cache(maxsize=32)
def _stamp(rel_path: str) -> str:
    """单个静态资源的指纹：mtime 纳秒 + 字节数。"""
    path = _STATIC_DIR / rel_path
    try:
        st = path.stat()
    except OSError:
        return "0"
    return f"{int(st.st_mtime_ns):x}{st.st_size:x}"


def asset_version() -> str:
    """全站资源版本号：取关键资源指纹的最大值。

    做成「全站一个号」而不是「每个文件一个号」，是为了让模板里直接写
    `{{ asset_v }}` 即可 —— 少一个参数就少一处漏改。代价是改任意一个
    资源会让所有资源重新下载，本地演示场景下完全可接受。
    """
    stamps = [_stamp(p) for p in (
        "js/app.js", "css/app.css", "css/login.css",
        "vendor/bootstrap.min.css", "vendor/bootstrap.bundle.min.js",
        "vendor/echarts.min.js",
    )]
    return max(stamps, default="0")


def clear_asset_cache() -> None:
    """开发期用：清掉指纹缓存（改完静态文件想立刻看到新版本时调）。"""
    _stamp.cache_clear()


def register_template_globals(templates) -> None:
    """把 asset_v / can 注册进 Jinja 环境。

    注册成 Jinja 全局而不是每个 `_render()` 都传，有两个原因：
    1. 登录页（`app/api/auth_api.py`）和错误页都不走 `_render()`，
       少传一次就会退回无版本号的老 URL，等于缓存击穿被绕过；
    2. `can()` 要在每个子页面的 block 里用，靠 `_render()` 传参数
       意味着每个页面都得自己转发一次，漏一个就静默失效。

    **为什么 `can` 不做成模板里的 lambda**：Jinja2 不支持 lambda 字面量
    （`{% set f = lambda x: ... %}` 直接 SyntaxError），只能用全局函数。
    """
    templates.env.globals["asset_v"] = asset_version

    def _can(permission: str, user_perms=None) -> bool:
        """判断一组权限里是否含某权限码。

        用法：`{% if can("rules:manage", user_perms) %}`。
        显式传 `user_perms` 是因为 Jinja 宏/块作用域里拿不到外部变量，
        隐式依赖会随模板结构变化而静默失效。
        """
        return bool(user_perms) and permission in user_perms

    templates.env.globals["can"] = _can
