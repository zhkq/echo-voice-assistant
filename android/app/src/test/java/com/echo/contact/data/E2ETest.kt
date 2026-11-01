package com.echo.contact.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test

/**
 * 方案 B 手机侧协议向量：二维码 ticket 必须能被 Kotlin 验签/解出。
 * 该 ticket 由 Python `app/e2e.py::new_ticket` 生成（2026-11-01）。
 */
class E2ETest {
    private val ticket =
        "eyJiYWNrZW5kIjp7ImJhc2VVcmwiOiJodHRwczovL2JhY2tlbmQ6ODkwMCIsInRsc1BpbiI6InNoYTI1Ni9hYmMifSwiY29kZSI6IjEyMzQ1NiIsImV4cCI6MjEwODgyODIwOSwiaG9zdCI6ImgiLCJub25jZSI6IjRJLTEzdG14eUdiREhmSFJaa1N0OTNNQyIsInB1YiI6IkJQUTdENWg0dS1yeEJBYXJPNTlfOUJDX05JTHIxcU5oYkptOUhyd045WndFd1EtRTVibU5oRlRDaTNUaUVYMjRRX3pMbHZzaks4SHZQU3BPejZRVjM0RSIsInJlbGF5IjoiaHR0cDovLzE5Mi4xNjguMS41Ojg5NzAvdjEvcmVsYXkvaC8iLCJzaWciOiJNRVFDSUM5TmtyMzZvYzZJLVJ6X0lWb01xTmNGTzI2dE5xS1p6Ny1vSVlVVEhXTEFBaUJ0d0NWam81M3R6WUlMZFQzbkRoYTBmOGlZQlRtLXI5WmlSd21iWngzSHlnIiwidiI6Mn0"

    @Test
    fun parsesEchoTicketAndVerifiesSignature() {
        val t = E2E.parseTicket(ticket)
        assertEquals("h", t.host)
        assertEquals("123456", t.code)
        assertEquals("4I-13tmxyGbDHfHRZkSt93MC", t.nonce)
        assertEquals("https://backend:8900", t.backendBaseUrl)
        assertEquals("sha256/abc", t.backendTlsPin)
    }

    @Test
    fun deviceIdIsStableAndNotThePublicKey() {
        val kp = E2E.newKeyPair()
        val pub = E2E.publicToStorage(kp.public)
        assertEquals(E2E.deviceId(pub), E2E.deviceId(pub))
        assertNotEquals(pub, E2E.deviceId(pub))
    }

    @Test
    fun sessionKeyIsStableForSameDevice() {
        val phone = E2E.newKeyPair()
        val echo = E2E.newKeyPair()
        val echoPub = E2E.publicToStorage(echo.public)
        val id = E2E.deviceId(E2E.publicToStorage(phone.public))
        val a = E2E.sessionKey(phone.private, echoPub, id)
        val b = E2E.sessionKey(phone.private, echoPub, id)
        assertEquals(a.toList(), b.toList())
    }

    @Test
    fun sessionKeyMatchesPythonFixture() {
        // 与 Python app/e2e.py 相同的 P-256/HKDF 参数；任何一侧改字段/盐都会红。
        val priv =
            "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgw7X5OkYRaX8TgAgxlZQBO7rYU1K8GjwyHwanntHliS-hRANCAAQny2Ej1mfYHITo1D8HqMQz0cmKrm1S-ynwIvXwRJ8l_-KNjFendiW7LXPUP9Oz4lFA5xUtDc8GFffJhwkJADKn"
        val echoPub =
            "BLrAvAE4Ha3Xc60u3xVgGRCT7oj0TY2f1Uc81pwvx5zg2qkL9yNhM1uYzaEHJGLN3a0ixwSvrTBSBgG9T5-ytf8"
        val key = E2E.sessionKey(E2E.privateFromStorage(priv), echoPub, "rs7eqlE8o10N8-ZCdI5TJsLggePAd6yZ")
        assertEquals(
            "07c9a4fc1411a6bfb40a6941f31beeb4ff525f725fce02860f56d70d40a3cdc4",
            key.joinToString("") { "%02x".format(it) },
        )
    }

    @Test
    fun dpopProofHasJoseShape() {
        val key = E2E.newKeyPair()
        val proof = E2E.dpopProof(key, "access-token", "/v1/tts")
        val parts = proof.split(".")
        assertEquals(3, parts.size)
        assertNotEquals("", parts[0])
        assertEquals(64, E2E.unb64u(parts[2]).size) // ES256 r||s
        val header = String(E2E.unb64u(parts[0]), Charsets.UTF_8)
        val payload = String(E2E.unb64u(parts[1]), Charsets.UTF_8)
        assertEquals(true, header.contains("\"alg\":\"ES256\""))
        assertEquals(true, payload.contains("\"htu\":\"/v1/tts\""))
    }
}
