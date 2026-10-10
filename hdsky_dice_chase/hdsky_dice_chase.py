#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HDSky 骰子自动追号脚本。

把 hdsky_dice_betting_assistant.user.js 里已验证的抓取 / 解析 / 下注重试逻辑搬到
命令行，并加上「连旱达标后每开一轮自动跟注」的常驻循环：

  * 轮询骰子列表页，按 topicid 游标发现新轮；
  * 每次轮询都从列表页幂等重算目标类型的连旱度（漏看不会污染计数）；
  * 连旱达到阈值后对之后每一个新开轮下注，目标类型开出即停手。

默认 dry-run：只打印「本应下注」，不向论坛发帖。真实下注需要显式开启。

只依赖标准库。日志与异常信息不会回显 Cookie（见 R20）。
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import logging
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

LOG = logging.getLogger("dice_chase")

# --------------------------------------------------------------------- 常量
BASE_URL = "https://hdsky.me"
FORUM_ID = 71
LIST_PATH = f"/forums.php?action=viewforum&forumid={FORUM_ID}"
POST_PATH = "/forums.php?action=post"

BET_TYPES = ("豹子", "顺子", "大", "小")
DEFAULT_TARGET = "豹子"

AMOUNT_MIN = 100
AMOUNT_MAX = 100000

# 论坛对成功投注回 302；被拒时用 200 渲染错误页
SUCCESS_STATUSES = (302, 303, 301, 307, 308)
REJECTED_STATUS = 200

REQUEST_TIMEOUT_S = 15.0
# 与 user.js 一致的重试节奏：首次 3s，其后每次 10s，上限 12 次
RETRY_FIRST_DELAY_S = 3.0
RETRY_DELAY_S = 10.0
RETRY_MAX_ATTEMPTS = 12
# 论坛限制同一用户连续发帖，两次 POST 之间的下限
MIN_POST_INTERVAL_S = 3.0

DEFAULT_POLL_INTERVAL_S = 15.0
DEFAULT_MIN_POLL_INTERVAL_S = 3.0
DEFAULT_MAX_LIST_PAGES = 10
MAX_REMEMBERED_BETS = 200

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# 解析规则沿用 user.js：链接文案与正则，而不是它的 CSS 选择器（标准库没有选择器引擎）。
# 行级分块是 KTD7 的落点：锁定与结果都在同一个 <tr> 内判定，避免跨行错配。
ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
LINK_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
LOCKED_RE = re.compile(r"<img\b[^>]*class=\"locked", re.IGNORECASE)
TOPICID_RE = re.compile(r"topicid=(\d+)")
RESULT_RE = re.compile(r"【\s*(豹子|顺子|大|小)")
DRAW_TIME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::\d{2})?")
LIST_PAGE_RE = re.compile(r"action=viewforum[^\"']*?[?&]page=(\d+)", re.IGNORECASE)
LOGIN_MARKER_RE = re.compile(r"login\.php|name=[\"']username[\"']", re.IGNORECASE)
REJECTION_TEXT_RE = re.compile(
    r"<td\b[^>]*class=\"[^\"]*text[^\"]*\"[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL
)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

LIST_ROUND_MARKER = "本轮开奖时间"


# --------------------------------------------------------------------- 异常
class ConfigError(Exception):
    """配置缺失或非法。"""


class DiceChaseError(Exception):
    """本脚本所有可预期错误的基类。"""


class DiceNetworkError(DiceChaseError):
    """传输层失败：连不上、超时、读不到页面。"""


class SessionExpired(DiceChaseError):
    """论坛会话失效（被重定向到登录页）。"""


