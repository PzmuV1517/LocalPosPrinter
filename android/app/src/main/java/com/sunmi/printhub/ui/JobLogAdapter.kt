package com.sunmi.printhub.ui

import android.graphics.Color
import android.view.LayoutInflater
import android.view.ViewGroup
import androidx.recyclerview.widget.RecyclerView
import com.sunmi.printhub.databinding.ItemJobBinding
import com.sunmi.printhub.db.JobLogEntry
import com.sunmi.printhub.db.JobStatus
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class JobLogAdapter(private var items: List<JobLogEntry>) :
    RecyclerView.Adapter<JobLogAdapter.VH>() {

    private val timeFmt = SimpleDateFormat("MM-dd HH:mm:ss", Locale.US)

    class VH(val binding: ItemJobBinding) : RecyclerView.ViewHolder(binding.root)

    fun setItems(newItems: List<JobLogEntry>) {
        items = newItems
        notifyDataSetChanged()
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH {
        val b = ItemJobBinding.inflate(LayoutInflater.from(parent.context), parent, false)
        return VH(b)
    }

    override fun onBindViewHolder(holder: VH, position: Int) {
        val e = items[position]
        val b = holder.binding
        b.jobStatus.text = e.status.wire
        b.jobStatus.setTextColor(statusColor(e.status))
        b.jobSourceFormat.text = "${e.source.wire} · ${e.format}"
        b.jobTime.text = timeFmt.format(Date(e.timestamp))
        val detail = e.error ?: e.title ?: e.text ?: ""
        b.jobDetail.text = detail
    }

    override fun getItemCount(): Int = items.size

    private fun statusColor(status: JobStatus): Int = when (status) {
        JobStatus.SUCCESS -> Color.parseColor("#2E7D32")
        JobStatus.FAILED -> Color.parseColor("#C62828")
        JobStatus.REJECTED -> Color.parseColor("#EF6C00")
        JobStatus.PRINTING, JobStatus.QUEUED -> Color.parseColor("#1565C0")
    }
}
