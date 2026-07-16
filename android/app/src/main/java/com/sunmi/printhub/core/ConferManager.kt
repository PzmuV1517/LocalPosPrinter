package com.sunmi.printhub.core

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.util.Log
import com.sunmi.printhub.net.ConferApi
import com.sunmi.printhub.net.ConferSocket
import com.sunmi.printhub.render.ConferRenderer
import com.sunmi.printhub.render.ImageUtils
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors

/**
 * Confer client: session, chat tree, subscriptions, and print decisions.
 *
 * Screen on with a chat open: that chat prints (yours right, others left). Screen off: subscribed
 * chats print, with a labelled separator when the source chat changes. Sending does not print
 * locally; the server echoes the message back and it prints via the same path.
 */
object ConferManager {

    private const val TAG = "ConferManager"
    const val MAX_CHARS = 888

    data class Message(
        val id: Int, val chatId: Int, val sender: String, val senderDisplay: String,
        val kind: String, val body: String, val ts: Double,
    ) {
        companion object {
            fun from(o: JSONObject) = Message(
                id = o.optInt("id"), chatId = o.optInt("chat_id"), sender = o.optString("sender"),
                senderDisplay = o.optString("sender_display", o.optString("sender")),
                kind = o.optString("kind", "text"), body = o.optString("body"), ts = o.optDouble("ts", 0.0),
            )
        }
    }

    data class Folder(val id: Int, val name: String, val parentId: Int?)
    data class Chat(val id: Int, val name: String, val folderId: Int?)

    interface Listener {
        fun onState()
        fun onTree()
        fun onMessages(chatId: Int)
        fun onError(message: String)
    }

    @Volatile var listener: Listener? = null

    private lateinit var appContext: Context
    private var power: PowerManager? = null
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    @Volatile private var api: ConferApi? = null
    @Volatile private var socket: ConferSocket? = null
    @Volatile var folders: List<Folder> = emptyList(); private set
    @Volatile var chats: List<Chat> = emptyList(); private set
    @Volatile private var subs: List<Pair<String, Int>> = emptyList()   // (type, id)
    @Volatile var activeChatId: Int = 0
    @Volatile var connected = false; private set   // dedicated Confer socket is open

    private val messages = HashMap<Int, MutableList<Message>>()
    @Volatile private var lastPrintedChatId: Int = -1
    // Sender of the last message actually printed, for grouping: a run from one sender hides the
    // repeated name (the ">"/"<" marker stays). Reset to null after any banner/separator.
    @Volatile private var lastPrintedSender: String? = null

    fun init(context: Context) {
        appContext = context.applicationContext
        power = appContext.getSystemService(Context.POWER_SERVICE) as? PowerManager
        rebuildApi()
    }

    private fun settings() = Hub.settings
    val loggedIn: Boolean get() = Hub.settings.conferToken.isNotBlank()
    val username: String get() = Hub.settings.conferUsername
    val displayName: String get() = Hub.settings.conferDisplay

    private fun rebuildApi() {
        val a = ConferApi(settings().conferServerEffective)
        a.token = settings().conferToken.ifBlank { null }
        api = a
    }

    // ---- session ----
    fun login(username: String, password: String, cb: (Boolean, String?) -> Unit) {
        io.execute {
            try {
                rebuildApi()
                val s = api!!.login(username, password)
                settings().conferToken = s.token
                settings().conferUsername = s.username
                settings().conferDisplay = s.display
                loadTreeBlocking()
                loadSubsBlocking()
                if (settings().conferMode) main.post { openSocket() }   // resume chat after re-login
                main.post { cb(true, null); listener?.onState(); listener?.onTree() }
            } catch (t: Throwable) {
                main.post { cb(false, t.message ?: "Login failed") }
            }
        }
    }

    fun logout() {
        setConferMode(false)
        settings().clearConfer()
        rebuildApi()
        connected = false
        messages.clear()
        main.post { listener?.onState() }
    }

    // ---- mode switching ----
    // Two signals: a "mode" frame on the print socket (pauses jobs, flips that server's badge),
    // and a separate ConferSocket to the Confer server that carries the chat.
    fun setConferMode(on: Boolean) {
        settings().conferMode = on
        if (on) {
            if (!loggedIn) { main.post { listener?.onError("Log in to Confer first") }; return }
            announcePrintMode(false)
            openSocket()
            printStartup()
            // Head the active chat's transcript now, so enabling the mode shows it without a chat switch.
            if (activeChatId != 0) printTranscriptStart(activeChatId)
        } else {
            announcePrintMode(true)    // back to Print mode → resume print jobs
            closeSocket()
        }
    }

    private fun printStartup() {
        io.execute {
            try {
                val s = settings()
                val lines = listOf(
                    "> initializing secure channel",
                    "> user   : ${displayName.ifBlank { username }} (@$username)",
                    "> device : ${s.deviceId}",
                    "> server : ${s.conferServerEffective}",
                    "> crypto : TLS + at-rest",
                    "> status : LINK UP",
                    "> awaiting transcripts_",
                )
                Hub.printer.printBitmap(ConferRenderer.conferStartup(lines, s.printWidthPx), false, 4)
                lastPrintedSender = null
            } catch (t: Throwable) { Log.e(TAG, "startup banner failed", t) }
        }
    }

