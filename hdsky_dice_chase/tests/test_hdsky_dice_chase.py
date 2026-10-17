#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hdsky_dice_chase 的纯逻辑与客户端判定测试。

全部离线：网络层用假 transport 注入，解析用 fixture —— 运行时不产生任何真实请求。
    python3 -m unittest discover -s tests
"""

import io
import json
import logging
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hdsky_dice_chase as chase  # noqa: E402

FIXTURE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "list_page.html")


# --------------------------------------------------------------------- 工具
def fixture_html() -> str:
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        return fh.read()


def row_html(topicid, result=None, locked=False, time_text="2026-10-08 15:00"):
    tail = f" 【 {result} 1,2,3 】" if result else ""
    lock = ' <img class="locked" src="pic/locked.gif">' if locked else ""
    return (
        f'<tr><td class="rowfollow">'
        f'<a href="forums.php?action=viewtopic&forumid=71&topicid={topicid}">'
        f"本轮开奖时间 {time_text}{tail}</a>{lock}</td></tr>"
    )


# 现场样本（2026-10-13 14:11，topicid=78016）：论坛对「间隔不足的第二注」回 200 + 拒绝页，
# 正文里认不出任何拒绝文案（extract_rejection_reason 还会先抓到页头导航单元格）。
# 它表示「没收下」而非「已收单」：同一用户隔 12–18 秒再发就能落单（见 ComboExecutionTests）。
INDEX_PAGE_200 = (
    '<html><head><title>HDSky :: 首页</title></head><body><table>'
    '<tr><td class="text">&nbsp;首&nbsp;&nbsp;页&nbsp; &nbsp;论&nbsp;&nbsp;坛&nbsp; '
    "EMBY订阅 4K BluRay</td></tr></table></body></html>"
)


def page_html(rows, total_pages=None):
    pager = ""
    if total_pages and total_pages > 1:
        links = " ".join(
            f'<a href="forums.php?action=viewforum&forumid=71&page={page}">{page}</a>'
            for page in range(2, total_pages + 1)
        )
        pager = f'<p class="pager">{links}</p>'
    return f"<html><body><table>{''.join(rows)}</table>{pager}</body></html>"


def topic_page_html(posts, uid=109991):
    """构造帖子页：导航里带「欢迎回来 , <用户名>」（当前 uid），每楼一个 pidNNNbody。

    posts: [(作者 uid, 发帖时间, 正文), ...]
    结构与现场页面一致（2026-10-17 抓的 topicid=78668）：作者链接在楼层头、正文在
    `<div id="pidNNNbody">` 里，左侧资料栏不含 userdetails 链接。
    """
    parts = [
        '<html><body><span class="medium">欢迎回来 , <span class="nowrap">'
        f'<a href="userdetails.php?id={uid}" class=\'PowerUser_Name\'><b>me</b></a>'
        '</span> [<a href="logout.php">退出</a>]</span>'
    ]
    for i, (author, when, text) in enumerate(posts):
        parts.append(
            f'<a href="forums.php?action=viewtopic&amp;topicid=1005&amp;page=p{i}#pid{i}">#{i}</a>'
            f'<span class="nowrap"><a href="userdetails.php?id={author}" '
            f"class='PowerUser_Name'><b>u{author}</b></a></span>"
            f'<span title="{when}">刚刚</span>'
            f'<div id="pid{i}body">{text}</div>'
        )
    parts.append("</body></html>")
    return "".join(parts)


def make_cfg(**overrides):
    base = dict(
        cookie="c_secure_uid=fake; c_secure_pass=fake",
        target_type="豹子",
        amount=100000,
        threshold=3,
        dry_run=True,
        max_total_stake=1000000,
        max_consecutive_bets=20,
        poll_interval=15.0,
        min_poll_interval=3.0,
        max_list_pages=10,
        state_file="state.json",
        lock_file="lock",
        mode="chase",
        combo_amount=100,
        combo_types=["大", "小"],
    )
    base.update(overrides)
    return chase.Config(**base)


def write_config(directory, **overrides):
    data = dict(
        cookie="c_secure_uid=abc; c_secure_pass=def",
        target_type="豹子",
        amount=100000,
        threshold=3,
        poll_interval=15,
        min_poll_interval=3,
        max_list_pages=5,
        dry_run=True,
        max_total_stake=500000,
        max_consecutive_bets=4,
        state_file=os.path.join(directory, "state.json"),
        lock_file=os.path.join(directory, "lock"),
        log_file=None,
        mode="chase",
        combo_amount=100,
        combo_types=["大", "小"],
    )
    data.update(overrides)
    path = os.path.join(directory, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return path


class FakeTransport:
    """按顺序吐出预置响应；预置成异常对象就抛出来。"""

    def __init__(self, get_results=None, post_results=None):
        self._gets = list(get_results or [])
        self._posts = list(post_results or [])
        self.get_urls = []
        self.post_calls = []

    def get(self, url):
        self.get_urls.append(url)
        return self._next(self._gets, "GET")

    def post(self, url, form):
        self.post_calls.append((url, form))
        return self._next(self._posts, "POST")

    @staticmethod
    def _next(queue, kind):
        if not queue:
            raise AssertionError(f"假 transport 收到了计划外的 {kind} 请求")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def fake_client(transport, clock_start=0.0):
    """sleep 不真的睡，clock 由 sleep 推进 —— 便于断言间隔。"""
    state = {"now": clock_start}

    def clock():
        return state["now"]

    def sleep(seconds):
        state["now"] += seconds

    return chase.ForumClient(transport, sleep=sleep, clock=clock), state


# --------------------------------------------------------------------- 配置
class ConfigTests(unittest.TestCase):
    def test_valid_config_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = chase.load_config(write_config(tmp, threshold=7, amount=50000))
        self.assertEqual(cfg.threshold, 7)
        self.assertEqual(cfg.amount, 50000)
        self.assertEqual(cfg.target_type, "豹子")

    def test_amount_below_min_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(chase.ConfigError) as ctx:
                chase.load_config(write_config(tmp, amount=99))
        self.assertIn(str(chase.AMOUNT_MIN), str(ctx.exception))
        self.assertIn(str(chase.AMOUNT_MAX), str(ctx.exception))

    def test_amount_above_max_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(chase.ConfigError):
                chase.load_config(write_config(tmp, amount=100001))

    def test_threshold_zero_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(chase.ConfigError):
                chase.load_config(write_config(tmp, threshold=0))

    def test_invalid_target_type_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(chase.ConfigError):
                chase.load_config(write_config(tmp, target_type="对子"))

    def test_missing_file_rejected(self):
        with self.assertRaises(chase.ConfigError) as ctx:
            chase.load_config("/definitely/not/here.json")
        self.assertIn("不存在", str(ctx.exception))

    def test_bad_json_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{ not json")
            with self.assertRaises(chase.ConfigError) as ctx:
                chase.load_config(path)
        self.assertIn("JSON", str(ctx.exception))

    def test_missing_cookie_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            del data["cookie"]
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            with self.assertRaises(chase.ConfigError) as ctx:
                chase.load_config(path)
        self.assertIn("cookie", str(ctx.exception))

    def test_unknown_field_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(chase.ConfigError) as ctx:
                chase.load_config(write_config(tmp, typo_field=1))
        self.assertIn("未知字段", str(ctx.exception))

    def test_cli_override_wins_over_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, amount=50000, threshold=4)
            cfg = chase.load_config(path, {"amount": 70000, "threshold": None})
        self.assertEqual(cfg.amount, 70000)
        self.assertEqual(cfg.threshold, 4)


# ------------------------------------------------------------------- 列表页解析
class ParserTests(unittest.TestCase):
    """用例基于一份真实列表页 HTML 存档（见 tests/fixtures/list_page.html）：

    这是用浏览器从 hdsky.me 另存的「Webpage, HTML Only」快照，里面混着
    Chrome 扩展的 DOM（immersive-translate / Surfingkeys）和 HDSky 自己的结构。
    解析器要能在这种有噪声的真实页面上正确抽到「本轮开奖时间」行。

    下面的精确断言（topicid 列表、drought 数值、list_total_pages）都是该快照
    在抓取那一刻的快照值；当 forum 跑出新轮、快照变成"过时"时这些值会变化，
    重新登录并另存一次 fixtures/list_page.html 即可更新。结构性断言（去重、
    行级锁定、开放/锁定的集合关系、draw_time 全可解析）则不随时效变化。
    """

    def setUp(self):
        self.raw = chase.parse_rounds(fixture_html())
        self.rounds = chase.dedupe_rounds(self.raw)
        self.by_id = {r.topicid: r for r in self.rounds}

    def test_fixture_parses_expected_rounds_in_dom_order(self):
        # 快照值（2026-10-08 抓取）：最新在前、topicid 77229 在原帖里已被删
        self.assertEqual(
            [r.topicid for r in self.rounds],
            [77245, 77244, 77243, 77242, 77241, 77240, 77239, 77238, 77237,
             77236, 77235, 77234, 77233, 77232, 77231, 77230, 77228],
        )

    def test_duplicate_topic_counted_once(self):
        # 真实快照里没有重复的 topicid；结构性断言是 raw 和 dedupe 等长
        self.assertEqual(len(self.raw), len(self.rounds))

    def test_drought_counts_consecutive_misses(self):
        # 快照里所有已开奖轮都不是「豹子」，所以豹子连旱 = 16
        self.assertEqual(chase.compute_drought(self.rounds, "豹子"), 16)
        # 结构性：最新一已开奖轮是「大」时，大 的连旱 = 0
        self.assertEqual(chase.compute_drought(self.rounds, "大"), 0)

    def test_open_rounds_exclude_locked(self):
        # 快照里只有 77245 是开放轮；其余都锁定
        self.assertEqual([r.topicid for r in self.rounds if r.open], [77245])
        self.assertFalse(self.by_id[77245].locked)
        # 与开放轮紧邻的上一轮（77244）是锁定的，验证「行级锁定」不会跨行误标
        self.assertTrue(self.by_id[77244].locked)

    def test_locked_marker_is_scoped_to_its_own_row(self):
        # KTD7：77244 的 <img class="locked*"> 不能污染相邻的 77245
        self.assertFalse(self.by_id[77245].locked)

    def test_malformed_draw_time_does_not_affect_drought(self):
        # 真实快照的开奖时间都形如 YYYY-MM-DD HH:MM:SS，都解析成功
        for r in self.rounds:
            self.assertIsNotNone(r.draw_time, f"topicid {r.topicid} 没拿到开奖时间")
            time.strptime(r.draw_time[:16], "%Y-%m-%d %H:%M")
        # 连旱仍然只取决于结果，不受开奖时间影响
        self.assertEqual(chase.compute_drought(self.rounds, "豹子"), 16)

    def test_list_total_pages_from_fixture(self):
        # 快照页面只有一页可见，解析后 list_total_pages = 1
        self.assertEqual(chase.list_total_pages(fixture_html()), 1)
        self.assertEqual(chase.list_total_pages(page_html([])), 1)

    def test_drought_zero_when_newest_drawn_round_is_target(self):
        rounds = chase.parse_rounds(
            page_html([row_html(2, "豹子"), row_html(1, "小")])
        )
        self.assertEqual(chase.compute_drought(rounds, "豹子"), 0)
        self.assertTrue(chase.target_just_hit(rounds, "豹子"))

    def test_drought_equals_drawn_count_when_target_absent(self):
        rounds = chase.parse_rounds(
            page_html([row_html(3, "小"), row_html(2, "大"), row_html(1, "顺子")])
        )
        self.assertEqual(chase.compute_drought(rounds, "豹子"), 3)
        self.assertFalse(chase.target_just_hit(rounds, "豹子"))

    def test_undrawn_round_not_counted_as_miss(self):
        rounds = chase.parse_rounds(page_html([row_html(2), row_html(1, "豹子")]))
        self.assertEqual(chase.compute_drought(rounds, "豹子"), 0)

    def test_interval_shrinks_when_draw_time_is_near(self):
        cfg = make_cfg(poll_interval=60.0, min_poll_interval=3.0)
        now = time.mktime(time.strptime("2026-10-08 14:50", "%Y-%m-%d %H:%M"))
        rounds = [chase.Round(topicid=1, draw_time="2026-10-08 14:52")]
        # 剩余 120s → 120/3 = 40，落在 [3, 60] 之间
        self.assertAlmostEqual(chase.next_poll_interval(cfg, rounds, now), 40.0, places=1)

    def test_interval_uses_configured_value_without_draw_time(self):
        cfg = make_cfg(poll_interval=15.0)
        self.assertEqual(chase.next_poll_interval(cfg, [chase.Round(topicid=1)]), 15.0)


# ---------------------------------------------------------------- 抓取与下注
class ClientTests(unittest.TestCase):
    def test_login_redirect_raises_session_expired(self):
        transport = FakeTransport(
            get_results=[chase.HttpResponse(302, "", "https://hdsky.me/login.php?returnto=forums.php")]
        )
        with self.assertRaises(chase.SessionExpired):
            chase.ForumClient(transport).fetch_list_page(1)

    def test_login_page_body_raises_session_expired(self):
        body = '<html><body><form name="login"><input name="username"></form></body></html>'
        transport = FakeTransport(get_results=[chase.HttpResponse(200, body)])
        with self.assertRaises(chase.SessionExpired):
            chase.ForumClient(transport).fetch_list_page(1)

    def test_bet_302_is_success(self):
        transport = FakeTransport(post_results=[chase.HttpResponse(302, "")])
        chase.ForumClient(transport).place_bet(1005, "豹子", 100000)
        self.assertEqual(len(transport.post_calls), 1)

    def test_bet_post_form_matches_userscript(self):
        transport = FakeTransport(post_results=[chase.HttpResponse(302, "")])
        chase.ForumClient(transport).place_bet(1005, "豹子", 100000)
        url, form = transport.post_calls[0]
        self.assertEqual(url, "https://hdsky.me/forums.php?action=post")
        self.assertEqual(form, {"id": "1005", "type": "reply", "body": "豹子 100000"})

    def test_bet_200_rejection_is_retryable_with_reason(self):
        transport = FakeTransport(
            post_results=[chase.HttpResponse(200, '<td class="text">请不要连续发帖，请稍后再试</td>')]
        )
        with self.assertRaises(chase.BetRejected) as ctx:
            chase.ForumClient(transport).place_bet(1005, "豹子", 100000)
        self.assertIn("请不要连续发帖", ctx.exception.reason)

    def test_bet_200_without_recognized_refusal_is_not_retried(self):
        # 认不出文案的 200 = 论坛没收下这一注（被收下一定是 302）。单注入口只判定「没收下」，
        # combo 第二注及之后的「按节奏重试」在 place_combo_bet_until_accepted（见 ComboExecutionTests）。
        # 现场（2026-10-13 18:21）那类 200 的正文不是带文案的拒绝页，extract_rejection_reason
        # 还会先抓到页头导航单元格（所以 reason 里是导航文字，不是拒绝原因）。
        transport = FakeTransport(post_results=[chase.HttpResponse(200, INDEX_PAGE_200)])
        client, _ = fake_client(transport)
        with self.assertRaises(chase.BetNotAccepted) as ctx:
            client.place_bet_until_accepted(1005, "小", 100)
        self.assertEqual(len(transport.post_calls), 1)
        self.assertIn("EMBY订阅", ctx.exception.reason)

    def test_unrecognized_200_body_is_logged_at_debug(self):
        """认不出的 200 要留一条正文痕迹：现场只有导航文案，事后无从判断论坛到底为什么没收下。"""
        transport = FakeTransport(post_results=[chase.HttpResponse(200, INDEX_PAGE_200)])
        client, _ = fake_client(transport)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        chase.LOG.addHandler(handler)
        previous_level = chase.LOG.level
        chase.LOG.setLevel(logging.DEBUG)
        self.addCleanup(chase.LOG.removeHandler, handler)
        self.addCleanup(chase.LOG.setLevel, previous_level)
        with self.assertRaises(chase.BetNotAccepted):
            client.place_bet_until_accepted(1005, "小", 100)
        logged = stream.getvalue()
        # 只断言「认不出的 200 正文被留痕」这件事本身。凭据不入日志由 test_cookie_never_reaches_logs
        # 负责 —— 本用例的 client 根本不持有 Cookie，在这里断言不含 c_secure_pass 只会恒真（评审 #5）。
        self.assertIn("HDSky :: 首页", logged)
        self.assertIn("EMBY订阅", logged)

    def test_bet_unknown_status_is_uncertain(self):
        transport = FakeTransport(post_results=[chase.HttpResponse(403, "forbidden")])
        with self.assertRaises(chase.BetUncertain):
            chase.ForumClient(transport).place_bet(1005, "豹子", 100000)

    def test_retry_then_success(self):
        rejection = '<td class="text">请不要连续发帖</td>'
        transport = FakeTransport(
            post_results=[
                chase.HttpResponse(200, rejection),
                chase.HttpResponse(200, rejection),
                chase.HttpResponse(302, ""),
            ]
        )
        client, _ = fake_client(transport)
        client.place_bet_until_accepted(1005, "豹子", 100000)
        self.assertEqual(len(transport.post_calls), 3)

    def test_retry_stops_at_cap(self):
        rejection = '<td class="text">请不要连续发帖</td>'
        transport = FakeTransport(
            post_results=[chase.HttpResponse(200, rejection)] * chase.RETRY_MAX_ATTEMPTS
        )
        client, _ = fake_client(transport)
        with self.assertRaises(chase.BetRejected):
            client.place_bet_until_accepted(1005, "豹子", 100000)
        self.assertEqual(len(transport.post_calls), chase.RETRY_MAX_ATTEMPTS)

    def test_network_error_is_not_retried(self):
        transport = FakeTransport(post_results=[chase.DiceNetworkError("connection reset")])
        client, _ = fake_client(transport)
        with self.assertRaises(chase.DiceNetworkError):
            client.place_bet_until_accepted(1005, "豹子", 100000)
        self.assertEqual(len(transport.post_calls), 1)

    def test_min_post_interval_enforced(self):
        transport = FakeTransport(
            post_results=[chase.HttpResponse(302, ""), chase.HttpResponse(302, "")]
        )
        client, clock = fake_client(transport)
        client.place_bet(1, "豹子", 100)
        before = clock["now"]
        client.place_bet(2, "豹子", 100)
        self.assertGreaterEqual(clock["now"] - before, chase.MIN_POST_INTERVAL_S)


# --------------------------------------------------------------------- 策略
def r(topicid, result=None, locked=False, draw_time=None):
    return chase.Round(topicid=topicid, result_type=result, locked=locked, draw_time=draw_time)


class StrategyTests(unittest.TestCase):
    def test_below_threshold_stays_idle(self):
        state = chase.State()
        rounds = [r(5), r(4, "小"), r(3, "大")]
        decision = chase.decide(rounds, make_cfg(threshold=3), state)
        self.assertFalse(decision.armed)
        self.assertIsNone(decision.bet_topicid)
        self.assertEqual(decision.drought, 2)

    def test_arms_at_threshold_and_bets_newest_open_round(self):
        state = chase.State()
        rounds = [r(5, draw_time="2026-10-08 15:00"), r(4, "小"), r(3, "大"), r(2, "顺子"), r(1, "豹子")]
        decision = chase.decide(rounds, make_cfg(threshold=3), state)
        self.assertTrue(decision.armed)
        self.assertEqual(decision.drought, 3)
        self.assertEqual(decision.bet_topicid, 5)

    def test_locked_open_round_is_skipped(self):
        state = chase.State()
        rounds = [r(6, locked=True), r(5), r(4, "小"), r(3, "大"), r(2, "顺子"), r(1, "豹子")]
        decision = chase.decide(rounds, make_cfg(threshold=3), state)
        self.assertEqual(decision.bet_topicid, 5)

    def test_same_topic_is_never_bet_twice(self):
        state = chase.State(armed=True, bet_topicids=[(5, "豹子")])
        rounds = [r(5), r(4, "小"), r(3, "大"), r(2, "顺子"), r(1, "豹子")]
        decision = chase.decide(rounds, make_cfg(threshold=3), state)
        self.assertIsNone(decision.bet_topicid)
        self.assertTrue(decision.armed)

    def test_target_hit_disarms(self):
        state = chase.State(armed=True, consecutive_bets=4)
        rounds = [r(5, "豹子"), r(4, "小"), r(3, "大")]
        decision = chase.decide(rounds, make_cfg(threshold=3), state)
        self.assertFalse(state.armed)
        self.assertEqual(state.consecutive_bets, 0)
        self.assertFalse(decision.armed)
        self.assertIsNone(decision.bet_topicid)

    def test_armed_without_open_round_waits(self):
        state = chase.State(armed=True)
        rounds = [r(4, "小"), r(3, "大"), r(2, "顺子"), r(1, "豹子")]
        decision = chase.decide(rounds, make_cfg(threshold=3), state)
        self.assertTrue(decision.armed)
        self.assertIsNone(decision.bet_topicid)
        self.assertIn("开放轮", decision.reason)


# ------------------------------------------------------------- 状态与保险护栏
class StateTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            original = chase.State(
                last_seen_topicid=7, armed=True, bet_topicids=[(1, "豹子"), (2, "豹子")], total_staked=200, consecutive_bets=2,
                refused_rounds=[5, 6],
            )
            chase.save_state(path, original)
            loaded, note = chase.load_state(path)
        self.assertIsNone(note)
        self.assertEqual(loaded, original)

    def test_missing_state_is_silent_and_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, note = chase.load_state(os.path.join(tmp, "nope.json"))
        self.assertIsNone(note)
        self.assertFalse(state.armed)
        self.assertEqual(state.last_seen_topicid, 0)

    def test_corrupt_state_falls_back_without_advancing_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{ this is not json")
            state, note = chase.load_state(path)
        self.assertIsNotNone(note)
        self.assertFalse(state.armed)
        self.assertEqual(state.last_seen_topicid, 0)
        self.assertEqual(state.total_staked, 0)

    def test_total_stake_cap(self):
        state = chase.State(total_staked=950000)
        blocker = chase.cap_blocker(state, make_cfg(max_total_stake=1000000, amount=100000), 100000)
        self.assertIsNotNone(blocker)

    def test_consecutive_bet_cap(self):
        state = chase.State(consecutive_bets=20)
        blocker = chase.cap_blocker(state, make_cfg(max_consecutive_bets=20), 100000)
        self.assertIsNotNone(blocker)

    def test_caps_can_be_disabled(self):
        state = chase.State(total_staked=10**9, consecutive_bets=10**6)
        blocker = chase.cap_blocker(
            state, make_cfg(max_total_stake=None, max_consecutive_bets=None), 100000
        )
        self.assertIsNone(blocker)

    def test_reset_run_state_clears_armed_and_consecutive_bets(self):
        state = chase.State(armed=True, consecutive_bets=5)
        self.assertTrue(chase.reset_run_state(state))
        self.assertFalse(state.armed)
        self.assertEqual(state.consecutive_bets, 0)

    def test_reset_run_state_zeroes_run_counters_but_keeps_dedup_records(self):
        """累计投入是「本次运行」的量，重启必须清零；去重记录与游标跨重启保留。"""
        state = chase.State(
            armed=True,
            consecutive_bets=4,
            total_staked=350,
            bet_topicids=[(1, "豹子"), (2, "豹子"), (3, "豹子")],
            last_seen_topicid=999,
        )
        self.assertTrue(chase.reset_run_state(state))
        self.assertFalse(state.armed)
        self.assertEqual(state.consecutive_bets, 0)
        self.assertEqual(state.total_staked, 0)
        self.assertEqual(state.bet_topicids, [(1, "豹子"), (2, "豹子"), (3, "豹子")])
        self.assertEqual(state.last_seen_topicid, 999)

    def test_reset_run_state_reports_change_when_only_total_staked_is_dirty(self):
        # 只带上一次运行的累计投入（armed / consecutive_bets 已归零）也要算「重置过」
        state = chase.State(total_staked=1100)
        self.assertTrue(chase.reset_run_state(state))
        self.assertEqual(state.total_staked, 0)

    def test_reset_run_state_noop_when_already_clean(self):
        state = chase.State()
        self.assertFalse(chase.reset_run_state(state))
        self.assertFalse(state.armed)
        self.assertEqual(state.consecutive_bets, 0)


class GuardTests(unittest.TestCase):
    def test_lock_blocks_second_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lock")
            chase.acquire_lock(path)
            try:
                with self.assertRaises(chase.LockHeld):
                    chase.acquire_lock(path)
            finally:
                chase.release_lock(path)
            self.assertFalse(os.path.exists(path))

    def test_stale_lock_is_taken_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lock")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("123456")
            with mock.patch("os.kill", side_effect=ProcessLookupError):
                acquired = chase.acquire_lock(path)
            try:
                self.assertEqual(acquired, path)
                with open(path, encoding="utf-8") as fh:
                    self.assertEqual(fh.read().strip(), str(os.getpid()))
            finally:
                chase.release_lock(path)

    def test_dry_run_sends_no_post_and_writes_no_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(dry_run=True, state_file=os.path.join(tmp, "state.json"))
            transport = FakeTransport()
            client, _ = fake_client(transport)
            state = chase.State()
            chase.place_bet_for_round(cfg, client, state, cfg.state_file, 1005, set())
            self.assertEqual(transport.post_calls, [])
            self.assertEqual(state.bet_topicids, [])
            self.assertEqual(state.total_staked, 0)
            self.assertFalse(os.path.exists(cfg.state_file))

    def test_live_bet_records_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(dry_run=False, state_file=os.path.join(tmp, "state.json"))
            transport = FakeTransport(post_results=[chase.HttpResponse(302, "")])
            client, _ = fake_client(transport)
            state = chase.State()
            chase.place_bet_for_round(cfg, client, state, cfg.state_file, 1005, set())
            loaded, note = chase.load_state(cfg.state_file)
        self.assertIsNone(note)
        self.assertEqual(state.bet_topicids, [(1005, "豹子")])
        self.assertEqual(state.total_staked, cfg.amount)
        self.assertEqual(state.consecutive_bets, 1)
        self.assertEqual(loaded.bet_topicids, [(1005, "豹子")])

    def test_chase_bet_refused_with_unrecognized_200_skips_without_stopping(self):
        """回归（2026-10-13 14:11）：chase 入口在「200 + 首页」下应放弃本轮、继续跑，进程不中断。

        与 combo 入口同结构；这条存在的理由是保证两处处理器在合并时不会漏掉一份。
        """
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(
                mode="chase",
                amount=100,
                target_type="豹子",
                max_total_stake=1000,
                dry_run=False,
                state_file=os.path.join(tmp, "state.json"),
            )
            transport = FakeTransport(post_results=[chase.HttpResponse(200, INDEX_PAGE_200)])
            client, _ = fake_client(transport)
            state = chase.State()
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            chase.LOG.addHandler(handler)
            previous_level = chase.LOG.level
            chase.LOG.setLevel(logging.INFO)
            self.addCleanup(chase.LOG.removeHandler, handler)
            self.addCleanup(chase.LOG.setLevel, previous_level)

            chase.place_bet_for_round(cfg, client, state, cfg.state_file, 1005, set())  # 不抛异常
            saved, _ = chase.load_state(cfg.state_file)

        self.assertEqual(len(transport.post_calls), 1)  # 没有按节奏重试 12 次
        self.assertEqual(state.bet_topicids, [])
        self.assertEqual(state.total_staked, 0)
        self.assertEqual(state.consecutive_bets, 0)
        self.assertEqual(saved.bet_topicids, [])
        # 评审 #1：被拒的轮要记到 refused_rounds,后续轮询不再尝试
        self.assertIn(1005, state.refused_rounds)
        self.assertIn(1005, saved.refused_rounds)
        self.assertIn("论坛没有收下这一注", stream.getvalue())

    def test_uncertain_bet_stops_without_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(dry_run=False, state_file=os.path.join(tmp, "state.json"))
            transport = FakeTransport(post_results=[chase.DiceNetworkError("connection reset")])
            client, _ = fake_client(transport)
            state = chase.State()
            with self.assertRaises(chase.StopRun) as ctx:
                chase.place_bet_for_round(cfg, client, state, cfg.state_file, 1005, set())
            self.assertEqual(ctx.exception.code, 4)
            self.assertEqual(state.bet_topicids, [])
            self.assertEqual(state.total_staked, 0)

    def test_rejected_bet_stops_without_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(dry_run=False, state_file=os.path.join(tmp, "state.json"))
            transport = FakeTransport(
                post_results=[
                    chase.HttpResponse(200, '<td class="text">请不要连续发帖</td>')
                ]
                * chase.RETRY_MAX_ATTEMPTS
            )
            client, _ = fake_client(transport)
            state = chase.State()
            with self.assertRaises(chase.StopRun) as ctx:
                chase.place_bet_for_round(cfg, client, state, cfg.state_file, 1005, set())
            self.assertEqual(ctx.exception.code, 5)
            self.assertEqual(state.bet_topicids, [])

    def test_cookie_never_reaches_logs(self):
        secret = "c_secure_pass=SUPERSECRETVALUE"
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(dry_run=False, cookie=secret, state_file=os.path.join(tmp, "state.json"))
            transport = FakeTransport(
                post_results=[chase.HttpResponse(200, '<td class="text">请不要连续发帖</td>')]
                * chase.RETRY_MAX_ATTEMPTS
            )
            client, _ = fake_client(transport)
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            chase.LOG.addHandler(handler)
            previous_level = chase.LOG.level
            chase.LOG.setLevel(logging.DEBUG)
            self.addCleanup(chase.LOG.removeHandler, handler)
            self.addCleanup(chase.LOG.setLevel, previous_level)
            try:
                chase.place_bet_for_round(cfg, client, chase.State(), cfg.state_file, 1005, set())
            except chase.StopRun:
                pass
        self.assertNotIn("SUPERSECRETVALUE", stream.getvalue())


# ------------------------------------------------------------------ 抓取编排
class CollectRoundsTests(unittest.TestCase):
    def test_stops_paging_once_target_is_seen(self):
        # 真实 fixture 里没「豹子」，所以这个测试用合成页：在第 1 页就出现目标
        page1 = page_html([row_html(2, "小"), row_html(1, "豹子")], total_pages=3)
        transport = FakeTransport(get_results=[chase.HttpResponse(200, page1)])
        rounds = chase.collect_rounds(chase.ForumClient(transport), make_cfg(max_list_pages=10))
        self.assertEqual(len(transport.get_urls), 1)
        self.assertTrue(any(x.result_type == "豹子" for x in rounds))

    def test_max_list_pages_is_respected(self):
        page1 = page_html([row_html(2, "小"), row_html(1, "大")], total_pages=2)
        page2 = page_html([row_html(0, "豹子")])
        transport = FakeTransport(
            get_results=[chase.HttpResponse(200, page1), chase.HttpResponse(200, page2)]
        )
        chase.collect_rounds(chase.ForumClient(transport), make_cfg(max_list_pages=1))
        self.assertEqual(len(transport.get_urls), 1)

    def test_pages_deeper_when_target_not_on_first_page(self):
        page1 = page_html([row_html(2, "小"), row_html(1, "大")], total_pages=2)
        page2 = page_html([row_html(0, "豹子")])
        transport = FakeTransport(
            get_results=[chase.HttpResponse(200, page1), chase.HttpResponse(200, page2)]
        )
        rounds = chase.collect_rounds(chase.ForumClient(transport), make_cfg(max_list_pages=2))
        self.assertEqual(len(transport.get_urls), 2)
        self.assertEqual(chase.compute_drought(rounds, "豹子"), 2)

    def test_page_two_network_failure_keeps_first_page(self):
        page1 = page_html([row_html(2, "小"), row_html(1, "大")], total_pages=2)
        transport = FakeTransport(
            get_results=[chase.HttpResponse(200, page1), chase.DiceNetworkError("boom")]
        )
        rounds = chase.collect_rounds(chase.ForumClient(transport), make_cfg(max_list_pages=2))
        self.assertEqual([x.topicid for x in rounds], [2, 1])


# ------------------------------------------------------------- 启动校验重试
class ValidateSessionRetryTests(unittest.TestCase):
    """启动校验对网络层抖动要有界重试。

    实测（2026-10-17，本机 Clash TUN 出口 → Cloudflare）：同一个列表页请求约 1/3 会在
    TLS 握手或读响应阶段卡死，且卡死的那次到 15s / 40s 都不会恢复 —— 只能换一条连接重来。
    只读请求幂等，重试安全；会话失效（SessionExpired）是 cookie 的问题，重试无用，必须立即上抛。
    """

    def test_retries_transient_network_failure_then_succeeds(self):
        """回归：一次瞬时读失败就把启动打掉时（现场 2026-10-17 11:13:54）这条会失败。"""
        page = page_html([row_html(2, "小")])
        transport = FakeTransport(
            get_results=[
                chase.DiceNetworkError("The read operation timed out"),
                chase.HttpResponse(200, page),
            ]
        )
        client, clock = fake_client(transport)

        chase.validate_session(client)

        self.assertEqual(len(transport.get_urls), 2)
        # 重试前按 RETRY_* 节奏等过一次，不是连着打
        self.assertEqual(clock["now"], chase.RETRY_FIRST_DELAY_S)

    def test_gives_up_after_bounded_attempts(self):
        transport = FakeTransport(get_results=[chase.DiceNetworkError("boom")] * 3)
        client, _ = fake_client(transport)

        with self.assertRaises(chase.DiceNetworkError):
            chase.validate_session(client)

        self.assertEqual(len(transport.get_urls), 3)

    def test_session_expired_is_not_retried(self):
        """会话失效重试无意义：必须一次就上抛，否则启动会白等两轮。"""
        transport = FakeTransport(
            get_results=[chase.SessionExpired("列表页是登录页内容，会话已失效")]
        )
        client, clock = fake_client(transport)

        with self.assertRaises(chase.SessionExpired):
            chase.validate_session(client)

        self.assertEqual(len(transport.get_urls), 1)
        self.assertEqual(clock["now"], 0.0)


# --------------------------------------------------------------- 主循环端到端
class LoopTests(unittest.TestCase):
    """用真实 fixture 驱动整个 run_loop —— 对应 plan 的「集成（离线）」一行。

    不起网络：假 transport 返回本地 fixture，sleep_interruptibly 被 patch 掉
    避免真等；循环的终止由 transport 在指定次数后置停机标志来触发。
    """

    def _drive(self, tmp, *, dry_run, stop_after_gets, stop_on_post=False, stop_after_posts=None,
               reset_state=False, html=None, **cfg_over):
        """跑一次 run_loop。

        stop_on_post 在第一次 POST 后置停机标志（chase 一轮一注，够用）；
        combo 一轮两注，要用 stop_after_posts=N 在第 N 次 POST 后才停，
        否则第二次下注的重试窗口会立刻看到停机标志并抛 StopRun(0)。
        """
        defaults = dict(threshold=1, amount=100)
        defaults.update(cfg_over)
        cfg = make_cfg(
            dry_run=dry_run,
            state_file=os.path.join(tmp, "state.json"),
            lock_file=os.path.join(tmp, "lock"),
            **defaults,
        )
        html = html if html is not None else fixture_html()
        stop = chase.StopFlag()

        class Transport:
            def __init__(self):
                self.gets = 0
                self.posts = 0

            def get(self, url):
                self.gets += 1
                if self.gets >= stop_after_gets:
                    stop.requested = True
                return chase.HttpResponse(200, html)

            def post(self, url, form):
                self.posts += 1
                if stop_on_post or (stop_after_posts is not None and self.posts >= stop_after_posts):
                    stop.requested = True
                return chase.HttpResponse(302, "")

        transport = Transport()
        client = chase.ForumClient(transport, sleep=lambda _s: None, clock=lambda: 0.0)
        state, _ = chase.load_state(cfg.state_file)
        # 模拟 main() 在加载后做的 reset_run_state —— 默认关闭,
        # 显式打开才能测「armed 跨重启不继承」的契约。
        if reset_state:
            chase.reset_run_state(state)

        logs = io.StringIO()
        handler = logging.StreamHandler(logs)
        chase.LOG.addHandler(handler)
        previous_level = chase.LOG.level
        chase.LOG.setLevel(logging.INFO)
        self.addCleanup(chase.LOG.removeHandler, handler)
        self.addCleanup(chase.LOG.setLevel, previous_level)

        with mock.patch.object(chase, "sleep_interruptibly"):
            code = chase.run_loop(cfg, client, state, stop, cfg.state_file)
        return code, state, transport, logs.getvalue()

    def test_dry_run_loop_logs_intent_once_and_records_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, state, transport, log = self._drive(tmp, dry_run=True, stop_after_gets=3)
            saved, _ = chase.load_state(os.path.join(tmp, "state.json"))

        self.assertEqual(code, 0)
        self.assertEqual(transport.posts, 0)
        # 真实 fixture 连旱 16、阈值 1 → 追号态；开放轮只有 77245
        self.assertIn("本应下注 豹子 100 @ topicid=77245", log)
        # 三轮里同一 topicid 只播报一次（announced 集合去重）
        self.assertEqual(log.count("本应下注"), 1)
        # dry-run 不写已下注记录与投入
        self.assertEqual(state.bet_topicids, [])
        self.assertEqual(state.total_staked, 0)
        # 但游标与追号态会推进
        self.assertEqual(state.last_seen_topicid, 77245)
        self.assertTrue(state.armed)
        self.assertEqual(saved.last_seen_topicid, 77245)
        self.assertTrue(saved.armed)

    def test_live_loop_bets_the_open_round_and_records_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, state, transport, log = self._drive(
                tmp, dry_run=False, stop_after_gets=2, stop_on_post=True
            )
            saved, note = chase.load_state(os.path.join(tmp, "state.json"))

        self.assertEqual(code, 0)
        self.assertEqual(transport.posts, 1)
        self.assertIn("下注成功", log)
        self.assertEqual(state.bet_topicids, [(77245, "豹子")])
        self.assertEqual(state.total_staked, 100)
        self.assertEqual(state.consecutive_bets, 1)
        self.assertIsNone(note)
        self.assertEqual(saved.bet_topicids, [(77245, "豹子")])

    def test_live_loop_posts_the_userscript_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(
                dry_run=False,
                threshold=1,
                amount=100,
                state_file=os.path.join(tmp, "state.json"),
                lock_file=os.path.join(tmp, "lock"),
            )
            captured = {}

            class Transport:
                def get(self, url):
                    return chase.HttpResponse(200, fixture_html())

                def post(self, url, form):
                    captured["url"] = url
                    captured["form"] = form
                    stop.requested = True
                    return chase.HttpResponse(302, "")

            stop = chase.StopFlag()
            client = chase.ForumClient(Transport(), sleep=lambda _s: None, clock=lambda: 0.0)
            with mock.patch.object(chase, "sleep_interruptibly"):
                chase.run_loop(cfg, client, chase.State(), stop, cfg.state_file)

        self.assertEqual(captured["url"], "https://hdsky.me/forums.php?action=post")
        self.assertEqual(captured["form"], {"id": "77245", "type": "reply", "body": "豹子 100"})

    def test_loop_exits_when_spend_cap_would_be_exceeded(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, state, transport, log = self._drive(
                tmp, dry_run=False, stop_after_gets=3, amount=100, max_total_stake=50
            )

        self.assertEqual(code, 3)
        self.assertEqual(transport.posts, 0)
        self.assertIn("触发保险上限", log)
        self.assertEqual(state.bet_topicids, [])
        self.assertEqual(state.total_staked, 0)

    def test_loop_skips_half_bet_round_after_it_becomes_locked(self):
        """回归（2026-10-13）：第一注落下后该轮不再收帖 → 第二注重试到上限仍被「200」挡下。

        期望：进程不中断；该轮记入 refused_rounds；下一轮轮询看到该轮已锁定就跳过它，
        并在新的开放轮上把两注下完。锁定-跳过实际发生在第三轮轮询（第二轮轮询里决定先挑
        更新的开放轮 102）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(
                mode="combo",
                combo_amount=100,
                combo_types=["大", "小"],
                dry_run=False,
                state_file=os.path.join(tmp, "state.json"),
                lock_file=os.path.join(tmp, "lock"),
            )
            page_before = page_html([row_html(101)])
            page_after = page_html([row_html(102), row_html(101, locked=True)])
            stop = chase.StopFlag()

            class Transport:
                def __init__(self):
                    self.gets = 0
                    self.posts = []

                def get(self, url):
                    self.gets += 1
                    if self.gets >= 3:
                        stop.requested = True
                    return chase.HttpResponse(200, page_before if self.gets == 1 else page_after)

                def post(self, url, form):
                    self.posts.append(form)
                    # 101 的第一注成功；101 不再收帖，第二注每次都被挡；102 两注都成功
                    if form["id"] == "101" and form["body"] == "小 100":
                        return chase.HttpResponse(200, INDEX_PAGE_200)
                    return chase.HttpResponse(302, "")

            transport = Transport()
            client = chase.ForumClient(transport, sleep=lambda _s: None, clock=lambda: 0.0)
            state = chase.State()

            with mock.patch.object(chase, "sleep_interruptibly"):
                code = chase.run_loop(cfg, client, state, stop, cfg.state_file)

        self.assertEqual(code, 0)
        self.assertEqual(state.bet_topicids, [(101, "大"), (102, "大"), (102, "小")])
        self.assertEqual(state.total_staked, 300)
        # 101: 大 1 次 + 小 试满上限;102: 大 + 小 各 1 次
        self.assertEqual(len(transport.posts), 1 + chase.COMBO_MAX_ATTEMPTS + 2)
        self.assertIn(101, state.refused_rounds)

    def test_loop_does_not_repost_a_refused_round_that_stays_open(self):
        """评审 #1 的回归：被拒的轮若一直保持开放，也不能每个轮询周期重发同一注。

        第 1 轮轮询：101 的第一注成功、第二注重试到上限都被「200」挡下（写入 refused_rounds）。
        之后 101 仍然开放（未锁定、未开奖）——旧行为会每次轮询重发 (101, "小")；现在应跳过 101,
        转去下新的开放轮 102。
        """
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(
                mode="combo",
                combo_amount=100,
                combo_types=["大", "小"],
                dry_run=False,
                state_file=os.path.join(tmp, "state.json"),
                lock_file=os.path.join(tmp, "lock"),
            )
            # 101 在两次抓取里都保持开放（没有 locked 标记），且排在 DOM 前面,所以第 1 轮会先挑它
            page = page_html([row_html(101), row_html(102)])
            stop = chase.StopFlag()

            class Transport:
                def __init__(self):
                    self.gets = 0
                    self.posts = []

                def get(self, url):
                    self.gets += 1
                    if self.gets >= 3:
                        stop.requested = True
                    return chase.HttpResponse(200, page)

                def post(self, url, form):
                    self.posts.append(form)
                    # 101 的第一注成功；101 不再收帖，第二注每次都被挡；102 两注都成功
                    if form["id"] == "101" and form["body"] == "小 100":
                        return chase.HttpResponse(200, INDEX_PAGE_200)
                    return chase.HttpResponse(302, "")

            transport = Transport()
            client = chase.ForumClient(transport, sleep=lambda _s: None, clock=lambda: 0.0)
            state = chase.State()

            with mock.patch.object(chase, "sleep_interruptibly"):
                code = chase.run_loop(cfg, client, state, stop, cfg.state_file)

        self.assertEqual(code, 0)
        # 101 只被尝试过一轮（第一注成功 + 第二注试满重试上限），没有在第 2 轮轮询里重发
        self.assertEqual(state.bet_topicids, [(101, "大"), (102, "大"), (102, "小")])
        self.assertEqual(state.total_staked, 300)
        self.assertEqual(len(transport.posts), 1 + chase.COMBO_MAX_ATTEMPTS + 2)
        self.assertIn(101, state.refused_rounds)
        self.assertEqual(
            transport.posts.count({"id": "101", "type": "reply", "body": "小 100"}),
            chase.COMBO_MAX_ATTEMPTS,
        )

    def test_second_start_ignores_previous_runs_total_staked(self):
        """回归：上次运行落盘的 total_staked=1100 不该让本次启动一注未下就触发保险上限。"""
        with tempfile.TemporaryDirectory() as tmp:
            chase.save_state(
                os.path.join(tmp, "state.json"),
                chase.State(
                    last_seen_topicid=77200,
                    armed=True,
                    bet_topicids=[(77200, "大"), (77200, "小")],
                    total_staked=1100,
                    consecutive_bets=0,
                ),
            )
            code, state, transport, log = self._drive(
                tmp,
                dry_run=False,
                stop_after_gets=3,
                stop_after_posts=2,  # combo 一轮两注：等都下完再停，否则第二注立刻被停机标志打断
                reset_state=True,
                mode="combo",
                combo_amount=100,
                max_total_stake=1000,
            )

        self.assertEqual(code, 0)
        self.assertNotIn("触发保险上限", log)
        self.assertEqual(transport.posts, 2)
        self.assertEqual(state.total_staked, 200)
        self.assertIn("本次运行累计投入 200", log)

    def test_combo_round_never_pushes_total_staked_over_the_cap(self):
        """回归：整轮两注要一起过预算检查，单次运行内也不能越过 max_total_stake。"""
        with tempfile.TemporaryDirectory() as tmp:
            code, state, transport, log = self._drive(
                tmp,
                dry_run=False,
                stop_after_gets=5,
                html=page_html([row_html(101), row_html(102), row_html(103)]),
                mode="combo",
                combo_amount=300,
                max_total_stake=1000,
            )

        self.assertEqual(code, 3)
        self.assertIn("触发保险上限", log)
        # 第二轮（整轮 600）在预算检查处就被拦下，没有下出去
        self.assertEqual(transport.posts, 2)
        self.assertEqual(state.total_staked, 600)
        self.assertLessEqual(state.total_staked, 1000)

    def test_loop_exits_when_consecutive_cap_reached(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 先写一份「历史连续下注已达上限」的状态，再启动
            chase.save_state(
                os.path.join(tmp, "state.json"), chase.State(consecutive_bets=1)
            )
            code, state, transport, log = self._drive(
                tmp, dry_run=False, stop_after_gets=3, max_consecutive_bets=1, max_total_stake=None
            )

        self.assertEqual(code, 3)
        self.assertEqual(transport.posts, 0)
        self.assertIn("触发保险上限", log)
        self.assertEqual(state.bet_topicids, [])

    def test_session_expiry_mid_run_stops_the_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(
                threshold=1,
                state_file=os.path.join(tmp, "state.json"),
                lock_file=os.path.join(tmp, "lock"),
            )

            class Transport:
                def get(self, url):
                    return chase.HttpResponse(302, "", "https://hdsky.me/login.php")

                def post(self, url, form):  # pragma: no cover - 不该走到这里
                    raise AssertionError("会话失效后不应再下注")

            client = chase.ForumClient(Transport(), sleep=lambda _s: None, clock=lambda: 0.0)
            with mock.patch.object(chase, "sleep_interruptibly"):
                with self.assertRaises(chase.SessionExpired):
                    chase.run_loop(cfg, client, chase.State(), chase.StopFlag(), cfg.state_file)

    def test_reset_state_does_not_resume_armed_when_threshold_unmet(self):
        # 上一次运行留下了 armed=True / consecutive_bets=3,本次启动如果不做 reset,
        # fixture 连旱 16 永远 < threshold=10000,就不会 arm,但 armed 残留会让循环
        # 直接进入「挑开放轮」分支,把 77245 静默下掉 —— 这正是用户最初踩到的坑。
        # _drive(reset_state=True) 模拟 main 的 reset_run_state,新阈值必须生效。
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            chase.save_state(
                state_path,
                chase.State(armed=True, consecutive_bets=3, bet_topicids=[]),
            )
            code, state, transport, log = self._drive(
                tmp,
                dry_run=False,
                stop_after_gets=2,
                threshold=10000,
                reset_state=True,
            )

        self.assertEqual(code, 0)
        self.assertEqual(transport.posts, 0)
        self.assertNotIn("下注成功", log)
        self.assertFalse(state.armed)
        self.assertEqual(state.consecutive_bets, 0)

    def test_no_reset_lets_armed_resume_with_stale_state(self):
        # 对照组:不模拟 main 的 reset,armed=True 的残留状态会让循环在连旱不足的情况下
        # 也直接进入「挑开放轮」分支,把 fixture 里唯一开放轮 77245 静默下掉。
        # 这条用例把「不 reset 会出事」钉死,避免 reset_run_state 被悄悄回滚。
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            chase.save_state(
                state_path,
                chase.State(armed=True, consecutive_bets=3, bet_topicids=[]),
            )
            code, state, transport, log = self._drive(
                tmp,
                dry_run=False,
                stop_after_gets=2,
                stop_on_post=True,
                threshold=10000,
                reset_state=False,
            )

        self.assertEqual(code, 0)
        self.assertEqual(transport.posts, 1, f"未 reset 时 armed 残留应继续追号,日志: {log}")
        self.assertIn("下注成功", log)


# =========================================================== combo 模式测试
class MainStartupTests(unittest.TestCase):
    """main() 的启动接线：上一次运行落盘的 total_staked 必须在进入循环前清零。

    上面 LoopTests 的 reset_state=True 是在测试里手工重放 main() 的重置动作，
    钉住的是 reset_run_state 的语义；这里驱动真正的 main()，钉住「启动时会调用它」。
    """

    def _drive_main(self, tmp, state):
        chase.save_state(os.path.join(tmp, "state.json"), state)
        config_path = write_config(tmp)
        seen = {}

        def fake_run_loop(cfg, client, loop_state, stop, state_path):
            seen["total_staked"] = loop_state.total_staked
            seen["armed"] = loop_state.armed
            seen["consecutive_bets"] = loop_state.consecutive_bets
            return 0

        with mock.patch.object(chase, "setup_logging"), mock.patch.object(
            chase, "install_signal_handlers"
        ), mock.patch.object(chase, "validate_session"), mock.patch.object(
            chase, "acquire_lock", return_value=os.path.join(tmp, "lock")
        ), mock.patch.object(chase, "run_loop", fake_run_loop):
            code = chase.main(["--config", config_path])
        return code, seen

    def test_main_zeroes_previous_runs_run_state_before_the_loop(self):
        """回归：main() 里那行 reset_run_state 被删掉/改坏时，这条会失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            code, seen = self._drive_main(
                tmp,
                chase.State(
                    armed=True,
                    total_staked=1100,
                    consecutive_bets=2,
                    bet_topicids=[(1, "大"), (1, "小")],
                    last_seen_topicid=1,
                ),
            )

        self.assertEqual(code, 0)
        # 「本次运行」的量在进入循环前已清零
        self.assertEqual(seen["total_staked"], 0)
        self.assertFalse(seen["armed"])
        self.assertEqual(seen["consecutive_bets"], 0)


class ComboConfigTests(unittest.TestCase):
    def test_combo_mode_default_loads_with_defaults(self):
        cfg = make_cfg(mode="combo", combo_amount=100, combo_types=["大", "小"])
        self.assertEqual(cfg.mode, "combo")
        self.assertEqual(cfg.combo_amount, 100)
        self.assertEqual(cfg.combo_types, ["大", "小"])

    def test_combo_amount_below_min_rejected(self):
        with self.assertRaises(chase.ConfigError):
            chase.validate_config(make_cfg(mode="combo", combo_amount=50))

    def test_combo_amount_above_max_rejected(self):
        with self.assertRaises(chase.ConfigError):
            chase.validate_config(make_cfg(mode="combo", combo_amount=999999))

    def test_combo_types_empty_rejected(self):
        with self.assertRaises(chase.ConfigError):
            chase.validate_config(make_cfg(mode="combo", combo_types=[]))

    def test_combo_types_too_short_rejected(self):
        with self.assertRaises(chase.ConfigError):
            chase.validate_config(make_cfg(mode="combo", combo_types=["大"]))

    def test_combo_types_invalid_element_rejected(self):
        with self.assertRaises(chase.ConfigError):
            chase.validate_config(make_cfg(mode="combo", combo_types=["大", "非法"]))

    def test_combo_types_duplicate_rejected(self):
        with self.assertRaises(chase.ConfigError):
            chase.validate_config(make_cfg(mode="combo", combo_types=["大", "大"]))

    def test_combo_mode_ignores_invalid_target_type_with_warning(self):
        with self.assertLogs(chase.LOG, level="WARNING") as cm:
            cfg = make_cfg(mode="combo", target_type="非法")
            chase.validate_config(cfg)
        self.assertTrue(any("target_type" in m for m in cm.output))

    def test_combo_mode_amount_mismatch_logs_warning(self):
        with self.assertLogs(chase.LOG, level="WARNING") as cm:
            cfg = make_cfg(mode="combo", amount=999, combo_amount=100)
            chase.validate_config(cfg)
        self.assertTrue(any("amount" in m and "combo_amount" in m for m in cm.output))

    def test_combo_field_round_trips_through_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, mode="combo", combo_amount=100, combo_types=["大", "小"])
            cfg = chase.load_config(path)
        self.assertEqual(cfg.mode, "combo")
        self.assertEqual(cfg.combo_amount, 100)
        self.assertEqual(cfg.combo_types, ["大", "小"])

    def test_invalid_mode_string_rejected(self):
        with self.assertRaises(chase.ConfigError):
            chase.validate_config(make_cfg(mode="badmode"))


class ComboStateTests(unittest.TestCase):
    def test_state_bet_type_matches_tuple(self):
        state = chase.State(bet_topicids=[(77535, "豹子"), (77536, "大")])
        self.assertTrue(chase.state_bet_type(state, 77535, "豹子"))
        self.assertTrue(chase.state_bet_type(state, 77536, "大"))
        self.assertFalse(chase.state_bet_type(state, 77535, "大"))
        self.assertFalse(chase.state_bet_type(state, 99999, "豹子"))

    def test_state_round_complete_all_or_none(self):
        state = chase.State(bet_topicids=[(1, "大"), (1, "小")])
        self.assertTrue(chase.state_round_complete(state, 1, ["大", "小"]))
        state2 = chase.State(bet_topicids=[(1, "大")])
        self.assertFalse(chase.state_round_complete(state2, 1, ["大", "小"]))

    def test_legacy_state_load_warns_and_drops(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "last_seen_topicid": 999,
                        "armed": True,
                        "bet_topicids": [77535, 77536],
                        "total_staked": 200,
                        "consecutive_bets": 1,
                    },
                    fh,
                )
            state, note = chase.load_state(path)
        self.assertIsNotNone(note)
        self.assertIn("旧", note)
        self.assertFalse(state.armed)
        self.assertEqual(state.bet_topicids, [])
        self.assertEqual(state.total_staked, 200)
        self.assertEqual(state.last_seen_topicid, 999)


class ComboExecutionTests(unittest.TestCase):
    def test_combo_dry_run_prints_two_lines_and_no_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(
                mode="combo",
                combo_amount=100,
                combo_types=["大", "小"],
                state_file=os.path.join(tmp, "state.json"),
            )
            transport = FakeTransport()
            client, _ = fake_client(transport)
            state = chase.State()
            chase.place_combo_for_round(cfg, client, state, cfg.state_file, 1005, set())
        self.assertEqual(transport.post_calls, [])
        self.assertEqual(state.bet_topicids, [])
        self.assertFalse(os.path.exists(cfg.state_file))

    def test_combo_two_bets_302_record_both_and_consecutive_increments(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(
                mode="combo",
                combo_amount=100,
                combo_types=["大", "小"],
                dry_run=False,
                state_file=os.path.join(tmp, "state.json"),
            )
            transport = FakeTransport(
                post_results=[chase.HttpResponse(302, ""), chase.HttpResponse(302, "")]
            )
            client, _ = fake_client(transport)
            state = chase.State()
            chase.place_combo_for_round(cfg, client, state, cfg.state_file, 1005, set())
        self.assertEqual(
            state.bet_topicids, [(1005, "大"), (1005, "小")]
        )
        self.assertEqual(state.total_staked, 200)
        self.assertEqual(state.consecutive_bets, 1)

    def test_combo_second_bet_recognized_refusals_exhausted_abandons_round(self):
        """回归（评审 #4）：小 连吃 12 个「认得出文案的拒绝页」时处置与认不出的 200 一致 ——
        放弃该轮并继续跑，而不是因为最后一次恰好是 BetRejected 就把整个进程停掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(
                mode="combo",
                combo_amount=100,
                combo_types=["大", "小"],
                dry_run=False,
                state_file=os.path.join(tmp, "state.json"),
            )
            transport = FakeTransport(
                post_results=[
                    chase.HttpResponse(302, ""),  # 大 成功
                    *[chase.HttpResponse(200, '<td class="text">请不要连续发帖</td>')]
                    * chase.COMBO_MAX_ATTEMPTS,  # 小 12 次可识别拒绝
                ]
            )
            client, _ = fake_client(transport)
            state = chase.State()
            chase.place_combo_for_round(cfg, client, state, cfg.state_file, 1005, set())  # 不抛异常
        self.assertEqual(len(transport.post_calls), 1 + chase.COMBO_MAX_ATTEMPTS)
        # 大已记下，小未记下，consecutive_bets 不递增，该轮记入 refused_rounds 后继续跑
        self.assertEqual(state.bet_topicids, [(1005, "大")])
        self.assertEqual(state.total_staked, 100)
        self.assertEqual(state.consecutive_bets, 0)
        self.assertIn(1005, state.refused_rounds)

    def test_combo_exhausted_retries_ignore_last_attempt_exception_type(self):
        """回归（评审 #4）：末次恰好是可识别拒绝页时结局不变 —— 与认不出的 200 交替出现也一样。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(
                mode="combo",
                combo_amount=100,
                combo_types=["大", "小"],
                dry_run=False,
                state_file=os.path.join(tmp, "state.json"),
            )
            transport = FakeTransport(
                post_results=[
                    chase.HttpResponse(302, ""),  # 大 成功
                    *[chase.HttpResponse(200, INDEX_PAGE_200)] * (chase.COMBO_MAX_ATTEMPTS - 1),
                    chase.HttpResponse(200, '<td class="text">请不要连续发帖</td>'),  # 末次:可识别
                ]
            )
            client, _ = fake_client(transport)
            state = chase.State()
            chase.place_combo_for_round(cfg, client, state, cfg.state_file, 1005, set())  # 不抛异常
        self.assertEqual(len(transport.post_calls), 1 + chase.COMBO_MAX_ATTEMPTS)
        self.assertEqual(state.bet_topicids, [(1005, "大")])
        self.assertIn(1005, state.refused_rounds)

    def test_combo_first_bet_is_persisted_before_the_second_bet_runs(self):
        """回归（评审 #1）：第一注落单后立刻落盘 —— 第二注还在重试窗口里时，磁盘上就得有 (topicid, 大)。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(
                mode="combo",
                combo_amount=100,
                combo_types=["大", "小"],
                dry_run=False,
                state_file=os.path.join(tmp, "state.json"),
            )
            on_disk = []

            class Transport:
                def post(self, url, form):
                    if form["body"] == "小 100":
                        # 第二注每次 POST 之前把磁盘状态读回来（等价于此刻进程被强杀后重启）
                        disk, _ = chase.load_state(cfg.state_file)
                        on_disk.append(list(disk.bet_topicids))
                        return chase.HttpResponse(200, INDEX_PAGE_200)
                    return chase.HttpResponse(302, "")

                def get(self, url):  # pragma: no cover - 本用例不需要 GET
                    raise AssertionError("本用例不该发 GET")

            client = chase.ForumClient(Transport(), sleep=lambda _s: None, clock=lambda: 0.0)
            chase.place_combo_for_round(cfg, client, chase.State(), cfg.state_file, 1005, set())

        self.assertTrue(on_disk, "第二注没有发出")
        self.assertEqual(on_disk[0], [(1005, "大")])  # 小 首次尝试之前,大 已在盘上
        self.assertEqual(on_disk[-1], [(1005, "大")])

    def test_combo_retry_stops_promptly_on_stop_signal(self):
        """回归（评审 #3）：重试窗口内收到停止请求就立即停手,不再往下注。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(
                mode="combo",
                combo_amount=100,
                combo_types=["大", "小"],
                dry_run=False,
                state_file=os.path.join(tmp, "state.json"),
            )
            stop = chase.StopFlag()

            class Transport:
                def __init__(self):
                    self.posts = []

                def post(self, url, form):
                    self.posts.append(form)
                    if form["body"] == "小 100":
                        stop.requested = True  # 小 被拒的同时用户按了 Ctrl-C
                        return chase.HttpResponse(200, INDEX_PAGE_200)
                    return chase.HttpResponse(302, "")

                def get(self, url):  # pragma: no cover - 本用例不需要 GET
                    raise AssertionError("本用例不该发 GET")

            transport = Transport()
            client = chase.ForumClient(transport, sleep=lambda _s: None, clock=lambda: 0.0)
            with self.assertRaises(chase.StopRun) as ctx:
                chase.place_combo_for_round(
                    cfg, client, chase.State(), cfg.state_file, 1005, set(), stop
                )
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(len(transport.posts), 2)  # 大 一次 + 小 一次,没有继续重试

    def test_combo_first_bet_rejected_twelve_times_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(
                mode="combo",
                combo_amount=100,
                combo_types=["大", "小"],
                dry_run=False,
                state_file=os.path.join(tmp, "state.json"),
            )
            transport = FakeTransport(
                post_results=[
                    chase.HttpResponse(200, '<td class="text">请不要连续发帖，请稍后再试</td>')
                ]
                * chase.RETRY_MAX_ATTEMPTS
            )
            client, _ = fake_client(transport)
            state = chase.State()
            with self.assertRaises(chase.StopRun) as ctx_exc:
                chase.place_combo_for_round(cfg, client, state, cfg.state_file, 1005, set())
        self.assertEqual(ctx_exc.exception.code, 5)
        self.assertEqual(state.bet_topicids, [])

    def test_combo_second_bet_retries_unrecognized_200_then_records_both(self):
        """回归（2026-10-13 18:21 现场）：论坛对 ~4 秒后的第二注回 200（拒绝页文案认不出），
        重试到间隔够了才被收下（302）—— 两注都要记账，不能放弃该轮。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(
                mode="combo",
                combo_amount=100,
                combo_types=["大", "小"],
                dry_run=False,
                state_file=os.path.join(tmp, "state.json"),
            )
            transport = FakeTransport(
                post_results=[
                    chase.HttpResponse(302, ""),  # 大 被收下
                    chase.HttpResponse(200, INDEX_PAGE_200),  # 小 第 1 次：间隔不够，没收下
                    chase.HttpResponse(302, ""),  # 小 第 2 次：被收下
                ]
            )
            client, _ = fake_client(transport)
            state = chase.State()
            chase.place_combo_for_round(cfg, client, state, cfg.state_file, 1005, set())
        self.assertEqual(len(transport.post_calls), 3)
        self.assertEqual(state.bet_topicids, [(1005, "大"), (1005, "小")])
        self.assertEqual(state.total_staked, 200)
        self.assertEqual(state.consecutive_bets, 1)
        self.assertEqual(state.refused_rounds, [])

    def test_combo_second_bet_retry_waits_combo_retry_delay(self):
        """重试的等待必须够长：大 等 RETRY_FIRST_DELAY_S，小 首次等 COMBO_FIRST_DELAY_S，
        重试再等 COMBO_RETRY_DELAY_S —— 与 user.js 一键大小的节奏一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(
                mode="combo",
                combo_amount=100,
                combo_types=["大", "小"],
                dry_run=False,
                state_file=os.path.join(tmp, "state.json"),
            )
            transport = FakeTransport(
                post_results=[
                    chase.HttpResponse(302, ""),
                    chase.HttpResponse(200, INDEX_PAGE_200),
                    chase.HttpResponse(302, ""),
                ]
            )
            client, clock = fake_client(transport)
            chase.place_combo_for_round(cfg, client, chase.State(), cfg.state_file, 1005, set())
        self.assertEqual(
            clock["now"],
            chase.RETRY_FIRST_DELAY_S + chase.COMBO_FIRST_DELAY_S + chase.COMBO_RETRY_DELAY_S,
        )

    def test_combo_second_bet_gives_up_round_after_retries_exhausted(self):
        """回归（2026-10-13 18:21）：小 一直拿 200 时按 COMBO_* 节奏重试到上限才放弃本轮剩余类型，
        进程继续跑（不打停），该轮记入 refused_rounds、下一轮轮询不再尝试。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(
                mode="combo",
                combo_amount=100,
                combo_types=["大", "小"],
                dry_run=False,
                state_file=os.path.join(tmp, "state.json"),
            )
            transport = FakeTransport(
                post_results=[chase.HttpResponse(302, "")]  # 大 成功
                + [chase.HttpResponse(200, INDEX_PAGE_200)] * chase.COMBO_MAX_ATTEMPTS
            )
            client, _ = fake_client(transport)
            state = chase.State()
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            chase.LOG.addHandler(handler)
            previous_level = chase.LOG.level
            chase.LOG.setLevel(logging.INFO)
            self.addCleanup(chase.LOG.removeHandler, handler)
            self.addCleanup(chase.LOG.setLevel, previous_level)

            chase.place_combo_for_round(cfg, client, state, cfg.state_file, 78016, set())  # 不抛异常
            saved, _ = chase.load_state(cfg.state_file)

        self.assertEqual(len(transport.post_calls), 1 + chase.COMBO_MAX_ATTEMPTS)
        self.assertEqual(state.bet_topicids, [(78016, "大")])
        self.assertEqual(state.total_staked, 100)
        self.assertEqual(state.consecutive_bets, 0)
        self.assertEqual(saved.bet_topicids, [(78016, "大")])  # 已落盘，下一轮据此重新评估
        self.assertIn("论坛没有收下这一注", stream.getvalue())
        # 评审 #1：该轮记到 refused_rounds,后续轮询不会再来下这个 (78016, "小")
        self.assertIn(78016, state.refused_rounds)
        self.assertIn(78016, saved.refused_rounds)

    def test_combo_skips_refused_rounds_in_decide(self):
        """评审 #1：被拒过的轮在 decide() 里直接跳过，不会被重复尝试。"""
        state = chase.State(refused_rounds=[101])
        rounds = [
            chase.Round(topicid=101, locked=False),  # 仍开放但记在拒单集里
            chase.Round(topicid=102, locked=False),  # 仍开放,会选中
        ]
        cfg = make_cfg(mode="combo", combo_types=["大", "小"])
        decision = chase.decide(rounds, cfg, state)
        self.assertEqual(decision.bet_topicid, 102)

    def test_combo_second_call_same_topicid_skips_already_bet_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(
                mode="combo",
                combo_amount=100,
                combo_types=["大", "小"],
                dry_run=False,
                state_file=os.path.join(tmp, "state.json"),
            )
            transport = FakeTransport()
            client, _ = fake_client(transport)
            state = chase.State(bet_topicids=[(1005, "大"), (1005, "小")])
            chase.place_combo_for_round(cfg, client, state, cfg.state_file, 1005, set())
        self.assertEqual(transport.post_calls, [])


