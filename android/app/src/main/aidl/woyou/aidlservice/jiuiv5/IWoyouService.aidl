// IWoyouService.aidl — Sunmi Woyou (jiuiv5) thermal printer AIDL service.
//
// ⚠️ METHOD ORDER IS CONTRACTUAL. Binder assigns each method a transaction id by its
// position in this interface, and the on-device service (package woyou.aidlservice.jiuiv5,
// what RawBT calls DRIVER_AIDL_WOYOU_JIUIV5) was compiled from Sunmi's canonical AIDL.
// If the order here differs, a call like printBitmap() is dispatched to the WRONG method on
// the printer and silently fails. Keep this order byte-for-byte with Sunmi's — do NOT
// reorder, insert, or delete any method at or above printBitmap.
package woyou.aidlservice.jiuiv5;

import woyou.aidlservice.jiuiv5.ICallback;
import android.graphics.Bitmap;

interface IWoyouService {
    void printerInit(in ICallback callback);                                                    // 1
    void printerSelfChecking(in ICallback callback);                                             // 2
    String getPrinterSerialNo();                                                                 // 3
    String getPrinterVersion();                                                                  // 4
    String getPrinterModal();                                                                    // 5
    int getPrintedLength();                                                                      // 6
    void lineWrap(int n, in ICallback callback);                                                 // 7
    void sendRAWData(in byte[] data, in ICallback callback);                                     // 8
    void setAlignment(int alignment, in ICallback callback);                                     // 9
    void setFontName(String typeface, in ICallback callback);                                    // 10
    void setFontSize(float fontsize, in ICallback callback);                                     // 11
    void printText(String text, in ICallback callback);                                          // 12
    void printTextWithFont(String text, String typeface, float fontsize, in ICallback callback);// 13
    void printColumnsText(in String[] colsTextArr, in int[] colsWidthArr, in int[] colsAlign, in ICallback callback); // 14
    void printColumnsString(in String[] colsTextArr, in int[] colsWidthArr, in int[] colsAlign, in ICallback callback); // 15
    void printBitmap(in Bitmap bitmap, in ICallback callback);                                   // 16
    void printBarCode(String data, int symbology, int height, int width, int textposition, in ICallback callback); // 17
    void printQRCode(String data, int modulesize, int errorlevel, in ICallback callback);        // 18
    void printOriginalText(String text, in ICallback callback);                                  // 19
    void commitPrinterBuffer();                                                                  // 20
    void enterPrinterBuffer(boolean clean);                                                      // 21
    void exitPrinterBuffer(boolean commit);                                                      // 22
    void cutPaper(in ICallback callback);                                                        // 23
    int getCutPaperTimes();                                                                      // 24
    void openDrawer(in ICallback callback);                                                      // 25
    int getOpenDrawerTimes();                                                                    // 26
    void printBitmapCustom(in Bitmap bitmap, int type, in ICallback callback);                   // 27
    // Label printing (V2 Pro Label version); appended by later firmware. Only used in label mode.
    void labelLocate();                                                                          // 28
    void labelOutput();                                                                          // 29
}
