package com.dyvenes.gesturebridge

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import com.dyvenes.gesturebridge.databinding.ActivityMainBinding

/**
 * Экран привязки: адрес брокера, токен, имя устройства.
 *
 * Он же принимает «Поделиться» из других приложений: ссылка или текст
 * уходит сервису, а экран при этом даже не обязан быть виден.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var settings: Settings

    // Камеру просим здесь, с видимого экрана: сервис из фона запросить
    // разрешение не может, а без него поднять камеру нечем.
    private val askPermissions =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { granted ->
            if (granted[Manifest.permission.CAMERA] == false) {
                Toast.makeText(
                    this,
                    "Без камеры приём ладонью работать не будет",
                    Toast.LENGTH_LONG,
                ).show()
            }
            if (granted[Manifest.permission.POST_NOTIFICATIONS] == false) {
                Toast.makeText(
                    this,
                    "Без уведомления сервис может быть остановлен системой",
                    Toast.LENGTH_LONG,
                ).show()
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        settings = Settings(this)
        binding.broker.setText(settings.broker)
        binding.token.setText(settings.token)
        binding.deviceName.setText(settings.deviceName)

        binding.connect.setOnClickListener { saveAndConnect() }
        binding.disconnect.setOnClickListener {
            BridgeService.stop(this)
            Bridge.setState("отключено")
        }

        requestPermissions()
        handleShare(intent)

        // Устройство уже привязано — поднимаем связь молча, без нажатий.
        if (settings.configured && settings.autoStart) BridgeService.start(this)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleShare(intent)
    }

    override fun onResume() {
        super.onResume()
        Bridge.listen("ui") { runOnUiThread(::render) }
        render()
    }

    override fun onPause() {
        Bridge.unlisten("ui")
        super.onPause()
    }

    private fun render() {
        binding.status.text = Bridge.state
        binding.log.text = Bridge.log
    }

    private fun saveAndConnect() {
        val broker = binding.broker.text.toString().trim()
        val token = binding.token.text.toString().trim()

        if (broker.isEmpty() || token.isEmpty()) {
            Toast.makeText(this, "Нужны адрес и токен", Toast.LENGTH_SHORT).show()
            return
        }
        if (!broker.startsWith("http://") && !broker.startsWith("https://")) {
            Toast.makeText(this, "Адрес должен начинаться с http:// или https://", Toast.LENGTH_LONG).show()
            return
        }

        settings.broker = broker
        settings.token = token
        settings.deviceName = binding.deviceName.text.toString().ifBlank { Build.MODEL ?: "Телефон" }
        // Кнопка «Подключиться» заодно возвращает автозапуск: её нажимают
        // именно тогда, когда хотят, чтобы связь жила дальше сама.
        settings.autoStart = true

        BridgeService.start(this)
        Bridge.note("настройки сохранены")
    }

    private fun handleShare(intent: Intent?) {
        if (intent?.action != Intent.ACTION_SEND) return
        val text = intent.getStringExtra(Intent.EXTRA_TEXT) ?: return

        if (!settings.configured) {
            Toast.makeText(this, "Сначала укажи адрес брокера и токен", Toast.LENGTH_LONG).show()
            return
        }
        BridgeService.share(this, text)
        Toast.makeText(this, "Отправлено", Toast.LENGTH_SHORT).show()
        // Пришли сюда только ради отправки — экран держать незачем.
        if (!isTaskRoot) finish()
    }

    private fun requestPermissions() {
        val wanted = buildList {
            if (checkSelfPermission(Manifest.permission.CAMERA) !=
                PackageManager.PERMISSION_GRANTED
            ) add(Manifest.permission.CAMERA)

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
                PackageManager.PERMISSION_GRANTED
            ) add(Manifest.permission.POST_NOTIFICATIONS)
        }
        if (wanted.isNotEmpty()) askPermissions.launch(wanted.toTypedArray())
    }
}
