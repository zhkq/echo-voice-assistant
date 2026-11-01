package com.echo.contact.data

import com.google.gson.Gson
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import java.math.BigInteger
import java.nio.charset.StandardCharsets
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.PrivateKey
import java.security.PublicKey
import java.security.SecureRandom
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.security.spec.PKCS8EncodedKeySpec
import javax.crypto.Cipher
import javax.crypto.KeyAgreement
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * 方案 B 手机侧密码学（P-256）。
 *
 * - ticket 里的 ECHO 公钥是信任根，来自二维码旁路；
 * - 配对 claim 用 ECDH + AES-GCM，relay 只看密文；
 * - 常规 API 用设备私钥 + ECHO 公钥派生的静态会话键封 E2E 信封；
 * - 后端 TTS 用 DPoP proof 证明设备持有私钥。
 *
 * 私钥落盘走 [PairStore] 的 EncryptedSharedPreferences；P-256 软件密钥在所有
 * minSdk 26 设备上一致（Android Keystore 的 EC agree 在低版本并不保证）。
 */
object E2E {
    private const val CURVE = "secp256r1"
    private val rnd = SecureRandom()
    private val gson = Gson()

    // ------------------------------------------------------------ base64url

    fun b64u(raw: ByteArray): String =
        java.util.Base64.getUrlEncoder().withoutPadding().encodeToString(raw)

    fun unb64u(text: String): ByteArray =
        java.util.Base64.getUrlDecoder().decode(text)

    // ------------------------------------------------------------ P-256

    fun newKeyPair(): KeyPair {
        val gen = KeyPairGenerator.getInstance("EC")
        gen.initialize(ECGenParameterSpec(CURVE), rnd)
        return gen.generateKeyPair()
    }

    fun publicRaw(pub: PublicKey): ByteArray {
        val point = pub as java.security.interfaces.ECPublicKey
        val x = point.w.affineX.toByteArray().let { if (it.size > 32) it.copyOfRange(it.size - 32, it.size) else it }
        val y = point.w.affineY.toByteArray().let { if (it.size > 32) it.copyOfRange(it.size - 32, it.size) else it }
        return byteArrayOf(0x04) + x.copyOfRange(maxOf(0, x.size - 32), x.size).padStart(32) +
                y.copyOfRange(maxOf(0, y.size - 32), y.size).padStart(32)
    }

    fun publicFromRaw(raw: ByteArray): PublicKey {
        require(raw.size == 65 && raw[0] == 0x04.toByte()) { "不是 P-256 未压缩点" }
        val x = BigInteger(1, raw.copyOfRange(1, 33))
        val y = BigInteger(1, raw.copyOfRange(33, 65))
        val params = java.security.AlgorithmParameters.getInstance("EC").apply {
            init(java.security.spec.ECGenParameterSpec(CURVE))
        }.getParameterSpec(java.security.spec.ECParameterSpec::class.java)
        val pub = java.security.spec.ECPublicKeySpec(java.security.spec.ECPoint(x, y), params)
        return KeyFactory.getInstance("EC").generatePublic(pub)
    }

    fun privateFromStorage(b64: String): PrivateKey {
        val raw = unb64u(b64)
        return KeyFactory.getInstance("EC").generatePrivate(PKCS8EncodedKeySpec(raw))
    }

    fun privateToStorage(key: PrivateKey): String = b64u(key.encoded)

    fun publicToStorage(key: PublicKey): String = b64u(publicRaw(key))

    fun ecdh(privateKey: PrivateKey, publicKey: PublicKey): ByteArray {
        val ka = KeyAgreement.getInstance("ECDH")
        ka.init(privateKey)
        ka.doPhase(publicKey, true)
        return ka.generateSecret()
    }

    fun hkdf(shared: ByteArray, salt: String, info: String, len: Int = 32): ByteArray {
        // RFC5869，单块足够（len=32）。
        val prk = hmac(salt.toByteArray(StandardCharsets.UTF_8), shared)
        return hmac(prk, info.toByteArray(StandardCharsets.UTF_8) + byteArrayOf(1)).copyOf(len)
    }

