package com.sunmi.printhub.ui

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.Editable
import android.text.InputFilter
import android.text.TextWatcher
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.EditText
import android.widget.ImageView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import com.sunmi.printhub.core.ConferManager
import com.sunmi.printhub.core.Hub
import com.sunmi.printhub.core.PrintDispatcher
import com.sunmi.printhub.databinding.ActivityMainBinding
import com.sunmi.printhub.db.JobSource
import com.sunmi.printhub.model.PrintFormat
import com.sunmi.printhub.model.PrintPayload
import com.sunmi.printhub.render.ReceiptRenderer
import com.sunmi.printhub.service.PrintHubService
import java.util.concurrent.Executors

class MainActivity : AppCompatActivity(), ConferManager.Listener {

    private lateinit var binding: ActivityMainBinding
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    // ---- Confer ----
    private val conferAdapter = ConferMessageAdapter { ConferManager.username }
    private var conferChatIds: List<Int> = emptyList()   // spinner position -> chat id
    private val pickImage =
        registerForActivityResult(ActivityResultContracts.GetContent()) { uri -> uri?.let { onImagePicked(it) } }

    private val formats = PrintFormat.values().map { it.wire }
    private val modes = listOf("receipt", "label")
    private val borderStyles = listOf(
        "line", "dashes", "equals", "asterisk", "at", "hash",
        "dot", "plus", "wave", "box", "double", "rounded"
    )

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Hub.init(this)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        supportActionBar?.title = getString(com.sunmi.printhub.R.string.app_name)

