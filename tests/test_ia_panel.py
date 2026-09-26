# -*- coding: utf-8 -*-
"""设置信息架构（IA）重构的守卫（2026-09-26）。

用户原话是验收标准：

> 所以配置应该分为三类：1、echo 通用设置，快捷键，打开形式，运行状态
> 2、会议、语音指令等核心业务使用配置，
> 3、智能体选择、模型路由、设备选择，下面附已配对后端连接情况，本地能力部署运行情况
>    （仅展示配置了要用的本地模型，其它的折叠起来）…
> whisper 的三个你建议留着的留下，但是界面设计的时候要默认折叠掉，现在的界面内容太多了，
> 易用性太差

所以这里钉住的不是"某个函数还在"，而是**信息架构本身**：

* 顶层就是那三类 + 历史位（第四个位置留给历史，这一轮只做占位）；
* 「其余默认折叠」（whisper 三档落在默认收起的折叠区里）；
* 「一个实体只有一处状态」：会议转写/分离/声纹由谁做只在会议卡里；播放设备池只在设备卡里；
* 废弃开关（`meetingDiarize` / `voiceprintEnabled`）在界面上一个字都不出现；
* 清理卡是"先预览、后确认"的（两个端点 + 勾选 + 保留钉子）。
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _view_body(html, view):
    """`#view-…` 那一段（到它自己的 `</section>` 为止）—— 视图内不再嵌 section。"""
    start = html.index('id="view-%s"' % view)
    return html[start:html.index("</section>", start)]


class IATabsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _read("web", "app.js")
        cls.html = _read("web", "index.html")

    def test_three_config_tabs_plus_history(self):
        views = re.search(r"const _VIEWS = \[(.*?)\]", self.js, re.S).group(1)
        for name in ("general", "business", "capability", "history"):
            self.assertIn('"%s"' % name, views, "少了 %s 页签" % name)
        for gone in ("voice", "agent"):
            self.assertNotIn('"%s"' % gone, views, "旧设置页签 %s 应已被三类替掉" % gone)
        # 旧的四个设置页签容器必须消失（内容挪进新页签，不是复制一份）
        for gone in ('id="view-voice"', 'id="view-agent"', 'id="setPaneVoice"',
                     'id="setPaneAgent"'):
            self.assertNotIn(gone, self.html, "%s 应已删除" % gone)
        for want in ('id="view-business"', 'id="setPaneBusiness"',
                     'id="view-capability"', 'id="setPaneCapability"'):
            self.assertIn(want, self.html, "index.html 缺少 %s" % want)

    def test_old_deep_links_are_folded_to_the_new_tabs(self):
        """书签/深链不能因为重构就失效（`?view=voice` 等）。"""
        aliases = re.search(r"const VIEW_ALIASES = \{(.*?)\};", self.js, re.S).group(1)
        for old, new in (("settings", "general"), ("boot", "general"), ("wizard", "general"),
                         ("voice", "business"), ("agent", "capability"),
                         ("failover", "capability"), ("capabilities", "capability")):
            with self.subTest(old=old):
                self.assertRegex(aliases, r"%s:\s*\"%s\"" % (old, new),
                                 "%s 没有折算到 %s" % (old, new))

    def test_history_is_two_real_pages_not_a_placeholder(self):
        """第四位（历史）**不再是占位** —— 指令历史 + 会议历史，两个子页签都真有内容。

        2026-09-26（第二轮）：这一页原来是"后续版本"说明 + 一个去「会议记录」的入口。
        现在子页签、两个列表宿主、清空按钮、筛选与「加载更多」都在这一页里。
        """
        body = _view_body(self.html, "history")
        for want in ('data-htab="commands"', 'data-htab="meetings"',
                     'id="htab-commands"', 'id="htab-meetings"'):
            self.assertIn(want, body, "历史页缺少 %s" % want)
        for gone in ("后续版本", "只预留位置", 'data-goto="meetings"'):
            self.assertNotIn(gone, body, "历史页不该再是占位（%s）" % gone)
        # 两个子页签各自的列表宿主、清空按钮都在这一页里（ID 不许搬家到别处）
        for want in ('id="historyList"', 'id="btnClearCmds"', 'id="meetingList"'):
            self.assertIn(want, body, "历史页缺少 %s" % want)
        # 子页签复用会议详情页那套控件（**不新造控件**）
        self.assertIn('class="tabs-sm"', body)
        self.assertIn('class="tab-sm active" data-htab="commands"', body)
        self.assertIn("function switchHistoryTab(", self.js)
        self.assertIn('$$("[data-htab]")', self.js)


class MeetingRecordsMergedIntoHistoryTests(unittest.TestCase):
    """「会议记录」整体并入「历史 → 会议历史」（顶层页签 6 → 5，2026-09-26 第二轮）。

    为什么并：那是**同一个实体**（同一批 `meetings` 行、同一个目录）。两个页签并存
    必然出现"同一场会议两处状态"，而用户的规矩是「一个实体只有一处状态」。
    所以这里钉的不是"某个函数还在"，而是**没有第二份**：
    一个列表宿主、一个渲染函数、一处状态字段。
    """

    @classmethod
    def setUpClass(cls):
        cls.js = _read("web", "app.js")
        cls.html = _read("web", "index.html")

    def test_top_level_tabs_are_five_and_meetings_is_gone(self):
        views = re.findall(r'"([a-z]+)"',
                           re.search(r"const _VIEWS = \[(.*?)\]", self.js, re.S).group(1))
        self.assertEqual(views, ["dashboard", "general", "business", "capability", "history"],
                         "并进历史后顶层应当是 5 个页签：%s" % views)
        self.assertNotIn('data-view="meetings"', self.html, "「会议记录」不再是顶层页签")
        self.assertNotIn('id="view-meetings"', self.html,
                         "独立的会议视图已删（内容整页并入历史页，不是复制一份）")
        for gone in ('id="setPaneMeetings"', 'data-view="会议记录"'):
            self.assertNotIn(gone, self.html)

    def test_old_deep_link_folds_to_the_meeting_history_sub_tab(self):
        """老书签 `?view=meetings` 不许失效：落到历史页**并自动打开「会议历史」**。"""
        aliases = re.search(r"const VIEW_ALIASES = \{(.*?)\};", self.js, re.S).group(1)
        self.assertRegex(aliases, r'meetings:\s*"history"', "meetings 没有折算到 history")
        self.assertIn("_bootWantHist", self.js, "深链要记住「落到会议历史子页签」")
        self.assertIn('switchHistoryTab(rawName === "meetings" ? "meetings" : _histTab)', self.js,
                      "switchView 要把老入口落到第二个子页签")

    def test_one_entity_one_place(self):
        """**同一实体只有一处渲染**：会议条目的 DOM 与渲染函数各只有一份。"""
        self.assertEqual(self.html.count('id="meetingList"'), 1,
                         "会议列表宿主只许有一个（并入时最容易出的错就是复制一份）")
        self.assertEqual(self.html.count('id="historyList"'), 1)
        self.assertEqual(self.html.count('id="compressBox"'), 1)
        self.assertEqual(self.html.count('id="importBox"'), 1)
        self.assertEqual(self.js.count("function renderMeetingItems("), 1,
                         "会议条目只许有一个渲染函数（仪表盘「最近几场」与历史页共用它）")
        self.assertEqual(self.js.count("function renderCmdList("), 1)
        # 「会议条目」的 HTML 模板只有 renderMeetingItems 里那一处
        self.assertEqual(self.js.count('class="meeting-item"'), 1,
                         "会议条目模板被复制了第二份")
        # 会议列表宿主只有两处被画：进入历史页加载时 + 转写进度轮询时。
        # （仪表盘那张小卡画的是 `#recentMeetings`，不是同一个宿主。）
        self.assertEqual(self.js.count('$("#meetingList")'), 2,
                         "#meetingList 的渲染点应当恰好两处：%d"
                         % self.js.count('$("#meetingList")'))

    def test_command_history_filters_and_load_more(self):
        """指令历史：按关键词/时间过滤 + 分页（「加载更多」）。

        过滤与翻页**都在服务端**（`q` / `since` / `limit` / `offset`）——
        拿"已加载的这一页"在本地过滤会让翻页语义错位（第 2 页可能是过滤后的第 0 条），
        而一次拉几千条正是这次要避免的事。所以这里连"参数名"一起钉住。
        """
        body = _view_body(self.html, "history")
        for want in ('id="cmdFilter"', 'id="cmdRange"', 'id="btnCmdFilter"', 'id="btnCmdMore"'):
            self.assertIn(want, body, "指令历史缺少 %s" % want)
        self.assertIn("function cmdQueryUrl(", self.js)
        q = self.js[self.js.index("function cmdQueryUrl("):]
        q = q[:q.index("\n}\n")]
        for token in ("limit=", "offset=", "q=", "since="):
            self.assertIn(token, q, "服务端过滤/分页的参数 %s 不见了" % token)
        self.assertIn("const CMD_PAGE", self.js, "一次拉多少条要是一个明面上的常量")
        more = self.js[self.js.index("async function loadMoreHistory()"):]
        more = more[:more.index("\n}\n")]
        self.assertIn("cmdQueryUrl(_cmdRows.length)", more, "「加载更多」要按已加载条数当 offset")
        self.assertIn("_cmdRows = _cmdRows.concat(", more, "要**追加**，不是重画一遍")

    def test_empty_states_are_written_out(self):
        """空态：两个历史页都要说人话（用户点名要求会议历史有空态）。"""
        # 指令历史：**从来没说过** 与 **被筛掉** 是两件事，话也不一样
        self.assertIn("还没有指令记录", self.js)
        self.assertIn("没有符合条件的指令", self.js)
        # 会议历史：一场都没有时说清"怎么开始第一场"，不是丢一句"暂无"
        self.assertIn("还没有会议记录", self.js)
        self.assertIn("「开始录音」", self.js)
        self.assertIn("「导入录音」", self.js)

    def test_meeting_operations_are_all_still_reachable(self):
        """**操作入口一个都不能丢**（并入历史时最容易丢的就是它们）。

        逐项核对：开始/停止录音、导入录音、重新转写、压缩、清空指令历史。
        删除会议本来就没有面板入口（只有 `DELETE /api/meetings/{id}`），
        这一条不假装它存在 —— 见 test_api_contract 的路由断言。
        """
        body = _view_body(self.html, "history")
        for bid in ("btnMeetingFromList",      # 开始/停止录音
                    "btnImportToggle", "btnImportStart",      # 导入录音
                    "btnCompressToggle", "btnCompressStart",  # 压缩
                    "btnCleanShort"):                         # 清理短录音
            with self.subTest(id=bid):
                self.assertIn('id="%s"' % bid, body, "并入历史后 %s 不见了" % bid)
        for bid in ("btnMeetingFromList", "btnImportToggle", "btnCompressToggle",
                    "btnCleanShort", "btnClearCmds", "btnCmdMore", "btnCmdFilter"):
            with self.subTest(id=bid):
                self.assertIn('$("#%s")' % bid, self.js, "app.js 没有接线 %s" % bid)
        # 重新转写 / 导出 / 纪要 都在会议详情页（历史条目点进去就是它）
        detail = _read("web", "meeting.html")
        for bid in ("btnRetranscribe", "btnExport", "btnRegenSummary", "btnWorklog"):
            with self.subTest(id=bid):
                self.assertIn('id="%s"' % bid, detail, "详情页少了 %s" % bid)
        # 条目上有「看转写」入口，直达详情页的转写页签（转写文本查看）
        self.assertIn('data-open="${m.id}"', self.js)
        self.assertIn('data-tab="transcript"', self.js)
        self.assertIn("function openMeetingDetail(id, tab)", self.js)
        # 详情页要真的认 `?tab=`（否则那个入口只是"打开详情页"，落不到转写）
        self.assertIn("function applyWantedTab()", detail)
        self.assertIn("switchDocTab(want)", detail)

    def test_meeting_history_shows_the_required_fields(self):
        """会议历史的列表字段：时间/时长/段数/状态/转写档位/说话人/是否已压缩。"""
        fn = self.js[self.js.index("function renderMeetingItems("):]
        fn = fn[:fn.index("\n  }).join(\"\");")]
        for token in ("m.started_at", "m.duration_seconds", "m.segments",
                      "MEETING_STATUS_TEXT[m.status]", "m.timestamps", "m.speakerNames",
                      "m.compression"):
            with self.subTest(token=token):
                self.assertIn(token, fn, "会议历史条目少了 %s" % token)
        # 「已压缩」文案与详情页**同一份字段**（数字全部来自后端，面板不自己算）
        for token in ("beforeText", "afterText", "savedPercent"):
            self.assertIn(token, fn)
        # 转写档位的中文由服务端给（面板不抄词汇表）
        self.assertIn("capability_admin.timestamps_summary", _read("app", "api.py"))
        self.assertIn("TIMESTAMPS_LABELS", _read("app", "capability_admin.py"))


class FoldingTests(unittest.TestCase):
    """规则①：只有"配置为要用的/当前生效的"默认展开。"""

    @classmethod
    def setUpClass(cls):
        cls.js = _read("web", "app.js")
        cls.html = _read("web", "index.html")

    def test_whisper_tiers_are_folded_by_default(self):
        """whisper 三档（权重在本机但已退役）**默认折叠**，点开才见（用户点名的要求）。"""
        # 折叠区那个 helper：默认 closed（只有显式传 open 才展开）
        self.assertIn("function foldGroup(", self.js)
        fold = self.js[self.js.index("function foldGroup("):]
        fold = fold[:fold.index("\n}\n")]
        self.assertIn('data-collapse-default="${o.open ? "open" : "closed"}"', fold,
                      "折叠区默认必须是收起")
        # 「其余本地引擎」= 没被任何设置选中的那些（whisper 三档就在里面）
        m = re.search(r'foldGroup\("cap-engines-rest",(.*?)\(\{', self.js, re.S)
        self.assertIsNotNone(m, "转写卡里应当把'其余本地引擎'折起来")
        self.assertNotIn("open: true", m.group(1))
        # 分堆的判据：**配置为要用的**（sttModel / meetingSttModel 折出来的 id）之外都进折叠区
        asr = self.js[self.js.index("function capAsrLocal()"):]
        asr = asr[:asr.index("\n}\n")]
        self.assertIn("const used = comps.filter", asr)
        self.assertIn("const rest = comps.filter", asr)
        self.assertIn('foldGroup("cap-engines-rest"', asr)

    def test_other_heavy_areas_are_closed_by_default(self):
        """其余几处"内容太多"的地方也默认收起：启动日志 / 运行环境 / 已配对后端 / 清理。"""
        for cid in ("boot-logs", "cap-env", "cap-pair", "model-cleanup"):
            with self.subTest(id=cid):
                m = re.search(r'data-collapse-id="%s"[^>]*>' % re.escape(cid), self.html, re.S)
                if m:
                    self.assertIn('data-collapse-default="closed"', m.group(0),
                                  "%s 应当默认收起" % cid)
                else:
                    # 动态渲染的那张（运行环境由 renderCapEnv 画）在 app.js 里
                    self.assertIn('data-collapse-id="%s"' % cid, self.js)
        self.assertIn('data-collapse-id="cap-env"', self.js)
        self.assertRegex(self.js, r'data-collapse-id="cap-env"\s*\n?\s*data-collapse-default="closed"',
                         "运行环境卡默认收起")

    def test_paired_backends_auto_expand_once_when_paired(self):
        """默认收起，但真配上了就自动展开**一次**（之后听用户的）。"""
        self.assertIn("function autoExpandOnce(", self.js)
        self.assertIn('autoExpandOnce("cap-pair", !!(r.pair && r.pair.paired))', self.js)


class OneEntityOnePlaceTests(unittest.TestCase):
    """规则②：同一信息不许在两个页签重复出现（当前界面冗长的主因）。"""

    @classmethod
    def setUpClass(cls):
        cls.js = _read("web", "app.js")
        cls.html = _read("web", "index.html")

    def test_meeting_channel_choice_lives_only_in_the_meeting_card(self):
        """转写走哪条路 / 分离与声纹由谁做 = 会议卡一处。"""
        self.assertNotIn('id="rtCapHost"', self.html, "路由卡里的会议能力通道已撤销")
        svc = self.js[self.js.index("function renderMeetingServiceCard()"):]
        svc = svc[:svc.index("\n}\n")]
        for key in ("capabilityMeetingAsrBackend", "capabilityDiarizeBackend",
                    "capabilityEmbedBackend"):
            with self.subTest(key=key):
                self.assertIn(key, svc, "会议卡要承载 %s" % key)
        # 会议卡把它画出来（SET_CARDS.business 的 dynAfter）
        self.assertRegex(self.js, r'id: "meet"[\s\S]{0,600}?dynAfter: \(\) => renderMeetingServiceCard\(\)')

    def test_output_device_pool_lives_only_in_the_device_card(self):
        """播放设备池（优先级）只在「设备选择」卡里，不在朗读反馈卡里重复一遍。"""
        fb = re.search(r'id: "fb", title: "朗读与反馈"([\s\S]*?)\},', self.js).group(1)
        for key in ("outputDeviceIds", "commandOutputDeviceId", "meetingOutputDeviceId"):
            with self.subTest(key=key):
                self.assertNotIn(key, fb, "%s 不该在朗读反馈卡里再出现一次" % key)
        dev = re.search(r'id: "dev", title: "设备选择（设备池与优先级）"([\s\S]*?)\},', self.js).group(1)
        for key in ("device", "outputDeviceIds", "commandOutputDeviceId",
                    "meetingOutputDeviceId"):
            with self.subTest(key=key):
                self.assertIn(key, dev)

    def test_deprecated_switches_are_gone_from_the_panel(self):
        """`meetingDiarize` / `voiceprintEnabled` 已废弃：面板上不该再有"要不要分离/声纹"。"""
        for token in ("meetingDiarize", "voiceprintEnabled"):
            with self.subTest(token=token):
                self.assertNotIn(token, self.js, "%s 不该在面板脚本里" % token)
                self.assertNotIn(token, self.html, "%s 不该在页面里" % token)
        # 只剩「改名即入库」+ 显式「加入声纹库」按钮
        self.assertIn("voiceprintAutoEnroll", self.js)
        self.assertIn("改名即入库", self.js)

    def test_settings_cards_are_regrouped_into_three_tabs(self):
        """卡片分组 = 三类（业务配置 5 张、能力与智能体 2 张 + 静态卡）。"""
        block = self.js[self.js.index("const SET_CARDS = {"):]
        block = block[:block.index("\nconst SET_PLACED_ELSEWHERE")]
        groups = re.findall(r"^  (\w+): \[", block, re.M)
        self.assertEqual(groups, ["general", "business", "capability"],
                         "SET_CARDS 只该有三组：%s" % groups)
        biz = block[block.index("business: ["):block.index("capability: [")]
        self.assertEqual(len(re.findall(r'\{ id: "', biz)), 5,
                         "业务配置应当是 5 张卡（会议/语音指令/朗读与反馈/队列/工作区与归档）")
        cap = block[block.index("capability: ["):]
        self.assertEqual(len(re.findall(r'\{ id: "', cap)), 2,
                         "能力与智能体应当是 2 张设置卡（智能体/设备选择）")

    def test_every_visible_setting_has_exactly_one_home(self):
        """**每个可见设置项都要有落点** —— 否则它会掉进「未归类」兜底卡。

        2026-09-26 实测抓到过一个漏网的：`meetingAutoCompressAudio`（转写完成后自动压缩，
        上一轮新加的键）没写进任何卡片，于是「ECHO 通用」页上多了一张"未归类 1 项"。
        重构时最容易出的错就是这个：**挪了位置却没挪全**。
        这条按 `app.config.DEFAULTS` 逐键对账（与 `scripts/audit-settings.py` 同一份事实，
        但它是静态的、不写盘、不进交付报告）。
        """
        import sys
        sys.path.insert(0, ROOT)
        from app import config as cfg

        block = self.js[self.js.index("const SET_CARDS = {"):]
        block = block[:block.index("\nconst SET_PLACED_ELSEWHERE")]
        placed = set()
        for name in ("SET_PLACED_BY_NOTE", "SET_PLACED_ELSEWHERE"):
            m = re.search(r"const %s = new Set\(\[(.*?)\]\);" % name, self.js, re.S)
            self.assertIsNotNone(m, "找不到 %s" % name)
            placed |= set(re.findall(r'"(\w+)"', m.group(1)))
        placed |= set(re.findall(r'"([a-zA-Z]\w*)"', block))

        stray = []
        for key, meta in cfg.DEFAULTS.items():
            if meta.get("deprecated") or meta.get("hidden"):
                continue          # 不下发的键由各自的渲染路径承载（不在设置卡片里）
            if key not in placed:
                stray.append(key)
        self.assertEqual(sorted(stray), [],
                         "这些可见设置项没有落点，会在界面上掉进「未归类」卡：%s" % sorted(stray))


class CleanupCardWiringTests(unittest.TestCase):
    """清理：先预览、后确认；能打「保留」钉子；如实回报。"""

    @classmethod
    def setUpClass(cls):
        cls.js = _read("web", "app.js")
        cls.html = _read("web", "index.html")

    def test_preview_then_confirm(self):
        self.assertIn("[data-pin-toggle]", self.js, "要有「保留」钉子按钮")
        self.assertIn("/api/models/cleanup/preview", self.js)
        self.assertIn("/api/models/cleanup", self.js)
        self.assertIn("async function runModelCleanup()", self.js)
        run = self.js[self.js.index("async function runModelCleanup()"):]
        run = run[:run.index("\n}\n")]
        self.assertIn("confirmDialog(", run, "删之前必须让人确认（默认只给建议）")
        self.assertIn("data-cleanup-pick", run, "只删勾选的那些")
        self.assertIn("freedBytes", run, "要如实报释放了多少字节")
        self.assertIn("r.failed", run, "失败要显示真原因")

    def test_cleanup_card_hosts_exist(self):
        for host in ("cleanupCard", "cleanupBadge", "cleanupBody", "cleanupDaysHost"):
            with self.subTest(id=host):
                self.assertIn('id="%s"' % host, self.html, "index.html 缺少 #%s" % host)
        # 除卡片本身（纯容器锚点）外，其余 host 都必须被 JS 真的引用
        for host in ("cleanupBadge", "cleanupBody", "cleanupDaysHost"):
            with self.subTest(id=host):
                self.assertIn('$("#%s")' % host, self.js, "app.js 没引用 #%s" % host)
        self.assertIn("modelCleanupDays", self.js, "阈值可配（默认 90 天）")

    def test_local_capability_rows_show_usage(self):
        """「本地能力」每一项都要带 上次使用 / 使用次数 / 占用（用户要求）。"""
        self.assertIn("function modelUsageBits(", self.js)
        bits = self.js[self.js.index("function modelUsageBits("):]
        bits = bits[:bits.index("\n}\n")]
        for token in ("local_mb", "lastUsedAt", "useCount", "inUse", "pinned"):
            with self.subTest(token=token):
                self.assertIn(token, bits)
        self.assertIn("modelUsageBits(modelById(", self.js, "组件行要把它渲染出来")


if __name__ == "__main__":
    unittest.main()
