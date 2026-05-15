"""回归：@ 单点路由要支持含空格的茶客名（如 ``Elon Musk``）。

旧实现 ``_AT_PATTERN = re.compile(r"@([^\\s...]+)")`` 在第一个空白处截断，``@Elon Musk``
只能捕到 ``Elon``，注册名是 ``Elon Musk`` → ``"Elon" in self._guests`` 为 False → @ 路由
miss → 悄悄掉进打分路径，能不能接全看打分模型今天心情。这里覆盖几个边界：

- 含空格的名字应该走 @ 单点路由
- 同前缀多名字（``Elon`` vs ``Elon Musk``）按最长前缀匹配
- ``@allies`` 不该被当 ``@all`` broadcast（词边界）
"""

from __future__ import annotations

from chahua.orchestrator import Orchestrator
from chahua.user_md import USER_SPEAKER_ID

from conftest import build_orch


def _say(orch: Orchestrator, text: str) -> None:
    orch.room.append(USER_SPEAKER_ID, text)


# ── @ 单点路由 ─────────────────────────────────────────────────────────────


def test_at_mention_matches_name_with_space():
    orch = build_orch("Elon Musk", "Yvonne")
    _say(orch, "@Elon Musk 你跟着 Trump 总统访华，有什么收获吗？")
    assert orch._find_user_mention() == "Elon Musk"


def test_at_mention_matches_chinese_name():
    orch = build_orch("唐三藏", "Yvonne")
    _say(orch, "@唐三藏 你怎么看")
    assert orch._find_user_mention() == "唐三藏"


def test_at_mention_longest_prefix_wins():
    """``Elon`` 与 ``Elon Musk`` 同时注册时，``@Elon Musk`` 应命中后者。"""
    orch = build_orch("Elon", "Elon Musk")
    _say(orch, "@Elon Musk hi")
    assert orch._find_user_mention() == "Elon Musk"


def test_at_mention_shorter_name_when_no_space():
    """``Elon`` 与 ``Elon Musk`` 同时注册时，``@Elon`` 单独命中 ``Elon``。"""
    orch = build_orch("Elon", "Elon Musk")
    _say(orch, "@Elon hi")
    assert orch._find_user_mention() == "Elon"


def test_at_mention_word_boundary_required():
    """``@Elonomics`` 不该匹到注册名 ``Elon``——后面是字母不是边界。"""
    orch = build_orch("Elon")
    _say(orch, "聊一下 @Elonomics 这个话题")
    assert orch._find_user_mention() is None


def test_at_mention_at_end_of_text():
    """``@Elon Musk`` 在句尾（无后续字符）也应该匹中。"""
    orch = build_orch("Elon Musk")
    _say(orch, "随便问问 @Elon Musk")
    assert orch._find_user_mention() == "Elon Musk"


def test_at_mention_followed_by_chinese_punct():
    """中文标点也是边界字符。"""
    orch = build_orch("Elon Musk")
    _say(orch, "@Elon Musk，你怎么看？")
    assert orch._find_user_mention() == "Elon Musk"


def test_at_mention_followed_by_english_period():
    """英文句号是常见边界 —— ``@Elon Musk.`` 在英文行尾很常见。"""
    orch = build_orch("Elon Musk")
    _say(orch, "Let's ask @Elon Musk.")
    assert orch._find_user_mention() == "Elon Musk"


def test_at_in_email_not_mention():
    """``foo@Elon.com`` 这种 email 不应被当作 @ 提及（``@`` 左边是字母）。"""
    orch = build_orch("Elon", "Elon Musk")
    _say(orch, "联系 foo@Elon.com 看看")
    assert orch._find_user_mention() is None


def test_at_at_start_after_quote_still_works():
    """开闭括号 / 引号是短语边界——``「@Elon Musk」`` 仍要识别。"""
    orch = build_orch("Elon Musk")
    _say(orch, "「@Elon Musk」你怎么看")
    assert orch._find_user_mention() == "Elon Musk"


def test_at_with_cjk_fullwidth_space():
    """CJK 全角空格 ``　`` 也算空白边界——中文输入法常打这个。"""
    orch = build_orch("唐三藏")
    _say(orch, "你好　@唐三藏　你怎么看")
    assert orch._find_user_mention() == "唐三藏"


def test_at_in_url_path_not_mention():
    """URL 里的 ``@`` 不该被当成提及——``@`` 左边是 ``/``，不属于短语边界。"""
    orch = build_orch("Elon", "Elon Musk")
    _say(orch, "看一下 https://x.com/@Elon/status 这条")
    assert orch._find_user_mention() is None


def test_no_mention_falls_through():
    orch = build_orch("Elon Musk", "Yvonne")
    _say(orch, "还有哪些老板跟你一起来的")
    assert orch._find_user_mention() is None


def test_at_not_at_message_end_ai_message():
    """只承认用户消息里的 @；AI 自己 @ 别人不走确定性路由。"""
    orch = build_orch("Elon Musk", "Yvonne")
    _say(orch, "随便聊聊")
    orch.room.append("Yvonne", "@Elon Musk 你怎么看？")
    assert orch._find_user_mention() is None


# ── @broadcast ────────────────────────────────────────────────────────────


def test_broadcast_at_all():
    orch = build_orch("Elon Musk", "唐三藏")
    _say(orch, "@all 都说两句")
    assert orch._find_user_broadcast() is True


def test_broadcast_at_chinese():
    orch = build_orch("Elon Musk")
    _say(orch, "@大家 都说两句")
    assert orch._find_user_broadcast() is True


def test_broadcast_at_all_case_insensitive():
    orch = build_orch("Elon Musk")
    _say(orch, "@ALL hi")
    assert orch._find_user_broadcast() is True


def test_broadcast_word_boundary_allies():
    """``@allies`` 不是 broadcast——``e`` 不是边界。"""
    orch = build_orch("Elon Musk")
    _say(orch, "@allies 大家好")
    assert orch._find_user_broadcast() is False


def test_broadcast_not_triggered_by_email():
    """``support@all.com`` 这种 email 不应触发 broadcast（``@`` 左边是字母）。"""
    orch = build_orch("Elon Musk")
    _say(orch, "联系 support@all.com")
    assert orch._find_user_broadcast() is False


def test_broadcast_not_triggered_by_hyphenated_word():
    """``@all-hands`` 是复合词，不该触发 broadcast——``-`` 不是短语边界。"""
    orch = build_orch("Elon Musk")
    _say(orch, "明天 @all-hands 开会")
    assert orch._find_user_broadcast() is False


def test_broadcast_skipped_in_mention_lookup():
    """``@all`` 出现在消息里时，``_find_user_mention`` 不能误把 ``all`` 当注册名。"""
    orch = build_orch("Elon Musk", "all")  # 极端：名字就叫 all
    _say(orch, "@all 看一下")
    # _find_user_mention 跳过 broadcast token，所以即使 "all" 注册了也不命中
    assert orch._find_user_mention() is None