    private fun hmac(key: ByteArray, data: ByteArray): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key, "HmacSHA256"))
        return mac.doFinal(data)
    }

    // ------------------------------------------------------------ AES-GCM

    fun encrypt(key: ByteArray, plain: ByteArray, aad: ByteArray): Pair<ByteArray, ByteArray> {
        val iv = ByteArray(12).also { rnd.nextBytes(it) }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, iv))
        cipher.updateAAD(aad)
        return iv to cipher.doFinal(plain)
    }

    fun decrypt(key: ByteArray, iv: ByteArray, cipherText: ByteArray, aad: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, iv))
        cipher.updateAAD(aad)
        return cipher.doFinal(cipherText)
    }

    // ------------------------------------------------------------ ticket

    data class Ticket(
        val host: String,
        val relay: String,
        val echoPublicKey: String,
        val nonce: String,
        val code: String,
        val expiresAt: Long,
        val backendBaseUrl: String,
        val backendTlsPin: String,
    )

    fun parseTicket(input: String): Ticket {
        var ticket = input.trim()
        if (ticket.startsWith("echo-pair://")) {
            val query = ticket.substringAfter('?', "")
            val value = query.split('&').firstOrNull { it.startsWith("ticket=") }
                ?.substringAfter("ticket=") ?: throw IllegalArgumentException("配对链接里没有 ticket")
            ticket = java.net.URLDecoder.decode(value, "UTF-8")
        }
        val payload = JsonParser.parseString(String(unb64u(ticket), StandardCharsets.UTF_8)).asJsonObject
        require(payload.get("v")?.asInt == 2) { "配对 ticket 版本不支持" }
        val sig = payload.remove("sig")?.asString ?: throw IllegalArgumentException("ticket 没有签名")
        val pub = publicFromRaw(unb64u(payload.get("pub").asString))
        val check = Signature.getInstance("SHA256withECDSA")
        check.initVerify(pub)
        check.update(canonical(payload).toByteArray(StandardCharsets.UTF_8))
        require(check.verify(unb64u(sig))) { "ticket 签名不对" }
        require(payload.get("exp").asLong > System.currentTimeMillis() / 1000) { "ticket 已过期" }
        val backend = payload.getAsJsonObject("backend")
        return Ticket(
            host = payload.get("host")?.asString.orEmpty(),
            relay = payload.get("relay")?.asString.orEmpty(),
            echoPublicKey = payload.get("pub").asString,
            nonce = payload.get("nonce").asString,
            code = payload.get("code").asString,
            expiresAt = payload.get("exp").asLong,
            backendBaseUrl = backend?.get("baseUrl")?.asString.orEmpty(),
            backendTlsPin = backend?.get("tlsPin")?.asString.orEmpty(),
        )
    }

    /** 与 Python `sort_keys=True, separators=(",", ":")` 对齐的 canonical JSON。 */
    fun canonical(obj: JsonObject): String {
        val sorted = java.util.TreeMap<String, Any?>()
        obj.entrySet().forEach { (k, v) -> sorted[k] = when {
            v.isJsonObject -> JsonParser.parseString(canonical(v.asJsonObject))
            v.isJsonArray -> JsonParser.parseString(v.toString())
            v.isJsonNull -> null
            v.isJsonPrimitive -> if (v.asJsonPrimitive.isBoolean) v.asBoolean
                else if (v.asJsonPrimitive.isNumber) v.asNumber else v.asString
            else -> v.toString()
        } }
        return gson.toJson(sorted)
    }

    // ------------------------------------------------------------ 配对 claim

    fun buildClaim(ticket: Ticket, deviceName: String, deviceKey: KeyPair): JsonObject {
        val echoPub = publicFromRaw(unb64u(ticket.echoPublicKey))
        val eph = newKeyPair()
        val shared = ecdh(eph.private, echoPub)
        val claimKey = hkdf(shared, ticket.nonce, "echo-pair-v2:claim")
        val devicePub = publicToStorage(deviceKey.public)
        val ephPub = publicToStorage(eph.public)
        val transcript = JsonObject().apply {
            addProperty("v", 2)
            addProperty("nonce", ticket.nonce)
            addProperty("ephPub", ephPub)
            addProperty("devicePublicKey", devicePub)
        }
        val signer = Signature.getInstance("SHA256withECDSA")
        signer.initSign(deviceKey.private)
        signer.update(canonical(transcript).toByteArray(StandardCharsets.UTF_8))
        val signature = b64u(signer.sign())
        val plain = JsonObject().apply {
            addProperty("code", ticket.code)
            addProperty("deviceName", deviceName)
            addProperty("devicePublicKey", devicePub)
            addProperty("signature", signature)
        }
        val (iv, cipher) = encrypt(claimKey,
            canonical(plain).toByteArray(StandardCharsets.UTF_8),
            canonical(JsonObject().apply {
                addProperty("v", 2); addProperty("nonce", ticket.nonce)
            }).toByteArray(StandardCharsets.UTF_8))
        return JsonObject().apply {
            addProperty("v", 2)
            addProperty("nonce", ticket.nonce)
            addProperty("ephPub", ephPub)
            addProperty("iv", b64u(iv))
            addProperty("ciphertext", b64u(cipher))
        }.also {
            // 响应密钥用同一个 ECDH shared secret 派生；调用方需要 eph 私钥。
            _pendingResponseKey = hkdf(shared, ticket.nonce, "echo-pair-v2:response")
        }
    }

    @Volatile private var _pendingResponseKey: ByteArray? = null

    fun decryptResponse(envelope: JsonObject): JsonObject {
        val key = _pendingResponseKey ?: throw IllegalStateException("没有待解密的配对响应")
        val plain = decrypt(key, unb64u(envelope.get("iv").asString),
            unb64u(envelope.get("ciphertext").asString),
            "echo-pair-response-v2".toByteArray(StandardCharsets.UTF_8))
        _pendingResponseKey = null
        return JsonParser.parseString(String(plain, StandardCharsets.UTF_8)).asJsonObject
    }

    // ------------------------------------------------------------ E2E 会话信封

    fun sessionKey(devicePrivate: PrivateKey, echoPublicKey: String, deviceId: String): ByteArray {
        val shared = ecdh(devicePrivate, publicFromRaw(unb64u(echoPublicKey)))
        return hkdf(shared, "echo-e2e-session-v2", "device:$deviceId")
    }

    fun sealEnvelope(sessionKey: ByteArray, inner: JsonObject, deviceId: String): JsonObject {
        val (iv, cipher) = encrypt(sessionKey,
            canonical(inner).toByteArray(StandardCharsets.UTF_8),
            "echo-e2e-v2".toByteArray(StandardCharsets.UTF_8))
        return JsonObject().apply {
            addProperty("v", 2)
            addProperty("device", deviceId)
            addProperty("iv", b64u(iv))
            addProperty("ciphertext", b64u(cipher))
        }
    }

    fun openEnvelope(sessionKey: ByteArray, envelope: JsonObject): JsonObject {
        val plain = decrypt(sessionKey, unb64u(envelope.get("iv").asString),
            unb64u(envelope.get("ciphertext").asString),
            "echo-e2e-v2".toByteArray(StandardCharsets.UTF_8))
        return JsonParser.parseString(String(plain, StandardCharsets.UTF_8)).asJsonObject
    }

    fun deviceId(publicKeyB64: String): String {
        val digest = java.security.MessageDigest.getInstance("SHA-256")
            .digest(unb64u(publicKeyB64))
        return b64u(digest).take(32)
    }

    // ------------------------------------------------------------ DPoP

    /** 把 JCA 的 DER ECDSA 签名转成 JOSE 的 r||s（各 32 字节）。 */
    fun derToJose(der: ByteArray): ByteArray {
        var i = 0
        require(der[i++] == 0x30.toByte()) { "DER 签名格式不对" }
        if (der[i].toInt() and 0xFF >= 0x80) i++ // long form length
        i++
        require(der[i++] == 0x02.toByte())
        val rLen = der[i++].toInt() and 0xFF
        val r = der.copyOfRange(i, i + rLen); i += rLen
        require(der[i++] == 0x02.toByte())
        val sLen = der[i++].toInt() and 0xFF
        val s = der.copyOfRange(i, i + sLen)
        fun fixed(v: ByteArray): ByteArray {
            val out = ByteArray(32)
            val clean = if (v.size > 32) v.copyOfRange(v.size - 32, v.size) else v
            System.arraycopy(clean, 0, out, 32 - clean.size, clean.size)
            return out
        }
        return fixed(r) + fixed(s)
    }

    fun jwkFor(publicKey: PublicKey): JsonObject {
        val p = publicKey as java.security.interfaces.ECPublicKey
        fun fixed(v: BigInteger): String {
            val raw = v.toByteArray().let { if (it.size > 32) it.copyOfRange(it.size - 32, it.size) else it }
            return b64u(raw.copyOfRange(maxOf(0, raw.size - 32), raw.size).padStart(32))
        }
        return JsonObject().apply {
            addProperty("kty", "EC"); addProperty("crv", "P-256")
            addProperty("x", fixed(p.w.affineX)); addProperty("y", fixed(p.w.affineY))
        }
    }

    fun dpopProof(deviceKey: KeyPair, accessToken: String, htu: String = "/v1/tts"): String {
        val header = JsonObject().apply {
            addProperty("typ", "dpop+jwt"); addProperty("alg", "ES256")
            add("jwk", jwkFor(deviceKey.public))
        }
        val now = System.currentTimeMillis() / 1000
        val payload = JsonObject().apply {
            addProperty("htm", "POST"); addProperty("htu", htu)
            addProperty("iat", now)
            addProperty("jti", b64u(ByteArray(12).also { rnd.nextBytes(it) }))
            addProperty("ath", b64u(java.security.MessageDigest.getInstance("SHA-256")
                .digest(accessToken.toByteArray(StandardCharsets.US_ASCII))))
        }
        val signingInput = b64u(header.toString().toByteArray(StandardCharsets.UTF_8)) + "." +
                b64u(payload.toString().toByteArray(StandardCharsets.UTF_8))
        val sig = Signature.getInstance("SHA256withECDSA")
        sig.initSign(deviceKey.private)
        sig.update(signingInput.toByteArray(StandardCharsets.US_ASCII))
        return signingInput + "." + b64u(derToJose(sig.sign()))
    }

    private fun ByteArray.padStart(size: Int): ByteArray {
        if (this.size >= size) return this
        return ByteArray(size - this.size) + this
    }
}
