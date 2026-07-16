package com.watchtower.mobile

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.util.Log
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.core.content.FileProvider
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * In-app updater. This repo hosts two apps, so we scan all releases and pick the latest whose
 * asset is named `WatchtowerMobile-*.apk`, never the Sunmi Print Hub release.
 */
object AppUpdater {
    private const val TAG = "AppUpdater"
    private const val RELEASES = "https://api.github.com/repos/PzmuV1517/LocalPosPrinter/releases?per_page=30"
    private const val ASSET_PREFIX = "WatchtowerMobile-"
    private val client = OkHttpClient.Builder().callTimeout(30, TimeUnit.SECONDS).build()

    private data class Rel(val version: String, val url: String)

    fun check(activity: Activity, silent: Boolean) {
        Thread {
            try {
                val arr = JSONArray(get(RELEASES))
                var found: Rel? = null
                outer@ for (i in 0 until arr.length()) {
                    val rel = arr.getJSONObject(i)
                    if (rel.optBoolean("draft") || rel.optBoolean("prerelease")) continue
                    val assets = rel.getJSONArray("assets")
                    for (j in 0 until assets.length()) {
                        val a = assets.getJSONObject(j)
                        val name = a.getString("name")
                        if (name.startsWith(ASSET_PREFIX) && name.endsWith(".apk", true)) {
                            val ver = rel.getString("tag_name").removePrefix("mobile-").removePrefix("v")
                            found = Rel(ver, a.getString("browser_download_url"))
                            break@outer
                        }
                    }
                }
                val rel = found
                if (rel == null) { if (!silent) toast(activity, "No mobile release found"); return@Thread }
                if (!isNewer(rel.version, BuildConfig.VERSION_NAME)) {
                    if (!silent) toast(activity, "Up to date (${BuildConfig.VERSION_NAME})")
                    return@Thread
                }
                activity.runOnUiThread { prompt(activity, rel) }
            } catch (t: Throwable) {
                Log.w(TAG, "update check failed", t)
                if (!silent) toast(activity, "Update check failed")
            }
        }.apply { isDaemon = true }.start()
    }

    private fun prompt(activity: Activity, rel: Rel) {
        AlertDialog.Builder(activity)
            .setTitle("Update available")
            .setMessage("Watchtower ${rel.version} is available. Download and install?")
            .setPositiveButton("Update") { _, _ -> download(activity, rel) }
            .setNegativeButton("Later", null)
            .show()
    }

    private fun download(activity: Activity, rel: Rel) {
        if (Build.VERSION.SDK_INT >= 26 && !activity.packageManager.canRequestPackageInstalls()) {
            activity.startActivity(
                Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${activity.packageName}")))
            toast(activity, "Allow installs, then check again")
            return
        }
        toast(activity, "Downloading ${rel.version}…")
        Thread {
            try {
                val dir = File(activity.cacheDir, "updates").apply { mkdirs() }
                val apk = File(dir, "watchtower-update.apk")
                client.newCall(Request.Builder().url(rel.url).build()).execute().use { resp ->
                    if (!resp.isSuccessful) { toast(activity, "Download failed (${resp.code})"); return@Thread }
                    apk.outputStream().use { out -> resp.body?.byteStream()?.copyTo(out) }
                }
                val uri = FileProvider.getUriForFile(activity, "${activity.packageName}.fileprovider", apk)
                activity.startActivity(Intent(Intent.ACTION_VIEW).apply {
                    setDataAndType(uri, "application/vnd.android.package-archive")
                    addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
                })
            } catch (t: Throwable) {
                Log.e(TAG, "download failed", t)
                toast(activity, "Update failed: ${t.message}")
            }
        }.apply { isDaemon = true }.start()
    }

    private fun isNewer(remote: String, local: String): Boolean {
        val r = remote.split(".")
        val l = local.split(".")
        for (i in 0 until maxOf(r.size, l.size)) {
            val rv = r.getOrNull(i)?.toIntOrNull() ?: 0
            val lv = l.getOrNull(i)?.toIntOrNull() ?: 0
            if (rv != lv) return rv > lv
        }
        return false
    }

    private fun get(url: String): String {
        client.newCall(Request.Builder().url(url).header("Accept", "application/vnd.github+json").build())
            .execute().use {
                if (!it.isSuccessful) throw IOException("HTTP ${it.code}")
                return it.body?.string() ?: throw IOException("empty response")
            }
    }

    private fun toast(activity: Activity, msg: String) =
        activity.runOnUiThread { Toast.makeText(activity, msg, Toast.LENGTH_SHORT).show() }
}
