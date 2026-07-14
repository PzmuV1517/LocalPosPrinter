package com.watchtower.mobile

import android.annotation.SuppressLint
import android.os.Bundle
import android.view.Menu
import android.view.MenuItem
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import com.watchtower.mobile.databinding.ActivityMainBinding

/**
 * The mobile Watchtower app is a WebView over the live dashboard: full feature parity with the
 * web, the same aesthetic, and always in sync with the site. Login persists across restarts via
 * the WebView's localStorage (DOM storage). The web session token is mirrored to native prefs so
 * the share-to-print flow can authenticate.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var store: Store

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = Store(this)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)

        val wv = binding.webview
        wv.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true       // localStorage -> persistent login
            databaseEnabled = true
            cacheMode = WebSettings.LOAD_DEFAULT
            mediaPlaybackRequiresUserGesture = false
            loadWithOverviewMode = true
            useWideViewPort = true
        }
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(wv, true)
        wv.addJavascriptInterface(Bridge(), "Android")
        wv.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView, url: String) {
                // Keep the native token in sync with the web session (used by share-to-print).
                view.evaluateJavascript(
                    "(function(){try{if(!window.__wtSync){window.__wtSync=setInterval(function(){" +
                        "var t=localStorage.getItem('wt_token')||'';" +
                        "if(t!==window.__wtLast){window.__wtLast=t;Android.saveToken(t);}},2000);}}catch(e){}})();",
                    null,
                )
            }
        }
        if (savedInstanceState == null) wv.loadUrl(store.serverUrl)
        AppUpdater.check(this, silent = true)
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (binding.webview.canGoBack()) binding.webview.goBack() else super.onBackPressed()
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.main, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        when (item.itemId) {
            R.id.action_reload -> binding.webview.reload()
            R.id.action_server -> editServerUrl()
            R.id.action_update -> AppUpdater.check(this, silent = false)
            else -> return super.onOptionsItemSelected(item)
        }
        return true
    }

    private fun editServerUrl() {
        val input = EditText(this).apply { setText(store.serverUrl) }
        AlertDialog.Builder(this)
            .setTitle("Server URL")
            .setView(input)
            .setPositiveButton("Save") { _, _ ->
                store.serverUrl = input.text.toString()
                binding.webview.loadUrl(store.serverUrl)
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private inner class Bridge {
        @JavascriptInterface
        fun saveToken(t: String?) {
            store.token = t ?: ""
        }
    }
}
