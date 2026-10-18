# -*- coding: utf-8 -*-
"""`gh-push.ps1` 的代理回退 + `build_min_kit.py` 的共享打包锁（2026-10-18）。

为什么要这一组用例
------------------
1. **裸域名被墙时才走代理**：这台内网把 `github.com` 解析成黑洞，正常 `git push` 连 DNS
   都过不去。`gh-push.ps1` 先试 `http.curloptResolve` 直连；只有直连真的失败（DNS 解析
   失败 / `CONNECT tunnel failed` / 502 / 443 超时）才现探活 IP、起本地 CONNECT 代理。
   顺序错了（先起代理）或端口写死 8899（旧代理还占着、还缓存着死 IP），都会让"重试"
   变成同一个 502 —— 这正是 2026-10-18 实测两次失败的那条路。
2. **代理必须关了**：不管推送成功还是失败，`Start-GhProxy` 都要在 `finally` 里配一个
   `Stop-GhProxy`。孤儿代理钉着死 IP，下一次即使换了新端口也会被它带崩。
3. **`build_min_kit.py` 的"组 kit"也要排队**：`build_kit.py` 早就有 `dist\\.pack.lock`，
   但 `build_min_kit.py` 直接调 `build_main_packages()` 绕过了它 → 同一分钟两个组包会
   互相 `rmtree` 对方正在写的 kit 目录。修法是复用 `build_kit.pack_lock`，不是复制一份。

测试纪律（硬约束）
------------------
**绝不真的 push**。`git` / `curl` / `node` 全部换成本文件生成的桩：
* `ECHO_GH_PUSH_GIT` / `ECHO_GH_PUSH_CURL` / `ECHO_GH_PUSH_NODE` / `ECHO_GH_PUSH_PROXY_JS`
  是脚本里的命令间接层，默认仍是真命令；
* 桩只往 `ECHO_TEST_TRACE_DIR` 写 JSON 行，并返回预先安排的退出码/HTTP 码；
* "node" 桩就是一个 Python 写的小 TCP 监听器（`PORT`/`GH_IP` 从环境读，和 `gh-proxy.js`
  的契约一致），这样"代理进程真的起来/真的被关掉"是可断言的。

跑法（别跑整轮 discover）::

    venv\\Scripts\\python.exe -m unittest tests.test_gh_push_fallback
"""
import contextlib
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PS1 = SCRIPTS / "gh-push.ps1"
BOM = b"\xef\xbb\xbf"
LOCAL_SHA = "1" * 40


