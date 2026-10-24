package com.dyvenes.gesturebridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import org.json.JSONObject

/**
 * Держит связь с брокером, пока приложение не на экране.
 *
 * Выбран foreground-сервис, а не пуши: он ничего не отдаёт наружу и не
 * тянет в проект зависимость от Google. Цена — постоянная строка в
 * шторке, и это осознанный размен в пользу того, что данные остаются
 * внутри своей сети.
 */
class BridgeService : Service() {

    companion object {
        const val ACTION_SHARE = "com.dyvenes.gesturebridge.SHARE"
        const val EXTRA_TEXT = "text"

        private const val CHANNEL_ID = "bridge-link"
        private const val NOTIFICATION_ID = 1

        fun start(context: Context) {
            val intent = Intent(context, BridgeService::class.java)
            context.startForegroundService(intent)
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, BridgeService::class.java))
        }

        fun share(context: Context, text: String) {
            val intent = Intent(context, BridgeService::class.java)
                .setAction(ACTION_SHARE)
                .putExtra(EXTRA_TEXT, text)
            context.startForegroundService(intent)
        }
    }

    private lateinit var settings: Settings
    private lateinit var client: BridgeClient

    override fun onCreate() {
        super.onCreate()
        settings = Settings(this)
        client = BridgeClient(settings, ::handle)

        createChannel()
        startForeground(NOTIFICATION_ID, buildNotification())

        Bridge.listener = { refreshNotification() }
        client.start()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_SHARE) {
            intent.getStringExtra(EXTRA_TEXT)?.let(::sendShare)
        }
        return START_STICKY
    }

    override fun onDestroy() {
        Bridge.listener = null
        client.stop()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // --- отправка -------------------------------------------------------

    private fun sendShare(raw: String) {
        val text = raw.trim()
        if (text.isEmpty()) return

        // Android часто кладёт ссылку внутрь произвольного текста.
        val url = Regex("""https?://\S+""").find(text)?.value

        val message = if (url != null) {
            JSONObject()
                .put("t", "grab")
                .put("kind", "url")
                .put("payload", JSONObject().put("url", url).put("title", ""))
                .put("preview", url.removePrefix("https://").removePrefix("http://").take(60))
        } else {
            JSONObject()
                .put("t", "grab")
                .put("kind", "text")
                .put("payload", JSONObject().put("text", text))
                .put("preview", text.replace('\n', ' ').take(60))
        }

        client.send(message)
        Bridge.note("отправлено: ${message.getString("preview")}")
    }

    // --- приём ----------------------------------------------------------

    private fun handle(message: JSONObject) {
        when (message.optString("t")) {
            "welcome" -> {
                val peers = message.optJSONArray("peers")
                val names = buildList {
                    for (i in 0 until (peers?.length() ?: 0)) {
                        add(peers!!.getJSONObject(i).optString("name"))
                    }
                }
                Bridge.peers = names.joinToString(", ")
                Bridge.note("на связи: ${Bridge.peers}")
            }

            "peers" -> {
                val peers = message.optJSONArray("peers")
                val names = buildList {
                    for (i in 0 until (peers?.length() ?: 0)) {
                        add(peers!!.getJSONObject(i).optString("name"))
                    }
                }
                Bridge.peers = names.joinToString(", ")
                Bridge.note("устройства: ${Bridge.peers}")
            }

            "grabbed" -> Bridge.note("захвачено: ${message.optString("preview")}")
            "pending" -> Bridge.note("для нас посылка: ${message.optString("preview")}")
            "deliver" -> Bridge.note("доставлено: ${message.optString("preview")}")
            "hovering" -> Bridge.note("посылка висит в очереди")
            "error" -> Bridge.note("брокер: ${message.optString("reason")}")
        }
    }

    // --- уведомление ----------------------------------------------------

    private fun createChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.channel_name),
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = getString(R.string.channel_desc)
            setShowBadge(false)
        }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun buildNotification(): Notification {
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(Bridge.state)
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setOngoing(true)
            .setSilent(true)
            .setContentIntent(open)
            .build()
    }

    private fun refreshNotification() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) !=
            android.content.pm.PackageManager.PERMISSION_GRANTED
        ) return

        getSystemService(NotificationManager::class.java)
            .notify(NOTIFICATION_ID, buildNotification())
    }
}
