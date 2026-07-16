package com.sunmi.printhub.ui

import android.view.Gravity
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.TextView
import androidx.recyclerview.widget.RecyclerView
import com.sunmi.printhub.R
import com.sunmi.printhub.core.ConferManager

/**
 * On-screen mirror of the chat (the authoritative copy prints on paper). Your messages sit on the
 * right; others' on the left, showing the sender name. Images are noted with a small marker, the
 * full picture goes to the print head, not this preview list.
 */
class ConferMessageAdapter(private val myUsername: () -> String) :
    RecyclerView.Adapter<ConferMessageAdapter.VH>() {

    private val items = ArrayList<ConferManager.Message>()

    fun submit(list: List<ConferManager.Message>) {
        items.clear(); items.addAll(list); notifyDataSetChanged()
    }

    class VH(view: View) : RecyclerView.ViewHolder(view) {
        val row: LinearLayout = view.findViewById(R.id.messageRow)
        val bubble: TextView = view.findViewById(R.id.bubble)
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH =
        VH(LayoutInflater.from(parent.context).inflate(R.layout.item_confer_message, parent, false))

    override fun onBindViewHolder(holder: VH, position: Int) {
        val m = items[position]
        val mine = m.sender == myUsername()
        val body = if (m.kind == "image") "🖼 image" else m.body
        holder.bubble.text = if (mine) body else "${m.senderDisplay}\n> $body"
        holder.row.gravity = if (mine) Gravity.END else Gravity.START
    }

    override fun getItemCount(): Int = items.size
}
