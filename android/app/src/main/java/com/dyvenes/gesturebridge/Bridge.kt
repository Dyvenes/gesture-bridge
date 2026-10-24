package com.dyvenes.gesturebridge

import java.util.concurrent.ConcurrentHashMap

/**
 * Общее состояние сервиса и экрана.
 *
 * Сервис и activity живут в одном процессе, поэтому обычный объект проще
 * и честнее, чем широковещательные сообщения: никакой сериализации и
 * никаких гонок при пересоздании экрана.
 *
 * Слушателей именно несколько, а не один. Сначала слот был один — и
 * сервис, стартовав после экрана, молча его перезаписывал: уведомление
 * обновлялось, а экран застывал на старом состоянии.
 */
object Bridge {

    private const val LOG_LIMIT = 40

    @Volatile
    var state: String = "не подключено"
        private set

    @Volatile
    var peers: String = ""

    private val lines = ArrayDeque<String>()
    private val listeners = ConcurrentHashMap<String, () -> Unit>()

    val log: String
        get() = synchronized(lines) { lines.joinToString("\n") }

    fun listen(key: String, callback: () -> Unit) {
        listeners[key] = callback
    }

    fun unlisten(key: String) {
        listeners.remove(key)
    }

    fun setState(value: String) {
        state = value
        notifyChanged()
    }

    fun note(line: String) {
        synchronized(lines) {
            lines.addLast(line)
            while (lines.size > LOG_LIMIT) lines.removeFirst()
        }
        notifyChanged()
    }

    private fun notifyChanged() {
        listeners.values.forEach { callback -> runCatching { callback() } }
    }
}
