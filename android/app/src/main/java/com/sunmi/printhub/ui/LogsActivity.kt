package com.sunmi.printhub.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.Process
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.sunmi.printhub.databinding.ActivityLogsBinding
import java.util.concurrent.Executors

/**
 * In-app log viewer — reads this app's own logcat output so diagnosing prints/connections
 * doesn't need a computer. On Android an app can read its own log entries without any
 * permission, so no READ_LOGS is required.
 */
class LogsActivity : AppCompatActivity() {

    private lateinit var binding: ActivityLogsBinding
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityLogsBinding.inflate(layoutInflater)
        setContentView(binding.root)
        title = "Logs"
        supportActionBar?.setDisplayHomeAsUpEnabled(true)

        binding.refreshBtn.setOnClickListener { load() }
        binding.clearBtn.setOnClickListener { clear() }
        binding.copyBtn.setOnClickListener { copy() }
    }

    override fun onResume() {
        super.onResume()
        load()
    }

    private fun load() {
        io.execute {
            val text = readLogcat()
            main.post {
                binding.logText.text = text
                binding.logScroll.post { binding.logScroll.fullScroll(View.FOCUS_DOWN) }
            }
        }
    }

    /** Dump the last ~1000 lines for this process (falls back to unfiltered if --pid is unsupported). */
    private fun readLogcat(): String {
        val pid = Process.myPid().toString()
        val commands = listOf(
            arrayOf("logcat", "-d", "-v", "time", "-t", "1000", "--pid=$pid"),
            arrayOf("logcat", "-d", "-v", "time", "-t", "1000"),
        )
        for (cmd in commands) {
            try {
                val proc = Runtime.getRuntime().exec(cmd)
                val out = proc.inputStream.bufferedReader().readText()
                proc.destroy()
                if (out.isNotBlank()) return out
            } catch (_: Throwable) {
            }
        }
        return "(no log output — try Refresh, or trigger a print first)"
    }

    private fun clear() {
        io.execute {
            try {
                Runtime.getRuntime().exec(arrayOf("logcat", "-c")).waitFor()
            } catch (_: Throwable) {
            }
            load()
        }
    }

    private fun copy() {
        val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        cm.setPrimaryClip(ClipData.newPlainText("printhub-logs", binding.logText.text))
        Toast.makeText(this, "Logs copied to clipboard", Toast.LENGTH_SHORT).show()
    }

    override fun onSupportNavigateUp(): Boolean {
        finish(); return true
    }
}