class ComboTimeoutVerificationTests(unittest.TestCase):
    """现场 2026-10-17 16:30:37：combo 第二注 POST 读超时 → 判「结果不明」→ 整轮只落单边、进程退出。

    真实情况是可以查清的：读一次帖子页就知道这一注到底落没落单。
    """

    def _cfg(self, tmp):
        return make_cfg(
            mode="combo",
            combo_amount=100,
            combo_types=["大", "小"],
            dry_run=False,
            state_file=os.path.join(tmp, "state.json"),
        )

    def test_second_bet_timeout_but_already_on_forum_counts_as_success(self):
        """第二注读超时，但帖子页上已经有我方这条 —— 按成功继续，绝不重复下注。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            page = topic_page_html(
                [
                    (109991, "2026-10-17 16:30:14", "大 100"),
                    (109991, "2026-10-17 16:30:40", "小 100"),
                ]
            )
            transport = FakeTransport(
                get_results=[chase.HttpResponse(200, page)],
                post_results=[
                    chase.HttpResponse(302, ""),  # 大 成功
                    chase.DiceNetworkError("The read operation timed out"),  # 小 读超时
                ],
            )
            client, _ = fake_client(transport)
            state = chase.State()
            chase.place_combo_for_round(cfg, client, state, cfg.state_file, 1005, set())

        self.assertEqual(len(transport.post_calls), 2)  # 没有补发
        self.assertEqual(state.bet_topicids, [(1005, "大"), (1005, "小")])
        self.assertEqual(state.total_staked, 200)
        self.assertEqual(state.consecutive_bets, 1)

    def test_second_bet_timeout_confirmed_absent_reposts_once(self):
        """读两次都确认没有我方这条 → 判定没落单，按节奏补发。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            page = topic_page_html([(109991, "2026-10-17 16:30:14", "大 100")])
            transport = FakeTransport(
                get_results=[chase.HttpResponse(200, page)] * chase.VERIFY_ABSENT_READS,
                post_results=[
                    chase.HttpResponse(302, ""),
                    chase.DiceNetworkError("The read operation timed out"),
                    chase.HttpResponse(302, ""),  # 补发成功
                ],
            )
            client, _ = fake_client(transport)
            state = chase.State()
            chase.place_combo_for_round(cfg, client, state, cfg.state_file, 1005, set())

        self.assertEqual(len(transport.post_calls), 3)
        self.assertEqual(state.bet_topicids, [(1005, "大"), (1005, "小")])
        self.assertEqual(state.consecutive_bets, 1)

    def test_second_bet_timeout_unverifiable_stops_for_manual_check(self):
        """帖子页也读不到 → 仍然只能判「结果不明」停手，交人工核对。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            transport = FakeTransport(
                get_results=[chase.DiceNetworkError("boom")] * chase.VERIFY_READ_ATTEMPTS,
                post_results=[
                    chase.HttpResponse(302, ""),
                    chase.DiceNetworkError("The read operation timed out"),
                ],
            )
            client, _ = fake_client(transport)
            state = chase.State()
            with self.assertRaises(chase.StopRun) as ctx:
                chase.place_combo_for_round(cfg, client, state, cfg.state_file, 1005, set())

        self.assertEqual(ctx.exception.code, 4)
        self.assertEqual(len(transport.post_calls), 2)  # 没核实清楚就不敢补发
        self.assertEqual(state.bet_topicids, [(1005, "大")])

    def test_other_users_same_text_does_not_count_as_our_bet(self):
        """别人也发「小 100」：只认我方 uid，否则会把没落单当成已落单、漏掉补发。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            page = topic_page_html([(110065, "2026-10-17 16:30:44", "小 100")])
            transport = FakeTransport(
                get_results=[chase.HttpResponse(200, page)] * chase.VERIFY_ABSENT_READS,
                post_results=[chase.HttpResponse(302, "")],
            )
            client, _ = fake_client(transport)

            self.assertFalse(client.verify_bet_landed(1005, "小", 100))

    def test_second_bet_waits_past_forum_min_post_interval(self):
        """论坛同一用户两次发帖的真实最小间隔实测 4–12 秒（12 秒才稳落单）。

        3 秒的首延会让第二注必然撞限流，而限流这次表现为把响应挂住 → 读超时。
        """
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            times = []
            clock = {"now": 0.0}

            class Transport:
                def post(self, url, form):
                    times.append(clock["now"])
                    return chase.HttpResponse(302, "")

                def get(self, url):  # pragma: no cover - 两注都成功，不需要核实
                    raise AssertionError("本用例不该发 GET")

            client = chase.ForumClient(
                Transport(), sleep=lambda s: clock.__setitem__("now", clock["now"] + s),
                clock=lambda: clock["now"],
            )
            chase.place_combo_for_round(cfg, client, chase.State(), cfg.state_file, 1005, set())

        self.assertGreaterEqual(times[1] - times[0], 12.0)


