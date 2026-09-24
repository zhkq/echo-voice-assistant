# -*- coding: utf-8 -*-
"""后端凭据的落盘边界（设计 §7.5 ⑥）。

这个文件是**整套方案里最值得保护的一件东西**：拿到它就等于拿到"这台机器"的身份，
能去用那台 GPU、并以这台机器的名义出现在服务端审计里。

所以这里不测"代码看起来对不对"，测**能观察到的事实**：

  1. **明文 secret 不出现在文件字节里**（Windows 上走 DPAPI；直接读字节来验）；
  2. POSIX 上文件权限是 `0600`；
  3. **读坏了不抛**（返回 None = 没配对），面板不该因为一个坏文件起不来；
  4. `repr` / `str` 里**不许出现完整 secret**（它会进日志、进异常栈）；
  5. 落盘**不带 access_token**（短命令牌重启后重新换，不该留在盘上）。

不碰任何真实后端；不写真实 `data/`（路径打桩到临时目录）。
"""
import base64
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.capabilities import credentials as cred                          # noqa: E402
from app.capabilities.credentials import BackendCredentials              # noqa: E402

SECRET = "S3cr3t-Very-Long-Token-abcdefghijklmnop"


def _dpapi_available_here() -> bool:
    """这里**故意**不调 `cred._dpapi_available()` —— 用例不该用自己的被测对象来判平台。"""
    return sys.platform == "win32"


class _CredCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-cred-")
        p = patch.object(cred, "credentials_path",
                         lambda: os.path.join(self.tmp, "backend.json"))
        p.start()
        self.addCleanup(p.stop)

    def _creds(self, **kw):
        base = dict(base_url="http://gpu-01:8900", client_id="cli-abc123",
                    secret=SECRET, server_name="gpu-01")
        base.update(kw)
        return BackendCredentials(**base)

    def _raw(self):
        with open(cred.credentials_path(), "rb") as fh:
            return fh.read()


class RoundTripTests(_CredCase):
    def test_save_then_load_returns_the_same_thing(self):
        cred.save(self._creds())
        got = cred.load()
        self.assertIsNotNone(got)
        self.assertEqual(got.base_url, "http://gpu-01:8900")
        self.assertEqual(got.client_id, "cli-abc123")
        self.assertEqual(got.secret, SECRET)
        self.assertEqual(got.server_name, "gpu-01")

    def test_load_returns_none_when_nothing_paired(self):
        """没配对 = 返回 None（**不是抛异常**）。"""
        self.assertIsNone(cred.load())

    def test_clear_forgets_it_and_is_idempotent(self):
        cred.save(self._creds())
        self.assertTrue(cred.clear())
        self.assertIsNone(cred.load())
        self.assertTrue(cred.clear(), "再删一次也该成功（文件已经不在了）")


def _leaky_encodings(secret: str):
    """一个秘密"等同于明文躺在文件里"的所有常见写法。

    **为什么不能只查原始字节**：落盘的信封是 `{"enc":…, "value": base64(...)}`，
    base64 之后的密文里**本来就不会有** secret 的原始字节 —— 只查原始字节的话，
    就算有人把 Windows 那条路改成"明文 + base64"，用例照样全绿。
    （这条是我自己写完复查时发现的：第一版就是这么写的，等于没测。）
    """
    raw = secret.encode("utf-8")
    b64 = base64.b64encode(raw)
    return [raw, b64, b64.rstrip(b"="), raw.hex().encode(), secret.encode("utf-16-le")]


