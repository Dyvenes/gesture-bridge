package com.dyvenes.gesturebridge

/**
 * Общее состояние сервиса и экрана.
 *
 * Сервис и activity живут в одном процессе, поэтому обычный объект проще
 * и честнее, чем широковещательные сообщения: никакой сериализации и
 * никаких гонок при пересоздании экрана.
 */
object Bridge {

    private const val LOG_LIMIT = 40

    @Volatile
    var state: String = "не подключено"
        private set

    @Volatile
    var peers: String = ""

    private val lines = ArrayDeque<String>()

    /** Экран подписывается сюда, чтобы перерисоваться. */
    @Volatile
    var listener: (() -> Unit)? = null

    val log: String
        get() = synchronized(lines) { lines.joinToString("\n") }

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
        listener?.invoke()
    }
}