class HalfHedgedWarningTests(unittest.TestCase):
    """半边注不自动补（用户 2026-10-17 决定）：只在启动时把最近那一轮打出来。"""

    def test_warns_only_about_latest_round_missing_a_type(self):
        state = chase.State(bet_topicids=[(1004, "大"), (1005, "大")])
        cfg = make_cfg(mode="combo", combo_types=["大", "小"])
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        chase.LOG.addHandler(handler)
        previous_level = chase.LOG.level
        chase.LOG.setLevel(logging.WARNING)
        self.addCleanup(chase.LOG.removeHandler, handler)
        self.addCleanup(chase.LOG.setLevel, previous_level)

        chase.warn_half_hedged(state, cfg)

        out = stream.getvalue()
        self.assertIn("1005", out)
        self.assertIn("小", out)
        self.assertNotIn("1004", out)  # 老轮次不刷屏

    def test_silent_for_chase_mode_and_complete_rounds(self):
        complete = chase.State(bet_topicids=[(1005, "大"), (1005, "小")])
        chase.warn_half_hedged(complete, make_cfg(mode="combo"))


class ComboDecideTests(unittest.TestCase):
    def test_combo_picks_first_uncovered_open_round(self):
        state = chase.State()
        rounds = [r(3), r(2, "小"), r(1, "豹子")]
        decision = chase.decide(rounds, make_cfg(mode="combo"), state)
        self.assertEqual(decision.bet_topicid, 3)

    def test_combo_skips_round_fully_covered_by_combo_types(self):
        state = chase.State(bet_topicids=[(3, "大"), (3, "小")])
        rounds = [r(3), r(2)]
        decision = chase.decide(rounds, make_cfg(mode="combo"), state)
        self.assertEqual(decision.bet_topicid, 2)

    def test_combo_ignores_drought_threshold(self):
        # chase 模式下 threshold=100 永远不可能触发 armed,combo 模式不依赖连旱
        # 验证:drought 远小于 threshold,但 combo 仍然挑开放轮下注
        state = chase.State()
        rounds = [r(2, "豹子"), r(1)]  # round 1 是开放轮
        decision = chase.decide(
            rounds, make_cfg(mode="combo", threshold=100), state
        )
        self.assertEqual(decision.bet_topicid, 1)