    private fun printTranscriptStart(chatId: Int) {
        io.execute {
            try {
                Hub.printer.printBitmap(
                    ConferRenderer.transcriptStart("# " + chatName(chatId), settings().printWidthPx), false, 4)
                lastPrintedSender = null
            } catch (t: Throwable) { Log.e(TAG, "transcript banner failed", t) }
        }
    }

    val conferModeOn: Boolean get() = settings().conferMode

    /** Announce Print vs Confer to the internet-listener (print) server so it pauses/resumes jobs. */
    private fun announcePrintMode(printMode: Boolean) {
        Hub.internet?.sendFrame(
            JSONObject().put("type", "mode").put("mode", if (printMode) "print" else "confer").toString())
    }

    private fun openSocket() {
        closeSocket()
        val s = ConferSocket(
            server = settings().conferServerEffective,
            token = settings().conferToken,
            onFrame = { onFrame(it) },
            onConnected = { c -> connected = c; main.post { listener?.onState() } },
            onAuthFailed = {
                settings().clearConfer()
                main.post { listener?.onError("Confer session expired, log in again"); listener?.onState() }
            },
        )
        socket = s
        s.start()
    }

    private fun closeSocket() {
        socket?.stop(); socket = null; connected = false
    }

    /** Called when the internet-listener socket (re)opens, so print-mode pausing survives reconnects. */
    fun onSocketOpen() {
        if (settings().conferMode && loggedIn) announcePrintMode(false)
    }

    // ---- incoming frames from the Confer socket (called on the WS thread) ----
    fun onFrame(frame: JSONObject) {
        when (frame.optString("type")) {
            "confer_msg" -> handleIncoming(Message.from(frame))
            "confer_catchup" -> handleCatchup(frame.optInt("chat_id"), frame.optJSONArray("messages"))
        }
    }

    private fun handleIncoming(msg: Message) {
        cache(msg)
        main.post { listener?.onMessages(msg.chatId) }
        val screenOn = power?.isInteractive ?: true
        if (screenOn) {
            if (msg.chatId == activeChatId) { printMessage(msg, separatorChat = null); markRead(msg.chatId, msg.id) }
        } else if (subscribedChatIds().contains(msg.chatId)) {
            val sep = if (lastPrintedChatId != msg.chatId) msg.chatId else null
            printMessage(msg, separatorChat = sep)
            lastPrintedChatId = msg.chatId
            markRead(msg.chatId, msg.id)
        }
    }

    private fun handleCatchup(chatId: Int, arr: JSONArray?) {
        if (arr == null || arr.length() == 0) return
        var last = 0
        // A batch of missed messages always leads with a labelled separator for its chat.
        printSeparator(chatId)
        lastPrintedChatId = chatId
        for (i in 0 until arr.length()) {
            val m = Message.from(arr.getJSONObject(i))
            cache(m)
            printMessage(m, separatorChat = null)
            last = m.id
        }
        if (last > 0) markRead(chatId, last)
        main.post { listener?.onMessages(chatId) }
    }

    // ---- printing ----
    private fun printSeparator(chatId: Int) {
        io.execute {
            try {
                Hub.printer.printBitmap(ConferRenderer.separator(chatName(chatId), settings().printWidthPx), false, 1)
                lastPrintedSender = null   // a new source chat → re-show the name
            } catch (t: Throwable) { Log.e(TAG, "separator print failed", t) }
        }
    }

    private fun printMessage(msg: Message, separatorChat: Int?) {
        io.execute {
            try {
                val w = settings().printWidthPx
                if (separatorChat != null) {
                    Hub.printer.printBitmap(ConferRenderer.separator(chatName(separatorChat), w), false, 1)
                    lastPrintedSender = null
                }
                val mine = msg.sender == username
                val showName = msg.sender != lastPrintedSender   // hide the repeated name in a run
                val bmp = if (msg.kind == "image") {
                    val img = ImageUtils.decodeBase64(msg.body) ?: return@execute
                    ConferRenderer.renderImage(if (mine) displayName.ifBlank { username } else msg.senderDisplay, img, showName, w)
                } else {
                    ConferRenderer.renderText(msg.senderDisplay, msg.body, mine, showName, w)
                }
                // Small feed so grouped messages stay tight but the last line still clears the head.
                Hub.printer.printBitmap(bmp, false, 2)
                lastPrintedSender = msg.sender
            } catch (t: Throwable) { Log.e(TAG, "message print failed", t) }
        }
    }

    private fun markRead(chatId: Int, lastId: Int) {
        socket?.send(
            JSONObject().put("type", "read").put("chat_id", chatId).put("last_msg_id", lastId).toString())
    }

    // ---- sending (validation + REST) ----
    fun sendText(chatId: Int, text: String) {
        val body = text.trim()
        if (body.isEmpty()) return
        if (body.length > MAX_CHARS) { listener?.onError("Message too long (max $MAX_CHARS characters)"); return }
        io.execute {
            try { api?.sendText(chatId, body) }
            catch (t: Throwable) { main.post { listener?.onError(t.message ?: "Send failed") } }
        }
    }

