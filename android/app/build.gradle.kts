plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.dyvenes.gesturebridge"
    // Берём платформу 34: она уже установлена в SDK, и сборка не тянет
    // ничего из сети. Камера и MediaPipe появятся на следующем этапе.
    compileSdk = 34

    defaultConfig {
        applicationId = "com.dyvenes.gesturebridge"
        minSdk = 29
        targetSdk = 34
        versionCode = 1
        versionName = "0.1"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        viewBinding = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    // OkHttp ради WebSocket: он же умеет и скачивание файлов.
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    // CameraX и lifecycle-service держим на версиях, совместимых с
    // compileSdk 34: более свежие требуют 35+, а платформы 35 в SDK нет.
    implementation("androidx.camera:camera-core:1.3.4")
    implementation("androidx.camera:camera-camera2:1.3.4")
    implementation("androidx.camera:camera-lifecycle:1.3.4")
    implementation("androidx.lifecycle:lifecycle-service:2.8.7")

    // Распознавание жестов — та же модель, что у агента на ПК.
    implementation("com.google.mediapipe:tasks-vision:0.10.35")
}
