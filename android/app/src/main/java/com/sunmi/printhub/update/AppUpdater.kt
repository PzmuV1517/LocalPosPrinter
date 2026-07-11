package com.sunmi.printhub.update

import android.app.Activity
import android.app.ProgressDialog
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.Log
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.core.content.FileProvider
import com.sunmi.printhub.BuildConfig
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * In-app updater: checks GitHub Releases for a newer version, downloads the APK, and hands it
 * to the system installer — so the end user never has to open a browser or fetch files.
 */
object AppUpdater {

    private const val TAG = "AppUpdater"
    private const val LATEST_API =
        "https://api.github.com/repos/PzmuV1517/LocalPosPrinter/releases/latest"

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    private data class Release(val version: String, val apkUrl: String)

    /**
     * @param silent when true, says nothing if already up to date or the check fails
     *               (used for the automatic check on launch).
     */
    fun check(activity: Activity, silent: Boolean) {
        io.execute {
            val release = try {
                fetchLatest()
            } catch (t: Throwable) {
                Log.w(TAG, "update check failed: ${t.message}")
                null
            }
            main.post {
                if (activity.isFinishing) return@post
                when {
                    release == null -> if (!silent) toast(activity, "Update check failed")
                    isNewer(release.version, BuildConfig.VERSION_NAME) -> promptUpdate(activity, release)
                    else -> if (!silent) toast(activity, "You're on the latest version (${BuildConfig.VERSION_NAME})")
                }
            }
        }
    }

    private fun fetchLatest(): Release {
        val req = Request.Builder()
            .url(LATEST_API)
            .header("Accept", "application/vnd.github+json")
            .header("User-Agent", "SunmiPrintHub")
            .build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) throw IOException("HTTP ${resp.code}")
            val json = JSONObject(resp.body?.string() ?: throw IOException("empty body"))
            val tag = json.getString("tag_name")
            val assets = json.getJSONArray("assets")
            var apkUrl = ""
            for (i in 0 until assets.length()) {
                val a = assets.getJSONObject(i)
                if (a.getString("name").endsWith(".apk", ignoreCase = true)) {
                    apkUrl = a.getString("browser_download_url")
                    break
                }
            }
            if (apkUrl.isEmpty()) throw IOException("latest release has no APK asset")
            return Release(tag.removePrefix("v").removePrefix("V"), apkUrl)
        }
    }

    /** True if [remote] is a strictly higher dotted version than [local]. */
    private fun isNewer(remote: String, local: String): Boolean {
        val r = remote.split(".", "-").mapNotNull { it.toIntOrNull() }
        val l = local.split(".", "-").mapNotNull { it.toIntOrNull() }
        for (i in 0 until maxOf(r.size, l.size)) {
            val rv = r.getOrElse(i) { 0 }
            val lv = l.getOrElse(i) { 0 }
            if (rv != lv) return rv > lv
        }
        return false
    }

    private fun promptUpdate(activity: Activity, release: Release) {
        AlertDialog.Builder(activity)
            .setTitle("Update available")
            .setMessage("Version ${release.version} is available (you have ${BuildConfig.VERSION_NAME}).\n\nDownload and install now?")
            .setPositiveButton("Update") { _, _ -> download(activity, release) }
            .setNegativeButton("Later", null)
            .show()
    }

    @Suppress("DEPRECATION")
    private fun download(activity: Activity, release: Release) {
        val dialog = ProgressDialog(activity).apply {
            setTitle("Downloading update")
            setProgressStyle(ProgressDialog.STYLE_HORIZONTAL)
            isIndeterminate = false
            max = 100
            setCancelable(false)
            show()
        }
        io.execute {
            val dir = File(activity.cacheDir, "updates").apply { mkdirs() }
            val apk = File(dir, "update.apk")
            try {
                client.newCall(Request.Builder().url(release.apkUrl).build()).execute().use { resp ->
                    if (!resp.isSuccessful) throw IOException("HTTP ${resp.code}")
                    val body = resp.body ?: throw IOException("empty body")
                    val total = body.contentLength()
                    body.byteStream().use { input ->
                        apk.outputStream().use { output ->
                            val buf = ByteArray(64 * 1024)
                            var read: Int
                            var done = 0L
                            while (input.read(buf).also { read = it } != -1) {
                                output.write(buf, 0, read)
                                done += read
                                if (total > 0) {
                                    val pct = (done * 100 / total).toInt()
                                    main.post { dialog.progress = pct }
                                }
                            }
                        }
                    }
                }
                main.post {
                    dialog.dismiss()
                    if (!activity.isFinishing) install(activity, apk)
                }
            } catch (t: Throwable) {
                Log.e(TAG, "download failed", t)
                main.post {
                    dialog.dismiss()
                    if (!activity.isFinishing) toast(activity, "Download failed: ${t.message}")
                }
            }
        }
    }

    private fun install(activity: Activity, apk: File) {
        // API 26+ requires the user to allow "install unknown apps" for this app.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O &&
            !activity.packageManager.canRequestPackageInstalls()
        ) {
            AlertDialog.Builder(activity)
                .setTitle("Allow app installs")
                .setMessage("To install updates, allow this app to install unknown apps, then tap Update again.")
                .setPositiveButton("Open settings") { _, _ ->
                    try {
                        activity.startActivity(
                            Intent(
                                Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                                Uri.parse("package:${activity.packageName}")
                            )
                        )
                    } catch (t: Throwable) {
                        toast(activity, "Couldn't open settings")
                    }
                }
                .setNegativeButton("Cancel", null)
                .show()
            return
        }

        try {
            val uri = FileProvider.getUriForFile(activity, "${activity.packageName}.fileprovider", apk)
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, "application/vnd.android.package-archive")
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            activity.startActivity(intent)
        } catch (t: Throwable) {
            Log.e(TAG, "install intent failed", t)
            toast(activity, "Could not launch installer: ${t.message}")
        }
    }

    private fun toast(activity: Activity, msg: String) =
        Toast.makeText(activity, msg, Toast.LENGTH_LONG).show()
}
