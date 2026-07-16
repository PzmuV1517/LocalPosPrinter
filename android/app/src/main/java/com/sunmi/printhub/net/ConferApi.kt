package com.sunmi.printhub.net

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Confer REST client (bearer-token, separate from the HMAC device identity).
 *
 * Login mints a Confer session token on the server; every other call carries it as a bearer.
 * All methods are **synchronous**, call them from a background thread (ConferManager does).
 * On any non-2xx the server returns ``{"error": "..."}``; that message is surfaced verbatim.
 */
class ConferApi(baseDomain: String) {

    class ConferException(message: String) : Exception(message)

    private val base: String = normalize(baseDomain)
    var token: String? = null

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    companion object {
        private val JSON = "application/json".toMediaType()

        /** Turn a bare domain / ws(s):// / http(s):// into an https base URL with no trailing slash. */
        private fun normalize(domain: String): String {
            val d = domain.trim().removeSuffix("/")
            return when {
                d.startsWith("http://") || d.startsWith("https://") -> d
                d.startsWith("ws://") -> "http://" + d.removePrefix("ws://")
                d.startsWith("wss://") -> "https://" + d.removePrefix("wss://")
                else -> "https://$d"
            }
        }
    }

    private fun post(path: String, body: JSONObject, auth: Boolean = true): JSONObject {
        val builder = Request.Builder()
            .url("$base$path")
            .post(body.toString().toRequestBody(JSON))
        if (auth) builder.header("Authorization", "Bearer ${token ?: ""}")
        client.newCall(builder.build()).execute().use { resp ->
            val text = resp.body?.string() ?: "{}"
            val json = try { JSONObject(text) } catch (t: Throwable) { JSONObject() }
            if (!resp.isSuccessful) {
                throw ConferException(json.optString("error", "HTTP ${resp.code}"))
            }
            return json
        }
    }

    data class Session(val token: String, val username: String, val display: String)

    fun login(username: String, password: String): Session {
        val r = post("/confer/login", JSONObject().put("username", username).put("password", password), auth = false)
        val user = r.getJSONObject("user")
        val t = r.getString("token")
        token = t
        return Session(t, user.optString("username", username), user.optString("display_name", username))
    }

    /** The shared folder/chat tree. Returns the raw object with "folders" and "chats" arrays. */
    fun tree(): JSONObject = post("/confer/tree", JSONObject())

    fun sendText(chatId: Int, text: String) {
        post("/confer/send", JSONObject().put("chat_id", chatId).put("kind", "text").put("text", text))
    }

    fun sendImage(chatId: Int, base64Png: String) {
        post("/confer/send", JSONObject().put("chat_id", chatId).put("kind", "image").put("image", base64Png))
    }

    fun history(chatId: Int, afterId: Int? = null): JSONArray {
        val body = JSONObject().put("chat_id", chatId)
        if (afterId != null) body.put("after_id", afterId)
        return post("/confer/history", body).optJSONArray("messages") ?: JSONArray()
    }

    fun subscriptions(): JSONArray =
        post("/confer/subscriptions", JSONObject()).optJSONArray("subscriptions") ?: JSONArray()

    fun setSubscription(targetType: String, targetId: Int, on: Boolean): JSONArray {
        val body = JSONObject().put("action", "set")
            .put("target_type", targetType).put("target_id", targetId).put("on", on)
        return post("/confer/subscriptions", body).optJSONArray("subscriptions") ?: JSONArray()
    }

    fun markRead(chatId: Int, lastMsgId: Int) {
        post("/confer/read", JSONObject().put("chat_id", chatId).put("last_msg_id", lastMsgId))
    }

    fun createFolder(name: String, parentId: Int? = null): JSONObject {
        val body = JSONObject().put("name", name)
        if (parentId != null) body.put("parent_id", parentId)
        return post("/confer/folder", body)
    }

    fun createChat(name: String, folderId: Int? = null): JSONObject {
        val body = JSONObject().put("name", name)
        if (folderId != null) body.put("folder_id", folderId)
        return post("/confer/chat", body)
    }
}
