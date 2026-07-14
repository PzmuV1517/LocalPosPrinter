package com.sunmi.printhub.ui

import android.annotation.SuppressLint
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.Settings as AndroidSettings
import android.widget.ArrayAdapter
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import com.sunmi.printhub.core.Hub
import com.sunmi.printhub.databinding.ActivitySettingsBinding
import com.sunmi.printhub.service.PrintHubService
import org.json.JSONObject

class SettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySettingsBinding
    private val modes = listOf("receipt", "label")

    // QR scanner (zxing-embedded handles the camera + permission prompt).
    private val scanLauncher = registerForActivityResult(ScanContract()) { result ->
        result.contents?.let { applyPairing(it) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Hub.init(this)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)
        title = getString(com.sunmi.printhub.R.string.settings_title)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)

        binding.defaultModeSpinner.adapter =
            ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, modes)

        loadInto()

        binding.regenButton.setOnClickListener {
            binding.accessPasswordInput.setText(Hub.settings.regenerateAccessPassword())
            toast("New access password generated")
        }
        binding.batteryButton.setOnClickListener { requestIgnoreBattery() }
        binding.saveButton.setOnClickListener { saveAndRestart() }
        binding.scanQrButton.setOnClickListener { startScan() }

        updateInternetStatus()
    }

    private fun startScan() {
        val opts = ScanOptions()
            .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
            .setPrompt("Scan the Watchtower pairing QR")
            .setBeepEnabled(false)
            .setOrientationLocked(false)
        scanLauncher.launch(opts)
    }

    /** Parse a Watchtower pairing QR ({"url","device_id","secret"}) and fill the fields. */
    private fun applyPairing(contents: String) {
        try {
            val obj = JSONObject(contents)
            val url = obj.optString("url").trim()
            val deviceId = obj.optString("device_id").trim()
            val secret = obj.optString("secret").trim()
            if (secret.isEmpty() || deviceId.isEmpty()) {
                toast("Not a valid pairing QR")
                return
            }
            if (url.isNotEmpty()) {
                binding.internetDomainInput.setText(url)
                binding.internetEnabled.isChecked = true
            }
            binding.deviceIdInput.setText(deviceId)
            binding.deviceSecretInput.setText(secret)
            toast("Paired '$deviceId' — review and Save")
        } catch (t: Throwable) {
            toast("Couldn't read pairing QR")
        }
    }

    override fun onResume() {
        super.onResume()
        updateInternetStatus()
    }

    private fun loadInto() {
        val s = Hub.settings
        binding.accessPasswordInput.setText(s.accessPassword)
        binding.widthInput.setText(s.printWidthPx.toString())
        binding.defaultModeSpinner.setSelection(modes.indexOf(s.defaultPrintMode).coerceAtLeast(0))

        binding.httpEnabled.isChecked = s.httpEnabled
        binding.httpPortInput.setText(s.httpPort.toString())

        binding.mqttEnabled.isChecked = s.mqttEnabled
        binding.mqttHostInput.setText(s.mqttHost)
        binding.mqttPortInput.setText(s.mqttPort.toString())
        binding.mqttUserInput.setText(s.mqttUser)
        binding.mqttPassInput.setText(s.mqttPass)
        binding.mqttTls.isChecked = s.mqttTls
        binding.mqttPrefixInput.setText(s.mqttPrefix)

        binding.internetEnabled.isChecked = s.internetEnabled
        binding.internetDomainInput.setText(s.internetDomain)

        binding.conferServerInput.setText(s.conferServer)

        binding.deviceIdInput.setText(s.deviceId)
        binding.deviceSecretInput.setText(s.deviceSecret)

        binding.autoStart.isChecked = s.autoStart
    }

    private fun saveAndRestart() {
        val s = Hub.settings
        s.accessPassword = binding.accessPasswordInput.text.toString().trim().ifEmpty { s.accessPassword }
        s.printWidthPx = binding.widthInput.text.toString().toIntOrNull()?.coerceIn(64, 1024) ?: 384
        s.defaultPrintMode = modes[binding.defaultModeSpinner.selectedItemPosition]

        s.httpEnabled = binding.httpEnabled.isChecked
        s.httpPort = binding.httpPortInput.text.toString().toIntOrNull()?.coerceIn(1, 65535) ?: 8080

        s.mqttEnabled = binding.mqttEnabled.isChecked
        s.mqttHost = binding.mqttHostInput.text.toString().trim()
        s.mqttPort = binding.mqttPortInput.text.toString().toIntOrNull()?.coerceIn(1, 65535) ?: 1883
        s.mqttUser = binding.mqttUserInput.text.toString()
        s.mqttPass = binding.mqttPassInput.text.toString()
        s.mqttTls = binding.mqttTls.isChecked
        s.mqttPrefix = binding.mqttPrefixInput.text.toString().trim()

        s.internetEnabled = binding.internetEnabled.isChecked
        s.internetDomain = binding.internetDomainInput.text.toString().trim()

        s.conferServer = binding.conferServerInput.text.toString().trim()

        s.deviceId = binding.deviceIdInput.text.toString().trim().ifEmpty { s.deviceId }
        s.deviceSecret = binding.deviceSecretInput.text.toString().trim()

        s.autoStart = binding.autoStart.isChecked

        PrintHubService.restart(this)
        toast("Saved — services restarted")
    }

    private fun updateInternetStatus() {
        val connected = Hub.internetConnected
        binding.internetStatus.text = "Status: " + if (connected) "connected" else "disconnected"
    }

    @SuppressLint("BatteryLife")
    private fun requestIgnoreBattery() {
        try {
            startActivity(
                Intent(
                    AndroidSettings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:$packageName")
                )
            )
        } catch (t: Throwable) {
            toast("Battery-optimization settings not available")
        }
    }

    override fun onSupportNavigateUp(): Boolean {
        finish(); return true
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
}
