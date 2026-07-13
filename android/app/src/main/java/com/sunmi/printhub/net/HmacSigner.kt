package com.sunmi.printhub.net

import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * HMAC request signing, kept byte-for-byte in lock-step with the server's crypto.signing_string:
 *
 *     signing_string = device_id \n timestamp \n nonce \n METHOD \n path \n sha256_hex(body)
 *     signature      = hex(HMAC_SHA256(device_secret, signing_string))
 *
 * The secret never travels; only the signature does, so a proxy that logs the URL/headers learns
 * nothing reusable. The server rejects stale timestamps and replayed nonces.
 */
object HmacSigner {

    private val rng = SecureRandom()
    private val HEX = "0123456789abcdef".toCharArray()

    fun hex(bytes: ByteArray): String {
        val out = CharArray(bytes.size * 2)
        for (i in bytes.indices) {
            val v = bytes[i].toInt() and 0xFF
            out[i * 2] = HEX[v ushr 4]
            out[i * 2 + 1] = HEX[v and 0x0F]
        }
        return String(out)
    }

    fun sha256Hex(data: ByteArray): String =
        hex(MessageDigest.getInstance("SHA-256").digest(data))

    fun hmacHex(secret: String, message: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(secret.toByteArray(Charsets.UTF_8), "HmacSHA256"))
        return hex(mac.doFinal(message.toByteArray(Charsets.UTF_8)))
    }

    private fun nonce(): String {
        val b = ByteArray(12)
        rng.nextBytes(b)
        return android.util.Base64.encodeToString(
            b, android.util.Base64.URL_SAFE or android.util.Base64.NO_PADDING or android.util.Base64.NO_WRAP
        )
    }

    /** Signed headers for a request. [path] must be the exact server path (e.g. "/messages"). */
    fun headers(
        secret: String,
        deviceId: String,
        method: String,
        path: String,
        body: ByteArray = ByteArray(0),
    ): Map<String, String> {
        val ts = (System.currentTimeMillis() / 1000L).toString()
        val n = nonce()
        val signing = listOf(deviceId, ts, n, method.uppercase(), path, sha256Hex(body)).joinToString("\n")
        return mapOf(
            "X-Device-Id" to deviceId,
            "X-Timestamp" to ts,
            "X-Nonce" to n,
            "X-Signature" to hmacHex(secret, signing),
        )
    }
}
