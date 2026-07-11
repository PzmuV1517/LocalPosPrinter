// IWoyouService.aidl — Sunmi V2 (Woyou) thermal printer AIDL service.
// Signatures match the classic published SUNMI OS interface for widest compatibility.
package woyou.aidlservice.jiuiv5;

import woyou.aidlservice.jiuiv5.ICallback;
import android.graphics.Bitmap;

interface IWoyouService {

    // --- lifecycle / info ---
    void printerInit(in ICallback callback);
    void printerSelfChecking(in ICallback callback);
    String getPrinterSerialNo();
    String getPrinterModal();
    String getPrinterVersion();
    void updatePrinterState();
    int updatePrinterStateInner();

    // --- raw / text ---
    void sendRAWData(in byte[] data, in ICallback callback);
    void setAlignment(int alignment, in ICallback callback);
    void setFontName(String typeface, in ICallback callback);
    void setFontSize(float fontsize, in ICallback callback);
    void printText(String text, in ICallback callback);
    void printTextWithFont(String text, String typeface, float fontsize, in ICallback callback);
    void printOriginalText(String text, in ICallback callback);
    void printColumnsText(in String[] colsTextArr, in int[] colsWidthArr, in int[] colsAlign, in ICallback callback);
    void printColumnsString(in String[] colsTextArr, in int[] colsWidthArr, in int[] colsAlign, in ICallback callback);

    // --- images ---
    void printBitmap(in Bitmap bitmap, in ICallback callback);
    void printBitmapCustom(in Bitmap bitmap, int type, in ICallback callback);

    // --- barcode / qr ---
    void printBarCode(String data, int symbology, int height, int width, int textposition, in ICallback callback);
    void printQRCode(String data, int modulesize, int errorlevel, in ICallback callback);

    // --- paper / buffer control ---
    void lineWrap(int n, in ICallback callback);
    void cutPaper(in ICallback callback);
    void enterPrinterBuffer(boolean clean);
    void commitPrinterBuffer();
    void exitPrinterBuffer(boolean commit);
    void commitPrinterBufferWithCallback(in ICallback callback);
    void exitPrinterBufferWithCallback(boolean commit, in ICallback callback);

    // --- label printing (V2 Pro Label version; may be absent on non-label firmware) ---
    void labelLocate();
    void labelOutput();
}
