package com.sunmi.printhub.db

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper

/** Where a job came from. */
enum class JobSource(val wire: String) {
    LOCAL("local"), HTTP("http"), MQTT("mqtt"), INTERNET("internet");

    companion object {
        fun from(v: String?) = values().firstOrNull { it.wire == v } ?: LOCAL
    }
}

/** Lifecycle of a job. */
enum class JobStatus(val wire: String) {
    QUEUED("queued"), PRINTING("printing"), SUCCESS("success"), FAILED("failed"), REJECTED("rejected");

    companion object {
        fun from(v: String?) = values().firstOrNull { it.wire == v } ?: QUEUED
    }
}

data class JobLogEntry(
    val id: Long,
    val source: JobSource,
    val timestamp: Long,
    val format: String,
    val title: String?,
    val text: String?,
    val status: JobStatus,
    val error: String?,
)

/**
 * SQLite-backed log of every print job from all four sources.
 */
class JobLog(context: Context) : SQLiteOpenHelper(context.applicationContext, DB_NAME, null, DB_VERSION) {

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE $TABLE (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                ts INTEGER NOT NULL,
                format TEXT,
                title TEXT,
                text TEXT,
                status TEXT NOT NULL,
                error TEXT
            )
            """.trimIndent()
        )
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        db.execSQL("DROP TABLE IF EXISTS $TABLE")
        onCreate(db)
    }

    /** Insert a new job row and return its id. */
    fun insert(
        source: JobSource,
        format: String,
        title: String?,
        text: String?,
        status: JobStatus,
        error: String? = null,
    ): Long {
        val cv = ContentValues().apply {
            put("source", source.wire)
            put("ts", System.currentTimeMillis())
            put("format", format)
            put("title", title)
            put("text", text?.take(2000))
            put("status", status.wire)
            put("error", error)
        }
        return writableDatabase.insert(TABLE, null, cv)
    }

    fun updateStatus(id: Long, status: JobStatus, error: String? = null) {
        val cv = ContentValues().apply {
            put("status", status.wire)
            put("error", error)
        }
        writableDatabase.update(TABLE, cv, "id = ?", arrayOf(id.toString()))
    }

    fun recent(limit: Int = 200): List<JobLogEntry> {
        val out = ArrayList<JobLogEntry>()
        readableDatabase.query(
            TABLE, null, null, null, null, null, "ts DESC", limit.toString()
        ).use { c ->
            val iId = c.getColumnIndexOrThrow("id")
            val iSrc = c.getColumnIndexOrThrow("source")
            val iTs = c.getColumnIndexOrThrow("ts")
            val iFmt = c.getColumnIndexOrThrow("format")
            val iTitle = c.getColumnIndexOrThrow("title")
            val iText = c.getColumnIndexOrThrow("text")
            val iStatus = c.getColumnIndexOrThrow("status")
            val iErr = c.getColumnIndexOrThrow("error")
            while (c.moveToNext()) {
                out.add(
                    JobLogEntry(
                        id = c.getLong(iId),
                        source = JobSource.from(c.getString(iSrc)),
                        timestamp = c.getLong(iTs),
                        format = c.getString(iFmt) ?: "",
                        title = c.getString(iTitle),
                        text = c.getString(iText),
                        status = JobStatus.from(c.getString(iStatus)),
                        error = c.getString(iErr),
                    )
                )
            }
        }
        return out
    }

    companion object {
        private const val DB_NAME = "printhub_jobs.db"
        private const val DB_VERSION = 1
        private const val TABLE = "jobs"
    }
}
