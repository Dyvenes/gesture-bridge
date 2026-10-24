package com.dyvenes.gesturebridge

import android.content.Context
import android.graphics.Bitmap
import android.os.SystemClock
import android.util.Log
import com.google.mediapipe.framework.image.BitmapImageBuilder
import com.google.mediapipe.tasks.core.BaseOptions
import com.google.mediapipe.tasks.core.Delegate
import com.google.mediapipe.tasks.vision.core.ImageProcessingOptions
import com.google.mediapipe.tasks.vision.core.RunningMode
import com.google.mediapipe.tasks.vision.gesturerecognizer.GestureRecognizer
import com.google.mediapipe.tasks.vision.gesturerecognizer.GestureRecognizerResult
import java.io.File
import java.nio.ByteBuffer
import kotlin.math.max
import kotlin.math.min

/**
 * Распознавание раскрытой ладони на кадрах с фронтальной камеры.
 *
 * Пороги и логика удержания те же, что у агента на ПК и в веб-приёмнике.
 * Повторю коротко, почему так: уверенность модели всё время дышит, и
 * требование непрерывной серии кадров выше одного порога обнуляет отсчёт
 * на каждом провале. Поэтому войти в удержание труднее, чем в нём
 * остаться, короткая потеря руки прощается, но сработать, когда руки в
 * кадре уже нет, нельзя.
 */
class GestureEngine(
    context: Context,
    modelFile: File,
    private val onPalm: (confidence: Float, handArea: Float) -> Unit,
) {
    companion object {
        const val PALM = "Open_Palm"

        private const val TAG = "GestureEngine"
        private const val ENTER_CONFIDENCE = 0.55f
        private const val STAY_CONFIDENCE = 0.35f
        private const val LOST_GRACE_MS = 350L
        private const val SEEN_RECENTLY_MS = 120L
        private const val HOLD_MS = 450L
        private const val COOLDOWN_MS = 1500L
    }

    private val recognizer: GestureRecognizer

    private var held = false
    private var heldSince = 0L
    private var lastSeen = 0L
    private var quietUntil = 0L
    private var bestScore = 0f
    private var bestArea = 0f

    /** Последнее, что увидела модель, — для строки отладки на экране. */
    @Volatile var debug: String = "—"
        private set

    init {
        val buffer = ByteBuffer.allocateDirect(modelFile.length().toInt()).apply {
            modelFile.inputStream().use { stream ->
                val bytes = stream.readBytes()
                put(bytes)
                rewind()
            }
        }

        val options = GestureRecognizer.GestureRecognizerOptions.builder()
            .setBaseOptions(
                BaseOptions.builder()
                    // Модель лежит в файлах приложения, а не в assets:
                    // она скачивается с брокера, чтобы не раздувать APK.
                    .setModelAssetBuffer(buffer)
                    .setDelegate(Delegate.CPU)
                    .build()
            )
            .setRunningMode(RunningMode.LIVE_STREAM)
            .setNumHands(1)
            .setResultListener(::consume)
            .setErrorListener { error ->
                Log.w(TAG, "распознаватель: ${error.message}")
                debug = "сбой: ${error.message}"
            }
            .build()

        recognizer = GestureRecognizer.createFromOptions(context, options)
    }

    fun close() = recognizer.close()

    fun reset() {
        held = false
        bestScore = 0f
        bestArea = 0f
        debug = "—"
    }

    fun feed(bitmap: Bitmap, rotationDegrees: Int) {
        val image = BitmapImageBuilder(bitmap).build()
        val processing = ImageProcessingOptions.builder()
            .setRotationDegrees(rotationDegrees)
            .build()
        recognizer.recognizeAsync(image, processing, SystemClock.uptimeMillis())
    }

    // --- разбор результата ----------------------------------------------

    private fun consume(result: GestureRecognizerResult, @Suppress("UNUSED_PARAMETER") input: com.google.mediapipe.framework.image.MPImage) {
        val top = result.gestures().firstOrNull()?.firstOrNull()
        val category = top?.categoryName()
        val score = top?.score() ?: 0f
        val area = result.landmarks().firstOrNull()?.let { marks ->
            val xs = marks.map { it.x() }
            val ys = marks.map { it.y() }
            val box = (xs.max() - xs.min()) * (ys.max() - ys.min())
            min(1f, max(0f, box))
        } ?: 0f

        consider(category, score, area)

        debug = if (result.landmarks().isEmpty()) {
            "руки в кадре нет"
        } else {
            val progress = if (held)
                min(100, ((SystemClock.uptimeMillis() - heldSince) * 100 / HOLD_MS).toInt())
            else 0
            "%s %.2f · рука %.3f · удержание %d%%".format(category ?: "—", score, area, progress)
        }
    }

    private fun consider(category: String?, score: Float, area: Float) {
        val now = SystemClock.uptimeMillis()
        val isPalm = category == PALM

        when {
            isPalm && score >= ENTER_CONFIDENCE -> {
                if (!held) {
                    held = true
                    heldSince = now
                    bestScore = 0f
                    bestArea = 0f
                }
                lastSeen = now
            }
            held && isPalm && score >= STAY_CONFIDENCE -> lastSeen = now
            held && now - lastSeen <= LOST_GRACE_MS -> Unit
            else -> {
                held = false
                return
            }
        }

        bestScore = max(bestScore, score)
        bestArea = max(bestArea, area)

        if (now < quietUntil) return
        if (now - heldSince < HOLD_MS) return
        if (now - lastSeen > SEEN_RECENTLY_MS) return

        quietUntil = now + COOLDOWN_MS
        held = false
        onPalm(bestScore, bestArea)
    }
}
