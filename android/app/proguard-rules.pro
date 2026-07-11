# Keep Sunmi printer library interfaces.
-keep class com.sunmi.peripheral.printer.** { *; }
-keep class woyou.aidlservice.jiuiv5.** { *; }
-dontwarn com.sunmi.peripheral.printer.**
# Paho.
-keep class org.eclipse.paho.** { *; }
-dontwarn org.eclipse.paho.**
# okhttp / okio.
-dontwarn okhttp3.**
-dontwarn okio.**
