"""房间 / 茶客 mutator facade。

P6.x 重构：本文件已切成 4 个职责文件 —— :mod:`chahua.admin_persona` /
:mod:`chahua.admin_room` / :mod:`chahua.admin_guest` / :mod:`chahua.admin_user`。
本模块仅作 reexport facade，让 ``from chahua.admin import xxx`` 老路径无破坏继续可用：

- ``persona`` 扫描 + ``sanitize_fs_name`` / ``normalize_room_id`` → :mod:`chahua.admin_persona`
- 房间 CRUD + ``[room.llm]`` / ``[scoring]`` / ``[summary]`` / ``[room]`` 编排参数 mutator
  + 共享 ``_write_text_and_validate`` / ``_rewrite_and_validate`` helper
  → :mod:`chahua.admin_room`
- 每位茶客的 mutator（add/remove/permission/llm/isolation/extra_mcp）→ :mod:`chahua.admin_guest`
- USER.md / 头像 / raw room.toml 编辑器 → :mod:`chahua.admin_user`

**`chahua.config` 只读 `room.toml`**；本系列 mutator 模块管所有写回去 / 建房 / 拆房的
可变操作。两边职责清晰：加载期严格白名单（错配置宁可炸），运行时 mutator 走这里。

**TOML 写出口径**：见 :mod:`chahua.admin_toml`（自带一个**只覆盖本仓库 schema** 的 mini
序列化器；不引 ``tomli_w``，理由见该文件 docstring）。

**改完即重载**：所有 mutator 函数返回 ``RoomConfig``（已经 ``load_room_config`` 跑过一遍校验）。
写盘失败 / 再加载失败都 raise 出去；调用方（server_inbound_admin 等）负责 emit 错误给前端。
"""

from __future__ import annotations

# persona 扫描 + 文件系统名规范化。
from .admin_persona import (  # noqa: F401
    PERSONAS_REL_DIR,
    _FS_NAME_FORBIDDEN,
    _FS_NAME_TRIM,
    _read_persona_toml_name,
    _search_roots,
    discover_personas,
    list_installed_personas,
    normalize_room_id,
    sanitize_fs_name,
)
# 房间级 mutator + snapshot 读写 helper（被 admin_guest / admin_user 共享）。
from .admin_room import (  # noqa: F401
    _ROOM_LLM_SECTIONS,
    _debug_config_to_dict,
    _persona_to_relative,
    _read_existing_for_mutate,
    _rewrite_and_validate,
    _room_config_to_dict,
    _validate_llm_spec_dict,
    _write_text_and_validate,
    create_room,
    delete_room,
    update_room_llm,
    update_room_orchestrator,
)
# 每位茶客的 mutator。
from .admin_guest import (  # noqa: F401
    _mutate_guest_in_snapshot,
    add_guest,
    remove_guest,
    update_guest_extra_mcp,
    update_guest_isolation,
    update_guest_llm,
    update_guest_permission,
)
# USER.md / 头像 / raw room.toml 编辑入口。
from .admin_user import (  # noqa: F401
    _AVATAR_MAX_BYTES,
    _PNG_MAGIC,
    _ROOM_TOML_MAX_BYTES,
    _USER_MD_MAX_BYTES,
    _user_md_target,
    parse_png_data_uri,
    update_room_toml,
    update_user_avatar,
    update_user_md,
)
