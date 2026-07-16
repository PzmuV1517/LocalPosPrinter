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
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
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
        binding.menuButton.setOnClickListener { showMenu(it) }

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

        // Session persistence: the native token is the source of truth. Before the web app's
        // scripts run, seed localStorage from it, so login survives even if the WebView drops
        // its own localStorage between launches.
        if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
            WebViewCompat.addDocumentStartJavaScript(
                wv,
                "(function(){try{var t=(window.Android&&Android.getToken)?Android.getToken():'';" +
                    "if(t&&!localStorage.getItem('wt_token'))localStorage.setItem('wt_token',t);}catch(e){}})();",
                setOf("*"),
            )
        }
        wv.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView, url: String) {
                // Mirror the web session token back to native (used on next launch + share-print).
                view.evaluateJavascript(
                    "(function(){try{if(!window.__wtSync){window.__wtSync=setInterval(function(){" +
                        "var t=localStorage.getItem('wt_token')||'';" +
                        "if(t!==window.__wtLast){window.__wtLast=t;Android.saveToken(t);}},2000);}}catch(e){}})();",
                    null,
                )
            }
        }
        wv.loadUrl(store.serverUrl)
        AppUpdater.check(this, silent = true)
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (binding.webview.canGoBack()) binding.webview.goBack() else super.onBackPressed()
    }

    private fun showMenu(anchor: android.view.View) {
        android.widget.PopupMenu(this, anchor).apply {
            menuInflater.inflate(R.menu.main, menu)
            setOnMenuItemClickListener { handleMenu(it.itemId) }
            show()
        }
    }

    private fun handleMenu(id: Int): Boolean {
        when (id) {
            R.id.action_reload -> binding.webview.reload()
            R.id.action_server -> editServerUrl()
            R.id.action_update -> AppUpdater.check(this, silent = false)
            else -> return false
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

        @JavascriptInterface
        fun getToken(): String = store.token
    }
}
