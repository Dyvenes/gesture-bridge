package com.dyvenes.gesturebridge

import android.annotation.SuppressLint
import android.content.Context
import android.util.Log
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import java.io.File
import java.util.concurrent.Executors

/**
 * Фронтальная камера без превью.
 *
 * Превью нет намеренно: кадры нужны распознавателю, а не человеку, и
 * отсутствие поверхности позволяет держать камеру прямо в сервисе — без
 * запуска activity из фона, который Android ограничивает.
 *
 * Камера поднимается только когда для устройства есть посылка, и гаснет
 * сразу после. Это и бережёт батарею, и почти убирает ложные жесты.
 */
class CameraEngine(
    private val context: Context,
    private val owner: LifecycleOwner,
    private val modelFile: File,
    private val onPalm: (confidence: Float, handArea: Float) -> Unit,
) {
    private companion object {
        const val TAG = "CameraEngine"
    }

    private val analysisExecutor = Executors.newSingleThreadExecutor()

    private var provider: ProcessCameraProvider? = null
    private var engine: GestureEngine? = null

    @Volatile
    var running = false
        private set

    val debug: String
        get() = engine?.debug ?: "камера выключена"

    fun start() {
        if (running) return
        running = true

        val future = ProcessCameraProvider.getInstance(context)
        future.addListener({
            if (!running) return@addListener
            runCatching { bind(future.get()) }.onFailure {
                Log.w(TAG, "камера не поднялась", it)
                Bridge.note("камера не поднялась: " + it.message)
                running = false
            }
        }, ContextCompat.getMainExecutor(context))
    }

    @SuppressLint("UnsafeOptInUsageError")
    private fun bind(cameraProvider: ProcessCameraProvider) {
        provider = cameraProvider
        engine = GestureEngine(context, modelFile, onPalm)

        val analysis = ImageAnalysis.Builder()
            // RGBA даёт готовый Bitmap без ручной конвертации YUV.
            .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888)
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .build()

        analysis.setAnalyzer(analysisExecutor) { proxy ->
            try {
                engine?.feed(proxy.toBitmap(), proxy.imageInfo.rotationDegrees)
            } catch (t: Throwable) {
                Log.w(TAG, "кадр не обработан", t)
            } finally {
                proxy.close()
            }
        }

        cameraProvider.unbindAll()
        cameraProvider.bindToLifecycle(
            owner,
            CameraSelector.DEFAULT_FRONT_CAMERA,
            analysis,
        )
        Bridge.note("камера поднята")
    }

    fun stop() {
        if (!running) return
        running = false
        ContextCompat.getMainExecutor(context).execute {
            runCatching { provider?.unbindAll() }
            engine?.close()
            engine = null
            Bridge.note("камера выключена")
        }
    }

    fun shutdown() {
        stop()
        analysisExecutor.shutdown()
    }
}