class BetRejected(DiceChaseError):
    """论坛明确渲染了拒绝页 —— 这类失败才可以安全重试。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class BetUncertain(DiceChaseError):
    """结果不明：请求可能已经到达论坛，重试会造成重复下注。"""


class LockHeld(DiceChaseError):
    """已有实例在运行。"""


class StopRun(Exception):
    """要求主循环停止并带指定退出码返回。"""

    def __init__(self, code: int, message: str = ""):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------- 配置
@dataclass
class Config:
    cookie: str
    user_agent: str = DEFAULT_USER_AGENT
    target_type: str = DEFAULT_TARGET
    amount: int = AMOUNT_MAX
    threshold: int = 5
    poll_interval: float = DEFAULT_POLL_INTERVAL_S
    min_poll_interval: float = DEFAULT_MIN_POLL_INTERVAL_S
    max_list_pages: int = DEFAULT_MAX_LIST_PAGES
    dry_run: bool = True
    max_total_stake: Optional[int] = 1000000
    max_consecutive_bets: Optional[int] = 20
    state_file: str = "dice_chase_state.json"
    lock_file: str = "dice_chase.lock"
    log_file: Optional[str] = None


CONFIG_FIELDS = tuple(Config.__dataclass_fields__)
INT_FIELDS = ("amount", "threshold", "max_list_pages")
FLOAT_FIELDS = ("poll_interval", "min_poll_interval")
STR_FIELDS = ("user_agent", "target_type", "state_file", "lock_file")
OPTIONAL_INT_FIELDS = ("max_total_stake", "max_consecutive_bets")


def _as_optional_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(value)


def load_config(path: str, overrides: Optional[Dict[str, object]] = None) -> Config:
    """读取并校验配置。命令行覆盖项优先于文件内容。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"配置文件不存在: {path}")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}: {exc}")
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON: {path} ({exc})")

    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是一个 JSON 对象")

    merged: Dict[str, object] = dict(raw)
    for key, value in (overrides or {}).items():
        if value is not None:
            merged[key] = value

    unknown = sorted(set(merged) - set(CONFIG_FIELDS))
    if unknown:
        raise ConfigError("配置里有未知字段: " + ", ".join(unknown))

    raw_cookie = merged.get("cookie")
    cookie = raw_cookie.strip() if isinstance(raw_cookie, str) else ""
    if not cookie:
        raise ConfigError("配置缺少 cookie —— 请从浏览器复制论坛会话 Cookie")

    cfg = Config(cookie=cookie)
    try:
        for name in STR_FIELDS:
            if merged.get(name) is not None:
                setattr(cfg, name, str(merged[name]))
        if merged.get("log_file") is not None:
            cfg.log_file = str(merged["log_file"])
        for name in INT_FIELDS:
            if merged.get(name) is not None:
                setattr(cfg, name, int(merged[name]))
        for name in FLOAT_FIELDS:
            if merged.get(name) is not None:
                setattr(cfg, name, float(merged[name]))
        for name in OPTIONAL_INT_FIELDS:
            if name in merged:
                setattr(cfg, name, _as_optional_int(merged[name]))
        if merged.get("dry_run") is not None:
            cfg.dry_run = bool(merged["dry_run"])
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置字段类型不正确: {exc}")

    validate_config(cfg)
    return cfg


def validate_config(cfg: Config) -> None:
    if cfg.target_type not in BET_TYPES:
        raise ConfigError(
            "目标类型必须是 " + " / ".join(BET_TYPES) + f" 之一，当前为 {cfg.target_type!r}"
        )
    if not AMOUNT_MIN <= cfg.amount <= AMOUNT_MAX:
        raise ConfigError(
            f"单注金额必须在 {AMOUNT_MIN}–{AMOUNT_MAX} 之间，当前为 {cfg.amount}"
        )
    if cfg.threshold < 1:
        raise ConfigError(f"连旱阈值必须 >= 1，当前为 {cfg.threshold}")
    if cfg.poll_interval <= 0:
        raise ConfigError(f"轮询间隔必须 > 0，当前为 {cfg.poll_interval}")
    if cfg.min_poll_interval <= 0:
        raise ConfigError(f"轮询间隔下限必须 > 0，当前为 {cfg.min_poll_interval}")
    if cfg.min_poll_interval > cfg.poll_interval:
        raise ConfigError("轮询间隔下限不能大于轮询间隔")
    if cfg.max_list_pages < 1:
        raise ConfigError(f"最大翻页数必须 >= 1，当前为 {cfg.max_list_pages}")
    for name in OPTIONAL_INT_FIELDS:
        value = getattr(cfg, name)
        if value is not None and value < 1:
            raise ConfigError(f"{name} 必须 >= 1 或留空(不设上限)，当前为 {value}")


