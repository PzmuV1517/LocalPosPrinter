# Keep Woyou AIDL interfaces.
-keep class woyou.aidlservice.jiuiv5.** { *; }
# Paho.
-keep class org.eclipse.paho.** { *; }
-dontwarn org.eclipse.paho.**
# okhttp / okio.
-dontwarn okhttp3.**
-dontwarn okio.**