    /** Encode + downscale a picked image to the print width and send it. */
    fun sendImage(chatId: Int, bitmap: android.graphics.Bitmap) {
        io.execute {
            try {
                val scaled = ImageUtils.scaleToWidth(bitmap, settings().printWidthPx)
                api?.sendImage(chatId, ImageUtils.encodeBase64Png(scaled))
            } catch (t: Throwable) { main.post { listener?.onError(t.message ?: "Image send failed") } }
        }
    }

    // ---- tree / history / subscriptions ----
    fun refreshTree() {
        io.execute {
            try { loadTreeBlocking(); loadSubsBlocking(); main.post { listener?.onTree() } }
            catch (t: Throwable) { main.post { listener?.onError(t.message ?: "Could not load chats") } }
        }
    }

    private fun loadTreeBlocking() {
        val t = api!!.tree()
        val fs = ArrayList<Folder>()
        t.optJSONArray("folders")?.let { for (i in 0 until it.length()) {
            val o = it.getJSONObject(i)
            fs.add(Folder(o.getInt("id"), o.getString("name"),
                if (o.isNull("parent_id")) null else o.getInt("parent_id")))
        } }
        val cs = ArrayList<Chat>()
        t.optJSONArray("chats")?.let { for (i in 0 until it.length()) {
            val o = it.getJSONObject(i)
            cs.add(Chat(o.getInt("id"), o.getString("name"),
                if (o.isNull("folder_id")) null else o.getInt("folder_id")))
        } }
        folders = fs; chats = cs
    }

    private fun loadSubsBlocking() {
        val arr = api!!.subscriptions()
        val out = ArrayList<Pair<String, Int>>()
        for (i in 0 until arr.length()) {
            val o = arr.getJSONObject(i); out.add(o.getString("type") to o.getInt("id"))
        }
        subs = out
    }

    fun openChat(chatId: Int) {
        val changed = chatId != activeChatId
        activeChatId = chatId
        // Terminal-style banner when a chat opens, but only in Confer mode (Print mode would waste paper).
        if (changed && conferModeOn) printTranscriptStart(chatId)
        io.execute {
            try {
                val arr = api!!.history(chatId)
                val list = ArrayList<Message>()
                for (i in 0 until arr.length()) list.add(Message.from(arr.getJSONObject(i)))
                synchronized(messages) { messages[chatId] = list }
                main.post { listener?.onMessages(chatId) }
            } catch (t: Throwable) { main.post { listener?.onError(t.message ?: "Could not load messages") } }
        }
    }

    fun messagesFor(chatId: Int): List<Message> = synchronized(messages) { messages[chatId]?.toList() ?: emptyList() }

    fun createChat(name: String, folderId: Int?) {
        io.execute {
            try { api?.createChat(name, folderId); loadTreeBlocking(); main.post { listener?.onTree() } }
            catch (t: Throwable) { main.post { listener?.onError(t.message ?: "Could not create chat") } }
        }
    }

    fun createFolder(name: String, parentId: Int?) {
        io.execute {
            try { api?.createFolder(name, parentId); loadTreeBlocking(); main.post { listener?.onTree() } }
            catch (t: Throwable) { main.post { listener?.onError(t.message ?: "Could not create folder") } }
        }
    }

    fun isSubscribed(type: String, id: Int): Boolean = subs.contains(type to id)

    fun toggleSubscription(type: String, id: Int, on: Boolean) {
        io.execute {
            try {
                val arr = api!!.setSubscription(type, id, on)
                val out = ArrayList<Pair<String, Int>>()
                for (i in 0 until arr.length()) { val o = arr.getJSONObject(i); out.add(o.getString("type") to o.getInt("id")) }
                subs = out
                main.post { listener?.onTree() }
            } catch (t: Throwable) { main.post { listener?.onError(t.message ?: "Could not update subscription") } }
        }
    }

    // ---- helpers ----
    private fun cache(msg: Message) {
        synchronized(messages) { messages.getOrPut(msg.chatId) { ArrayList() }.add(msg) }
    }

    fun chatName(chatId: Int): String = chats.firstOrNull { it.id == chatId }?.name ?: "chat $chatId"

    /** Flatten chat + folder subscriptions into the set of chat ids that should print when idle. */
    private fun subscribedChatIds(): Set<Int> {
        val ids = HashSet<Int>()
        val childFolders = HashMap<Int?, MutableList<Int>>()
        for (f in folders) childFolders.getOrPut(f.parentId) { ArrayList() }.add(f.id)
        for ((type, id) in subs) {
            if (type == "chat") ids.add(id)
            else if (type == "folder") {
                val wanted = HashSet<Int>()
                val stack = ArrayDeque<Int>(); stack.add(id)
                while (stack.isNotEmpty()) {
                    val fid = stack.removeLast(); wanted.add(fid)
                    childFolders[fid]?.let { stack.addAll(it) }
                }
                for (c in chats) if (c.folderId != null && wanted.contains(c.folderId)) ids.add(c.id)
            }
        }
        return ids
    }
}