class ComboCapTests(unittest.TestCase):
    def test_pending_round_amount_counts_uncovered_types(self):
        # 整轮预算 = 该轮还没下的类型数 × combo_amount；chase 模式就是单注金额
        cfg = make_cfg(mode="combo", combo_amount=300, combo_types=["大", "小"])
        self.assertEqual(chase.pending_round_amount(chase.State(), cfg, 8), 600)
        self.assertEqual(
            chase.pending_round_amount(chase.State(bet_topicids=[(7, "大")]), cfg, 7), 300
        )
        self.assertEqual(
            chase.pending_round_amount(
                chase.State(bet_topicids=[(7, "大"), (7, "小")]), cfg, 7
            ),
            0,
        )
        self.assertEqual(
            chase.pending_round_amount(chase.State(), make_cfg(mode="chase", amount=100000), 7),
            100000,
        )

    def test_combo_round_budget_blocks_before_overshooting_cap(self):
        # 起点 900 + 整轮 200 > 1000 → 整轮预算检查就该拦下（只算本注 100 的旧实现会放到 1100）
        state = chase.State(total_staked=900)
        cfg = make_cfg(mode="combo", combo_amount=100, max_total_stake=1000)
        pending = chase.pending_round_amount(state, cfg, 42)
        self.assertEqual(pending, 200)
        self.assertIsNotNone(chase.cap_blocker(state, cfg, pending))

    def test_cap_blocker_combo_uses_combo_amount(self):
        # total_staked=950, combo_amount=100, cap=1000 => 950+100 > 1000? 1050 > 1000 -> blocked
        state = chase.State(total_staked=950)
        blocker = chase.cap_blocker(
            state, make_cfg(mode="combo", combo_amount=100, max_total_stake=1000), 100
        )
        self.assertIsNotNone(blocker)

    def test_cap_blocker_chase_uses_amount(self):
        # total_staked=950, amount=100, cap=1000 => 950+100 > 1000 -> blocked
        state = chase.State(total_staked=950)
        blocker = chase.cap_blocker(
            state, make_cfg(mode="chase", amount=100, max_total_stake=1000), 100
        )
        self.assertIsNotNone(blocker)


if __name__ == "__main__":
    unittest.main()
