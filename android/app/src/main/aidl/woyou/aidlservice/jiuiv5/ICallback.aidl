// ICallback.aidl — Sunmi Woyou printer service async callback.
package woyou.aidlservice.jiuiv5;

interface ICallback {
    void onRunResult(boolean isSuccess);
    void onReturnString(String result);
    void onRaiseException(int code, String msg);
    void onPrintResult(int code, String msg);
}
