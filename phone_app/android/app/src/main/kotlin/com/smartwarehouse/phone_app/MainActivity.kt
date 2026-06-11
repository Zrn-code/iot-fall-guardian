package com.smartwarehouse.phone_app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.os.Build
import android.os.Bundle
import io.flutter.embedding.android.FlutterActivity

class MainActivity : FlutterActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // FCM's fallback channel is only DEFAULT importance, which never shows a
        // heads-up banner — register a HIGH channel for fall alerts ourselves.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                "guardian_alerts",
                "跌倒緊急告警",
                NotificationManager.IMPORTANCE_HIGH,
            )
            channel.description = "配戴者跌倒 / 倒地告警"
            channel.enableVibration(true)
            (getSystemService(NOTIFICATION_SERVICE) as NotificationManager)
                .createNotificationChannel(channel)
        }
    }
}
