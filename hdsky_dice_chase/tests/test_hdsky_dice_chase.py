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


def page_html(rows, total_pages=None):
    pager = ""
    if total_pages and total_pages > 1:
        links = " ".join(
            f'<a href="forums.php?action=viewforum&forumid=71&page={page}">{page}</a>'
            for page in range(2, total_pages + 1)
        )
        pager = f'<p class="pager">{links}</p>'
    return f"<html><body><table>{''.join(rows)}</table>{pager}</body></html>"


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
        state = chase.State(armed=True, bet_topicids=[5])
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
                last_seen_topicid=7, armed=True, bet_topicids=[1, 2], total_staked=200, consecutive_bets=2
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
        blocker = chase.cap_blocker(state, make_cfg(max_total_stake=1000000, amount=100000))
        self.assertIsNotNone(blocker)

    def test_consecutive_bet_cap(self):
        state = chase.State(consecutive_bets=20)
        blocker = chase.cap_blocker(state, make_cfg(max_consecutive_bets=20))
        self.assertIsNotNone(blocker)

    def test_caps_can_be_disabled(self):
        state = chase.State(total_staked=10**9, consecutive_bets=10**6)
        blocker = chase.cap_blocker(
            state, make_cfg(max_total_stake=None, max_consecutive_bets=None)
        )
        self.assertIsNone(blocker)

    def test_reset_run_state_clears_armed_and_consecutive_bets(self):
        state = chase.State(armed=True, consecutive_bets=5)
        self.assertTrue(chase.reset_run_state(state))
        self.assertFalse(state.armed)
        self.assertEqual(state.consecutive_bets, 0)

    def test_reset_run_state_preserves_other_fields(self):
        state = chase.State(
            armed=True,
            consecutive_bets=4,
            total_staked=350,
            bet_topicids=[1, 2, 3],
            last_seen_topicid=999,
        )
        chase.reset_run_state(state)
        self.assertFalse(state.armed)
        self.assertEqual(state.consecutive_bets, 0)
        self.assertEqual(state.total_staked, 350)
        self.assertEqual(state.bet_topicids, [1, 2, 3])
        self.assertEqual(state.last_seen_topicid, 999)

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
        self.assertEqual(state.bet_topicids, [1005])
        self.assertEqual(state.total_staked, cfg.amount)
        self.assertEqual(state.consecutive_bets, 1)
        self.assertEqual(loaded.bet_topicids, [1005])

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


# --------------------------------------------------------------- 主循环端到端
class LoopTests(unittest.TestCase):
    """用真实 fixture 驱动整个 run_loop —— 对应 plan 的「集成（离线）」一行。

    不起网络：假 transport 返回本地 fixture，sleep_interruptibly 被 patch 掉
    避免真等；循环的终止由 transport 在指定次数后置停机标志来触发。
    """

    def _drive(self, tmp, *, dry_run, stop_after_gets, stop_on_post=False, reset_state=False, **cfg_over):
        defaults = dict(threshold=1, amount=100)
        defaults.update(cfg_over)
        cfg = make_cfg(
            dry_run=dry_run,
            state_file=os.path.join(tmp, "state.json"),
            lock_file=os.path.join(tmp, "lock"),
            **defaults,
        )
        html = fixture_html()
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
                if stop_on_post:
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
        self.assertEqual(state.bet_topicids, [77245])
        self.assertEqual(state.total_staked, 100)
        self.assertEqual(state.consecutive_bets, 1)
        self.assertIsNone(note)
        self.assertEqual(saved.bet_topicids, [77245])

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


if __name__ == "__main__":
    unittest.main()
