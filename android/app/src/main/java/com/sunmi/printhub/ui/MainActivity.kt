package com.sunmi.printhub.ui

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.sunmi.printhub.core.Hub
import com.sunmi.printhub.core.PrintDispatcher
import com.sunmi.printhub.databinding.ActivityMainBinding
import com.sunmi.printhub.db.JobSource
import com.sunmi.printhub.model.PrintFormat
import com.sunmi.printhub.model.PrintPayload
import com.sunmi.printhub.render.ReceiptRenderer
import com.sunmi.printhub.service.PrintHubService
import java.util.concurrent.Executors

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

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
            val result = PrintDispatcher.dispatch(payload, JobSource.LOCAL, requireCode = false)
            main.post {
                toast("Print ${result.status.wire}" + (result.error?.let { ": $it" } ?: ""))
            }
        }
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
            com.sunmi.printhub.R.id.action_help -> {
                showHelpDialog(); true
            }
            else -> super.onOptionsItemSelected(item)
        }
    }
}
