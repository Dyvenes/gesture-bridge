package com.dyvenes.gesturebridge

import android.os.Handler
import android.os.Looper
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import kotlin.math.min

/**
 * WebSocket к брокеру: протокол тот же, что у агента на ПК и у PWA.
 *
 * Переподключается сам с нарастающей паузой. Сообщения, отправленные в
 * момент обрыва, не теряются: они ждут в очереди до следующего hello.
 */
class BridgeClient(
    private val settings: Settings,
    private val onMessage: (JSONObject) -> Unit,
) {
    private companion object {
        const val TAG = "BridgeClient"
        const val MAX_RETRY_MS = 30_000L
    }

    private val http = OkHttpClient.Builder()
        .pingInterval(25, TimeUnit.SECONDS)
        .build()

    private val main = Handler(Looper.getMainLooper())
    private val outbox = ArrayDeque<JSONObject>()

    @Volatile private var socket: WebSocket? = null
    @Volatile private var stopped = true
    private var retryMs = 1_000L

    val connected: Boolean get() = socket != null

    fun start() {
        stopped = false
        connect()
    }

    fun stop() {
        stopped = true
        main.removeCallbacksAndMessages(null)
        socket?.close(1000, null)
        socket = null
        Bridge.setState("отключено")
    }

    /** Кладёт сообщение в сокет либо в очередь, если связи сейчас нет. */
    fun send(message: JSONObject) {
        val live = socket
        if (live == null || !live.send(message.toString())) {
            synchronized(outbox) { outbox.addLast(message) }
        }
    }

    private fun connect() {
        if (stopped) return
        if (!settings.configured) {
            Bridge.setState("не настроено")
            return
        }
        // https -> wss, http -> ws
        val url = settings.broker.replaceFirst("http", "ws") +
                "/ws?token=" + settings.token
        Bridge.setState("подключаюсь…")
        http.newWebSocket(Request.Builder().url(url).build(), listener)
    }

    private fun scheduleReconnect() {
        if (stopped) return
        Bridge.setState("нет связи, переподключаюсь через ${retryMs / 1000} с")
        main.postDelayed({ connect() }, retryMs)
        retryMs = min(retryMs * 2, MAX_RETRY_MS)
    }

    private fun greet(ws: WebSocket) {
        val hello = JSONObject()
            .put("t", "hello")
            .put("name", settings.deviceName)
            .put("device", "phone")
            .put("caps", JSONArray(listOf("grab_share", "open_url", "clipboard", "files")))
        ws.send(hello.toString())

        val queued = synchronized(outbox) {
            val copy = outbox.toList()
            outbox.clear()
            copy
        }
        queued.forEach { ws.send(it.toString()) }
    }

    private val listener = object : WebSocketListener() {

        override fun onOpen(ws: WebSocket, response: Response) {
            socket = ws
            retryMs = 1_000L
            Bridge.setState("на связи")
            Bridge.note("подключился к брокеру")
            greet(ws)
        }

        override fun onMessage(ws: WebSocket, text: String) {
            val parsed = runCatching { JSONObject(text) }.getOrNull()
            if (parsed == null) {
                Log.w(TAG, "невалидный JSON: ${text.take(120)}")
                return
            }
            main.post { onMessage(parsed) }
        }

        override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
            socket = null
            val code = response?.code
            if (code == 403) {
                stopped = true
                Bridge.setState("брокер отверг токен")
                Bridge.note("проверь токен: брокер ответил 403")
                return
            }
            Log.w(TAG, "обрыв связи", t)
            Bridge.note("обрыв: ${t.message ?: t.javaClass.simpleName}")
            scheduleReconnect()
        }

        override fun onClosed(ws: WebSocket, code: Int, reason: String) {
            socket = null
            if (!stopped) scheduleReconnect()
        }
    }
}
