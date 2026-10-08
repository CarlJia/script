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
    def setUp(self):
        self.raw = chase.parse_rounds(fixture_html())
        self.rounds = chase.dedupe_rounds(self.raw)
        self.by_id = {r.topicid: r for r in self.rounds}

    def test_fixture_parses_expected_rounds_in_dom_order(self):
        self.assertEqual(
            [r.topicid for r in self.rounds], [1005, 1004, 1003, 1002, 1001, 1000, 999, 998]
        )

    def test_duplicate_topic_counted_once(self):
        self.assertEqual(len(self.raw), 9)
        self.assertEqual(len(self.rounds), 8)

    def test_drought_counts_consecutive_misses(self):
        # 1003 小 / 1002 大 / 1001 顺子 / 1000 豹子 → 3
        self.assertEqual(chase.compute_drought(self.rounds, "豹子"), 3)

    def test_open_rounds_exclude_locked(self):
        self.assertFalse(self.by_id[1005].locked)
        self.assertTrue(self.by_id[1004].locked)
        self.assertEqual([r.topicid for r in self.rounds if r.open], [1005, 998])

    def test_locked_marker_is_scoped_to_its_own_row(self):
        # 行级判定（KTD7）：1004 的 locked 图片不能污染相邻的 1005
        self.assertFalse(self.by_id[1005].locked)

    def test_malformed_draw_time_does_not_affect_drought(self):
        self.assertIsNone(self.by_id[998].draw_time)
        self.assertEqual(chase.compute_drought(self.rounds, "豹子"), 3)

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

    def test_list_total_pages(self):
        self.assertEqual(chase.list_total_pages(fixture_html()), 2)
        self.assertEqual(chase.list_total_pages(page_html([])), 1)

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
        transport = FakeTransport(get_results=[chase.HttpResponse(200, fixture_html())])
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


if __name__ == "__main__":
    unittest.main()