# --------------------------------------------------------------- HTTP 传输层
@dataclass
class HttpResponse:
    status: int
    body: str
    final_url: str = ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向，让 302 以 HTTPError 的形式浮出来。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class UrllibTransport:
    """真实传输层：带上配置里的 Cookie，关闭自动重定向。

    Cookie 只存在于请求头里，绝不进日志。
    """

    def __init__(self, cookie: str, user_agent: str, timeout: float = REQUEST_TIMEOUT_S):
        self._cookie = cookie
        self._user_agent = user_agent
        self._timeout = timeout
        self._opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "User-Agent": self._user_agent,
            "Cookie": self._cookie,
            "Accept": "text/html,application/xhtml+xml",
        }
        if extra:
            headers.update(extra)
        return headers

    def _send(self, request: urllib.request.Request) -> HttpResponse:
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return HttpResponse(
                    response.status,
                    response.read().decode("utf-8", "replace"),
                    response.geturl(),
                )
        except urllib.error.HTTPError as exc:
            # 关闭重定向后，302/303 会走到这里，HTTPError.code 就是状态码
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:  # pragma: no cover - 读 body 失败时退化为空
                body = ""
            return HttpResponse(exc.code, body, getattr(exc, "url", request.full_url))
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise DiceNetworkError(str(exc))

    def get(self, url: str) -> HttpResponse:
        return self._send(urllib.request.Request(url, headers=self._headers()))

    def post(self, url: str, form: Dict[str, str]) -> HttpResponse:
        data = urllib.parse.urlencode(form).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers=self._headers({"Content-Type": "application/x-www-form-urlencoded"}),
        )
        return self._send(request)