        binding.formatSpinner.adapter =
            ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, formats)
        binding.modeSpinner.adapter =
            ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, modes)
        binding.modeSpinner.setSelection(modes.indexOf(Hub.settings.defaultPrintMode).coerceAtLeast(0))
        binding.borderStyleSpinner.adapter =
            ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, borderStyles)

        binding.formatSpinner.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(p: AdapterView<*>?, v: View?, pos: Int, id: Long) {
                val fmt = formats[pos]
                binding.barcodeTypeInput.visibility =
                    if (fmt == PrintFormat.BARCODE.wire) View.VISIBLE else View.GONE
                val boxed = fmt == PrintFormat.BOXED.wire
                val boxedVis = if (boxed) View.VISIBLE else View.GONE
                binding.borderStyleLabel.visibility = boxedVis
                binding.borderStyleSpinner.visibility = boxedVis
                binding.textSizeInput.visibility =
                    if (isTextSizeFormat(fmt)) View.VISIBLE else View.GONE
            }

            override fun onNothingSelected(p: AdapterView<*>?) {}
        }

        binding.previewButton.setOnClickListener { renderPreview() }
        binding.printButton.setOnClickListener { doPrint() }

        // Ensure the network channels are running.
        PrintHubService.start(this)

        // Ask the OS to stop killing us for "battery" — a POS hub must stay listening 24/7.
        requestIgnoreBatteryOptimizations()

        // Quietly check GitHub for a newer release; prompts only if one is available.
        com.sunmi.printhub.update.AppUpdater.check(this, silent = true)

        setupTabs()
        setupConfer()
    }

    // ---------------------------------------------------------------------------
    // Tabs (Print | Confer) — simple visibility toggle, no fragments.
    // ---------------------------------------------------------------------------
    private fun setupTabs() {
        binding.tabPrint.setOnClickListener { showTab(confer = false) }
        binding.tabConfer.setOnClickListener { showTab(confer = true) }
        showTab(confer = false)
    }

    private fun showTab(confer: Boolean) {
        binding.printContainer.visibility = if (confer) View.GONE else View.VISIBLE
        binding.conferContainer.visibility = if (confer) View.VISIBLE else View.GONE
        if (confer) {
            refreshConferUi()
            if (ConferManager.loggedIn) ConferManager.refreshTree()
        }
    }

    // ---------------------------------------------------------------------------
    // Confer wiring
    // ---------------------------------------------------------------------------
    private fun setupConfer() {
        binding.messagesList.layoutManager = LinearLayoutManager(this).apply { stackFromEnd = true }
        binding.messagesList.adapter = conferAdapter
        binding.conferServerHint.text = "Server: " + Hub.settings.conferServerEffective.ifBlank { "(set a Confer server in Settings)" }

        binding.conferLoginBtn.setOnClickListener { doConferLogin() }
        binding.conferLogoutBtn.setOnClickListener {
            ConferManager.logout(); refreshConferUi(); toast("Signed out of Confer")
        }
        binding.conferModeSwitch.setOnCheckedChangeListener { btn, checked ->
            if (btn.isPressed) {
                if (checked && !ConferManager.loggedIn) { btn.isChecked = false; return@setOnCheckedChangeListener }
                ConferManager.setConferMode(checked)
                updateConferStatus()
            }
        }
        binding.newChatBtn.setOnClickListener { promptCreate(isFolder = false) }
        binding.newFolderBtn.setOnClickListener { promptCreate(isFolder = true) }

        binding.chatSpinner.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(p: AdapterView<*>?, v: View?, pos: Int, id: Long) {
                val chatId = conferChatIds.getOrNull(pos) ?: return
                ConferManager.openChat(chatId)
                binding.subscribeCheck.setOnCheckedChangeListener(null)
                binding.subscribeCheck.isChecked = ConferManager.isSubscribed("chat", chatId)
                binding.subscribeCheck.setOnCheckedChangeListener { _, checked ->
                    ConferManager.toggleSubscription("chat", chatId, checked)
                }
            }
            override fun onNothingSelected(p: AdapterView<*>?) {}
        }

        binding.composeInput.filters = arrayOf(InputFilter.LengthFilter(ConferManager.MAX_CHARS))
        binding.composeInput.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {
                binding.charCount.text = "${s?.length ?: 0}/${ConferManager.MAX_CHARS}"
            }
            override fun afterTextChanged(s: Editable?) {}
        })
        binding.sendBtn.setOnClickListener { sendCurrent() }
        binding.imageBtn.setOnClickListener {
            if (selectedChatId() == null) { toast("Pick a chat first"); return@setOnClickListener }
            pickImage.launch("image/*")
        }
    }

    private fun refreshConferUi() {
        val loggedIn = ConferManager.loggedIn
        binding.conferLogin.visibility = if (loggedIn) View.GONE else View.VISIBLE
        binding.conferMain.visibility = if (loggedIn) View.VISIBLE else View.GONE
        if (loggedIn) {
            binding.conferWho.text = "Signed in as ${ConferManager.displayName.ifBlank { ConferManager.username }}"
            binding.conferModeSwitch.isChecked = ConferManager.conferModeOn
            updateConferStatus()
        }
    }

    private fun updateConferStatus() {
        binding.conferStatus.text = when {
            !ConferManager.conferModeOn -> "Print mode"
            ConferManager.connected -> "In Confer mode — connected"
            else -> "Confer mode — connecting…"
        }
    }

    private fun doConferLogin() {
        val user = binding.conferUser.text?.toString()?.trim().orEmpty()
        val pass = binding.conferPass.text?.toString()?.trim().orEmpty()
        if (user.isEmpty() || pass.isEmpty()) { toast("Enter username and password"); return }
        if (Hub.settings.internetDomain.isBlank()) { toast("Set the internet domain in Settings first"); return }
        binding.conferLoginBtn.isEnabled = false
        ConferManager.login(user, pass) { ok, err ->
            binding.conferLoginBtn.isEnabled = true
            if (ok) { binding.conferPass.setText(""); refreshConferUi(); toast("Signed in to Confer") }
            else toast(err ?: "Login failed")
        }
    }

    private fun selectedChatId(): Int? = conferChatIds.getOrNull(binding.chatSpinner.selectedItemPosition)

    private fun promptCreate(isFolder: Boolean) {
        val input = EditText(this).apply { hint = if (isFolder) "Folder name" else "Chat name" }
        AlertDialog.Builder(this)
            .setTitle(if (isFolder) "New folder" else "New chat")
            .setView(input)
            .setPositiveButton("Create") { _, _ ->
                val name = input.text?.toString()?.trim().orEmpty()
                if (name.isEmpty()) return@setPositiveButton
                if (isFolder) ConferManager.createFolder(name, null) else ConferManager.createChat(name, null)
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun sendCurrent() {
        val chatId = selectedChatId() ?: run { toast("Pick a chat first"); return }
        val text = binding.composeInput.text?.toString().orEmpty()
        if (text.isBlank()) return
        ConferManager.sendText(chatId, text)
        binding.composeInput.setText("")
    }

    private fun onImagePicked(uri: Uri) {
        val chatId = selectedChatId() ?: return
        val bmp: Bitmap? = try {
            contentResolver.openInputStream(uri).use { BitmapFactory.decodeStream(it) }
        } catch (t: Throwable) { null }
        if (bmp == null) { toast("Couldn't read that image"); return }
        // Preview before sending (mirrors Watchtower Mobile's send-preview).
        val preview = ImageView(this).apply {
            setImageBitmap(bmp); adjustViewBounds = true; setPadding(24, 24, 24, 24)
        }
        AlertDialog.Builder(this)
            .setTitle("Send this image?")
            .setView(preview)
            .setPositiveButton("Send") { _, _ -> ConferManager.sendImage(chatId, bmp); toast("Image sent") }
            .setNegativeButton("Cancel", null)
            .show()
    }

    // ---- ConferManager.Listener (UI updates on the main thread) ----
    override fun onState() { refreshConferUi() }

    override fun onTree() { populateChatSpinner() }

    override fun onMessages(chatId: Int) {
        if (chatId == ConferManager.activeChatId) {
            conferAdapter.submit(ConferManager.messagesFor(chatId))
            binding.messagesList.scrollToPosition(conferAdapter.itemCount - 1)
        }
    }

    override fun onError(message: String) { toast(message) }

    private fun populateChatSpinner() {
        val folderName = ConferManager.folders.associate { it.id to it.name }
        val chats = ConferManager.chats
        conferChatIds = chats.map { it.id }
        val labels = chats.map { c ->
            val prefix = c.folderId?.let { folderName[it]?.plus(" / ") } ?: ""
            "$prefix${c.name}"
        }
        val prev = binding.chatSpinner.selectedItemPosition
        binding.chatSpinner.adapter =
            ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, labels)
        if (prev in labels.indices) binding.chatSpinner.setSelection(prev)
    }

    override fun onResume() {
        super.onResume()
        ConferManager.listener = this
        if (binding.conferContainer.visibility == View.VISIBLE) refreshConferUi()
    }

    override fun onPause() {
        super.onPause()
        if (ConferManager.listener === this) ConferManager.listener = null
    }

    private fun buildPayload(): PrintPayload = PrintPayload(
        format = formats[binding.formatSpinner.selectedItemPosition],
        printMode = modes[binding.modeSpinner.selectedItemPosition],
        title = binding.titleInput.text?.toString()?.takeIf { it.isNotBlank() },
        text = binding.textInput.text?.toString(),
        barcodeType = binding.barcodeTypeInput.text?.toString()?.takeIf { it.isNotBlank() },
        borderStyle = if (formats[binding.formatSpinner.selectedItemPosition] == PrintFormat.BOXED.wire)
            borderStyles[binding.borderStyleSpinner.selectedItemPosition] else null,
        textSize = if (isTextSizeFormat(formats[binding.formatSpinner.selectedItemPosition]))
            binding.textSizeInput.text?.toString()?.toIntOrNull() else null,
    )

    private fun isTextSizeFormat(fmt: String): Boolean = fmt in setOf(
        PrintFormat.PLAIN.wire, PrintFormat.CENTERED.wire, PrintFormat.BOXED.wire,
        PrintFormat.HEADER_BODY.wire, PrintFormat.LIST.wire,
    )

    private fun renderPreview() {
        val payload = buildPayload()
        val width = Hub.settings.printWidthPx
        io.execute {
            try {
                val bmp = ReceiptRenderer.render(payload, width)
                main.post { binding.previewImage.setImageBitmap(bmp) }
            } catch (t: Throwable) {
                main.post { toast("Preview error: ${t.message}") }
            }
        }
    }

    private fun doPrint() {
        val payload = buildPayload()
        io.execute {
            val result = PrintDispatcher.dispatch(payload, JobSource.LOCAL, requirePassword = false)
            main.post {
                toast("Print ${result.status.wire}" + (result.error?.let { ": $it" } ?: ""))
            }
        }
    }

    /**
     * One-time nudge to whitelist the app from Doze / battery optimization. Without this the OS
     * can freeze or kill the foreground service in the background; with it the print listener
     * stays alive. If already whitelisted (or the dialog is unavailable) this is a no-op.
     */
    private fun requestIgnoreBatteryOptimizations() {
        if (android.os.Build.VERSION.SDK_INT < android.os.Build.VERSION_CODES.M) return
        try {
            val pm = getSystemService(POWER_SERVICE) as android.os.PowerManager
            if (pm.isIgnoringBatteryOptimizations(packageName)) return
            val i = android.content.Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
                .setData(android.net.Uri.parse("package:$packageName"))
            startActivity(i)
        } catch (_: Throwable) { /* some ROMs lack this screen; the service still runs */ }
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_LONG).show()

    private fun showHelpDialog() {
        val msg = """
            Inline tags — put each on its own line inside the Text field (text formats only):

            @#@divider="-="
              Repeats the pattern to fill the line width. The pattern can be any
              characters, e.g. "=", "*~", "-·-". (@#@devider is also accepted.)

            @#@cats
              Inserts a random simple ASCII cat.

            Browse more art in the galleries below, then paste any piece straight into Text.
        """.trimIndent()

        androidx.appcompat.app.AlertDialog.Builder(this)
            .setTitle("Help — inline tags")
            .setMessage(msg)
            .setPositiveButton("Divider gallery") { _, _ ->
                openUrl("https://www.asciiart.eu/ascii-dividers/gallery")
            }
            .setNeutralButton("Cat gallery") { _, _ ->
                openUrl("https://www.asciiart.eu/animals/cats")
            }
            .setNegativeButton("Close", null)
            .show()
    }

    private fun openUrl(url: String) {
        try {
            startActivity(android.content.Intent(android.content.Intent.ACTION_VIEW, android.net.Uri.parse(url)))
        } catch (t: Throwable) {
            toast("No browser available")
        }
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(com.sunmi.printhub.R.menu.main_menu, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            com.sunmi.printhub.R.id.action_settings -> {
                startActivity(android.content.Intent(this, SettingsActivity::class.java)); true
            }
            com.sunmi.printhub.R.id.action_job_log -> {
                startActivity(android.content.Intent(this, JobLogActivity::class.java)); true
            }
            com.sunmi.printhub.R.id.action_logs -> {
                startActivity(android.content.Intent(this, LogsActivity::class.java)); true
            }
            com.sunmi.printhub.R.id.action_update -> {
                com.sunmi.printhub.update.AppUpdater.check(this, silent = false); true
            }
            com.sunmi.printhub.R.id.action_help -> {
                showHelpDialog(); true
            }
            else -> super.onOptionsItemSelected(item)
        }
    }
}