class SecretNeverLandsInPlaintextTests(_CredCase):
    def test_plaintext_secret_is_not_in_the_file_bytes(self):
        """**直接读文件字节**来看，不看代码。

        这条是这一层的全部意义：盘上的东西被拷走、被别的进程读到、被同步到网盘，
        也**不该**等于"拿到了这台机器的身份"。
        """
        cred.save(self._creds())
        blob = self._raw()
        for probe in _leaky_encodings(SECRET):
            self.assertNotIn(probe, blob,
                             "secret 以可逆形式落盘了（%r…）—— 换台机器读这个文件就能冒充这台机器"
                             % probe[:12])

    def test_posix_plain_envelope_is_the_documented_tradeoff(self):
        """POSIX 上落的就是**可逆**的 base64（靠 0600 挡人），这是设计里写明的取舍。

        所以这条用例是"把取舍钉住"，不是"证明它安全"：
        哪天有人想在 Linux 上也加一层保护，会先看到这里写着当时为什么没加。
        """
        if _dpapi_available_here():
            self.skipTest("Windows 上走 DPAPI，没有这个取舍")
        cred.save(self._creds())
        data = json.loads(self._raw().decode("utf-8"))
        self.assertEqual(data["secret"]["enc"], "plain")
        self.assertEqual(base64.b64decode(data["secret"]["value"]),
                         SECRET.encode("utf-8"),
                         "换成别的写法了 —— 那 0600 这道墙就不是唯一的墙了，得同步改文档")

    def test_envelope_says_which_protection_was_used(self):
        """信封要写明用了哪种保护：Windows=dpapi，别处=plain+0600。

        写明白才有意义 —— 以后有人把 Windows 上的文件拷到 Linux，
        代码能据此说"解不开"，而不是退回明文路径去猜。
        """
        cred.save(self._creds())
        data = json.loads(self._raw().decode("utf-8"))
        self.assertIn(data["secret"]["enc"], ("dpapi", "plain"))
        if sys.platform == "win32":
            self.assertEqual(data["secret"]["enc"], "dpapi",
                             "Windows 上应当走 DPAPI")

    def test_access_token_is_not_written_to_disk(self):
        """短命令牌**不落盘**：它是短命的，重启后重新换即可；留在盘上只是多一个泄漏面。"""
        c = self._creds()
        c.access_token = "eyJhbGciOiJIUzI1NiJ9.fake.jwt"
        c.token_expires_at = 9_999_999_999.0
        cred.save(c)
        raw = self._raw()
        self.assertNotIn(b"eyJhbGciOiJIUzI1NiJ9", raw, "令牌落盘了")
        self.assertNotIn(b"token_expires_at", raw)

    def test_repr_masks_the_secret(self):
        """`repr` 会进日志与异常栈 —— 里面不许出现完整 secret。"""
        text = repr(self._creds())
        self.assertNotIn(SECRET, text)
        self.assertIn("cli-abc123", text, "别把有用的信息也一起遮没了")
        self.assertNotIn(SECRET, str(self._creds()))

    def test_posix_file_mode_is_0600(self):
        if os.name == "nt":
            self.skipTest("Windows 上靠 DPAPI，不靠文件权限")
        cred.save(self._creds())
        mode = os.stat(cred.credentials_path()).st_mode & 0o777
        self.assertEqual(mode, 0o600, "权限是 %o，别的用户能读" % mode)


class BrokenFileTests(_CredCase):
    """**读坏了不许炸** —— 否则一个坏文件能让面板起不来。"""

    def _write(self, text):
        with open(cred.credentials_path(), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_garbage_json_is_none(self):
        self._write("这不是 JSON {{{")
        self.assertIsNone(cred.load())

    def test_missing_fields_is_none(self):
        self._write(json.dumps({"base_url": "http://x"}))
        self.assertIsNone(cred.load(), "没有 client_id/secret 就等于没配对")

    def test_corrupt_secret_envelope_is_none(self):
        self._write(json.dumps({"client_id": "cli-x",
                                "secret": {"enc": "dpapi", "value": "!!!not-base64!!!"}}))
        self.assertIsNone(cred.load())

    def test_unknown_envelope_kind_is_none_not_a_guess(self):
        """认不出保护方式 → 当没有。**不许退回明文路径去猜。**"""
        import base64
        self._write(json.dumps({"client_id": "cli-x",
                                "secret": {"enc": "rot13",
                                           "value": base64.b64encode(b"x").decode()}}))
        self.assertIsNone(cred.load())

    def test_foreign_dpapi_blob_gives_none_not_an_exception(self):
        """把 Windows 上配的文件拷到 Linux（或反过来）→ 解不开就是 `None`。"""
        import base64
        self._write(json.dumps({"client_id": "cli-x",
                                "secret": {"enc": "dpapi",
                                           "value": base64.b64encode(b"garbage").decode()}}))
        self.assertIsNone(cred.load())


class TokenFreshnessTests(_CredCase):
    def test_no_token_is_not_fresh(self):
        self.assertFalse(self._creds().token_fresh())

    def test_fresh_token(self):
        import time
        c = self._creds()
        c.access_token = "t"
        c.token_expires_at = time.time() + 3600
        self.assertTrue(c.token_fresh())

    def test_token_inside_the_skew_window_is_already_stale(self):
        """留 60 秒余量：内网机器时钟未必准，卡着到期的令牌用出去就是 401。"""
        import time
        c = self._creds()
        c.access_token = "t"
        c.token_expires_at = time.time() + 30
        self.assertFalse(c.token_fresh(skew_s=60))


class PathTests(unittest.TestCase):
    def test_path_follows_data_root(self):
        """凭据跟随 `paths.data_root()`（用户改过数据目录就跟着走）。"""
        from app import paths
        self.assertEqual(os.path.dirname(cred.credentials_path()),
                         os.path.abspath(paths.data_root()))

    def test_filename_matches_the_design(self):
        """设计 §7.5 ⑥ 写的是 `{echoBase}/data/backend.json`。"""
        self.assertEqual(cred.FILENAME, "backend.json")


if __name__ == "__main__":
    unittest.main()