class ForumClient:
    """论坛读写：抓列表页、下注，并把成功 / 被拒 / 结果不明三态判干净。"""

    def __init__(
        self,
        transport,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._last_post_at: Optional[float] = None

    # -- 读 ---------------------------------------------------------------
    def fetch_list_page(self, page: int) -> str:
        url = f"{BASE_URL}{LIST_PATH}"
        if page > 1:
            url += f"&page={page}"
        response = self._transport.get(url)
        if response.status != 200:
            raise SessionExpired(
                f"列表页返回 {response.status}（很可能是会话失效后被重定向到登录页）"
            )
        if LIST_ROUND_MARKER not in response.body and (
            LOGIN_MARKER_RE.search(response.final_url or "")
            or LOGIN_MARKER_RE.search(response.body[:4000])
        ):
            raise SessionExpired("列表页是登录页内容，会话已失效")
        return response.body

    # -- 写 ---------------------------------------------------------------
    def place_bet(self, topicid: int, bet_type: str, amount: int) -> None:
        """下注一次。成功即返回，失败抛 BetRejected（可重试）或 BetUncertain（不可重试）。"""
        self._respect_post_interval()
        form = {"id": str(topicid), "type": "reply", "body": f"{bet_type} {amount}"}
        response = self._transport.post(f"{BASE_URL}{POST_PATH}", form)
        self._last_post_at = self._clock()

        if response.status in SUCCESS_STATUSES:
            return
        if response.status == REJECTED_STATUS:
            raise BetRejected(extract_rejection_reason(response.body))
        raise BetUncertain(
            f"下注返回了无法判定的状态 {response.status}，不确定论坛是否已收单"
        )

    def place_bet_until_accepted(
        self,
        topicid: int,
        bet_type: str,
        amount: int,
        on_attempt: Optional[Callable[[int], None]] = None,
    ) -> None:
        """沿用 user.js 的重试节奏：只有论坛明确拒绝才重试，其余失败立即上抛。"""
        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            if on_attempt:
                on_attempt(attempt)
            self._sleep(RETRY_FIRST_DELAY_S if attempt == 1 else RETRY_DELAY_S)
            try:
                self.place_bet(topicid, bet_type, amount)
                return
            except BetRejected:
                if attempt >= RETRY_MAX_ATTEMPTS:
                    raise

    def _respect_post_interval(self) -> None:
        if self._last_post_at is None:
            return
        elapsed = self._clock() - self._last_post_at
        if elapsed < MIN_POST_INTERVAL_S:
            self._sleep(MIN_POST_INTERVAL_S - elapsed)


def extract_rejection_reason(html: str) -> str:
    for pattern in (REJECTION_TEXT_RE, TITLE_RE):
        match = pattern.search(html or "")
        if not match:
            continue
        text = re.sub(r"\s+", " ", TAG_RE.sub(" ", match.group(1))).strip()
        if text:
            return text[:120]
    return "论坛拒绝了下注，但没有给出可读原因"


# ------------------------------------------------------------------ 列表页解析
@dataclass
class Round:
    topicid: int
    result_type: Optional[str] = None
    locked: bool = False
    draw_time: Optional[str] = None

    @property
    def open(self) -> bool:
        """还能下注的轮：尚未开奖且未锁定。"""
        return self.result_type is None and not self.locked


def parse_rounds(html: str) -> List[Round]:
    """按 <tr> 分块解析列表页，返回 DOM 顺序（最新在前）的轮次。"""
    rounds: List[Round] = []
    for row in ROW_RE.findall(html or ""):
        for match in LINK_RE.finditer(row):
            attrs, text = match.group(1), TAG_RE.sub("", match.group(2))
            if LIST_ROUND_MARKER not in text:
                continue
            topicid = TOPICID_RE.search(attrs)
            if not topicid:
                continue
            result = RESULT_RE.search(text)
            draw_time = DRAW_TIME_RE.search(text)
            rounds.append(
                Round(
                    topicid=int(topicid.group(1)),
                    result_type=result.group(1) if result else None,
                    locked=bool(LOCKED_RE.search(row)),
                    draw_time=draw_time.group(0) if draw_time else None,
                )
            )
            break
    return rounds


def dedupe_rounds(rounds: Sequence[Round]) -> List[Round]:
    """按 DOM 顺序去重（对应 user.js 里的 seen 集合）。"""
    seen = set()
    unique: List[Round] = []
    for round_ in rounds:
        if round_.topicid in seen:
            continue
        seen.add(round_.topicid)
        unique.append(round_)
    return unique


def list_total_pages(html: str) -> int:
    pages = [int(value) for value in LIST_PAGE_RE.findall(html or "")]
    return max(pages) if pages else 1


def compute_drought(rounds: Sequence[Round], target: str) -> int:
    """连旱度：从最新一轮往下连续多少个「已开奖且不是目标类型」的轮次。

    未开奖的当前轮不计入。全程未出现目标类型时返回已统计的轮数
    （在翻页受限的情况下这是下界，见 U3 的最大翻页数上限）。
    """
    missed = 0
    for round_ in rounds:
        if round_.result_type is None:
            continue
        if round_.result_type == target:
            return missed
        missed += 1
    return missed


def target_just_hit(rounds: Sequence[Round], target: str) -> bool:
    """最新一个已开奖轮是否就是目标类型。"""
    for round_ in rounds:
        if round_.result_type is None:
            continue
        return round_.result_type == target
    return False


def next_poll_interval(cfg: Config, rounds: Sequence[Round], now_ts: Optional[float] = None) -> float:
    """能读到本轮开奖时间时把间隔压到剩余时间的三分之一，否则用配置值。"""
    remaining = _seconds_to_draw(rounds, now_ts)
    if remaining is None:
        return cfg.poll_interval
    return max(cfg.min_poll_interval, min(cfg.poll_interval, remaining / 3.0))


def _seconds_to_draw(rounds: Sequence[Round], now_ts: Optional[float] = None) -> Optional[float]:
    now_ts = time.time() if now_ts is None else now_ts
    for round_ in rounds:
        if not round_.open or not round_.draw_time:
            continue
        try:
            target_ts = time.mktime(time.strptime(round_.draw_time[:16], "%Y-%m-%d %H:%M"))
        except ValueError:
            continue
        return max(0.0, target_ts - now_ts)
    return None


# ------------------------------------------------------------------ 状态文件
@dataclass
class State:
    last_seen_topicid: int = 0
    armed: bool = False
    bet_topicids: List[int] = field(default_factory=list)
    total_staked: int = 0
    consecutive_bets: int = 0


SAFE_STATE_NOTE = "状态文件缺失或损坏：以「不追号、不补单」的安全默认启动"


def load_state(path: str):
    """返回 (State, 警告文本或 None)。损坏时回落到安全默认，绝不让游标凭空前进。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return State(), None
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return State(), f"{SAFE_STATE_NOTE}（{exc}）"

    if not isinstance(raw, dict):
        return State(), SAFE_STATE_NOTE
    try:
        state = State(
            last_seen_topicid=int(raw.get("last_seen_topicid") or 0),
            armed=bool(raw.get("armed")),
            bet_topicids=[int(x) for x in (raw.get("bet_topicids") or [])],
            total_staked=int(raw.get("total_staked") or 0),
            consecutive_bets=int(raw.get("consecutive_bets") or 0),
        )
    except (TypeError, ValueError):
        return State(), SAFE_STATE_NOTE
    return state, None


def save_state(path: str, state: State) -> None:
    """原子写盘：先写临时文件再替换，避免崩溃留下半截状态。"""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(asdict(state), fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def reset_run_state(state: State) -> bool:
    """每次启动重置 armed 与连续下注计数,让阈值重新评估。

    total_staked / bet_topicids / last_seen_topicid 保留:
    - total_staked 是跨重启的累计护栏,语义不该因重启清零
    - bet_topicids / last_seen_topicid 是去重记录,保留能避免重启后重复下注
    """
    changed = state.armed or state.consecutive_bets > 0
    state.armed = False
    state.consecutive_bets = 0
    return changed


def cap_blocker(state: State, cfg: Config) -> Optional[str]:
    if cfg.max_total_stake is not None and state.total_staked + cfg.amount > cfg.max_total_stake:
        return (
            f"累计投入 {state.total_staked} + 本注 {cfg.amount} 会超过总投入上限 "
            f"{cfg.max_total_stake}"
        )
    if cfg.max_consecutive_bets is not None and state.consecutive_bets + 1 > cfg.max_consecutive_bets:
        return f"连续下注已达上限 {cfg.max_consecutive_bets}"
    return None


# -------------------------------------------------------------------- 策略
@dataclass
class Decision:
    armed: bool = False
    drought: int = 0
    bet_topicid: Optional[int] = None
    reason: str = ""


def decide(rounds: Sequence[Round], cfg: Config, state: State) -> Decision:
    """根据连旱度推进追号态，并挑出这一轮要下注的开放轮（每轮最多一次）。"""
    drought = compute_drought(rounds, cfg.target_type)

    if target_just_hit(rounds, cfg.target_type):
        state.armed = False
        state.consecutive_bets = 0
    if not state.armed and drought >= cfg.threshold:
        state.armed = True

    decision = Decision(armed=state.armed, drought=drought)
    if not state.armed:
        decision.reason = f"连旱 {drought} < 阈值 {cfg.threshold}，待机"
        return decision

    bet_already = set(state.bet_topicids)
    for round_ in rounds:
        if round_.topicid in bet_already or not round_.open:
            continue
        decision.bet_topicid = round_.topicid
        return decision

    decision.reason = "追号中，但当前没有可下注的开放轮"
    return decision


# ------------------------------------------------------------------ 并发保护
def acquire_lock(path: str) -> str:
    """独占创建锁文件；陈旧锁（持有进程已退出）自动接管。"""
    for attempt in (1, 2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if attempt == 2 or not _lock_is_stale(path):
                raise LockHeld(f"锁文件已存在，另一个实例正在运行: {path}")
            LOG.warning("发现陈旧锁文件（持有进程已退出），接管: %s", path)
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        else:
            with os.fdopen(fd, "w") as fh:
                fh.write(str(os.getpid()))
            return path
    raise LockHeld(f"无法获取锁文件: {path}")


def _lock_is_stale(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return True
    if pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def release_lock(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


# -------------------------------------------------------------------- 主循环
class StopFlag:
    def __init__(self) -> None:
        self.requested = False


def install_signal_handlers(stop: StopFlag) -> None:
    def handler(signum, _frame):
        stop.requested = True
        LOG.info("收到信号 %s，将在安全点退出", signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)


def collect_rounds(client: ForumClient, cfg: Config) -> List[Round]:
    """抓取列表页（必要时翻页）并返回去重后的轮次。

    翻页在「已找到目标类型」或「到达最大翻页数」时停止。
    """
    html = client.fetch_list_page(1)
    rounds = dedupe_rounds(parse_rounds(html))
    last_page = min(list_total_pages(html), cfg.max_list_pages)
    page = 1
    while page < last_page and not any(r.result_type == cfg.target_type for r in rounds):
        page += 1
        try:
            more = client.fetch_list_page(page)
        except DiceNetworkError as exc:
            LOG.warning("读取第 %s 页失败，先用已抓到的 %s 轮继续: %s", page, len(rounds), exc)
            break
        parsed = dedupe_rounds(parse_rounds(more))
        if not parsed:
            break
        rounds = dedupe_rounds(list(rounds) + parsed)
    return rounds


def place_bet_for_round(
    cfg: Config,
    client: ForumClient,
    state: State,
    state_path: str,
    topicid: int,
    announced: set,
) -> None:
    """下注（或 dry-run 打印）。状态在下注结果确认后才落盘。"""
    if cfg.dry_run:
        if topicid not in announced:
            announced.add(topicid)
            LOG.info(
                "[dry-run] 本应下注 %s %s @ topicid=%s（未写入状态文件）",
                cfg.target_type,
                cfg.amount,
                topicid,
            )
        return

    try:
        client.place_bet_until_accepted(
            topicid,
            cfg.target_type,
            cfg.amount,
            on_attempt=lambda attempt: LOG.info(
                "下注尝试 %s/%s @ topicid=%s", attempt, RETRY_MAX_ATTEMPTS, topicid
            ),
        )
    except (BetUncertain, DiceNetworkError) as exc:
        # 请求可能已经到达论坛，重试会造成重复下注 —— 停手并交人工核对
        save_state(state_path, state)
        LOG.error("下注结果不明，已停手，请人工核对论坛侧是否已收到这一注: %s", exc)
        raise StopRun(4) from exc
    except BetRejected as exc:
        save_state(state_path, state)
        LOG.error("论坛拒绝下注，重试 %s 次后仍失败: %s", RETRY_MAX_ATTEMPTS, exc.reason)
        raise StopRun(5) from exc

    state.bet_topicids.append(topicid)
    state.bet_topicids = state.bet_topicids[-MAX_REMEMBERED_BETS:]
    state.total_staked += cfg.amount
    state.consecutive_bets += 1
    LOG.info(
        "下注成功：%s %s @ topicid=%s（本次运行累计投入 %s）",
        cfg.target_type,
        cfg.amount,
        topicid,
        state.total_staked,
    )
    save_state(state_path, state)


def sleep_interruptibly(seconds: float, stop: StopFlag, chunk: float = 1.0) -> None:
    end = time.monotonic() + max(0.0, seconds)
    while not stop.requested:
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(chunk, left))


def run_loop(cfg: Config, client: ForumClient, state: State, stop: StopFlag, state_path: str) -> int:
    announced: set = set()
    while not stop.requested:
        try:
            rounds = collect_rounds(client, cfg)
        except SessionExpired:
            raise
        except DiceNetworkError as exc:
            # 只读轮询的瞬时失败不值得终止整个进程
            LOG.warning("本轮抓取失败，稍后重试: %s", exc)
            sleep_interruptibly(cfg.poll_interval, stop)
            continue

        if not rounds:
            LOG.warning("列表页没有解析到任何轮次，稍后重试")
            sleep_interruptibly(cfg.poll_interval, stop)
            continue

        decision = decide(rounds, cfg, state)

        if decision.bet_topicid is not None:
            blocker = cap_blocker(state, cfg)
            if blocker:
                save_state(state_path, state)
                LOG.error("触发保险上限，停止投注并退出: %s", blocker)
                return 3
            place_bet_for_round(cfg, client, state, state_path, decision.bet_topicid, announced)
        else:
            LOG.info(
                "连旱 %s / 阈值 %s，%s",
                decision.drought,
                cfg.threshold,
                decision.reason or "待机",
            )

        newest = max((r.topicid for r in rounds), default=state.last_seen_topicid)
        if newest > state.last_seen_topicid:
            state.last_seen_topicid = newest
        save_state(state_path, state)

        sleep_interruptibly(next_poll_interval(cfg, rounds), stop)

    return 0


# ----------------------------------------------------------------------- 入口
def setup_logging(level: int = logging.INFO, log_file: Optional[str] = None) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="HDSky 骰子自动追号脚本（默认 dry-run，不会真实下注）"
    )
    parser.add_argument("--config", default="config.json", help="配置文件路径（默认 config.json）")
    parser.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=None,
        help="只打印本应下注的内容（默认行为）",
    )
    parser.add_argument(
        "--live", dest="dry_run", action="store_false",
        help="真实下注 —— 会真的花掉积分，请先用 dry-run 验证",
    )
    parser.add_argument("--target-type", dest="target_type", choices=BET_TYPES, help="追号的目标类型")
    parser.add_argument("--amount", type=int, help=f"单注金额（{AMOUNT_MIN}–{AMOUNT_MAX}）")
    parser.add_argument("--threshold", type=int, help="连旱阈值 N")
    parser.add_argument("--poll-interval", dest="poll_interval", type=float, help="轮询间隔（秒）")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    return parser.parse_args(argv)


def cli_overrides(args: argparse.Namespace) -> Dict[str, object]:
    return {
        "dry_run": args.dry_run,
        "target_type": args.target_type,
        "amount": args.amount,
        "threshold": args.threshold,
        "poll_interval": args.poll_interval,
    }


def validate_session(client: ForumClient) -> None:
    """拉一次列表页确认会话可用；失效就退出，不进循环。"""
    client.fetch_list_page(1)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)

    try:
        cfg = load_config(args.config, cli_overrides(args))
    except ConfigError as exc:
        LOG.error("配置错误: %s", exc)
        return 2

    if cfg.log_file:
        # 已经在 setup_logging 里建好 stdout handler；现在追加文件 handler
        logging.getLogger().addHandler(logging.FileHandler(cfg.log_file, encoding="utf-8"))

    stop = StopFlag()
    install_signal_handlers(stop)

    state_path = cfg.state_file
    state, note = load_state(state_path)
    if note:
        LOG.warning("%s", note)
    if reset_run_state(state):
        LOG.info("本次启动重置追号态：armed 与连续下注计数清零,阈值将重新评估")

    client = ForumClient(UrllibTransport(cfg.cookie, cfg.user_agent))

    try:
        validate_session(client)
    except SessionExpired as exc:
        LOG.error("会话校验失败: %s", exc)
        return 2
    except DiceNetworkError as exc:
        LOG.error("无法访问论坛: %s", exc)
        return 2

    try:
        lock = acquire_lock(cfg.lock_file)
    except LockHeld as exc:
        LOG.error("%s", exc)
        return 2

    LOG.info(
        "启动：目标=%s 金额=%s 阈值=%s %s",
        cfg.target_type,
        cfg.amount,
        cfg.threshold,
        "（dry-run，不会真实下注）" if cfg.dry_run else "（实盘，会真的花积分）",
    )

    try:
        return run_loop(cfg, client, state, stop, state_path)
    except StopRun as exc:
        return exc.code
    except SessionExpired as exc:
        LOG.error("运行中会话失效，已停止（请刷新 Cookie 后重启）: %s", exc)
        return 2
    finally:
        release_lock(lock)


if __name__ == "__main__":
    sys.exit(main())