def _load_module(name, path):
    """把 scripts/*.py 当模块加载（它们不在包路径里）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build_kit = _load_module("build_kit_ghpush_under_test", SCRIPTS / "build_kit.py")
min_kit = _load_module("build_min_kit_ghpush_under_test", SCRIPTS / "build_min_kit.py")


def _powershell():
    for name in ("pwsh", "powershell.exe", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("找不到 PowerShell")


# --------------------------------------------------------------------------- 桩
# 每个桩都用 ASCII 写（cmd.exe 按码页读 .cmd；桩本身只处理 ASCII 参数与 JSON）。
_GIT_STUB = r'''# -*- coding: utf-8 -*-
import json, os, sys, time
args = sys.argv[1:]
trace = os.environ["ECHO_TEST_TRACE_DIR"]
def log(event, **kw):
    kw.update(event=event, t=time.time(), pid=os.getpid())
    with open(os.path.join(trace, "git.log"), "a", encoding="utf-8") as f:
        f.write(json.dumps(kw, ensure_ascii=True) + "\n")
log("git", args=args)
if "symbolic-ref" in args:
    print(os.environ.get("ECHO_TEST_BRANCH", "main"))
    sys.exit(0)
if "rev-parse" in args:
    print(os.environ.get("ECHO_TEST_LOCAL_SHA", "1" * 40))
    sys.exit(0)
if "ls-remote" in args:
    remote = os.environ.get("ECHO_TEST_REMOTE_SHA", os.environ.get("ECHO_TEST_LOCAL_SHA", "1" * 40))
    print(remote + "\trefs/heads/" + os.environ.get("ECHO_TEST_BRANCH", "main"))
    sys.exit(0)
if "status" in args:
    sys.exit(0)
if "push" in args:
    proxy = [a for a in args if a.startswith("http.proxy=")]
    kind = "proxy" if proxy else "direct"
    mode = os.environ.get("ECHO_TEST_PROXY_FAIL", "0")
    if kind == "direct":
        fail = os.environ.get("ECHO_TEST_DIRECT_FAIL", "0") == "1"
    else:
        count_path = os.path.join(trace, "proxy_push_count")
        n = 0
        if os.path.exists(count_path):
            n = int(open(count_path, encoding="utf-8").read().strip() or "0")
        fail = (mode == "1") or (mode == "first" and n == 0)
        with open(count_path, "w", encoding="utf-8") as f:
            f.write(str(n + 1))
    log("push", kind=kind, fail=fail, proxy=(proxy[0] if proxy else ""), args=args)
    if fail:
        sys.stderr.write("fatal: injected %s push failure\n" % kind)
        sys.exit(1)
    sys.exit(0)
sys.exit(0)
'''

_CURL_STUB = r'''# -*- coding: utf-8 -*-
import json, os, sys, time
args = sys.argv[1:]
trace = os.environ["ECHO_TEST_TRACE_DIR"]
def log(event, **kw):
    kw.update(event=event, t=time.time(), pid=os.getpid())
    with open(os.path.join(trace, "curl.log"), "a", encoding="utf-8") as f:
        f.write(json.dumps(kw, ensure_ascii=True) + "\n")
log("curl", args=args)
ip = ""
for i, a in enumerate(args):
    if a == "--resolve" and i + 1 < len(args):
        ip = args[i + 1].rsplit(":", 1)[-1]
    elif a.startswith("--resolve="):
        ip = a.split("=", 1)[1].rsplit(":", 1)[-1]
codes = {}
for item in os.environ.get("ECHO_TEST_CURL_CODES", "").split(","):
    if "=" in item:
        key, value = item.split("=", 1)
        codes[key.strip()] = value.strip()
code = codes.get(ip, os.environ.get("ECHO_TEST_CURL_DEFAULT", "401"))
print("%s 0.010" % code)
sys.exit(0)
'''

_PROXY_STUB = r'''# -*- coding: utf-8 -*-
"""node stand-in: only the observable part of gh-proxy.js - listen on PORT/GH_IP."""
import json, os, socket, time
port = int(os.environ["PORT"])
ip = os.environ.get("GH_IP", "")
trace = os.environ["ECHO_TEST_TRACE_DIR"]
with open(os.path.join(trace, "proxy.log"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"event": "proxy", "port": port, "ip": ip,
                        "pid": os.getpid(), "t": time.time()}) + "\n")
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", port))
srv.listen(16)
srv.settimeout(0.2)
while True:
    try:
        client, _addr = srv.accept()
        client.close()
    except socket.timeout:
        pass
'''


def _write_cmd(path, python_exe, stub):
    """造一个 cmd.exe 包装器：PowerShell 用 `& <cmd>` 调它，参数原样转发。"""
    path.write_text(
        ("@echo off\r\n"
         '"{py}" "{stub}" %*\r\n'
         "exit /b %ERRORLEVEL%\r\n").format(py=python_exe, stub=stub),
        encoding="ascii",
    )
    return path


def _event_lines(path):
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _wait_dead(pid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not build_kit._pid_alive(pid):
            return True
        time.sleep(0.1)
    return not build_kit._pid_alive(pid)


def _port_closed(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return False
    except OSError:
        return True


# --------------------------------------------------------------- 1. gh-push.ps1
@unittest.skipUnless(os.name == "nt", "gh-push.ps1 只在 Windows 上跑")
class GhPushProxyFallback(unittest.TestCase):
    """直连优先 / 失败才代理 / 401 才算活 / 新端口 / 关代理 / 真实原因。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="echoghpush-")
        self.tmp = Path(self._tmp.name)
        self.trace = self.tmp / "trace"
        self.trace.mkdir()
        self.py = sys.executable

        self.git_stub = self.tmp / "stub_git.py"
        self.git_stub.write_text(_GIT_STUB, encoding="ascii")
        self.curl_stub = self.tmp / "stub_curl.py"
        self.curl_stub.write_text(_CURL_STUB, encoding="ascii")
        self.proxy_stub = self.tmp / "stub_proxy.py"
        self.proxy_stub.write_text(_PROXY_STUB, encoding="ascii")

        self.git_cmd = _write_cmd(self.tmp / "git.cmd", self.py, self.git_stub)
        self.curl_cmd = _write_cmd(self.tmp / "curl.cmd", self.py, self.curl_stub)

    def tearDown(self):
        # 兜底清理：任何一次失败都不许把桩代理留在后台（那正是被测脚本要避免的孤儿）。
        for ev in _event_lines(self.trace / "proxy.log"):
            pid = int(ev.get("pid") or 0)
            if pid and build_kit._pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        self._tmp.cleanup()

    # ---------------------------------------------------------------- helpers
    def env(self, **over):
        env = os.environ.copy()
        env.update({
            "ECHO_GH_PUSH_GIT": str(self.git_cmd),
            "ECHO_GH_PUSH_CURL": str(self.curl_cmd),
            "ECHO_GH_PUSH_NODE": self.py,
            "ECHO_GH_PUSH_PROXY_JS": str(self.proxy_stub),
            "ECHO_TEST_TRACE_DIR": str(self.trace),
            "ECHO_TEST_LOCAL_SHA": LOCAL_SHA,
            "ECHO_TEST_REMOTE_SHA": LOCAL_SHA,
            "ECHO_TEST_DIRECT_FAIL": "0",
            "ECHO_TEST_PROXY_FAIL": "0",
            "ECHO_TEST_CURL_DEFAULT": "401",
        })
        env.update({k: str(v) for k, v in over.items()})
        return env

    def run_push(self, *extra, **env_over):
        env = self.env(**env_over)
        argv = [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(PS1), "-SkipCheck", "-Retries", "1", "-TopN", "2"]
        argv += [str(x) for x in extra]
        proc = subprocess.run(argv, cwd=str(ROOT), env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=180)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def git_events(self):
        return _event_lines(self.trace / "git.log")

    def curl_events(self):
        return _event_lines(self.trace / "curl.log")

    def proxy_events(self):
        return _event_lines(self.trace / "proxy.log")

    def pushes(self):
        return [e for e in self.git_events() if e.get("event") == "push"]

    # ---------------------------------------------------------------- tests
    def test_direct_success_never_starts_a_proxy(self):
        """能直连就别折腾：一次 `git push`（带 curloptResolve）就该结束，代理一次都不起。"""
        code, out = self.run_push(ECHO_TEST_DIRECT_FAIL="0")
        self.assertEqual(code, 0, out[-3000:])
        pushes = self.pushes()
        self.assertEqual(len(pushes), 1, f"直连成功却推了不止一次：{pushes}")
        self.assertEqual(pushes[0]["kind"], "direct")
        self.assertFalse(pushes[0]["fail"])
        self.assertEqual(self.proxy_events(), [], "直连成功还起了代理（违反『能通就别折腾』）")
        self.assertIn("push OK", out)
        # 直连仍然是用 curloptResolve 钉 IP 的（这条路径没有被代理改动）
        self.assertTrue(any(a.startswith("http.curloptResolve=")
                            for a in pushes[0]["args"]), pushes[0]["args"])

    def test_probe_counts_only_401_and_uses_ssl_no_revoke(self):
        """探活判据：`--resolve github.com:443:<ip>` + receive-pack + 只有 401 算可用。"""
        code, out = self.run_push(
            ECHO_TEST_DIRECT_FAIL="0",
            ECHO_TEST_CURL_CODES="20.27.177.113=200,20.205.243.166=401",
            ECHO_TEST_CURL_DEFAULT="000",
        )
        self.assertEqual(code, 0, out[-3000:])
        curls = self.curl_events()
        self.assertTrue(curls, "一次探活都没有")
        for ev in curls:
            args = ev["args"]
            self.assertIn("--ssl-no-revoke", args, "本机 schannel 不带它会因吊销列表不可达返回 000")
            self.assertIn("--resolve", args)
            resolve = args[args.index("--resolve") + 1]
            self.assertRegex(resolve, r"^github\.com:443:\d+\.\d+\.\d+\.\d+$",
                             "--resolve 的分隔符必须是冒号（写成 = 会得到看似断网的 000）")
            self.assertTrue(any("info/refs?service=git-receive-pack" in a for a in args),
                            f"探活没打 receive-pack 端点：{args}")
        # 200 的那台不算活；只有 401 的那台进了直连的 resolve 串
        pushes = self.pushes()
        self.assertEqual(len(pushes), 1, pushes)
        resolve = [a for a in pushes[0]["args"] if a.startswith("http.curloptResolve=")][0]
        self.assertIn("20.205.243.166", resolve)
        self.assertNotIn("20.27.177.113", resolve,
                         "200 的 IP 被当成可用了 —— 它在 CONNECT 阶段仍会 502")

    def test_all_non_401_means_no_reachable_ip(self):
        """全网只回 200/000 时必须说"没有可用 IP"，而不是拿一个 502 的 IP 硬推。"""
        code, out = self.run_push(ECHO_TEST_DIRECT_FAIL="0",
                                  ECHO_TEST_CURL_DEFAULT="200")
        self.assertEqual(code, 1, out[-3000:])
        self.assertIn("no reachable IP", out)
        self.assertEqual(self.pushes(), [], "没有可用 IP 却还是发了 push")

    def test_direct_failure_reprobes_then_proxies_on_a_fresh_port_and_verifies(self):
        """直连失败 → **现探** 401 IP → 新端口起代理 → `-c http.proxy=` 推 → 核实 → 关代理。"""
        code, out = self.run_push(
            ECHO_TEST_DIRECT_FAIL="1",
            ECHO_TEST_PROXY_FAIL="first",       # 第一个代理失败，第二个成功（顺带验证换端口）
        )
        self.assertEqual(code, 0, out[-4000:])

        pushes = self.pushes()
        self.assertEqual([p["kind"] for p in pushes], ["direct", "proxy", "proxy"], pushes)
        direct_push = pushes[0]
        self.assertTrue(direct_push["fail"])
        self.assertTrue(pushes[1]["fail"])
        self.assertFalse(pushes[2]["fail"])

        # 直连失败后**重新探活**（第一轮探活在直连之前，这里要求直连之后还有一整轮）
        curls = self.curl_events()
        after = [e for e in curls if e["t"] > direct_push["t"]]
        self.assertGreaterEqual(len(after), 9,
                                f"直连失败后没有重新探活（只有 {len(after)} 次 curl）")
        for ev in after:
            self.assertIn("--ssl-no-revoke", ev["args"])

        proxies = self.proxy_events()
        self.assertEqual(len(proxies), 2, f"应该起两个代理（先失败一个、再成功一个）：{proxies}")
        ports = [int(p["port"]) for p in proxies]
        self.assertEqual(len(set(ports)), 2, f"两次代理用了同一个端口：{ports}")
        self.assertNotIn(8899, ports, "不许硬用 8899（旧代理可能占着且缓存旧 IP）")
        for ev in proxies:
            self.assertGreater(ev["t"], direct_push["t"], "代理在直连失败之前就起了")

        # 代理推送与核实都必须走 `-c http.proxy=http://127.0.0.1:<port>`
        proxy_pushes = [p for p in pushes if p["kind"] == "proxy"]
        for p in proxy_pushes:
            self.assertRegex(p["proxy"], r"^http\.proxy=http://127\.0\.0\.1:\d+$")
        ok_push = proxy_pushes[-1]
        ok_port = int(ok_push["proxy"].rsplit(":", 1)[-1])
        self.assertIn(ok_port, ports)
        ls_proxy = [e for e in self.git_events()
                    if e.get("event") == "git" and "ls-remote" in e.get("args", [])
                    and any(a.startswith("http.proxy=") for a in e.get("args", []))]
        self.assertTrue(ls_proxy, "代理路径没有核实远端 == 本地（ls-remote 没走代理）")
        self.assertIn(f"http.proxy=http://127.0.0.1:{ok_port}", ls_proxy[-1]["args"])

        # 两个代理都必须被关掉（第一个失败后、最后一个成功后）
        for ev in proxies:
            self.assertTrue(_wait_dead(int(ev["pid"])), f"代理进程没关：pid={ev['pid']}")
        for port in ports:
            self.assertTrue(_port_closed(port), f"代理端口还开着：{port}")
        self.assertIn("push OK via proxy", out)

    def test_proxy_failure_reports_the_real_reason_and_leaves_no_orphan(self):
        """代理也失败：必须写明"直连失败 → 代理也失败（原因）"，且两个代理都被关掉。"""
        code, out = self.run_push(
            ECHO_TEST_DIRECT_FAIL="1",
            ECHO_TEST_PROXY_FAIL="1",
        )
        self.assertEqual(code, 1, out[-4000:])
        self.assertIn("direct attempt 1 failed (git exit 1): fatal: injected direct push failure",
                      out, "直连的真实原因没打出来")
        self.assertIn("[FAIL] direct push failed; the CONNECT proxy also failed (last reason:",
                      out, "没有一句明确的『直连失败 → 代理也失败（原因）』")
        self.assertIn("fatal: injected proxy push failure", out,
                      "代理失败的真实原因没打出来")

        proxies = self.proxy_events()
        self.assertEqual(len(proxies), 2, f"应该试两个 IP 的代理：{proxies}")
        ports = [int(p["port"]) for p in proxies]
        self.assertEqual(len(set(ports)), 2)
        self.assertNotIn(8899, ports)
        for ev in proxies:
            self.assertTrue(_wait_dead(int(ev["pid"])), f"代理进程没关（孤儿）：pid={ev['pid']}")
        for port in ports:
            self.assertTrue(_port_closed(port), f"代理端口没关：{port}")

    def test_missing_node_is_reported_not_silently_skipped(self):
        """没有 node 就如实说"走代理需要 node"，不许静默跳过。"""
        code, out = self.run_push(
            ECHO_TEST_DIRECT_FAIL="1",
            ECHO_GH_PUSH_NODE=str(self.tmp / "no-such-node.exe"),
        )
        self.assertEqual(code, 1, out[-3000:])
        self.assertIn("node was not found", out)
        self.assertIn("CONNECT proxy", out)
        self.assertIn("[FAIL] direct push failed; the CONNECT proxy could not run", out)
        self.assertEqual(self.proxy_events(), [], "没有 node 却起了代理")

    def test_zero_exit_without_matching_remote_is_not_success(self):
        """git exit 0 不等于推上去了：远端 HEAD 对不上就必须报失败，不许说 push OK。"""
        code, out = self.run_push(
            ECHO_TEST_DIRECT_FAIL="0",
            ECHO_TEST_REMOTE_SHA="2" * 40,
        )
        self.assertEqual(code, 1, out[-3000:])
        self.assertIn("not reporting success", out)
        self.assertNotIn("push OK", out)

    def test_script_keeps_its_encoding_and_parses(self):
        """改 .ps1 不许破坏编码：含非 ASCII 就必须带 BOM；并用 Parser.ParseFile 复核。"""
        raw = PS1.read_bytes()
        body = raw[len(BOM):] if raw.startswith(BOM) else raw
        body.decode("utf-8")
        if any(b > 0x7F for b in body):
            self.assertTrue(raw.startswith(BOM),
                            "gh-push.ps1 含非 ASCII 却丢了 BOM —— WinPS 5.1 会按 GBK 解析")
        checker = (
            "$errs = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
            f"'{PS1}', [ref]$null, [ref]$errs); "
            "if ($errs.Count) { $errs | ForEach-Object { $_.Message }; exit 1 }"
        )
        proc = subprocess.run([_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-Command", checker], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0,
                         f"Parser.ParseFile 报错（WinPS 5.1 会解析失败）：\n{proc.stdout}\n{proc.stderr}")


# ------------------------------------------------- 2. build_min_kit 的共享打包锁
class MinKitUsesTheSharedPackLock(unittest.TestCase):
    """组 kit 必须进 `build_kit.pack_lock` —— 同一把锁，不是复制一份。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="echominkit-")
        self.tmp = Path(self._tmp.name)
        self.dist = self.tmp / "dist"

    def tearDown(self):
        self._tmp.cleanup()

    def _patch_common(self, stage, make_zip=None, lock=None):
        """把 main() 的几个重步骤换掉，只留锁与调用顺序可断言。"""
        saved = {}
        for owner, name, value in (
            (min_kit, "load_build_kit", lambda: build_kit),
            (min_kit, "stage_kit", stage),
            (min_kit, "verify_kit", lambda *a, **k: []),
            (min_kit, "print_summary", lambda *a, **k: None),
        ):
            saved[(owner, name)] = getattr(owner, name)
            setattr(owner, name, value)
        saved[(build_kit, "make_zip")] = build_kit.make_zip
        build_kit.make_zip = make_zip or (lambda kit, zpath, top: Path(zpath).write_bytes(b"zip"))
        if lock is not None:
            saved[(build_kit, "pack_lock")] = build_kit.pack_lock
            build_kit.pack_lock = lock

        def restore():
            for (owner, name), value in saved.items():
                setattr(owner, name, value)

        self.addCleanup(restore)

    @staticmethod
    def _stats():
        return {"main_zip": "x.zip", "main_mb": 0.0, "wheels": [], "wheels_mb": 0.0,
                "models": [], "runtime": []}

    def test_it_holds_build_kits_lock_around_stage_verify_and_zip(self):
        events = []
        real_lock = build_kit.pack_lock

        @contextlib.contextmanager
        def spy(dist, timeout=600.0):
            events.append(("lock-enter", str(dist)))
            try:
                yield "token"
            finally:
                events.append(("lock-exit", str(dist)))

        def fake_stage(args, bk):
            events.append(("stage", args.stamp))
            kit = Path(args.dist).resolve() / ("ECHO-kit-min-" + args.stamp)
            kit.mkdir(parents=True, exist_ok=True)
            return kit, self._stats()

        def fake_zip(kit, zpath, top):
            events.append(("zip", top))
            Path(zpath).write_bytes(b"zip")

        self._patch_common(fake_stage, fake_zip, spy)
        code = min_kit.main(["--dist", str(self.dist), "--stamp", "20261018-0111"])
        self.assertEqual(code, 0, events)
        self.assertEqual(events[0], ("lock-enter", str(self.dist.resolve())),
                         "组 kit 没有进 build_kit.pack_lock")
        self.assertEqual(events[-1][0], "lock-exit", "锁没放（或放晚了）")
        names = [e[0] for e in events]
        self.assertIn("stage", names)
        self.assertIn("zip", names)
        self.assertLess(names.index("stage"), names.index("lock-exit"),
                        "stage 跑在锁外面")
        self.assertLess(names.index("zip"), names.index("lock-exit"),
                        "zip 跑在锁外面")
        self.assertNotEqual(build_kit.pack_lock, real_lock, "测试自己的替换没生效？")

    def test_two_concurrent_min_kit_builds_do_not_overlap(self):
        """同一分钟两个组 kit：必须串行（第二个等锁），不许互相 rmtree。"""
        intervals = []
        mutex = threading.Lock()

        def fake_stage(args, bk):
            started = time.monotonic()
            time.sleep(0.35)                     # 模拟"正在写 kit 目录"
            kit = Path(args.dist).resolve() / ("ECHO-kit-min-" + args.stamp)
            kit.mkdir(parents=True, exist_ok=True)
            with mutex:
                intervals.append((threading.current_thread().name, started, time.monotonic()))
            return kit, self._stats()

        self._patch_common(fake_stage)           # 用真的 pack_lock

        results = {}

        def one(name):
            results[name] = min_kit.main(["--dist", str(self.dist),
                                          "--stamp", "20261018-0112"])

        threads = [threading.Thread(target=one, args=(n,), name=n) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        self.assertEqual(sorted(results.values()), [0, 0], f"并发组包没有都成功：{results}")
        self.assertEqual(len(intervals), 2)
        intervals.sort(key=lambda x: x[1])
        self.assertLessEqual(intervals[0][2], intervals[1][1],
                             f"两个组 kit 重叠了（锁没起作用）：{intervals}")
        self.assertFalse((self.dist / build_kit.LOCK_NAME).exists(), "锁文件没释放")

    def test_source_reuses_pack_lock_instead_of_copying_it(self):
        """契约：复用 `build_kit.pack_lock`，不许在 build_min_kit 里再实现一把锁。"""
        src = (SCRIPTS / "build_min_kit.py").read_text(encoding="utf-8")
        self.assertIn("with build_kit.pack_lock(", src,
                      "组 kit 没走 build_kit.pack_lock")
        self.assertNotIn("def pack_lock", src,
                         "build_min_kit 自己复制了一份 pack_lock（两把锁关不住对方）")
        self.assertIn("build_kit.build_main_packages", src,
                      "主包仍然复用 build_kit（不重抄白名单）")


if __name__ == "__main__":
    unittest.main()
