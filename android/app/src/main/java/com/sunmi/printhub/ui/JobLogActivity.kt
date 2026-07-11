package com.sunmi.printhub.ui

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import com.sunmi.printhub.core.Hub
import com.sunmi.printhub.databinding.ActivityJobLogBinding
import java.util.concurrent.Executors

class JobLogActivity : AppCompatActivity() {

    private lateinit var binding: ActivityJobLogBinding
    private val adapter = JobLogAdapter(emptyList())
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Hub.init(this)
        binding = ActivityJobLogBinding.inflate(layoutInflater)
        setContentView(binding.root)
        title = getString(com.sunmi.printhub.R.string.joblog_title)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)

        binding.jobList.layoutManager = LinearLayoutManager(this)
        binding.jobList.adapter = adapter
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    private fun refresh() {
        io.execute {
            val jobs = Hub.jobLog.recent(200)
            main.post {
                adapter.setItems(jobs)
                binding.emptyView.visibility = if (jobs.isEmpty()) View.VISIBLE else View.GONE
            }
        }
    }

    override fun onSupportNavigateUp(): Boolean {
        finish(); return true
    }
}
