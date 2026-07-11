package com.sunmi.printhub.model

import com.google.gson.Gson
import com.google.gson.annotations.SerializedName

/** Print formats supported across all channels. Wire values are lowercase. */
enum class PrintFormat(val wire: String) {
    PLAIN("plain"),
    CENTERED("centered"),
    BOXED("boxed"),
    HEADER_BODY("header_body"),
    BANNER("banner"),
    LIST("list"),
    IMAGE("image"),
    BARCODE("barcode"),
    QRCODE("qrcode");

    companion object {
        fun from(value: String?): PrintFormat =
            values().firstOrNull { it.wire.equals(value?.trim(), ignoreCase = true) } ?: PLAIN
    }
}

enum class PrintMode(val wire: String) {
    RECEIPT("receipt"),
    LABEL("label");

    companion object {
        fun from(value: String?): PrintMode =
            values().firstOrNull { it.wire.equals(value?.trim(), ignoreCase = true) } ?: RECEIPT
    }
}

enum class ImagePosition(val wire: String) {
    TOP("top"),
    BOTTOM("bottom");

    companion object {
        fun from(value: String?): ImagePosition =
            values().firstOrNull { it.wire.equals(value?.trim(), ignoreCase = true) } ?: TOP
    }
}

data class ListItem(
    @SerializedName("label") val label: String? = null,
    @SerializedName("value") val value: String? = null,
)

/**
 * The shared payload schema used by HTTP, MQTT and the internet listener.
 * Parsed with Gson; unknown fields are ignored.
 */
data class PrintPayload(
    @SerializedName("code") val code: String? = null,
    @SerializedName("format") val format: String? = null,
    @SerializedName("print_mode") val printMode: String? = null,
    @SerializedName("title") val title: String? = null,
    @SerializedName("text") val text: String? = null,
    @SerializedName("barcode_type") val barcodeType: String? = null,
    @SerializedName("border_style") val borderStyle: String? = null,
    @SerializedName("text_size") val textSize: Int? = null,
    @SerializedName("items") val items: List<ListItem>? = null,
    @SerializedName("image") val image: String? = null,
    @SerializedName("image_raw_bitmap") val imageRawBitmap: String? = null,
    @SerializedName("image_position") val imagePosition: String? = null,
) {
    val formatEnum: PrintFormat get() = PrintFormat.from(format)
    val printModeEnum: PrintMode get() = PrintMode.from(printMode)
    val imagePositionEnum: ImagePosition get() = ImagePosition.from(imagePosition)

    /** image_raw_bitmap wins when both are present (more explicit / deliberate path). */
    val hasRawBitmap: Boolean get() = !imageRawBitmap.isNullOrBlank()
    val hasStandardImage: Boolean get() = !hasRawBitmap && !image.isNullOrBlank()

    companion object {
        private val gson = Gson()

        fun parse(json: String): PrintPayload = gson.fromJson(json, PrintPayload::class.java)
    }
}
