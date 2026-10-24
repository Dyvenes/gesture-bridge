package com.dyvenes.gesturebridge

import android.content.Context
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File

/**
 * Модель распознавания жестов.
 *
 * Качается с брокера, а не кладётся в APK: восемь мегабайт в установщике
 * ради файла, который и так раздаётся по сети, — лишние. Брокер отдаёт
 * ровно тот же файл, что использует агент на ПК, поэтому обе стороны
 * всегда на одной версии модели.
 */
object ModelStore {

    private const val TAG = "ModelStore"
    private const val NAME = "gesture_recognizer.task"
    private const val MIN_SIZE = 1_000_000L

    fun file(context: Context): File = File(context.filesDir, NAME)

    fun ready(context: Context): Boolean {
        val target = file(context)
        return target.exists() && target.length() >= MIN_SIZE
    }

    /** Блокирующая загрузка. Вызывать только с фонового потока. */
    fun ensure(context: Context, settings: Settings, http: OkHttpClient): File? {
        val target = file(context)
        if (ready(context)) return target

        if (!settings.configured) {
            Bridge.note("модель не скачать: не задан брокер")
            return null
        }

        val url = settings.broker + "/models/" + NAME
        Bridge.note("скачиваю модель распознавания…")

        val partial = File(context.filesDir, "$NAME.part")
        return runCatching {
            http.newCall(Request.Builder().url(url).build()).execute().use { response ->
                if (!response.isSuccessful) error("код " + response.code)
                val body = response.body ?: error("пустой ответ")
                partial.outputStream().use { out -> body.byteStream().copyTo(out) }
            }
            if (partial.length() < MIN_SIZE) error("файл подозрительно мал")
            // Переименование в конце: оборванная загрузка не притворится готовой.
            if (target.exists()) target.delete()
            if (!partial.renameTo(target)) error("не удалось переименовать")
            Bridge.note("модель готова: " + (target.length() / 1024 / 1024) + " МБ")
            target
        }.onFailure {
            Log.w(TAG, "модель не скачалась", it)
            Bridge.note("модель не скачалась: " + it.message)
            partial.delete()
        }.getOrNull()
    }
}
