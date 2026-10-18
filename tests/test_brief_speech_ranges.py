# -*- coding: utf-8 -*-
"""语音简报里的**温度区间**：`8~19℃` 不许再被念成「819 度」（2026-10-18 真机）。

事故实证（**不是在猜**）：`D:\\ECHO\\data\\echo.db` 的 `commands` 表 id=181
（`2026-10-18 20:59:35`，mobile）：

  * `reply`（给人看的原文，好的）：`北京明天（10月19日）晴转多云，8~19℃，微风无雨…`
  * `brief`（= 手机念的 `speakText`，坏的）：`…晴转多云，819度，微风无雨…`
    —— codepoints 实测是 `U+0038 U+0031 U+0039` + `度`，**没有**空格/分隔符。

根因在 `app/assistant.py::_clean_line()`：`re.sub(r"[_~^]", "", t)` 把 `8~19`
里的 `~` 当成 **markdown 强调符**删掉了，剩 `819`；紧接着的 `℃→度` 把它读成
「八百一十九度」。用真源码里的 `_clean_line`/`conclusion_only`/`brief_text`
喂真 `reply` 重算，与库里的 `brief` **逐字节相等**（id 178/181/182 三条全中）。

本文件钉住的判据（修完必须全绿，且原有行为一条不坏）：

  1. **真实那句**：`brief_text(真 reply)` 含 `8 到 19`，不再含 `819`；
  2. `8~19`（无单位）/`8~19度`/`8~19℃`/`8~19%` 都救；
  3. **不成对/非区间的 `~` 行为不变**：`a~b` → `ab`（旧行为照旧），
     `~~删除线~~` → `删除线`（成对的 markdown 语义仍剥）；
  4. `10-18`、`2026-10-18`、`v2.1.7`、`最低-5度` **逐字节不变**
     （`-`/`—`/`～` 是**下游** `speech_text` 的事，这里刻意不动）；
  5. `/api/commands` 行上的 `speakText` 与 `brief` **同一串**（都出自
     `speak_text_for_reply()`）；`reply` 原样不动；
  6. 幂等：同一段文本洗两遍与洗一遍相同。

为什么"先救区间、再洗 markdown"：`~` 在**数字之间**是"到"，在 markdown 里是
强调/删除线；一旦先被 `[_~^]` 删掉，就再也分不出两者了。所以顺序本身就是判据。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                                   # noqa: E402
from fastapi.testclient import TestClient                                     # noqa: E402

from app import assistant                                                     # noqa: E402
from app import db                                                            # noqa: E402
from app.api import router                                                    # noqa: E402
from app.config import settings                                               # noqa: E402

#: **真原样**：`D:\ECHO\data\echo.db` → `commands.id = 181` 的 `reply`。
#: 一个字都没改（含表格、链接、详情段）—— 判据要的就是"这条真回复现在念什么"。
REAL_REPLY = (
    "北京明天（10月19日）晴转多云，8~19℃，微风无雨，白天回暖明显，"
    "早晚偏凉、昼夜温差11℃。\n\n详情如下：\n\n"
    "**一、明天（2026年10月19日 周一）北京天气**\n\n"
    "| 项目 | 情况 |\n|---|---|\n"
    "| 天气 | 白天晴，夜间转多云 |\n"
    "| 气温 | 最高19℃，最低8℃（清晨5–6℃、上午9℃、午后19.2℃、夜间15℃→13℃） |\n"
    "| 风力 | 白天南风、夜间转北风，均微风（<3级） |\n"
    "| 降水 | 无，全天气干燥 |\n"
    "| 湿度与气压 | 相对湿度约60%–78%，气压1013–1017hPa |\n"
    "| 生活指数 | 感冒少发、运动适宜、洗车适宜、紫外线强、穿衣“较舒适” |\n\n"
    "数据来自中央气象台10月18日20:00发布的北京城市预报和"
    "《[中国天气网北京7天预报](https://www.weather.com.cn/weather/101010100.shtml)》，两处一致。\n\n"
    "**二、出行与穿衣建议**\n\n"
    "- 白天薄外套加内搭即可，但清晨（8℃以下）和夜间偏凉，早出晚归建议带一件厚一点的外套。\n"
    "- 紫外线指数为“强”，午后外出可涂防晒；空气干燥，注意补水。\n"
    "**三、需要留意：后天（10月20日 周二）大幅降温**\n\n"
    "10月20日北京阴转晴，最高仅13℃、最低2℃，比明天骤降6–10℃；"
    "再往后10月21日晴14℃/3℃、22日晴16℃/4℃。\n\n"
    "**四、参考来源**\n\n"
    "- [中国天气网 · 北京7天天气预报](https://www.weather.com.cn/weather/101010100.shtml)\n"
    "- [中央气象台 · 北京天气预报（10-18 20:00发布）](https://www.nmc.cn/publish/forecast/ABJ/beijing.html)"
)

#: 修好之后这条真回复该念的那段话（= 库里那份 `brief` 只差 `819` → `8 到 19`）。
EXPECTED_BRIEF = ("北京明天（10月19日）晴转多云，8 到 19度，微风无雨，"
                  "白天回暖明显，早晚偏凉、昼夜温差11度。")


class RealRowTests(unittest.TestCase):
    """真回复（id=181）：区间要救回来，`reply` 一个字不许动。"""

    def test_the_real_line_says_eight_to_nineteen(self):
        """真那句 `…8~19℃，…` → 念 `8 到 19度`，不再是 `819度`。

        这是本次事故的**唯一**判据：同一条 `reply`，修前 `brief` 是 `819度`，
        修后必须含 `8 到 19`；`819` 这串连成一个数的形状不许再出现。
        """
        brief = assistant.brief_text(REAL_REPLY)
        self.assertIn("8 到 19", brief)
        self.assertNotIn("819", brief, "修前就是它（`8~19` 被当成强调符删成 `819`）")
        self.assertEqual(EXPECTED_BRIEF, brief)

    def test_the_same_text_goes_through_speak_text_for_reply(self):
        """`speakText` 那条链（`speak_text_for_reply`）拿到**同一个字符串**。

        电脑喇叭与手机的 `speakText` 是单一出处（既有约定）；这里只确认新规则
        真的在这条链上生效，而不是只改了 `_clean_line` 的某个旁路。
        """
        self.assertEqual(EXPECTED_BRIEF, assistant.speak_text_for_reply(REAL_REPLY))
        self.assertEqual(assistant.brief_text(REAL_REPLY),
                         assistant.speak_text_for_reply(REAL_REPLY))

    def test_the_reply_itself_is_untouched(self):
        """`reply`（给人看的原文）不进任何清洗 —— 原文永远带 `8~19℃`。

        `brief_text()` 是纯函数、返回新串；这条用"洗完之后原文仍然逐字是原文"
        把"只动 brief/speakText、不动 reply"钉住（`_command_row` 也原样带 `reply`）。
        """
        before = REAL_REPLY
        assistant.brief_text(REAL_REPLY)
        assistant.speak_text_for_reply(REAL_REPLY)
        self.assertEqual(before, REAL_REPLY)
        self.assertIn("8~19℃", REAL_REPLY)


class RangeRescueTests(unittest.TestCase):
    """数字 ~ 数字（可带单位）→ `数字 到 数字(单位)`。"""

    #: (原文, 期望 brief)。单位四种、小数、首尾带单位、空格写法都覆盖。
    CASES = (
        ("气温 8~19℃", "气温 8 到 19度"),
        ("气温8~19度", "气温8 到 19度"),
        ("气温8~19%", "气温8 到 19%"),
        ("8~19", "8 到 19"),                       # 无单位也救（上游没有日期歧义：日期没有 `~`）
        ("午后20~22℃", "午后20 到 22度"),
        # 小数：**前面故意带一个字** —— 行首 `8.5` 会被既有的"去行首编号"规则
        # （`^\s*\d+[\.、]`）吃掉，那是本次没动的旧行为，不该混进这条判据。
        ("气温8.5~9.5℃", "气温8.5 到 9.5度"),
        ("8℃~19℃", "8 到 19度"),                   # 单位写在前一个数上（常见天气写法）
        ("30%~60%", "30 到 60%"),
        ("气温 8 ~ 19℃", "气温 8 到 19度"),          # `~` 两侧有空格
        ("8~19℃，", "8 到 19度，"),                  # 标点原样跟着
    )

    def test_ranges_become_dao(self):
        for src, want in self.CASES:
            with self.subTest(src=src):
                self.assertEqual(want, assistant.brief_text(src))

    def test_the_range_is_rescued_before_markdown_cleaning(self):
        """顺序判据：**先**救区间、**再**洗 markdown。

        `**8~19℃**` 是加粗包着的区间；顺序错了（先洗 markdown 也还好，但若先
        删 `~` 就完了）。这里同时钉住"加粗照样剥、区间照样救"。
        """
        self.assertEqual("8 到 19度", assistant.brief_text("**8~19℃**"))

    def test_idempotent(self):
        """幂等：洗两遍 = 洗一遍（简报可能被再次送进同一条链）。"""
        for src, _ in self.CASES:
            once = assistant.brief_text(src)
            with self.subTest(src=src):
                self.assertEqual(once, assistant.brief_text(once))
        self.assertEqual(EXPECTED_BRIEF,
                         assistant.brief_text(assistant.brief_text(REAL_REPLY)))
        self.assertEqual(EXPECTED_BRIEF,
                         assistant.speak_text_for_reply(EXPECTED_BRIEF))


class MarkdownTildeTests(unittest.TestCase):
    """`~` 的 markdown 语义**只剥成对的**；不成对的单个 `~` 照旧删（既有行为）。"""

    def test_paired_strikethrough_is_still_stripped(self):
        self.assertEqual("删除线", assistant.brief_text("~~删除线~~"))
        self.assertEqual("这是删掉的", assistant.brief_text("这是~~删掉~~的"))
        self.assertEqual("819", assistant.brief_text("~~819~~"))   # 成对 → 只剥标记

    def test_lone_tilde_between_non_digits_is_removed_like_before(self):
        self.assertEqual("ab", assistant.brief_text("a~b"),
                         "非数字区间的单个 `~` 行为不变（旧实现也是 `ab`）")
        self.assertEqual("强调", assistant.brief_text("~强调~"))
        self.assertEqual("单个波浪号", assistant.brief_text("单个~波浪号"))
        self.assertEqual("ab", assistant.brief_text("a~~b"))       # 不成对：照旧删


class UnchangedTokensTests(unittest.TestCase):
    """`~` 规则**不许**顺手碰到别的形状（日期/版本/负数逐字节不变）。"""

    def test_dates_versions_and_negatives_are_byte_identical(self):
        for token in ("10-18", "2026-10-18", "v2.1.7", "最低-5度"):
            with self.subTest(token=token):
                self.assertEqual(token, assistant.brief_text(token))

    def test_a_leading_minus_is_still_the_old_list_marker_rule(self):
        """行首的 `-` **本来就是**列表符（`_clean_line` 既有规则，本次没动）。

        用户要求 `-5度` 逐字节不变，指的是句子里的负号（`最低-5度`，见上一条）；
        行首这个 `-` 被当成 markdown 列表符是**改之前就有**的行为，这里只把它钉住
        —— 本次改动既没让它变好、也没让它变坏（别有人以为这是本次引入的）。
        """
        self.assertEqual("5度", assistant.brief_text("-5度"))
        self.assertEqual("气温 -5度", assistant.brief_text("气温 -5度"))


class ApiChainTests(unittest.TestCase):
    """`/api/commands` 行：`speakText` 与 `brief` 同一串，且 `reply` 原样。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-range-db-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.tmp
        db.DB_FILE = os.path.join(self.tmp, "test.db")
        db.init()
        # 进程级设置缓存不受 db 补丁约束：不清会读到真库（比如真的开着鉴权）。
        settings._drop_cache()

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

        self.addCleanup(self._clean)

    def _clean(self):
        db.DATA_DIR, db.DB_FILE = self._old_db
        settings._drop_cache()
        try:
            settings.get("apiAuthEnabled")
        except Exception:                                          # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _api_row(self, cmd_id):
        r = self.client.get("/api/commands")
        self.assertEqual(200, r.status_code, r.text)
        for it in r.json()["items"]:
            if it.get("id") == cmd_id:
                return it
        self.fail("`GET /api/commands` 里找不到指令 %s" % (cmd_id,))

    def test_speak_text_matches_brief_and_reply_stays_verbatim(self):
        """新落的行：`brief`（当时念的）= `speakText`，`reply` 仍是原文。"""
        brief = assistant.speak_text_for_reply(REAL_REPLY)
        cid = db.add_command("北京明天天气", source="mobile", status="done")
        db.update_command(cid, reply=REAL_REPLY, brief=brief)

        row = self._api_row(cid)
        self.assertEqual(brief, row["speakText"])
        self.assertEqual(row["brief"], row["speakText"], "两把尺子必须是同一把")
        self.assertIn("8 到 19", row["speakText"])
        self.assertNotIn("819", row["speakText"])
        self.assertEqual(REAL_REPLY, row["reply"], "`reply` 原样（含 `8~19℃`）")
        self.assertIn("8~19℃", row["reply"])

    def test_an_old_row_without_brief_is_computed_with_the_same_rule(self):
        """老记录没有 `brief` → 接口现算，规则仍是同一份（也救区间）。"""
        cid = db.add_command("北京明天天气", source="mobile", status="done")
        db.update_command(cid, reply=REAL_REPLY)                    # 故意不写 brief

        row = self._api_row(cid)
        self.assertEqual("", row["brief"], "前提：这条老记录没有 brief")
        self.assertEqual(EXPECTED_BRIEF, row["speakText"])
        self.assertIn("8 到 19", row["speakText"])


if __name__ == "__main__":
    unittest.main()
