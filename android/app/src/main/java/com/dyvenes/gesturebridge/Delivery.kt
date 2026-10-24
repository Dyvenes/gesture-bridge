package com.dyvenes.gesturebridge

import android.content.ClipData
import android.content.ClipboardManager
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.provider.MediaStore
import android.util.Log
import android.widget.Toast
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.InputStream

/**
 * Что делать с приехавшей посылкой.
 *
 * Ссылку открываем, текст кладём в буфер обмена, файлы скачиваем в
 * «Загрузки». Всё то же, что делает агент на ПК. Разница в том, что
 * нативное приложение это может, а веб-страница не могла: браузер не
 * даёт ни писать в буфер, ни открывать вкладки без явного касания.
 */
object Delivery {

    private const val TAG = "Delivery"

    fun handle(
        context: Context,
        settings: Settings,
        http: OkHttpClient,
        message: JSONObject,
    ) {
        val payload = message.optJSONObject("payload") ?: return
        when (message.optString("kind")) {
            "url" -> openUrl(context, payload.optString("url"))
            "text" -> copyText(context, payload.optString("text"))
            "files" -> downloadFiles(context, settings, http, payload)
            else -> Log.w(TAG, "непонятный тип посылки")
        }
    }

    private fun openUrl(context: Context, url: String) {
        if (url.isEmpty()) return
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        runCatching { context.startActivity(intent) }
            .onFailure { Bridge.note("нечем открыть ссылку") }
        Bridge.note("открываю: " + url.take(60))
    }

    private fun copyText(context: Context, text: String) {
        if (text.isEmpty()) return
        val clipboard = context.getSystemService(ClipboardManager::class.java)
        clipboard.setPrimaryClip(ClipData.newPlainText("Gesture Bridge", text))
        Bridge.note("в буфере обмена: " + text.length + " символов")
        toast(context, "Текст в буфере обмена")
    }

    private fun downloadFiles(
        context: Context,
        settings: Settings,
        http: OkHttpClient,
        payload: JSONObject,
    ) {
        val files = payload.optJSONArray("files") ?: return

        Thread {
            var saved = 0
            for (index in 0 until files.length()) {
                val entry = files.optJSONObject(index) ?: continue
                val id = entry.optString("id")
                val name = entry.optString("name").ifBlank { "file" }
                if (id.isEmpty()) continue

                val url = settings.broker + "/blob/" + id + "?token=" + settings.token
                runCatching {
                    http.newCall(Request.Builder().url(url).build()).execute().use { response ->
                        if (!response.isSuccessful) error("код " + response.code)
                        val body = response.body ?: error("пустой ответ")
                        writeToDownloads(context, name, body.byteStream())
                    }
                    saved++
                    Bridge.note("сохранено: " + name)
                }.onFailure { failure ->
                    Log.w(TAG, "не скачался " + name, failure)
                    Bridge.note("файл не доехал: " + failure.message)
                }
            }
            if (saved > 0) {
                val text = if (saved == 1) "Файл в «Загрузках»"
                else "Файлов в «Загрузках»: " + saved
                toast(context, text)
            }
        }.start()
    }

    private fun writeToDownloads(context: Context, name: String, input: InputStream) {
        val values = ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, name)
            put(MediaStore.Downloads.MIME_TYPE, "application/octet-stream")
            put(MediaStore.Downloads.IS_PENDING, 1)
        }
        val resolver = context.contentResolver
        val collection = MediaStore.Downloads.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
        val uri = resolver.insert(collection, values) ?: error("MediaStore отказал")

        resolver.openOutputStream(uri)?.use { output -> input.copyTo(output) }
            ?: error("нет потока записи")

        values.clear()
        values.put(MediaStore.Downloads.IS_PENDING, 0)
        resolver.update(uri, values, null, null)
    }

    private fun toast(context: Context, text: String) {
        Handler(context.mainLooper).post {
            Toast.makeText(context, text, Toast.LENGTH_SHORT).show()
        }
    }
}
