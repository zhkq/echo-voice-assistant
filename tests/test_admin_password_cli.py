# -*- coding: utf-8 -*-
"""`--set-password` / `prepare-backend.sh --admin-password-stdin`：**自己指定管理员口令**。

## 这一组用例要回答的那个问题

装完后端之后，建管理员只有一条路：`--new-admin NAME` → `admin.new_password()` 给一串
**随机**口令、只打印一次。想用"自己记得住的"就得回去手搓一段 python
（`settings.load` → `auth.open_store` → `admin.hash_password` → `store.upsert_admin`）——
用户为这件事绕了两回。所以这里加了 `--set-password NAME`（**从 stdin 读**），
并且**刻意没有** `--password xxx`（那会进 shell 历史与 `ps` 的进程参数）。

## 用例盯的四件事

1. **能用**：写进库的那个口令，走 `admin.verify_password()`（= 管理面登录那条路）
   验得过 —— 也验得**旧口令不再有效**（"重置"得真重置）。
2. **不合规就什么也不做**：太短 / 太长 / 空 → 中文报错 + 退出码非 0 + **库里的行一字未变**
   （连审计都不留 —— "试了一个不合规的口令"不该在审计里留痕）。
3. **口令不出现在进程参数里**：真起一个子进程，用 `psutil` 读它的 `cmdline()` 断言
   （同机器上 `ps aux` 看到的就是这个）。
4. **与 `--new-admin` 共存**：随机那条行为不变（只打印一次、能登录），
   且它的输出里多了那句路标（`--set-password`）。

## 隔离（本仓库的红线）

所有用例都在 `tempfile.mkdtemp()` 里造**临时配置 + 临时库**（`auth.db` 写在 tmp 下），
`tmp.root` 也指到 tmp —— **不碰仓库里的 `data/**`、不碰 `data/meetings/**`**。
子进程用例同样把 cwd 设在仓库根但显式 `--config <tmp>/server.yaml`，
于是它写的库也只能是临时那一个。

## 脚本那条路（`prepare-backend.sh`）

本机是 Windows，脚本第 1 步就要求 `uname -s == Linux` 且有 `nvidia-smi` /
`nvidia-container-toolkit` / `docker` —— **真跑这条分支需要一台 Linux GPU 宿主**，
所以这组用例对脚本只做**静态**核对（开关在不在、口令是不是走 stdin、
`docker exec` 的参数里没有 `--password`、LF/UTF-8/无 BOM）。
"真机上没验过"这件事在报告与文档里都如实写明，不在这里假装跑过。
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import admin as admin_mod                                # noqa: E402
from server import main as server_main                               # noqa: E402
from server import settings as settings_mod                          # noqa: E402
from server import store as store_mod                                # noqa: E402

#: 本文件与 `scripts/prepare-backend.sh` 都在仓库里，路径从本文件推（不写死盘符）。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREPARE_SH = os.path.join(ROOT, "scripts", "prepare-backend.sh")
SERVER_MAIN = os.path.join(ROOT, "server", "main.py")

#: 子进程用**哪一个 python**：就是跑这组用例的这个（venv 里那个），
#: 免得"测试用的解释器"和"被测的解释器"不是同一个。
PYTHON = sys.executable


class _CliCase(unittest.TestCase):
    """所有用例的公共底座：**临时配置 + 临时库 + 临时 data root**。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-admin-pwd-")
        self.addCleanup(self._cleanup_tmp)
        self.cfg_path = os.path.join(self.tmp, "server.yaml")
        self.db_path = os.path.join(self.tmp, "auth.db")
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            # 这个配置**只指向临时目录**：库在 tmp 下、tmp.root 也在 tmp 下。
            # 不给 specs —— 这组用例一个模型都不会加载（管理动作只开库）。
            fh.write(
                "server: {id: admin-pwd-cli, listen: '127.0.0.1:8901'}\n"
                "auth:\n"
                "  enabled: true\n"
                "  mode: jwt\n"
                "  jwt_secret: '0123456789abcdef0123456789abcdef'\n"
                "  db: '%s'\n"
                "tmp: {root: '%s'}\n"
                % (self.db_path.replace("\\", "/"), self.tmp.replace("\\", "/")))

    def _cleanup_tmp(self):
        # Windows 上 sqlite 文件可能还被 GC 前的连接占着；关不掉就算了，tmp 归系统清。
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 直接调 `main()`（in-process）----

    def run_cli(self, *argv, stdin=""):
        """跑一次命令行入口：返回 `(退出码, stdout, stderr)`。

        `stdin` 模拟"从管道喂进去的那一行"。给的是**空串**就等于 `</dev/null`
        （`StringIO("")` 的 `readline()` 立刻返回空）—— 这正是"读不到口令"那条路的形状。
        """
        out, err = io.StringIO(), io.StringIO()
        real_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            import contextlib
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = server_main.main(["--config", self.cfg_path] + list(argv))
        finally:
            sys.stdin = real_stdin
        return rc, out.getvalue(), err.getvalue()

    def run_cli_expecting_usage_error(self, *argv, stdin=""):
        """跑一次**用法错误**的命令，返回 `(SystemExit 的退出码, stdout)`。

        ⚠️ **不要断言 argparse 那段 stderr**：`argparse.error()` → `exit()` 内部用的是
        `_sys.stderr`（它自己 import 的那一份），`contextlib.redirect_stderr` 拦不到
        （实测：断言会红，而消息其实打在了真实 stderr 上）。这里只钉退出码 ——
        也就是"它确实拒绝了"，这已经够用（拒绝的理由在 `--help` 与源码注释里）。
        """
        out = io.StringIO()
        real_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            import contextlib
            with contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit) as ctx:
                    server_main.main(["--config", self.cfg_path] + list(argv))
        finally:
            sys.stdin = real_stdin
        self.assertNotEqual(ctx.exception.code, 0, "用法错误竟然以 0 退出")
        return ctx.exception.code, out.getvalue()

    # ---- 真起子进程（"命令行参数里有没有口令"只能这样断言）----

    def run_subprocess_cli(self, *argv, stdin="", poll=60):
        """`python -m server.main --config <tmp> …`，口令从 **stdin** 喂。

        返回 `(完成后的 process, 采样到的 cmdline 列表, stdout, stderr)`。
        采样是在进程还活着的时候做的 —— `cmdline()` 不变，抓到一次就够，
        所以没必要把 scrypt 的耗时也算进断言里。
        """
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env.pop("ECHO_STATE_ROOT", None)
        proc = subprocess.Popen(
            [PYTHON, "-m", "server.main", "--config", self.cfg_path] + list(argv),
            cwd=ROOT, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            proc.stdin.write(stdin.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):        # pragma: no cover - 进程起崩了
            pass
        cmdlines = []
        deadline = time.time() + 40.0
        import psutil
        while proc.poll() is None and time.time() < deadline:
            try:
                cmdlines.append(list(psutil.Process(proc.pid).cmdline()))
            except Exception:                     # 进程可能刚 exit / 权限不足
                pass
            time.sleep(0.02)
        out = proc.stdout.read().decode("utf-8", "replace")
        err = proc.stderr.read().decode("utf-8", "replace")
        proc.stdout.close()
        proc.stderr.close()
        return proc, cmdlines, out, err

    # ---- 断言用的两个小工具（都读**临时**那个库）----

    def admin_row(self, username):
        """读管理员那一行（**测试自己开一个 Store**，不是被测进程那个连接）。"""
        store = store_mod.Store(self.db_path)
        try:
            return store.admin(username)
        finally:
            store.close()

    def db_has_admin(self, username):
        """库里有没有这个管理员（不建库：库文件还不存在就是"没有"）。"""
        if not os.path.exists(self.db_path):
            return False
        return self.admin_row(username) is not None

    def login_like_the_console(self, username, password):
        """模拟管理面登录时**那一句判断**（`admin.py` 的 `/admin/api/login`）。

        刻意复用同一个 `admin_mod.verify_password`：如果哪天哈希算法换了、
        而这里另写一套，这条用例就会变成"测了个假的"。
        """
        row = self.admin_row(username)
        if row is None or int(row.get("disabled") or 0):
            return False
        return admin_mod.verify_password(password, row["password_hash"] or "")

class SetPasswordTests(_CliCase):
    """`--set-password NAME`：正常 / 不合规 / 与 `--new-admin` 共存。"""

    def test_it_writes_the_password_and_that_password_logs_in(self):
        """核心那条：**自己指定的口令写进库，而且能登录**。

        "能登录"是照管理面登录那条判断走 `admin_mod.verify_password()`，
        不是新起一个 HTTP 服务（那会把这条用例变成"起服务"的测试）。
        """
        pw = "wo-de-kou-ling-2026"
        rc, out, err = self.run_cli("--set-password", "ops", stdin=pw + "\n")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("已建", out)
        self.assertTrue(self.login_like_the_console("ops", pw),
                        "自己指定的口令登录不过 —— 那这条路等于没通")
        self.assertNotIn(pw, out, "口令被回显到 stdout 了（它只该从 stdin 进来）")
        self.assertNotIn(pw, err, "口令被回显到 stderr 了")

    def test_a_reset_stops_the_old_password_from_working(self):
        """"重置 = 真重置"：`--new-admin` 建一个 → `--set-password` 换掉 → **旧的进不去**。"""
        rc, out, _ = self.run_cli("--new-admin", "ops")
        self.assertEqual(rc, 0, out)
        old = [ln.strip() for ln in out.splitlines()
               if ln.strip() and "已建" not in ln and "管理面板" not in ln
               and "set-password" not in ln][0]
        self.assertTrue(self.login_like_the_console("ops", old), "随机口令本来就登录不过？")

        new = "huan-cheng-zhe-ge-2026"
        rc, out, err = self.run_cli("--set-password", "ops", stdin=new + "\n")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(self.login_like_the_console("ops", new))
        self.assertFalse(self.login_like_the_console("ops", old),
                         "换完口令之后旧口令还能登录 —— 那不是重置")

    def test_it_can_also_create_the_account(self):
        """`--set-password` 的语义 = **建或重置**（与 `--new-admin` 同一格）。

        为什么不让它"只许改已有的"：那要多一个 `--create` 开关，而"第一次就把口令设成
        自己记得住的"正是这条路存在的原因 —— 部署脚本用的就是它（那时账号还不存在）。
        所以这里钉住"没有账号时会建出来"，免得以后有人把它改成"只许改"。
        """
        rc, out, err = self.run_cli("--set-password", "opsi", stdin="something-2026\n")
        self.assertEqual(rc, 0, out + err)
        self.assertIsNotNone(self.admin_row("opsi"), "第一次用它建号没建出来")
        self.assertTrue(self.login_like_the_console("opsi", "something-2026"))
        self.assertIsNone(self.admin_row("ops"), "建 opsi 的时候把 ops 也建了？")

    # ---- 不合规：中文报错 + 非零退出 + **库未变** ----

    def _seed_and_expect_refusal(self, bad_password, label):
        """先放一个正常账号，再拿畸形口令去改 —— **库里必须一字未变**。"""
        good = "yuan-lai-de-kou-ling-2026"
        rc, out, err = self.run_cli("--set-password", "ops", stdin=good + "\n")
        self.assertEqual(rc, 0, out + err)
        before = self.admin_row("ops")

        rc, out, err = self.run_cli("--set-password", "ops", stdin=bad_password)
        text = out + err
        self.assertNotEqual(rc, 0, "%s 竟然成功了：\n%s" % (label, text))
        self.assertIn("口令不合规", text, "%s 的报错不是那句中文提示：\n%s" % (label, text))
        self.assertNotIn("Traceback", text, "%s 报的是 traceback，不是中文原因" % label)
        after = self.admin_row("ops")
        self.assertEqual(after, before, "%s 被拒了，但库里的行变了" % label)
        self.assertTrue(self.login_like_the_console("ops", good),
                        "%s 被拒之后，原来的口令不能用了" % label)

    def test_a_too_short_password_is_refused_and_nothing_is_written(self):
        self._seed_and_expect_refusal("x\n", "太短的口令")

    def test_a_too_long_password_is_refused_and_nothing_is_written(self):
        long_one = "y" * (admin_mod.MAX_PASSWORD_LEN + 1)
        self._seed_and_expect_refusal(long_one + "\n", "太长的口令")

    def test_an_empty_password_is_refused_and_nothing_is_written(self):
        """空口令 = **库里那一行**给的那句"新口令不能为空"（不是另一句话）。

        它与"没有 stdin"是**两件事**：`printf '' | …` 是"给了个空口令"，
        `… </dev/null` 是"根本没读到"。两句报错分开，人才知道该改哪一边。
        """
        self._seed_and_expect_refusal("\n", "空口令")

    def test_no_stdin_at_all_is_refused_without_hanging(self):
        """`</dev/null`：**读不到** → 非零退出、**不卡住**（这条用例能跑完就是证明）。

        报的是口令策略那句话（`新口令不能为空`）—— 空串在"读回来的字节"上与
        `printf '' | …` 无法区分（`readline()` 两种情况返回的都是空串），
        所以这里**不编造**一个"没有 stdin"的独立说法，只用一句真话：
        口令没成、库没动，再看一眼管道。要紧的是**不挂住**、**不落库**。
        """
        rc, out, err = self.run_cli("--set-password", "ops", stdin="")
        text = out + err
        self.assertNotEqual(rc, 0, text)
        self.assertIn("口令不合规", text)
        self.assertIn("不能为空", text)
        # 读不到口令时给的那两条用法提示（都在 stderr；stdout 不掺提示）
        self.assertIn("stdin", err)
        self.assertIsNone(self.admin_row("ops"), "没有 stdin 却把库动了")

    def test_the_policy_error_comes_from_the_shared_implementation(self):
        """策略是**复用** `admin.password_policy_error()`，不是另写一套。

        判据不是"看起来像"：拿边界值对一遍 —— `MIN_PASSWORD_LEN` 那一位**刚好通过**，
        少一位**刚好被拒**。两套策略各自演化时，这种边界最先漂。
        """
        self.assertGreater(admin_mod.MIN_PASSWORD_LEN, 1)
        shortest = "a" * admin_mod.MIN_PASSWORD_LEN
        rc, out, err = self.run_cli("--set-password", "ops", stdin=shortest + "\n")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(self.login_like_the_console("ops", shortest),
                        "刚好 %d 位（= MIN_PASSWORD_LEN）该通过" % admin_mod.MIN_PASSWORD_LEN)

        too_short = "a" * (admin_mod.MIN_PASSWORD_LEN - 1)
        rc, out, err = self.run_cli("--set-password", "ops", stdin=too_short + "\n")
        self.assertNotEqual(rc, 0, out + err)
        self.assertIn(str(admin_mod.MIN_PASSWORD_LEN), out + err,
                      "报错里连下限数字都没有 —— 用的是哪份策略？")
        self.assertTrue(self.login_like_the_console("ops", shortest),
                        "被拒的那次把口令改掉了")

    def test_password_is_not_in_the_process_arguments(self):
        """**口令不出现在进程参数里** —— 这一条只能真起一个子进程来验。

        做法：`Popen([python, "-m", "server.main", "--set-password", "ops"])`，
        口令只从 stdin 喂，在它算 scrypt 的时候用 `psutil` 读 `cmdline()`
        （同机器上 `ps aux` 看到的就是这个）。

        ⚠️ **不要**把它改成"把口令写进 argv 再看"那种反向实验：真那样做的时候，
        这个进程列表里会出现明文口令，而这台机器上可能正跑着别的东西。
        """
        pw = "argv-zhong-bu-ying-gai-you-2026"
        proc, cmdlines, out, err = self.run_subprocess_cli(
            "--set-password", "ops", stdin=pw + "\n")
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertTrue(cmdlines, "没采到子进程的命令行 —— 这条断言就没意义了")
        for line in cmdlines:
            joined = " ".join(line)
            self.assertNotIn(pw, joined, "口令出现在进程参数里：%s" % joined)
            self.assertNotIn("--password", line, "命令行里出现了 --password 这种写法")
        self.assertTrue(self.login_like_the_console("ops", pw),
                        "子进程那条路没把口令写进库")

    def test_the_command_line_flag_refuses_to_combine_with_new_admin(self):
        """`--new-admin` 与 `--set-password` **只能给一个**（"随机"与"我给的"是同一格）。

        为什么不静默二选一：那会把"我打错了"变成"口令不是你给的那个" ——
        最难查的那种（当事人会觉得"我明明给了口令"）。
        """
        code, out = self.run_cli_expecting_usage_error(
            "--new-admin", "ops", "--set-password", "ops", stdin="something-2026\n")
        self.assertGreaterEqual(code, 2, "用法错误该是非 0（argparse 用 2）")
        self.assertFalse(self.db_has_admin("ops"), "参数冲突被拒了，但库被动了")

    def test_it_is_registered_as_an_admin_action(self):
        """开关忘了接进 `admin_actions` 的话，这条命令会**转头去起服务**（很糟）。"""
        src = open(SERVER_MAIN, encoding="utf-8").read()
        self.assertIn("args.set_password,\n", src,
                      "--set-password 没被算成管理动作：不给 --config 之外的东西时会去起服务")

    def test_there_is_deliberately_no_password_value_flag(self):
        """**刻意没有** `--password xxx` / `--password-stdin` 这种写法，而且不是"忘了加"。

        两个原因：① 口令当参数会进 shell 历史与 `ps`；
        ② 这里选的是 `--set-password NAME` 这个**独立动作**（名字就能说明它是干什么的），
        所以不需要 `--new-admin NAME --password-stdin` 那条等价写法 —— 少一条写法，
        就少一处"两条路各自演化"的地方。所以 `--password-stdin` **必须**是"不认识的参数"。
        """
        code, out = self.run_cli_expecting_usage_error(
            "--new-admin", "ops", "--password-stdin", stdin="something-2026\n")
        self.assertGreaterEqual(code, 2, "不认识的参数该是非 0")
        self.assertFalse(self.db_has_admin("ops"), "参数不认识，但库被动了")

    def test_the_source_has_no_password_valued_option(self):
        """在**源码**上再钉一次：`server/main.py` 里不许出现 `--password` 这种开关。

        为什么要源码级的这一条：行为级的用例挡不住"以后有人顺手加一个
        `--password`"（命令行的写法太多，用例只能覆盖想到的那些）。
        它会误报的情形我也想过：`.sh` 脚本里那句提示文案不含 `--password`；
        这个文件里的注释提到过 `--password xxx`（是**否定式**的说明）——
        所以判据限定在**带值的那种开关**（`"--password"` 紧跟逗号、等号或空格）。
        """
        src = open(SERVER_MAIN, encoding="utf-8").read()
        for bad in ('"--password"', "'--password'", '"--password="', "'--password='"):
            with self.subTest(bad=bad):
                self.assertNotIn(bad, src,
                                 "server/main.py 里出现了把口令当参数传的开关：%s" % bad)


class NewAdminStillWorksTests(_CliCase):
    """`--new-admin` 的行为**不变**：随机 + 只打印一次 + 输出里多一句路标。"""

    def test_it_still_prints_a_random_password_once_and_it_works(self):
        rc, out, err = self.run_cli("--new-admin", "ops")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("只出现这一次", out)
        candidates = [ln.strip() for ln in out.splitlines()
                      if ln.strip() and "已建" not in ln and "管理面板" not in ln
                      and "set-password" not in ln]
        self.assertEqual(len(candidates), 1,
                         "随机口令那一段的行数变了（应当恰好一行是口令）：\n" + out)
        pwd = candidates[0]
        self.assertTrue(self.login_like_the_console("ops", pwd))
        row = self.admin_row("ops")
        self.assertNotIn(pwd, row["password_hash"], "口令被明文存进去了")
        self.assertTrue(row["password_hash"].startswith("scrypt$"))

    def test_it_points_at_set_password(self):
        """随机那条路的输出里要有那句路标（否则人还是只会看到一串随机串）。"""
        rc, out, err = self.run_cli("--new-admin", "ops")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("--set-password", out)
        self.assertIn("stdin", out)


class ScriptTests(unittest.TestCase):
    """`scripts/prepare-backend.sh` 的**静态**核对（真跑要 Linux GPU 宿主，见模块头）。"""

    @classmethod
    def setUpClass(cls):
        with open(PREPARE_SH, "rb") as fh:
            cls.raw = fh.read()
        cls.src = cls.raw.decode("utf-8")

    def test_the_new_switch_exists_and_is_documented(self):
        self.assertIn("--admin-password-stdin", self.src,
                      "脚本里没有这个开关（文档与真实能力就对不上了）")
        self.assertIn("ADMIN_PASSWORD_STDIN=1", self.src, "开关没有真的接进参数解析")
        # `--help` 的正文是 usage() 里 heredoc 出去的那段，也要提到它
        self.assertIn("--admin-password-stdin\n", self.src)

    def test_the_password_is_only_ever_read_from_stdin(self):
        """口令只从 stdin 来：**读一遍、喂给容器**，没有 `--password 口令` 那种写法。

        这几条一起构成"口令不进 shell 历史 / 不进进程参数"的静态证据。
        ⚠️ 判据写的是 `ADMIN_PASSWORD=1`（**变量被赋成的东西**）而不是
        `ADMIN_PASSWORD=` —— 后者会把 `ADMIN_PASSWORD_STDIN=1` 这个**开关**也一起误报
        （这里第一版就写错过，被 `bash -n` 之外的一次试跑抓到）。
        """
        self.assertIn("IFS= read -r ADMIN_PASSWORD", self.src,
                      "没有从 stdin 读那一行 —— 那口令是从哪来的？")
        self.assertNotIn("ADMIN_PASSWORD=1", self.src, "口令被赋成了字面量")
        self.assertNotIn("--password", self.src,
                         "脚本里出现了 `--password` 这种把口令当参数传的形状")
        self.assertNotIn('--set-password "$ADMIN_PASSWORD"', self.src,
                         "口令被当成 docker exec 的参数传进去了（ps 里谁都看得见）")
        # 正解的形状：`printf '%s\n' "$ADMIN_PASSWORD" | docker exec -i …`
        # （源码里就是**单引号**包着 `%s\n`；写成双引号的话 `\n` 会被 printf 当转义处理，
        #   反而变成"没有换行"—— 静态断言要按源码的实际形状写，不能按脑子里的形状写。）
        self.assertIn("printf '%s\\n' \"$ADMIN_PASSWORD\"", self.src,
                      "没找到把口令从 stdin 喂给容器的那一行")

    def test_the_new_branch_runs_inside_the_容器_step(self):
        """新分支要在"建管理员"那一步里，且**只在没有账号时**起作用。"""
        import re
        m = re.search(r"# 8\. 建管理员\n# =+\n(.*?)\n# =+\n# 9\. 验收",
                      self.src, re.S)
        self.assertIsNotNone(m, "找不到第 8 步（建管理员）那一段 —— 结构变了吗？")
        step8 = m.group(1)
        self.assertIn("--admin-password-stdin", step8 + self.src)   # 开关在 usage 里
        self.assertIn('--set-password "$ADMIN_NAME"', step8,
                      "第 8 步没有走 --set-password")
        self.assertIn("还没有管理员账号", step8)
        self.assertIn("--new-admin", step8, "随机那条路被删了？")
        self.assertIn("-i", step8, "docker exec 没有 -i，stdin 就传不进容器")

    def test_encoding_lf_utf8_and_no_bom_are_preserved(self):
        """这个脚本在 Linux 上跑：**LF + UTF-8 无 BOM** 是它的运行前提。

        （`edit`/`write` 工具改动时最容易丢的就是这个；`Get-Content | Set-Content`
        往返会连中文一起毁掉，所以本仓库只用工具或显式 utf-8 的 python 改它。）
        """
        self.assertNotIn(b"\r\n", self.raw, "出现了 CRLF —— 在 Linux 上会以 \\r 的形状报错")
        self.assertEqual(self.raw.count(b"\r"), 0, "有孤立 CR")
        self.assertFalse(self.raw.startswith(b"\xef\xbb\xbf"), "多了 UTF-8 BOM")
        self.assertTrue(self.raw.startswith(b"#!/usr/bin/env bash"))
        self.assertIn("管理员账号", self.src, "中文读不出来了？")


if __name__ == "__main__":
    unittest.main()
