package com.dyvenes.gesturebridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import androidx.core.app.NotificationCompat
import androidx.lifecycle.LifecycleService
import okhttp3.OkHttpClient
import org.json.JSONObject

/**
 * Держит связь с брокером и поднимает камеру, когда для нас есть посылка.
 *
 * Выбран foreground-сервис, а не пуши: он ничего не отдаёт наружу и не
 * тянет в проект зависимость от Google. Цена — постоянная строка в
 * шторке, и это осознанный размен в пользу того, что данные остаются
 * внутри своей сети.
 *
 * LifecycleService нужен ради CameraX: ему требуется владелец жизненного
 * цикла, а запускать ради этого activity из фона Android не позволяет.
 */
class BridgeService : LifecycleService() {

    companion object {
        const val ACTION_SHARE = "com.dyvenes.gesturebridge.SHARE"
        const val ACTION_STOP = "com.dyvenes.gesturebridge.STOP"
        const val EXTRA_TEXT = "text"

        private const val CHANNEL_ID = "bridge-link"
        private const val NOTIFICATION_ID = 1
        private const val CAMERA_WINDOW_MS = 16_000L

        fun start(context: Context) {
            context.startForegroundService(Intent(context, BridgeService::class.java))
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

    private val http = OkHttpClient()
    private val main = Handler(Looper.getMainLooper())

    private var camera: CameraEngine? = null
    private var stopCameraAt: Runnable? = null

    override fun onCreate() {
        super.onCreate()
        settings = Settings(this)
        client = BridgeClient(settings, ::handle)

        createChannel()
        startForeground(NOTIFICATION_ID, buildNotification())

        Bridge.listen("service") { refreshNotification() }
        client.start()

        // Модель качаем заранее, чтобы первый же жест не ждал сети.
        Thread { ModelStore.ensure(this, settings, http) }.start()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        when (intent?.action) {
            ACTION_SHARE -> intent.getStringExtra(EXTRA_TEXT)?.let(::sendShare)

            // Остановка по кнопке в шторке. Автозапуск при этом выключаем:
            // иначе сервис вернётся после перезагрузки, хотя его просили уйти.
            ACTION_STOP -> {
                settings.autoStart = false
                Bridge.note("остановлен по кнопке")
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        Bridge.unlisten("service")
        cancelCameraTimeout()
        camera?.shutdown()
        camera = null
        client.stop()
        super.onDestroy()
    }

    // --- камера ---------------------------------------------------------

    private fun hasCameraPermission(): Boolean =
        checkSelfPermission(android.Manifest.permission.CAMERA) ==
                PackageManager.PERMISSION_GRANTED

    private fun wakeCamera(ttlSeconds: Int) {
        if (!hasCameraPermission()) {
            Bridge.note("нет разрешения на камеру — открой приложение")
            return
        }
        val model = ModelStore.file(this)
        if (!ModelStore.ready(this)) {
            Bridge.note("модель ещё не скачана")
            Thread { ModelStore.ensure(this, settings, http) }.start()
            return
        }

        val engine = camera ?: CameraEngine(this, this, model, ::onPalm).also { camera = it }
        engine.start()

        cancelCameraTimeout()
        val window = if (ttlSeconds > 0) ttlSeconds * 1000L + 1000L else CAMERA_WINDOW_MS
        stopCameraAt = Runnable { sleepCamera() }.also { main.postDelayed(it, window) }
    }

    private fun sleepCamera() {
        cancelCameraTimeout()
        camera?.stop()
        refreshNotification()
    }

    private fun cancelCameraTimeout() {
        stopCameraAt?.let(main::removeCallbacks)
        stopCameraAt = null
    }

    /** Ладонь удержана: идём на аукцион, а не забираем посылку сами. */
    private fun onPalm(confidence: Float, handArea: Float) {
        val signals = JSONObject()
            .put("hand", handArea.toDouble())
            .put("conf", confidence.toDouble())
            .put("idle", 0)

        client.send(JSONObject().put("t", "bid").put("role", "drop").put("signals", signals))
        Bridge.note("ладонь принята, жду брокера")
    }

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
        Bridge.note("отправлено: " + message.getString("preview"))
    }

    // --- приём ----------------------------------------------------------

    private fun names(message: JSONObject): String {
        val peers = message.optJSONArray("peers") ?: return ""
        return (0 until peers.length())
            .mapNotNull { peers.optJSONObject(it)?.optString("name") }
            .joinToString(", ")
    }

    private fun handle(message: JSONObject) {
        when (message.optString("t")) {
            "welcome", "peers" -> {
                Bridge.peers = names(message)
                Bridge.note("устройства: " + Bridge.peers)
            }

            "grabbed" -> Bridge.note("захвачено: " + message.optString("preview"))

            "pending" -> {
                Bridge.note("для нас посылка: " + message.optString("preview"))
                wakeCamera(message.optInt("ttl", 15))
            }

            "deliver" -> {
                sleepCamera()
                Bridge.note("принято: " + message.optString("preview"))
                Delivery.handle(this, settings, http, message)
            }

            "taken" -> {
                sleepCamera()
                Bridge.note("забрало другое устройство: " + message.optString("by"))
            }

            "lost" -> Bridge.note("жест отдан: " + message.optString("to"))
            "ignored" -> Bridge.note("жест смазался, покажи ещё раз")

            "nothing_to_take" -> {
                sleepCamera()
                Bridge.note("брать нечего")
            }

            "hovering" -> {
                sleepCamera()
                Bridge.note("посылка висит в очереди")
            }

            "error" -> Bridge.note("брокер: " + message.optString("reason"))
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
        val stop = PendingIntent.getService(
            this,
            1,
            Intent(this, BridgeService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE,
        )

        val text = if (camera?.running == true) "Покажи раскрытую ладонь" else Bridge.state
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setOngoing(true)
            .setSilent(true)
            .setContentIntent(open)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Остановить", stop)
            .build()
    }

    private fun refreshNotification() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) return

        getSystemService(NotificationManager::class.java)
            .notify(NOTIFICATION_ID, buildNotification())
    }
}
