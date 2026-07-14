# Keep the JS bridge interface (called from WebView JavaScript).
-keepclassmembers class com.watchtower.mobile.** {
    @android.webkit.JavascriptInterface <methods>;
}
-dontwarn okhttp3.**
-dontwarn okio.**
